import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import random
import re
import time

import httpx
import websockets

from server.shelly_discovery import discover_shelly_ipv4


log = logging.getLogger("smarthomeboard.shelly")
RPC_SOURCE = "smarthomeboard-server"
BLE_SCRIPT_NAME = "SmartHomeBoard BLE v1"
BLE_SCANNER_SCRIPT = '''// SmartHomeBoard BTHome relay v1
const BTHOME_SERVICE = "fcd2";
function toHex(buffer) {
  let value = "";
  for (let index = 0; index < buffer.length; index++) {
    let byte = buffer.at(index).toString(16);
    value += byte.length === 1 ? "0" + byte : byte;
  }
  return value;
}
function scan(event, result) {
  if (event !== BLE.Scanner.SCAN_RESULT || !result || !result.service_data) return;
  let data = result.service_data[BTHOME_SERVICE];
  if (typeof data !== "string" || data.length === 0) return;
  Shelly.emitEvent("shb_bthome", {
    addr: result.addr,
    rssi: result.rssi,
    local_name: result.local_name || "",
    adv_data: "d2fc" + toHex(data)
  });
}
if (typeof BLE.Scanner.Subscribe === "function") {
  BLE.Scanner.Subscribe(scan);
} else {
  BLE.Scanner.subscribe(scan);
}
let scanOptions = {duration_ms: BLE.Scanner.INFINITE_SCAN, active: false};
if (typeof BLE.Scanner.Start === "function") {
  BLE.Scanner.Start(scanOptions);
} else {
  BLE.Scanner.start(scanOptions);
}
'''

BLU_TEMPLATES = {
    "button": {"title": "Shelly BLU Button", "objects": []},
    "rc_button_4": {"title": "Shelly BLU RC Button 4", "objects": []},
    "door_window": {"title": "Shelly BLU Door/Window", "objects": [1, 5, 45, 63]},
    "motion": {"title": "Shelly BLU Motion", "objects": [1, 5, 33, 100]},
    "ht": {"title": "Shelly BLU H&T", "objects": [1, 46, 69]},
    "generic": {"title": "BTHome Sensor", "objects": [1]},
}

BTHOME_OBJECTS = {
    1: ("Batterie", 8, "%"),
    2: ("Temperatur", 5, "°C"),
    3: ("Luftfeuchtigkeit", 7, "%"),
    4: ("Luftdruck", 94, "hPa"),
    5: ("Helligkeit", 23, "lx"),
    33: ("Bewegung", 13, ""),
    45: ("Kontakt", 14, ""),
    46: ("Luftfeuchtigkeit", 7, "%"),
    58: ("Taster", 40, "text"),
    63: ("Öffnungswinkel", 222, "°"),
    69: ("Temperatur", 5, "°C"),
    100: ("Helligkeitsstufe", 23, ""),
}


def manifest():
    template_options = [{"value": key, "label": value["title"]} for key, value in BLU_TEMPLATES.items()]
    return {
        "id": "shelly",
        "name": "Shelly Gen2+ / BLU",
        "version": "1.0.0",
        "icon": "dot.radiowaves.left.and.right",
        "description": (
            "Erkennt Shelly Gen2, Gen3 und Gen4 per mDNS, hält pro Gerät eine RPC-WebSocket-Verbindung "
            "und ordnet Add-on-Komponenten dem jeweiligen Hauptgerät zu. BLU/BTHome-Geräte werden "
            "gateway-unabhängig über ihre MAC-Adresse zusammengeführt."
        ),
        "supportsDiscovery": True,
        "supportsMultipleInstances": False,
        "fields": [
            {"key": "manual_hosts", "type": "text", "title": "Zusätzliche IP-Adressen (optional)",
             "placeholder": "192.168.178.40, 192.168.178.41",
             "help": "Ergänzung, falls Multicast/mDNS zwischen Docker und dem LAN gefiltert wird."},
            {"key": "password", "type": "password", "title": "Shelly-Gerätepasswort (optional)",
             "help": "Benutzer ist bei Shelly Gen2+ immer admin. Das Passwort wird getrennt als Server-Secret gespeichert."},
            {"key": "scan_seconds", "type": "duration", "title": "Netzwerkscan", "default": 60,
             "minimum": 20, "maximum": 3600, "unit": "s"},
        ],
        "actions": [
            {"id": "refresh", "title": "Shellys jetzt neu suchen", "icon": "arrow.clockwise"},
            {"id": "start_blu_learning", "title": "BLU-Gerät 30 Sekunden anlernen", "icon": "antenna.radiowaves.left.and.right",
             "fields": [
                 {"key": "template", "type": "select", "title": "Gerätevorlage", "required": True,
                  "options": template_options},
                 {"key": "name", "type": "text", "title": "Anzeigename", "placeholder": "z. B. Fenster Büro"},
                 {"key": "key", "type": "password", "title": "BTHome AES-Schlüssel (nur verschlüsselte Geräte)",
                  "pattern": "[0-9a-fA-F]{32}", "maxlength": 32},
             ]},
            {"id": "stop_blu_learning", "title": "BLU-Anlernen beenden", "icon": "stop.circle", "role": "destructive"},
        ],
    }


def create(configuration, context):
    return ShellyAdapter(configuration, context)


