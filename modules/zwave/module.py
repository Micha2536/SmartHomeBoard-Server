"""Z-Wave integration backed by the official Z-Wave JS WebSocket server."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time

log = logging.getLogger("smarthomeboard.zwave")


def manifest():
    return {
        "id": "zwave",
        "name": "Z-Wave",
        "version": "1.0.1",
        "icon": "wave.3.right",
        "description": (
            "Lokale Z-Wave-Integration über den offiziellen Z-Wave-JS-Treiber. "
            "Geräte, Livewerte und Steuerbefehle laufen persistent auf dem Server."
        ),
        "supportsDiscovery": True,
        "supportsMultipleInstances": False,
        "fields": [
            {
                "key": "ws_url", "type": "text", "title": "Z-Wave-JS WebSocket-URL",
                "default": "ws://127.0.0.1:3000", "required": True,
                "help": "Beim mitgelieferten Docker-Profil ist ws://127.0.0.1:3000 korrekt.",
            },
        ],
        "actions": [
            {"id": "refresh", "title": "Alle Geräte neu einlesen", "icon": "arrow.clockwise"},
            {"id": "start_inclusion", "title": "Gerät anlernen", "icon": "plus.circle"},
            {"id": "stop_inclusion", "title": "Anlernen beenden", "icon": "stop.circle"},
            {
                "id": "enter_pin", "title": "S2-PIN bestätigen", "icon": "lock.shield",
                "fields": [{
                    "key": "pin", "type": "password", "title": "Fünfstellige S2-PIN",
                    "required": True, "pattern": "[0-9]{5}", "maxlength": 5,
                    "placeholder": "12345", "help": "Die ersten fünf Ziffern des DSK-Aufklebers am Gerät.",
                }],
            },
            {"id": "start_exclusion", "title": "Gerät ausschließen", "icon": "minus.circle", "role": "destructive"},
            {"id": "stop_exclusion", "title": "Ausschließen beenden", "icon": "stop.circle"},
        ],
    }


def create(configuration, context):
    return ZWaveAdapter(configuration, context)


class ZWaveAdapter:
    def __init__(self, configuration, context):
        self.configuration = configuration
        self.context = context
        self.client = None
        self.controller = None
        self.session = None
        self.listen_task = None
        self.runner_task = None
        self.event_tasks = set()
        self.stopping = False
        self.first_connection = None
        self.pending_dsk = ""
        self.node_by_published_id = {}
        self.attribute_values = {}
        state = context.load_state({}) or {}
        self.value_offsets = dict(state.get("value_offsets", {})) if isinstance(state, dict) else {}
        self.next_offset = max([int(value) for value in self.value_offsets.values()] or [0]) + 1

    async def start(self):
        url = str(self.configuration.get("ws_url", "ws://127.0.0.1:3000")).strip()
        if not url.startswith(("ws://", "wss://")):
            raise ValueError("Die Z-Wave-JS-Adresse muss mit ws:// oder wss:// beginnen")
        self.first_connection = asyncio.get_running_loop().create_future()
        self.runner_task = asyncio.create_task(self._connection_loop(url))
        try:
            await asyncio.wait_for(asyncio.shield(self.first_connection), timeout=35)
        except asyncio.TimeoutError as error:
            raise ConnectionError("Z-Wave JS antwortet nicht. Läuft der Zusatzcontainer und ist Port 3000 aktiviert?") from error

    async def stop(self):
        self.stopping = True
        if self.runner_task:
            self.runner_task.cancel()
        if self.listen_task:
            self.listen_task.cancel()
        for task in list(self.event_tasks):
            task.cancel()
        if self.client:
            with contextlib.suppress(Exception):
                await self.client.disconnect()
        if self.session:
            with contextlib.suppress(Exception):
                await self.session.close()
        if self.runner_task:
            await asyncio.gather(self.runner_task, return_exceptions=True)
        self.client = self.controller = self.session = None

    async def health_check(self):
        if not self.client or not self.client.connected or not self.controller:
            raise ConnectionError("Keine Verbindung zu Z-Wave JS")

    async def action(self, action_id, payload):
        if not self.controller:
            raise ConnectionError("Z-Wave JS ist nicht verbunden")
        if action_id == "refresh":
            for node in self.controller.nodes.values():
                if not node.is_controller_node:
                    await self._publish_node(node)
            return {"message": f"{len(self.node_by_published_id)} Z-Wave-Geräte neu eingelesen"}
        if action_id == "start_inclusion":
            from zwave_js_server.const import InclusionStrategy
            started = await self.controller.async_begin_inclusion(InclusionStrategy.DEFAULT)
            if not started:
                raise RuntimeError("Der Z-Wave-Controller konnte den Anlernmodus nicht starten")
            await self.context.set_status("Anlernmodus aktiv")
            return {"message": "Z-Wave-Anlernmodus gestartet. Gerät jetzt in den Anlernmodus versetzen."}
        if action_id == "stop_inclusion":
            await self.controller.async_stop_inclusion()
            self.pending_dsk = ""
            await self.context.set_status("Verbunden")
            return {"message": "Z-Wave-Anlernmodus beendet"}
        if action_id == "enter_pin":
            pin = str(payload.get("pin", "")).strip()
            if not re.fullmatch(r"\d{5}", pin):
                raise ValueError("Die S2-PIN muss genau fünf Ziffern enthalten")
            if not self.pending_dsk:
                raise ValueError("Aktuell wartet kein Z-Wave-Gerät auf eine S2-PIN")
            await self.controller.async_validate_dsk_and_enter_pin(pin)
            self.pending_dsk = ""
            await self.context.set_status("S2-Sicherheit wird eingerichtet")
            return {"message": "S2-PIN bestätigt. Das Gerät wird sicher eingebunden."}
        if action_id == "start_exclusion":
            started = await self.controller.async_begin_exclusion()
            if not started:
                raise RuntimeError("Der Z-Wave-Controller konnte den Ausschlussmodus nicht starten")
            await self.context.set_status("Ausschlussmodus aktiv")
            return {"message": "Z-Wave-Ausschlussmodus gestartet. Gerät jetzt betätigen."}
        if action_id == "stop_exclusion":
            await self.controller.async_stop_exclusion()
            await self.context.set_status("Verbunden")
            return {"message": "Z-Wave-Ausschlussmodus beendet"}
        raise ValueError("Unbekannte Z-Wave-Modulaktion")

    async def set_value(self, node_id, attribute_id, value):
        node = self.node_by_published_id.get(int(node_id))
        value_id = self.attribute_values.get((int(node_id), int(attribute_id)))
        if not node or not value_id:
            raise KeyError("Der Z-Wave-Wert ist nicht mehr verfügbar")
        zwave_value = node.values.get(value_id)
        if not zwave_value:
            raise KeyError("Der Z-Wave-Wert wurde vom Gerät entfernt")
        converted = _command_value(zwave_value, value)
        await node.async_set_value(zwave_value, converted)

    async def _connection_loop(self, url):
        first_attempt = True
        while not self.stopping:
            try:
                await self._connected_session(url)
                if first_attempt and self.first_connection and not self.first_connection.done():
                    self.first_connection.set_result(True)
                first_attempt = False
                await self._apply_inclusion_state(self.controller.inclusion_state)
                await self.listen_task
                if not self.stopping:
                    await self.context.set_status("Verbindung unterbrochen", "Z-Wave JS wurde getrennt; neuer Versuch läuft")
            except asyncio.CancelledError:
                return
            except Exception as error:
                log.exception("Z-Wave-JS-Verbindung fehlgeschlagen")
                if first_attempt and self.first_connection and not self.first_connection.done():
                    self.first_connection.set_exception(ConnectionError(f"Z-Wave JS konnte nicht verbunden werden: {error}"))
                    await self._close_connection()
                    return
                await self.context.set_status("Verbindung unterbrochen", str(error))
            await self._close_connection()
            if not self.stopping:
                await asyncio.sleep(5)

    async def _connected_session(self, url):
        import aiohttp
        from zwave_js_server.client import Client

        self.session = aiohttp.ClientSession()
        self.client = Client(url, self.session, additional_user_agent_components={"SmartHomeBoard": "0.15.0"})
        await self.client.connect()
        ready = asyncio.Event()
        self.listen_task = asyncio.create_task(self.client.listen(ready))
        ready_task = asyncio.create_task(ready.wait())
        try:
            done, _ = await asyncio.wait(
                [self.listen_task, ready_task], timeout=30,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not ready.is_set():
                if self.listen_task in done:
                    await self.listen_task
                raise ConnectionError("Z-Wave JS hat keinen vollständigen Zustand geliefert")
        finally:
            ready_task.cancel()
            await asyncio.gather(ready_task, return_exceptions=True)
        self.controller = self.client.driver.controller
        self._wire_controller()
        await self._apply_inclusion_state(self.controller.inclusion_state)
        for node in self.controller.nodes.values():
            if not node.is_controller_node:
                self._wire_node(node)
                await self._publish_node(node)

    async def _close_connection(self):
        if self.client:
            with contextlib.suppress(Exception):
                await self.client.disconnect()
        if self.session:
            with contextlib.suppress(Exception):
                await self.session.close()
        self.client = self.controller = self.session = self.listen_task = None

    def _wire_controller(self):
        self.controller.on("node added", lambda event: self._node_added(event["node"]))
        self.controller.on("node removed", lambda event: self._node_removed(event["node"]))
        self.controller.on("grant security classes", self._grant_security)
        self.controller.on("validate dsk and enter pin", self._request_pin)
        self.controller.on("inclusion state changed", self._inclusion_state_changed)
        self.controller.on("inclusion aborted", lambda _event: self._finish_pairing())
        self.controller.on("inclusion failed", lambda event: self._pairing_failed("Anlernen fehlgeschlagen", event))
        self.controller.on("exclusion failed", lambda event: self._pairing_failed("Ausschließen fehlgeschlagen", event))
        self.controller.on("inclusion stopped", lambda _event: self._finish_pairing())
        self.controller.on("exclusion stopped", lambda _event: self._finish_pairing())

    def _wire_node(self, node):
        for event_name in ("value added", "value updated", "value removed", "metadata updated", "interview completed", "ready", "alive", "dead", "wake up", "sleep"):
            node.on(event_name, lambda _event, current=node: self._schedule(self._publish_node(current)))

    def _node_added(self, node):
        if not node.is_controller_node:
            self._wire_node(node)
            self._schedule(self._publish_node(node))
            self._finish_pairing()

    def _node_removed(self, node):
        published_id = self.context.stable_node_id(f"node-{node.node_id}")
        self.node_by_published_id.pop(published_id, None)
        for key in [key for key in self.attribute_values if key[0] == published_id]:
            self.attribute_values.pop(key, None)
        self._schedule(self.context.remove_node(published_id))

    def _grant_security(self, event):
        requested = event.get("requested_grant")
        if requested:
            self._schedule(self.controller.async_grant_security_classes(requested))

    def _request_pin(self, event):
        self.pending_dsk = str(event.get("dsk", ""))
        suffix = f" · DSK {self.pending_dsk}" if self.pending_dsk else ""
        self._schedule(self._status(f"S2-PIN erforderlich{suffix}"))

    def _inclusion_state_changed(self, event):
        self._schedule(self._apply_inclusion_state(event.get("state")))

    async def _apply_inclusion_state(self, state):
        status = _inclusion_state_status(state)
        if status == "Verbunden":
            self.pending_dsk = ""
        await self.context.set_status(status)

    def _finish_pairing(self):
        self.pending_dsk = ""
        self._schedule(self._status("Verbunden"))

    def _pairing_failed(self, status, event):
        self.pending_dsk = ""
        self._schedule(self._status(status, _event_reason(event)))

    def _status(self, status, error=None):
        return self.context.set_status(status, error)

    def _schedule(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.event_tasks.add(task)
        task.add_done_callback(self.event_tasks.discard)

    async def _publish_node(self, node):
        published_id = self.context.stable_node_id(f"node-{node.node_id}")
        self.node_by_published_id[published_id] = node
        attributes = []
        self.attribute_values = {key: val for key, val in self.attribute_values.items() if key[0] != published_id}
        for current, command in _presentable_values(node):
            identity = current.value_id
            offset = self._offset_for(node.node_id, identity)
            attribute_id = self.context.attribute_id(published_id, offset)
            attribute = _attribute_from_value(published_id, attribute_id, current, command)
            if not attribute:
                continue
            attributes.append(attribute)
            if command is not None:
                self.attribute_values[(published_id, attribute_id)] = command.value_id
        name = _node_name(node)
        details = _node_details(node)
        await self.context.publish_node({
            "id": published_id, "name": name, "note": f"Server · Z-Wave · {details}",
            "state": 2 if _node_is_dead(node) else (1 if node.ready else 5),
            "profile": _node_profile(attributes), "protocol": 17, "image": _node_image(attributes),
            "state_changed": time.time(), "attributes": attributes,
        })

    def _offset_for(self, zwave_node_id, value_id):
        key = f"{zwave_node_id}:{value_id}"
        if key not in self.value_offsets:
            self.value_offsets[key] = self.next_offset
            self.next_offset += 1
            self.context.save_state({"value_offsets": self.value_offsets})
        return int(self.value_offsets[key])


def _presentable_values(node):
    values = list(node.values.values())
    by_key = {(value.command_class, value.endpoint or 0, value.property_, value.property_key): value for value in values}
    result = []
    pairs = {"currentValue": "targetValue", "currentState": "targetState", "currentColor": "targetColor"}
    reverse_pairs = {target: current for current, target in pairs.items()}
    consumed = set()
    for value in values:
        if value.value_id in consumed or not _is_presentable(value):
            continue
        property_name = str(value.property_)
        feedback = value
        command = value if value.metadata.writeable else None

        # Z-Wave JS exposes many actuators as separate current/target values.
        # Always publish the current value as feedback and use only the target
        # value for writes, regardless of the order returned by Z-Wave JS.
        current_property = reverse_pairs.get(property_name)
        if current_property:
            current = by_key.get((value.command_class, value.endpoint or 0, current_property, value.property_key))
            if current and _is_presentable(current):
                feedback = current
                command = value if value.metadata.writeable else None
                consumed.add(current.value_id)

        target_property = pairs.get(str(feedback.property_))
        if target_property:
            target = by_key.get((feedback.command_class, feedback.endpoint or 0, target_property, feedback.property_key))
            if target and target.metadata.writeable:
                command = target
                consumed.add(target.value_id)
        consumed.add(feedback.value_id)
        result.append((feedback, command))
    return sorted(result, key=lambda pair: pair[0].value_id)


def _is_presentable(value):
    metadata = value.metadata
    if metadata.secret or metadata.readable is False or value.command_class in {112}:
        return False
    raw = value.value
    if raw is None and not metadata.writeable:
        return False
    return metadata.type not in {"buffer", "any"} and not isinstance(raw, (dict, list, bytes, bytearray))


def _attribute_from_value(node_id, attribute_id, value, command):
    raw = value.value
    metadata = value.metadata
    label = _value_label(value)
    unit = str(metadata.unit or "")
    states = _numeric_states(metadata.states)
    numeric = _numeric(raw)
    attribute_type = _attribute_type(value.command_class, label, unit, raw)
    item = {
        "id": attribute_id, "node_id": node_id, "type": attribute_type,
        "instance": (value.endpoint or 0) + 1, "name": label, "unit": unit,
        "current_value": numeric if numeric is not None else 0,
        "editable": command is not None, "last_changed": time.time(),
    }
    if command is not None:
        target_numeric = _numeric(command.value)
        item["target_value"] = target_numeric if target_numeric is not None else item["current_value"]
    if states:
        selected = next((text for number, text in states if numeric is not None and abs(number - numeric) < 0.000001), str(raw or ""))
        item["unit"] = "choice"
        item["data"] = json.dumps({"label": selected, "options": [{"value": number, "label": text} for number, text in states]}, ensure_ascii=False, separators=(",", ":"))
        item["minimum"] = min(number for number, _ in states)
        item["maximum"] = max(number for number, _ in states)
        item["step_value"] = 1
    elif numeric is None:
        item["unit"] = "text"
        item["data"] = str(raw if raw is not None else "Nicht verfügbar")
    else:
        if metadata.min is not None:
            item["minimum"] = metadata.min
        if metadata.max is not None:
            item["maximum"] = metadata.max
        if isinstance(raw, bool):
            item.update(minimum=0, maximum=1, step_value=1)
    return item


def _value_label(value):
    parts = [str(value.metadata.label or value.property_name or value.property_)]
    if value.property_key_name:
        parts.append(str(value.property_key_name))
    if value.endpoint:
        parts.append(f"Kanal {value.endpoint}")
    return " · ".join(part for part in parts if part)


def _numeric(value):
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _numeric_states(states):
    result = []
    for key, label in (states or {}).items():
        try:
            result.append((float(key), str(label)))
        except (TypeError, ValueError):
            continue
    return sorted(result)


def _command_value(value, incoming):
    if value.metadata.states:
        numeric = float(incoming)
        for key in value.metadata.states:
            try:
                if abs(float(key) - numeric) < 0.000001:
                    return int(numeric) if numeric.is_integer() else numeric
            except (TypeError, ValueError):
                continue
    if value.metadata.type == "boolean":
        return float(incoming) >= 0.5
    if value.metadata.type in {"number", "duration"}:
        numeric = float(incoming)
        return int(numeric) if numeric.is_integer() else numeric
    return incoming


def _attribute_type(command_class, label, unit, raw):
    text = f"{label} {unit}".casefold()
    if command_class == 128 or "batter" in text: return 8
    if "zieltemperatur" in text or "setpoint" in text: return 6
    if "temperatur" in text or "temperature" in text or unit in ("°C", "°F"): return 5
    if "feuchte" in text or "humidity" in text: return 7
    if "beweg" in text or "motion" in text: return 25
    if any(word in text for word in ("contact", "door", "window", "fenster", "tür")): return 14
    if "rauch" in text or "smoke" in text: return 16
    if "wasser" in text or "leak" in text: return 12
    if "co2" in text: return 20
    if "helligkeit" in text or "illuminance" in text or unit == "lx": return 11
    if unit in ("W", "watt"): return 3
    if unit.casefold() in ("kwh", "wh"): return 4
    if unit == "V": return 195
    if unit == "A": return 193
    if command_class == 98 or "lock" in text or "schloss" in text: return 232
    if "tamper" in text or "manipulation" in text: return 30
    if command_class == 38: return 15 if any(word in text for word in ("position", "cover", "blind", "shutter")) else 2
    if command_class == 37 or isinstance(raw, bool): return 1
    return 213


def _node_name(node):
    configured = str(node.name or "").strip()
    if configured:
        return configured
    device_config = getattr(node, "device_config", None)
    label = str(
        getattr(node, "label", None)
        or getattr(device_config, "label", None)
        or getattr(device_config, "description", None)
        or ""
    ).strip()
    manufacturer = str(
        getattr(node, "manufacturer", None)
        or getattr(device_config, "manufacturer", None)
        or ""
    ).strip()
    return " ".join(part for part in (manufacturer, label) if part) or f"Z-Wave Node {node.node_id}"


def _node_details(node):
    """Keep the identity selected by the Z-Wave JS Config DB visible in SmartHomeBoard."""
    device_config = getattr(node, "device_config", None)
    manufacturer = str(
        getattr(node, "manufacturer", None)
        or getattr(device_config, "manufacturer", None)
        or ""
    ).strip()
    product = str(
        getattr(node, "label", None)
        or getattr(device_config, "label", None)
        or ""
    ).strip()
    description = str(getattr(device_config, "description", None) or "").strip()
    manufacturer_id = _hex_id(getattr(node, "manufacturer_id", None))
    product_type = _hex_id(getattr(node, "product_type", None))
    product_id = _hex_id(getattr(node, "product_id", None))
    firmware = str(getattr(node, "firmware_version", None) or "").strip()
    device_class = getattr(node, "device_class", None)
    specific_class = str(getattr(getattr(device_class, "specific", None), "label", "") or "").strip()
    parts = [f"Node {node.node_id}"]
    if manufacturer:
        parts.append(f"Hersteller {manufacturer}" + (f" ({manufacturer_id})" if manufacturer_id else ""))
    elif manufacturer_id:
        parts.append(f"Hersteller-ID {manufacturer_id}")
    if product:
        parts.append(f"Produkt {product}")
    if description and description.casefold() != product.casefold():
        parts.append(description)
    if product_type:
        parts.append(f"Produkttyp {product_type}")
    if product_id:
        parts.append(f"Produkt-ID {product_id}")
    if specific_class and specific_class.casefold() not in {"not used", "unknown"}:
        parts.append(f"Geräteklasse {specific_class}")
    if firmware:
        parts.append(f"Firmware {firmware}")
    if device_config and getattr(device_config, "supports_zwave_plus", None) is True:
        parts.append("Z-Wave Plus")
    return " · ".join(parts)


def _hex_id(value):
    try:
        return f"0x{int(value):04x}"
    except (TypeError, ValueError):
        return ""


def _node_is_dead(node):
    return int(getattr(node, "status", 0)) == 3


def _node_profile(attributes):
    types = {item["type"] for item in attributes}
    if 15 in types: return 2004
    if 6 in types: return 3003
    if 25 in types: return 4010
    if 14 in types: return 2000
    if 1 in types: return 10
    return 0


def _node_image(attributes):
    types = {item["type"] for item in attributes}
    if 15 in types: return "nodeicon_shutter"
    if 6 in types: return "thermometer"
    if 25 in types: return "sensor.tag.radiowaves.forward"
    if 1 in types: return "powerplug"
    return "wave.3.right"


def _event_reason(event):
    return str(event.get("reason") or event.get("error") or "Unbekannter Z-Wave-Fehler")


def _inclusion_state_status(state):
    try:
        value = int(state)
    except (TypeError, ValueError):
        return "Verbunden"
    return {
        1: "Anlernmodus aktiv",
        2: "Ausschlussmodus aktiv",
        3: "Z-Wave-Controller beschäftigt",
        4: "SmartStart aktiv",
    }.get(value, "Verbunden")