class ShellyAdapter:
    def __init__(self, configuration, context):
        self.configuration = configuration
        self.context = context
        self.http = None
        self.password = ""
        self.gateways = {}
        self.host_to_device = {}
        self.ws_tasks = {}
        self.ws_connections = {}
        self.ws_diagnostics = []
        self.discovery_request_ids = {}
        self.ble_script_ready = set()
        self.discovery_task = None
        self.learning_task = None
        self.learning = None
        self.last_blu_events = {}
        self.attribute_controls = {}
        state = context.load_state({}) or {}
        self.learned_blu = state.get("blu_devices", {}) if isinstance(state.get("blu_devices", {}), dict) else {}
        for mac, device in self.learned_blu.items():
            legacy_key = device.pop("key", "") if isinstance(device, dict) else ""
            if legacy_key:
                context.save_secret(_blu_secret_name(mac), legacy_key)
        self.attribute_offsets = state.get("attribute_offsets", {}) if isinstance(state.get("attribute_offsets", {}), dict) else {}
        self.next_attribute_offset = max([int(value) for value in self.attribute_offsets.values()] + [0]) + 1
        self.blu_values = state.get("blu_values", {}) if isinstance(state.get("blu_values", {}), dict) else {}
        self.startup_status = "Shelly-Suche läuft"
        self.startup_error = None

    async def start(self):
        supplied_password = str(self.configuration.get("password", "")).strip()
        if supplied_password:
            self.context.save_secret("password", supplied_password)
            self.context.clear_configuration_value("password")
        self.password = supplied_password or str(self.context.load_secret("password", ""))
        auth = httpx.DigestAuth("admin", self.password) if self.password else None
        self.http = httpx.AsyncClient(timeout=8, trust_env=False, auth=auth)
        count = await self._refresh_discovery()
        self.startup_status = f"Verbunden · {count} Shelly-Geräte" if count else "Bereit · keine Shellys gefunden"
        self.discovery_task = asyncio.create_task(self._discovery_loop())

    async def stop(self):
        for task in [self.discovery_task, self.learning_task, *self.ws_tasks.values()]:
            if task:
                task.cancel()
        tasks = [task for task in [self.discovery_task, self.learning_task, *self.ws_tasks.values()] if task]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.ws_tasks.clear()
        self.ws_connections.clear()
        if self.http:
            await self.http.aclose()

    async def health_check(self):
        count = await self._refresh_discovery()
        if not count:
            raise ConnectionError("Keine Shelly Gen2+/Gen3/Gen4-Geräte im lokalen Netz gefunden")

    async def action(self, action_id, payload):
        if action_id == "refresh":
            count = await self._refresh_discovery()
            return {"status": "refreshed", "gateways": count, **self._management()}
        if action_id == "get_management":
            return self._management()
        if action_id == "start_blu_learning":
            return await self._start_blu_learning(payload)
        if action_id == "stop_blu_learning":
            await self._stop_blu_learning()
            return self._management()
        if action_id == "delete_blu_device":
            mac = _mac(payload.get("mac"))
            if not mac or mac not in self.learned_blu:
                raise ValueError("Das BLU-Gerät ist nicht angelernt")
            node_id = self.context.stable_node_id(f"bthome:{mac}")
            await asyncio.gather(
                *(self._unregister_blu(gateway, mac) for gateway in self.gateways.values()),
                return_exceptions=True,
            )
            self.learned_blu.pop(mac, None)
            self.blu_values.pop(mac, None)
            self.context.save_secret(_blu_secret_name(mac), "")
            self._save_state()
            await self.context.remove_node(node_id)
            return self._management()
        raise ValueError("Unbekannte Shelly-Aktion")

    async def set_value(self, node_id, attribute_id, value):
        control = self.attribute_controls.get(int(attribute_id))
        if not control or int(node_id) != control["node_id"]:
            raise ValueError("Dieses Shelly-Attribut ist nicht schreibbar")
        params = {"id": control["component_id"]}
        if control["field"] == "output":
            params["on"] = float(value) >= 0.5
            method = f"{control['namespace']}.Set"
        elif control["field"] == "current_pos":
            params["pos"] = max(0, min(100, round(float(value))))
            method = "Cover.GoToPosition"
        elif control["field"] == "brightness":
            params["brightness"] = max(0, min(100, round(float(value))))
            method = "Light.Set"
        else:
            raise ValueError("Dieses Shelly-Attribut ist nicht schreibbar")
        await self._rpc(control["host"], method, params)
        await self._reload_gateway(control["host"])

    async def _discovery_loop(self):
        while True:
            try:
                await asyncio.sleep(max(20, min(3600, int(float(self.configuration.get("scan_seconds", 60))))))
                count = await self._refresh_discovery()
                await self.context.set_status(f"Verbunden · {count} Shelly-Geräte")
            except asyncio.CancelledError:
                self.ws_connections.pop(device_id, None)
                if device_id in self.gateways:
                    self.gateways[device_id]["ws_connected"] = False
                return
            except Exception as error:
                await self.context.set_status("Shelly-Suche gestört", str(error))

    async def _refresh_discovery(self):
        manual = re.split(r"[,;\s]+", str(self.configuration.get("manual_hosts", "")))
        hosts = {item.strip() for item in manual if item.strip()}
        try:
            hosts.update(await asyncio.to_thread(discover_shelly_ipv4, 3.0))
        except OSError as error:
            log.warning("Shelly-mDNS-Suche fehlgeschlagen: %s", error)
        hosts = sorted(hosts)
        results = await asyncio.gather(*(self._ensure_gateway(host) for host in hosts), return_exceptions=True)
        for host, result in zip(hosts, results):
            if isinstance(result, Exception):
                log.debug("Shelly %s wurde übersprungen: %s", host, result)
        return len(self.gateways)

    async def _ensure_gateway(self, host):
        info = await self._device_info(host)
        generation = int(_number(info.get("gen")))
        if generation not in (2, 3, 4):
            raise ValueError("Kein Shelly Gen2+/Gen3/Gen4")
        device_id = str(info.get("id") or info.get("mac") or host).lower()
        old_host = self.gateways.get(device_id, {}).get("host")
        if old_host and old_host != host:
            self.host_to_device.pop(old_host, None)
        self.host_to_device[host] = device_id
        await self._load_gateway(host, info)
        gateway = self.gateways[device_id]
        if gateway.get("script_support"):
            try:
                await self._ensure_ble_script(gateway)
                gateway["blu_mode"] = "native+script" if gateway.get("native_bthome") else "script"
            except Exception as error:
                gateway["blu_mode"] = "native" if gateway.get("native_bthome") else "unavailable"
                gateway["blu_error"] = str(error)
                log.warning("Shelly-BLE-Script auf %s nicht verfügbar: %s", host, error)
        if gateway.get("native_bthome") and any(self._gateway_needs_blu_sync(gateway, mac) for mac in self.learned_blu):
            for mac in self.learned_blu:
                with contextlib.suppress(Exception):
                    await self._register_blu(gateway, mac)
            await self._reload_gateway(host)
        current = self.ws_tasks.get(device_id)
        if not current or current.done() or old_host != host:
            if current:
                current.cancel()
            self.ws_tasks[device_id] = asyncio.create_task(self._websocket_loop(device_id, host))
        return device_id

    async def _device_info(self, host):
        response = await self.http.get(f"http://{host}/shelly")
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError("Ungültige Shelly-Geräteinformation")
        return result

    async def _load_gateway(self, host, info=None):
        info = info or await self._device_info(host)
        status, config, components, method_result = await asyncio.gather(
            self._rpc(host, "Shelly.GetStatus"),
            self._rpc(host, "Shelly.GetConfig"),
            self._get_components(host),
            self._rpc(host, "Shelly.ListMethods"),
        )
        device_id = str(info.get("id") or info.get("mac") or host).lower()
        gateway = self.gateways.setdefault(device_id, {})
        for item in components:
            key = str(item.get("key", ""))
            component_status = item.get("status")
            if key and isinstance(component_status, dict):
                status[key] = _deep_merge(status.get(key), component_status)
            component_config = item.get("config")
            if key and isinstance(component_config, dict):
                config[key] = _deep_merge(config.get(key), component_config)
        methods = method_result.get("methods", method_result) if isinstance(method_result, dict) else method_result
        methods = set(methods if isinstance(methods, list) else [])
        gateway.update({"host": host, "info": info, "status": status, "config": config, "components": components,
                        "methods": methods, "native_bthome": "BTHome.StartDeviceDiscovery" in methods,
                        "script_support": {"Script.List", "Script.Create", "Script.PutCode", "Script.SetConfig",
                                           "Script.GetStatus", "Script.Start"}.issubset(methods)})
        gateway["blu_mode"] = "native" if gateway["native_bthome"] else gateway.get("blu_mode", "unavailable")
        self._index_bthome_components(gateway)
        await self._publish_gateway(gateway)
        await self._publish_known_blu()

    async def _reload_gateway(self, host):
        info = await self._device_info(host)
        await self._load_gateway(host, info)

    async def _rpc(self, host, method, params=None):
        frame = {"id": random.randint(1, 2_000_000_000), "src": RPC_SOURCE, "method": method}
        if params:
            frame["params"] = params
        response = await self.http.post(f"http://{host}/rpc", json=frame)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get("error"):
            error = payload["error"]
            raise ValueError(str(error.get("message") if isinstance(error, dict) else error))
        return payload.get("result", payload) if isinstance(payload, dict) else payload

    async def _get_components(self, host):
        result = []
        offset = 0
        for _ in range(20):
            page = await self._rpc(host, "Shelly.GetComponents", {"offset": offset, "include": ["config", "status"]})
            components = page.get("components", []) if isinstance(page, dict) else []
            result.extend(item for item in components if isinstance(item, dict) and item.get("key"))
            total = int(_number(page.get("total"))) if isinstance(page, dict) else len(result)
            if not components or len(result) >= total:
                break
            offset += len(components)
        return result

    async def _ensure_ble_script(self, gateway):
        if gateway["host"] in self.ble_script_ready:
            return
        response = await self._rpc(gateway["host"], "Script.List")
        scripts = response.get("scripts", response) if isinstance(response, dict) else response
        scripts = scripts if isinstance(scripts, list) else []
        existing = next((item for item in scripts if str(item.get("name", "")) == BLE_SCRIPT_NAME), None)
        if existing:
            script_id = int(_number(existing.get("id")))
        else:
            created = await self._rpc(gateway["host"], "Script.Create", {"name": BLE_SCRIPT_NAME})
            script_id = int(_number(created.get("id")))
            if script_id <= 0:
                raise ValueError("Shelly hat keinen freien Script-Platz")
        with contextlib.suppress(Exception):
            await self._rpc(gateway["host"], "Script.Stop", {"id": script_id})
        await self._rpc(gateway["host"], "Script.PutCode", {"id": script_id, "code": BLE_SCANNER_SCRIPT, "append": False})
        await self._rpc(gateway["host"], "Script.SetConfig", {"id": script_id, "config": {"enable": True}})
        status = await self._rpc(gateway["host"], "Script.GetStatus", {"id": script_id})
        if not status.get("running"):
            await self._rpc(gateway["host"], "Script.Start", {"id": script_id})
        gateway["ble_script_id"] = script_id
        self.ble_script_ready.add(gateway["host"])

    async def _read_cloud_relay_candidates(self, gateway):
        offset = 0
        for _ in range(20):
            result = await self._rpc(gateway["host"], "BLE.CloudRelay.ListInfos", {"offset": offset})
            devices = result.get("devices", {}) if isinstance(result, dict) else {}
            entries = []
            if isinstance(devices, dict):
                entries = [(mac, info) for mac, info in devices.items() if isinstance(info, dict)]
            elif isinstance(devices, list):
                for item in devices:
                    if not isinstance(item, dict):
                        continue
                    nested = [(mac, info) for mac, info in item.items()
                              if _mac(mac) and isinstance(info, dict)]
                    if nested:
                        entries.extend(nested)
                    else:
                        entries.append((item.get("mac") or item.get("addr") or item.get("address"), item))
            for address, info in entries:
                service_data = info.get("sdata") if isinstance(info.get("sdata"), dict) else {}
                encoded = next((value for key, value in service_data.items()
                                if str(key).lower().replace("0x", "") == "fcd2"), None)
                if not encoded:
                    continue
                try:
                    raw = base64.b64decode(str(encoded), validate=True)
                except (ValueError, TypeError):
                    continue
                payload = {"addr": address, "rssi": info.get("rssi", -999),
                           "local_name": info.get("name") or "", "adv_data": "d2fc" + raw.hex()}
                self._record_ws_diagnostic(gateway, "BLE.CloudRelay.ListInfos", payload, True)
                await self._handle_raw_bthome(gateway, payload)
            count = int(_number(result.get("count"))) if isinstance(result, dict) else len(entries)
            total = int(_number(result.get("total"))) if isinstance(result, dict) else len(entries)
            if count <= 0 or offset + count >= total:
                break
            offset += count

    async def _handle_raw_bthome(self, gateway, data):
        mac = _mac(data.get("addr"))
        decoded = _parse_bthome_advertisement(data.get("adv_data"))
        if not mac or decoded is None:
            return
        rssi = int(_number(data.get("rssi")) or -999)
        if self.learning is not None and mac not in self.learned_blu:
            device = {"addr": mac, "local_name": str(data.get("local_name", "")), "rssi": rssi,
                      "encrypted": decoded["encrypted"]}
            candidate = self.learning["candidates"].setdefault(
                mac, {"gateways": set(), "rssi": -999, "device": device}
            )
            candidate["gateways"].add(gateway["host"])
            candidate["rssi"] = max(candidate["rssi"], rssi)
        if mac not in self.learned_blu or decoded["encrypted"]:
            return
        packet = decoded["values"].get(0)
        fingerprint = packet if packet is not None else hashlib.sha256(str(data.get("adv_data", "")).encode()).hexdigest()[:16]
        dedupe = f"{mac}:raw:{fingerprint}"
        if time.time() - self.last_blu_events.get(dedupe, 0) < 5:
            return
        self.last_blu_events[dedupe] = time.time()
        values = self.blu_values.setdefault(mac, {})
        stamp = time.time()
        values["rssi"] = {"value": rssi, "ts": stamp}
        for obj_id, value in decoded["values"].items():
            if obj_id == 0:
                continue
            if obj_id == 58:
                values["event:0"] = {"value": _bthome_button_event(value), "ts": stamp}
            else:
                values[f"obj:{obj_id}:0"] = {"value": value, "ts": stamp}
        self._save_state()
        await self._publish_blu(mac)

    async def _websocket_loop(self, device_id, host):
        delay = 1
        while True:
            try:
                async with websockets.connect(f"ws://{host}/rpc", open_timeout=8, ping_interval=25, ping_timeout=10) as socket:
                    request_id = random.randint(1, 2_000_000_000)
                    request = {"id": request_id, "src": RPC_SOURCE, "method": "Shelly.GetStatus"}
                    await socket.send(json.dumps(request, separators=(",", ":")))
                    delay = 1
                    async for raw in socket:
                        message = json.loads(raw)
                        if message.get("id") == request_id and _error_code(message) == 401:
                            if not self.password:
                                raise PermissionError(f"Shelly {host} verlangt ein Gerätepasswort")
                            challenge = json.loads(message["error"]["message"])
                            request_id += 1
                            authenticated = {"id": request_id, "src": RPC_SOURCE, "method": "Shelly.GetStatus",
                                             "auth": _websocket_auth(challenge, self.password)}
                            await socket.send(json.dumps(authenticated, separators=(",", ":")))
                            continue
                        if message.get("id") == request_id and not message.get("error"):
                            self.ws_connections[device_id] = socket
                            if device_id in self.gateways:
                                self.gateways[device_id]["ws_connected"] = True
                        await self._handle_websocket_message(device_id, message)
            except asyncio.CancelledError:
                return
            except Exception as error:
                log.warning("Shelly-WebSocket %s getrennt: %s", host, error)
                if self.ws_connections.get(device_id) is not None:
                    self.ws_connections.pop(device_id, None)
                if device_id in self.gateways:
                    self.gateways[device_id]["ws_connected"] = False
                await asyncio.sleep(delay)
                delay = min(60, delay * 2)

    async def _handle_websocket_message(self, device_id, message):
        gateway = self.gateways.get(device_id)
        if not gateway or not isinstance(message, dict):
            return
        method = message.get("method")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if message.get("id") in self.discovery_request_ids:
            self.discovery_request_ids.pop(message.get("id"), None)
            self._record_ws_diagnostic(gateway, "BTHome.StartDeviceDiscovery ← WS", message, True)
            if message.get("error"):
                gateway["blu_error"] = str(message.get("error"))
        if method in ("NotifyStatus", "NotifyFullStatus", "NotifyEvent"):
            keys = [key for key in params if key != "ts"]
            relevant = any(str(key).startswith("bthome") for key in keys)
            if method == "NotifyEvent":
                relevant = relevant or any(
                    str(event.get("component", "")).startswith(("bthome", "script:"))
                    for event in params.get("events", []) if isinstance(event, dict)
                )
            if self.learning is not None or relevant:
                self._record_ws_diagnostic(gateway, method, message, relevant)
        if method in ("NotifyStatus", "NotifyFullStatus"):
            updates = {key: value for key, value in params.items() if ":" in key or key in gateway.get("status", {})}
            for key, value in updates.items():
                if isinstance(value, dict):
                    current = gateway["status"].setdefault(key, {})
                    gateway["status"][key] = _deep_merge(current, value)
                    if key.startswith("bthome") and key not in gateway.get("blu_components", {}):
                        with contextlib.suppress(Exception):
                            await self._reload_gateway(gateway["host"])
                    await self._handle_blu_status(gateway, key, gateway["status"][key])
            await self._publish_gateway(gateway)
        elif method == "NotifyEvent":
            for event in params.get("events", []):
                if isinstance(event, dict):
                    await self._handle_event(gateway, event)

    async def _handle_event(self, gateway, event):
        if event.get("component") == "sys" and event.get("event") in {"component_added", "component_removed", "config_changed"}:
            await self._reload_gateway(gateway["host"])
            return
        if event.get("component") == "bthome" and event.get("event") == "device_discovered":
            device = event.get("device") if isinstance(event.get("device"), dict) else {}
            mac = _mac(device.get("addr") or (device.get("shelly_mfdata") or {}).get("mac"))
            if mac and self.learning is not None and mac not in self.learned_blu:
                candidate = self.learning["candidates"].setdefault(mac, {"gateways": set(), "rssi": -999, "device": device})
                candidate["gateways"].add(gateway["host"])
                candidate["rssi"] = max(candidate["rssi"], int(_number(device.get("rssi")) or -999))
            return

        if str(event.get("component", "")).startswith("script:") and event.get("event") == "shb_bthome":
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            await self._handle_raw_bthome(gateway, data)
            return

        component = str(event.get("component", ""))
        if component.startswith("bthome") and component not in gateway.get("blu_components", {}):
            with contextlib.suppress(Exception):
                await self._reload_gateway(gateway["host"])
        mapping = gateway.get("blu_components", {}).get(component)
        if not mapping:
            return
        mac = mapping["mac"]
        self._observe_learning_candidate(gateway, mac, mapping.get("name", ""), event.get("rssi"))
        event_name = str(event.get("event", "event"))
        idx = int(_number(event.get("idx")))
        stamp = float(_number(event.get("ts")) or time.time())
        dedupe = f"{mac}:{event_name}:{idx}"
        if stamp - self.last_blu_events.get(dedupe, 0) < 1.5:
            return
        self.last_blu_events[dedupe] = stamp
        values = self.blu_values.setdefault(mac, {})
        values[f"event:{idx}"] = {"value": event_name, "ts": stamp}
        self._save_state()
        await self._publish_blu(mac)

    async def _handle_blu_status(self, gateway, component, status):
        mapping = gateway.get("blu_components", {}).get(component)
        if not mapping:
            return
        mac = mapping["mac"]
        self._observe_learning_candidate(gateway, mac, mapping.get("name", ""), status.get("rssi"))
        values = self.blu_values.setdefault(mac, {})
        if mapping["kind"] == "device":
            packet = status.get("packet_id")
            packet_key = f"{mac}:packet:{packet}"
            if packet is not None and time.time() - self.last_blu_events.get(packet_key, 0) < 2:
                return
            self.last_blu_events[packet_key] = time.time()
            for key in ("battery", "rssi", "fw_ver"):
                if status.get(key) is not None:
                    values[key] = {"value": status[key], "ts": status.get("last_update_ts", time.time())}
        else:
            obj_id = mapping.get("obj_id")
            idx = mapping.get("idx", 0)
            value = status.get("value", status.get("current_value"))
            if value is not None:
                values[f"obj:{obj_id}:{idx}"] = {"value": value, "ts": status.get("last_update_ts", time.time())}
        self._save_state()
        await self._publish_blu(mac)

    def _index_bthome_components(self, gateway):
        mapping = {}
        for item in gateway.get("components", []):
            key = str(item.get("key", ""))
            config = item.get("config") if isinstance(item.get("config"), dict) else {}
            mac = _mac(config.get("addr"))
            if not mac:
                continue
            if key.startswith("bthomedevice:"):
                mapping[key] = {"kind": "device", "mac": mac, "name": str(config.get("name") or "")}
            elif key.startswith("bthomesensor:"):
                mapping[key] = {"kind": "sensor", "mac": mac,
                                "name": str(config.get("name") or ""),
                                "obj_id": int(_number(config.get("obj_id"))),
                                "idx": int(_number(config.get("idx", config.get("obj_idx", 0))))}
        gateway["blu_components"] = mapping

    def _observe_learning_candidate(self, gateway, mac, name="", rssi=None):
        if self.learning is None or mac in self.learned_blu:
            return
        signal = int(_number(rssi)) if rssi is not None else -999
        device = {"addr": mac, "local_name": str(name or ""), "rssi": signal, "encrypted": False}
        candidate = self.learning["candidates"].setdefault(
            mac, {"gateways": set(), "rssi": -999, "device": device}
        )
        candidate["gateways"].add(gateway["host"])
        candidate["rssi"] = max(candidate["rssi"], signal)

    async def _start_gateway_discovery(self, device_id, gateway):
        socket = self.ws_connections.get(device_id)
        if socket is not None:
            frame = {"id": random.randint(1, 2_000_000_000), "src": RPC_SOURCE,
                     "method": "BTHome.StartDeviceDiscovery", "params": {"duration": 30}}
            try:
                await socket.send(json.dumps(frame, separators=(",", ":")))
                self.discovery_request_ids[frame["id"]] = device_id
                self._record_ws_diagnostic(gateway, "BTHome.StartDeviceDiscovery → WS", frame, True)
                return {"channel": "websocket"}
            except Exception as error:
                log.warning("BTHome-Suche über WebSocket %s fehlgeschlagen: %s", gateway["host"], error)
        result = await self._rpc(gateway["host"], "BTHome.StartDeviceDiscovery", {"duration": 30})
        self._record_ws_diagnostic(gateway, "BTHome.StartDeviceDiscovery → HTTP", result, True)
        return result

    def _record_ws_diagnostic(self, gateway, method, message, relevant):
        try:
            content = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            content = str(message)
        self.ws_diagnostics.append({
            "timestamp": time.time(), "gateway": self._gateway_name(gateway), "host": gateway.get("host", ""),
            "method": str(method), "blu_relevant": bool(relevant), "content": content[:4000],
        })
        if len(self.ws_diagnostics) > 100:
            del self.ws_diagnostics[:-100]

    async def _start_blu_learning(self, payload):
        template = str(payload.get("template", "generic"))
        if template not in BLU_TEMPLATES:
            raise ValueError("Bitte eine gültige BLU-Gerätevorlage wählen")
        if self.learning_task and not self.learning_task.done():
            raise ValueError("Das BLU-Anlernen läuft bereits")
        key = str(payload.get("key", "")).strip().lower()
        if key and not re.fullmatch(r"[0-9a-f]{32}", key):
            raise ValueError("Der BTHome AES-Schlüssel muss aus genau 32 Hex-Zeichen bestehen")
        self.learning = {"template": template, "name": str(payload.get("name", "")).strip(),
                         "key": key, "candidates": {},
                         "started_at": time.time(), "duration": 30}
        self.ws_diagnostics = []
        relay_calls = [self._read_cloud_relay_candidates(gateway) for gateway in self.gateways.values()
                       if "BLE.CloudRelay.ListInfos" in gateway.get("methods", set())]
        await asyncio.gather(*relay_calls, return_exceptions=True)
        calls = [self._start_gateway_discovery(device_id, gateway)
                 for device_id, gateway in self.gateways.items() if gateway.get("native_bthome")]
        results = await asyncio.gather(*calls, return_exceptions=True)
        script_gateways = [gateway for gateway in self.gateways.values()
                           if "script" in gateway.get("blu_mode", "")]
        if (not calls or all(isinstance(result, Exception) for result in results)) and not script_gateways:
            self.learning = None
            details = next((gateway.get("blu_error") for gateway in self.gateways.values() if gateway.get("blu_error")), "")
            raise ValueError("Kein gefundener Shelly unterstützt die BTHome-Gerätesuche" + (f": {details}" if details else ""))
        self.learning_task = asyncio.create_task(self._finish_blu_learning())
        await self.context.set_status("BLU-Anlernen aktiv · 30 Sekunden")
        return self._management()

    async def _stop_blu_learning(self):
        if self.learning_task:
            self.learning_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.learning_task
        self.learning_task = None
        self.learning = None
        await self.context.set_status(f"Verbunden · {len(self.gateways)} Shelly-Geräte")

    async def _finish_blu_learning(self):
        try:
            await asyncio.sleep(30.5)
            if not self.learning or not self.learning["candidates"]:
                await self.context.set_status("BLU-Anlernen beendet · kein neues Gerät empfangen")
                self.learning = None
                return
            mac, candidate = max(self.learning["candidates"].items(), key=lambda item: (len(item[1]["gateways"]), item[1]["rssi"]))
            if candidate["device"].get("encrypted") and not self.learning["key"]:
                self.learning = None
                await self.context.set_status("BLU-Anlernen beendet · AES-Schlüssel fehlt")
                return
            template = self.learning["template"]
            default_name = candidate["device"].get("local_name") or BLU_TEMPLATES[template]["title"]
            self.learned_blu[mac] = {"mac": mac, "template": template,
                                             "name": self.learning["name"] or default_name,
                                             "created_at": time.time()}
            if self.learning["key"]:
                self.context.save_secret(_blu_secret_name(mac), self.learning["key"])
            self.learning = None
            self._save_state()
            await asyncio.gather(*(self._register_blu(gateway, mac) for gateway in self.gateways.values()
                                   if gateway.get("native_bthome")), return_exceptions=True)
            await asyncio.gather(*(self._reload_gateway(gateway["host"]) for gateway in list(self.gateways.values())), return_exceptions=True)
            await self._publish_blu(mac)
            await self.context.set_status(f"BLU-Gerät angelernt · {self.learned_blu[mac]['name']}")
        finally:
            self.learning_task = None

    async def _register_blu(self, gateway, mac):
        known = {item["mac"] for item in gateway.get("blu_components", {}).values() if item["kind"] == "device"}
        device = self.learned_blu[mac]
        if mac not in known:
            config = {"addr": mac, "name": device["name"]}
            key = str(self.context.load_secret(_blu_secret_name(mac), device.get("key", ""))).strip()
            if key:
                config["key"] = key
            await self._rpc(gateway["host"], "BTHome.AddDevice", {"config": config})
        await asyncio.sleep(0.4)
        await self._reload_gateway(gateway["host"])
        existing = {(item.get("obj_id"), item.get("idx", 0)) for item in gateway.get("blu_components", {}).values()
                    if item.get("mac") == mac and item["kind"] == "sensor"}
        for obj_id in BLU_TEMPLATES[device["template"]]["objects"]:
            if (obj_id, 0) in existing:
                continue
            with contextlib.suppress(Exception):
                await self._rpc(gateway["host"], "BTHome.AddSensor",
                                {"config": {"addr": mac, "obj_id": obj_id, "obj_idx": 0,
                                            "name": BTHOME_OBJECTS.get(obj_id, (f"Objekt {obj_id}",))[0]}})

    async def _unregister_blu(self, gateway, mac):
        matching = [
            (component, mapping)
            for component, mapping in gateway.get("blu_components", {}).items()
            if mapping.get("mac") == mac
        ]
        for component, mapping in matching:
            if mapping.get("kind") == "sensor":
                with contextlib.suppress(Exception):
                    await self._rpc(gateway["host"], "BTHome.DeleteSensor", {"id": int(component.split(":", 1)[1])})
        for component, mapping in matching:
            if mapping.get("kind") == "device":
                with contextlib.suppress(Exception):
                    await self._rpc(gateway["host"], "BTHome.DeleteDevice", {"id": int(component.split(":", 1)[1])})
        with contextlib.suppress(Exception):
            await self._reload_gateway(gateway["host"])

    def _gateway_needs_blu_sync(self, gateway, mac):
        mappings = [item for item in gateway.get("blu_components", {}).values() if item.get("mac") == mac]
        if not any(item.get("kind") == "device" for item in mappings):
            return True
        expected = set(BLU_TEMPLATES.get(self.learned_blu.get(mac, {}).get("template"), BLU_TEMPLATES["generic"])["objects"])
        present = {item.get("obj_id") for item in mappings if item.get("kind") == "sensor"}
        return not expected.issubset(present)

    def _management(self):
        remaining = 0
        if self.learning:
            remaining = max(0, int(self.learning["duration"] - (time.time() - self.learning["started_at"])))
        return {
            "learning": self.learning is not None,
            "learning_seconds": remaining,
            "gateways": [{"id": key, "host": value["host"], "name": self._gateway_name(value),
                          "generation": int(_number(value["info"].get("gen"))),
                          "model": str(value["info"].get("model", "")),
                          "blu_mode": value.get("blu_mode", "unavailable"),
                          "blu_error": value.get("blu_error", ""),
                          "ws_connected": bool(value.get("ws_connected"))} for key, value in self.gateways.items()],
            "templates": [{"id": key, "name": value["title"]} for key, value in BLU_TEMPLATES.items()],
            "devices": [{"mac": mac, "name": value.get("name", mac), "template": value.get("template", "generic")}
                        for mac, value in self.learned_blu.items()],
            "ws_messages": list(reversed(self.ws_diagnostics[-20:])),
        }

    async def _publish_gateway(self, gateway):
        info = gateway["info"]
        mac = _mac(info.get("mac")) or str(info.get("id") or gateway["host"])
        node_id = self.context.stable_node_id(f"shelly:{mac}")
        now = time.time()
        attributes = []
        self.attribute_controls = {key: value for key, value in self.attribute_controls.items() if value["node_id"] != node_id}
        configs = gateway.get("config", {})
        statuses = gateway.get("status", {})
        component_items = {str(item.get("key")): item for item in gateway.get("components", [])}
        for component, status in sorted(statuses.items()):
            if not isinstance(status, dict) or component.startswith(("sys", "wifi", "cloud", "mqtt", "ws", "ble", "bthome")):
                continue
            kind, _, id_text = component.partition(":")
            if kind not in {"switch", "cover", "light", "input", "temperature", "humidity", "voltmeter", "illuminance", "devicepower", "pm1", "em1"}:
                continue
            component_config = configs.get(component, {}) if isinstance(configs.get(component), dict) else {}
            if component in component_items and isinstance(component_items[component].get("config"), dict):
                component_config = _deep_merge(component_config, component_items[component]["config"])
            title = self._component_title(gateway, component, component_config)
            for field, value, attribute_type, unit, editable, data in _component_values(kind, status):
                external = f"shelly:{mac}:{component}:{field}"
                attribute_id = self._attribute_id(node_id, external)
                item = {"id": attribute_id, "node_id": node_id, "type": attribute_type,
                        "name": f"{title} · {data or _field_title(field)}", "unit": unit,
                        "current_value": _value_number(value), "editable": editable,
                        "last_changed": float(_number(status.get("last_updated_ts")) or now)}
                if isinstance(value, str) or data:
                    item["data"] = str(value) if isinstance(value, str) else str(data)
                if editable:
                    item["target_value"] = _value_number(value)
                    item["minimum"], item["maximum"], item["step_value"] = (0, 100, 1) if field in ("current_pos", "brightness") else (0, 1, 1)
                    self.attribute_controls[attribute_id] = {"node_id": node_id, "host": gateway["host"],
                        "namespace": kind.capitalize(), "component_id": int(_number(id_text)), "field": field}
                attributes.append(item)
        await self.context.publish_node({
            "id": node_id, "name": self._gateway_name(gateway),
            "note": f"Server · Shelly Gen{int(_number(info.get('gen')))} · {info.get('model', '')} · {gateway['host']} · {mac}",
            "state": 1, "profile": 0, "protocol": 20, "image": _gateway_icon(statuses),
            "state_changed": now, "attributes": attributes,
        })

    async def _publish_known_blu(self):
        for mac in self.learned_blu:
            await self._publish_blu(mac)

    async def _publish_blu(self, mac):
        device = self.learned_blu.get(mac)
        if not device:
            return
        node_id = self.context.stable_node_id(f"bthome:{mac}")
        now = time.time()
        values = self.blu_values.get(mac, {})
        attributes = []
        objects = list(BLU_TEMPLATES.get(device.get("template"), BLU_TEMPLATES["generic"])["objects"])
        for obj_id in objects:
            label, attribute_type, unit = BTHOME_OBJECTS.get(obj_id, (f"BTHome Objekt {obj_id}", 222, ""))
            entry = values.get(f"obj:{obj_id}:0", values.get("battery") if obj_id == 1 else {})
            value = entry.get("value", 0) if isinstance(entry, dict) else 0
            attributes.append({"id": self._attribute_id(node_id, f"bthome:{mac}:obj:{obj_id}:0"),
                "node_id": node_id, "type": attribute_type, "name": label, "unit": unit,
                "current_value": _value_number(value), "editable": False,
                "last_changed": float(_number(entry.get("ts")) or now) if isinstance(entry, dict) else now})
        for key, entry in sorted(values.items()):
            if not key.startswith("event:") or not isinstance(entry, dict):
                continue
            idx = key.split(":", 1)[1]
            attributes.append({"id": self._attribute_id(node_id, f"bthome:{mac}:event:{idx}"),
                "node_id": node_id, "type": 40, "name": f"Taster {int(idx) + 1}", "unit": "text",
                "current_value": 1, "data": str(entry.get("value", "Ereignis")), "editable": False,
                "last_changed": float(_number(entry.get("ts")) or now)})
        rssi = values.get("rssi", {})
        attributes.append({"id": self._attribute_id(node_id, f"bthome:{mac}:rssi"), "node_id": node_id,
            "type": 33, "name": "Empfangsstärke", "unit": "dBm",
            "current_value": _value_number(rssi.get("value", 0) if isinstance(rssi, dict) else 0),
            "editable": False, "last_changed": now})
        await self.context.publish_node({"id": node_id, "name": device.get("name") or mac,
            "note": f"Server · Shelly BLU/BTHome · {mac} · gateway-unabhängig", "state": 1,
            "profile": 0, "protocol": 20, "image": _blu_icon(device.get("template")),
            "state_changed": now, "attributes": attributes})

    def _gateway_name(self, gateway):
        sys_config = gateway.get("config", {}).get("sys", {})
        configured = ((sys_config.get("device") or {}).get("name") if isinstance(sys_config, dict) else None)
        info = gateway["info"]
        return str(configured or info.get("name") or info.get("app") or info.get("id") or "Shelly")

    def _component_title(self, gateway, component, config):
        name = str(config.get("name") or "").strip()
        kind, _, id_text = component.partition(":")
        base = name or {"switch": "Relais", "cover": "Rollladen", "light": "Licht", "input": "Eingang",
                        "temperature": "Temperatur", "humidity": "Luftfeuchtigkeit", "voltmeter": "Spannung",
                        "illuminance": "Helligkeit", "devicepower": "Geräteversorgung", "pm1": "Leistungsmessung",
                        "em1": "Energiemessung"}.get(kind, kind.title())
        if not name and id_text not in ("", "0"):
            base += f" {int(_number(id_text)) + 1}"
        sys_config = gateway.get("config", {}).get("sys", {})
        addon = (sys_config.get("device") or {}).get("addon_type") if isinstance(sys_config, dict) else None
        is_addon = bool(addon) and (kind in {"temperature", "humidity", "voltmeter"}
                                   or (kind == "input" and (config.get("type") == "analog" or int(_number(id_text)) > 0)))
        return f"Plus Add-on · {base}" if is_addon else base

    def _attribute_id(self, node_id, external):
        if external not in self.attribute_offsets:
            self.attribute_offsets[external] = self.next_attribute_offset
            self.next_attribute_offset += 1
            self._save_state()
        return node_id * 1000 + int(self.attribute_offsets[external])

    def _save_state(self):
        self.context.save_state({"blu_devices": self.learned_blu, "blu_values": self.blu_values,
                                 "attribute_offsets": self.attribute_offsets})


def _component_values(kind, status):
    result = []
    if kind in ("switch", "light"):
        if "output" in status: result.append(("output", status["output"], 1, "", True, None))
        if "brightness" in status: result.append(("brightness", status["brightness"], 15, "%", True, None))
        if "apower" in status: result.append(("apower", status["apower"], 3, "W", False, None))
        if "voltage" in status: result.append(("voltage", status["voltage"], 195, "V", False, None))
        if "current" in status: result.append(("current", status["current"], 193, "A", False, None))
        total = (status.get("aenergy") or {}).get("total") if isinstance(status.get("aenergy"), dict) else None
        if total is not None: result.append(("energy", float(total) / 1000, 4, "kWh", False, None))
    elif kind == "cover":
        if "current_pos" in status: result.append(("current_pos", status["current_pos"], 14, "%", True, None))
        if "apower" in status: result.append(("apower", status["apower"], 3, "W", False, None))
        if "state" in status: result.append(("state", status["state"], 213, "text", False, "Betriebszustand"))
    elif kind == "input":
        if "state" in status and status["state"] is not None: result.append(("state", status["state"], 11, "", False, None))
        for field, unit in (("percent", "%"), ("xpercent", "%"), ("counts", ""), ("freq", "Hz")):
            if field in status: result.append((field, status[field], 222, unit, False, None))
    elif kind == "temperature":
        if "tC" in status: result.append(("tC", status["tC"], 5, "°C", False, "Messwert"))
    elif kind == "humidity":
        if "rh" in status: result.append(("rh", status["rh"], 7, "%", False, "Messwert"))
    elif kind == "voltmeter":
        for field in ("voltage", "voltage_rms"):
            if field in status: result.append((field, status[field], 195, "V", False, "Messwert"))
    elif kind == "illuminance":
        if "lux" in status: result.append(("lux", status["lux"], 23, "lx", False, "Messwert"))
    elif kind == "devicepower":
        battery = status.get("battery") if isinstance(status.get("battery"), dict) else {}
        if battery.get("percent") is not None: result.append(("battery", battery["percent"], 8, "%", False, None))
    elif kind in ("pm1", "em1"):
        for field, attribute_type, unit in (
            ("apower", 3, "W"), ("act_power", 3, "W"), ("aprt_power", 3, "VA"),
            ("voltage", 195, "V"), ("current", 193, "A"), ("pf", 222, ""), ("freq", 222, "Hz"),
        ):
            if field in status: result.append((field, status[field], attribute_type, unit, False, None))
        for field in ("aenergy", "total_act_energy"):
            energy = status.get(field)
            total = energy.get("total") if isinstance(energy, dict) else energy
            if total is not None: result.append((field, float(total) / 1000, 4, "kWh", False, None))
    return result


def _field_title(field):
    return {"output": "Schalten", "brightness": "Helligkeit", "apower": "Leistung", "energy": "Energie",
            "voltage": "Spannung", "current": "Strom", "current_pos": "Position", "state": "Status",
            "act_power": "Wirkleistung", "aprt_power": "Scheinleistung", "pf": "Leistungsfaktor",
            "percent": "Analogwert", "xpercent": "Analogwert", "counts": "Zähler", "freq": "Frequenz",
            "battery": "Batterie", "aenergy": "Energie", "total_act_energy": "Energie"}.get(field, field.replace("_", " ").title())


def _deep_merge(current, update):
    result = dict(current) if isinstance(current, dict) else {}
    for key, value in update.items():
        result[key] = _deep_merge(result.get(key), value) if isinstance(value, dict) else value
    return result


def _websocket_auth(challenge, password):
    realm = str(challenge["realm"])
    nonce = challenge["nonce"]
    cnonce = random.randint(1, 2_000_000_000)
    nc = "00000001"
    ha1 = hashlib.sha256(f"admin:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.sha256(b"dummy_method:dummy_uri").hexdigest()
    response = hashlib.sha256(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}".encode()).hexdigest()
    return {"realm": realm, "username": "admin", "nonce": nonce, "cnonce": cnonce, "nc": nc,
            "response": response, "algorithm": "SHA-256"}


def _error_code(message):
    error = message.get("error") if isinstance(message, dict) else None
    return int(_number(error.get("code"))) if isinstance(error, dict) else 0


def _number(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0


def _value_number(value):
    if isinstance(value, bool):
        return 1 if value else 0
    return _number(value)


def _mac(value):
    cleaned = re.sub(r"[^0-9a-f]", "", str(value or "").lower())
    return ":".join(cleaned[index:index + 2] for index in range(0, 12, 2)) if len(cleaned) == 12 else ""


def _blu_secret_name(mac):
    return f"bthome_key_{str(mac).replace(':', '').lower()}"


def _parse_bthome_advertisement(value):
    cleaned = re.sub(r"[^0-9a-f]", "", str(value or "").lower())
    if len(cleaned) < 8 or len(cleaned) % 2:
        return None
    try:
        advertisement = bytes.fromhex(cleaned)
    except ValueError:
        return None
    service = None
    offset = 0
    while offset < len(advertisement):
        length = advertisement[offset]
        if length == 0:
            break
        end = offset + 1 + length
        if end > len(advertisement):
            break
        ad_type = advertisement[offset + 1]
        data = advertisement[offset + 2:end]
        if ad_type == 0x16 and data.startswith(b"\xd2\xfc"):
            service = data[2:]
            break
        offset = end
    if service is None:
        marker = advertisement.find(b"\xd2\xfc")
        if marker >= 0:
            service = advertisement[marker + 2:]
    if not service:
        return None
    device_info = service[0]
    encrypted = bool(device_info & 0x01)
    result = {"encrypted": encrypted, "values": {}}
    if encrypted:
        return result
    specs = {
        0: (1, False, 1), 1: (1, False, 1), 5: (3, False, 0.01),
        33: (1, False, 1), 45: (1, False, 1), 46: (1, False, 1),
        58: (1, False, 1), 63: (2, True, 0.1), 69: (2, True, 0.1),
        100: (1, False, 1),
    }
    index = 1
    while index < len(service):
        obj_id = service[index]
        index += 1
        spec = specs.get(obj_id)
        if not spec or index + spec[0] > len(service):
            break
        raw = int.from_bytes(service[index:index + spec[0]], "little", signed=spec[1])
        result["values"][obj_id] = raw * spec[2]
        index += spec[0]
    return result


def _bthome_button_event(value):
    return {0: "Kein Tastendruck", 1: "Einfach gedrückt", 2: "Doppelt gedrückt", 3: "Dreifach gedrückt",
            4: "Lang gedrückt", 254: "Gehalten"}.get(int(_number(value)), f"Tasterereignis {int(_number(value))}")


def _gateway_icon(status):
    keys = set(status)
    if any(key.startswith("cover:") for key in keys): return "window.shade.closed"
    if any(key.startswith("light:") for key in keys): return "lightbulb.fill"
    if any(key.startswith("switch:") for key in keys): return "switch.2"
    return "dot.radiowaves.left.and.right"


def _blu_icon(template):
    return {"button": "button.programmable", "rc_button_4": "button.programmable",
            "door_window": "door.left.hand.open", "motion": "figure.walk.motion",
            "ht": "thermometer.medium"}.get(template, "sensor.fill")
