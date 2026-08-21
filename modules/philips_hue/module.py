import asyncio
import contextlib
import json
import math
import time

import httpx

from server.shelly_discovery import discover_service_ipv4


HUE_SERVICE = "_hue._tcp.local"
SUPPORTED_TYPES = {
    "light", "grouped_light", "motion", "temperature", "light_level",
    "device_power", "contact", "button", "zigbee_connectivity",
}
OWNER_TYPES = {"device", "room", "zone"}


def manifest():
    return {
        "id": "philips_hue",
        "name": "Philips Hue Bridge",
        "version": "1.0.0",
        "icon": "lightbulb.2",
        "description": (
            "Lokale Philips-Hue-API v2 mit Link-Button-Anmeldung. Lampen, Räume und Sensoren "
            "werden von der Bridge geladen und über den SSE-Eventstream live aktualisiert."
        ),
        "supportsDiscovery": True,
        "supportsMultipleInstances": True,
        "fields": [
            {"key": "bridge_ip", "type": "text", "title": "Bridge-IP oder Hostname (optional)",
             "placeholder": "192.168.178.30",
             "help": "Leer lassen, um eine Hue Bridge per mDNS im lokalen Netz zu suchen."},
            {"key": "application_key", "type": "password", "title": "Application Key (optional)",
             "help": "Nur für eine bereits vorhandene Hue-Anmeldung. Sonst den Link-Button verwenden."},
            {"key": "refresh_seconds", "type": "duration", "title": "Vollständiger Abgleich",
             "default": 300, "minimum": 30, "maximum": 86400, "unit": "s"},
        ],
        "actions": [
            {"id": "pair", "title": "Bridge-Taste drücken und verbinden", "icon": "link"},
            {"id": "refresh", "title": "Hue-Geräte neu einlesen", "icon": "arrow.clockwise"},
        ],
    }


def create(configuration, context):
    return HueAdapter(configuration, context)


class HueAdapter:
    def __init__(self, configuration, context):
        self.configuration, self.context = configuration, context
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(15, read=30), trust_env=False, verify=False,
            headers={"Accept": "application/json"},
        )
        self.host = ""
        self.application_key = ""
        self.resources = {}
        self.owner_resources = {}
        self.resource_owner = {}
        self.controls = {}
        self.event_task = None
        self.refresh_task = None
        state = context.load_state({}) or {}
        offsets = state.get("attribute_offsets", {})
        self.attribute_offsets = offsets if isinstance(offsets, dict) else {}

    async def start(self):
        supplied_key = str(self.configuration.get("application_key", "")).strip()
        if supplied_key:
            self.context.save_secret("application_key", supplied_key)
            self.context.clear_configuration_value("application_key")
        self.application_key = supplied_key or str(self.context.load_secret("application_key", ""))
        await self._resolve_host()
        if not self.application_key:
            await self.context.set_status("Kopplung erforderlich", "Hue-Bridge-Taste drücken und anschließend verbinden")
            return
        await self._refresh()
        self._start_background_tasks()
        await self.context.set_status("Verbunden · SSE aktiv")

    async def stop(self):
        for task in (self.event_task, self.refresh_task):
            if task:
                task.cancel()
        for task in (self.event_task, self.refresh_task):
            if task:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await self.client.aclose()

    async def health_check(self):
        if not self.application_key:
            raise ValueError("Hue Bridge ist noch nicht gekoppelt")
        await self._request("GET", "/clip/v2/resource/bridge")

    async def action(self, action_id, payload):
        if action_id == "pair":
            await self._resolve_host()
            result = await self._pair()
            await self._refresh()
            self._start_background_tasks()
            await self.context.set_status("Verbunden · SSE aktiv")
            return result
        if action_id == "refresh":
            if not self.application_key:
                raise ValueError("Zuerst die Taste der Hue Bridge drücken und die Bridge verbinden")
            count = await self._refresh()
            return {"status": "refreshed", "devices": count}
        raise ValueError("Unbekannte Hue-Aktion")

    async def set_value(self, node_id, attribute_id, value):
        control = self.controls.get(int(attribute_id))
        if not control or int(node_id) != control["node_id"]:
            raise ValueError("Dieses Hue-Attribut ist nicht schreibbar")
        field = control["field"]
        if field == "on":
            body = {"on": {"on": float(value) >= 0.5}}
        elif field == "brightness":
            body = {"dimming": {"brightness": max(0.0, min(100.0, float(value)))}}
        elif field == "color_temperature":
            kelvin = max(control["minimum"], min(control["maximum"], float(value)))
            body = {"color_temperature": {"mirek": round(1_000_000 / kelvin)}}
        elif field == "color":
            red, green, blue = _decimal_to_rgb(value)
            x, y = _rgb_to_xy(red, green, blue)
            body = {"color": {"xy": {"x": x, "y": y}}}
        elif field in {"effect", "effect_v2"}:
            effect = _choice_at(control.get("choices", []), value)
            if effect is None:
                raise ValueError("Dieser Hue-Effekt wird vom Gerät nicht unterstützt")
            body = ({"effects_v2": {"action": {"effect": effect}}} if field == "effect_v2"
                    else {"effects": {"effect": effect}})
        elif field == "identify":
            if float(value) < 0.5:
                return
            body = {"identify": {"action": "identify"}}
        else:
            raise ValueError("Dieses Hue-Attribut ist nicht schreibbar")
        await self._put_resource(control["type"], control["resource_id"], body)
        resource = self.resources.get(control["resource_id"], {})
        self.resources[control["resource_id"]] = _deep_merge(resource, body)
        await self._publish_owner(control["owner_id"])

    def _start_background_tasks(self):
        if not self.event_task or self.event_task.done():
            self.event_task = asyncio.create_task(self._event_loop())
        if not self.refresh_task or self.refresh_task.done():
            self.refresh_task = asyncio.create_task(self._refresh_loop())

    async def _resolve_host(self):
        configured = str(self.configuration.get("bridge_ip", "")).strip()
        if configured:
            self.host = configured.removeprefix("https://").removeprefix("http://").rstrip("/")
            return self.host
        hosts = await asyncio.to_thread(discover_service_ipv4, HUE_SERVICE, 3.0)
        if not hosts:
            raise ValueError("Keine Philips Hue Bridge im lokalen Netz gefunden; bitte IP-Adresse eintragen")
        self.host = hosts[0]
        return self.host

    async def _pair(self):
        response = await self.client.post(
            f"https://{self.host}/api",
            json={"devicetype": "smarthomeboard#server", "generateclientkey": True},
        )
        response.raise_for_status()
        payload = response.json()
        item = payload[0] if isinstance(payload, list) and payload else {}
        success = item.get("success") if isinstance(item, dict) else None
        if not isinstance(success, dict) or not success.get("username"):
            error = item.get("error", {}) if isinstance(item, dict) else {}
            description = error.get("description") if isinstance(error, dict) else None
            raise ValueError(str(description or "Hue-Anmeldung fehlgeschlagen; wurde die Bridge-Taste gedrückt?"))
        self.application_key = str(success["username"])
        self.context.save_secret("application_key", self.application_key)
        if success.get("clientkey"):
            self.context.save_secret("client_key", str(success["clientkey"]))
        return {"status": "paired", "bridge": self.host}

    async def _refresh(self):
        payload = await self._request("GET", "/clip/v2/resource")
        resources = payload.get("data", []) if isinstance(payload, dict) else []
        if not isinstance(resources, list):
            raise ValueError("Hue Bridge hat keine gültige Ressourcenliste geliefert")
        previous_owners = set(self.owner_resources)
        self.resources = {
            str(item["id"]): item for item in resources
            if isinstance(item, dict) and item.get("id") and item.get("type")
        }
        self._rebuild_index()
        await self._publish_all()
        for owner_id in previous_owners - set(self.owner_resources):
            await self.context.remove_node(self.context.stable_node_id(f"hue:{owner_id}"))
        return len(self.owner_resources)

    async def _refresh_loop(self):
        while True:
            try:
                seconds = max(30, min(86400, int(float(self.configuration.get("refresh_seconds", 300)))))
                await asyncio.sleep(seconds)
                await self._refresh()
            except asyncio.CancelledError:
                return
            except Exception as error:
                await self.context.set_status("Hue-Abgleich gestört", str(error))

    async def _event_loop(self):
        delay = 1
        while True:
            try:
                headers = self._headers({"Accept": "text/event-stream"})
                timeout = httpx.Timeout(connect=10, read=None, write=10, pool=10)
                async with self.client.stream(
                    "GET", f"https://{self.host}/eventstream/clip/v2", headers=headers, timeout=timeout
                ) as response:
                    response.raise_for_status()
                    delay = 1
                    await self.context.set_status("Verbunden · SSE aktiv")
                    data_lines = []
                    async for line in response.aiter_lines():
                        if line == "":
                            if data_lines:
                                await self._consume_sse_data("\n".join(data_lines))
                                data_lines = []
                            continue
                        if line.startswith("data:"):
                            data_lines.append(line[5:].lstrip())
            except asyncio.CancelledError:
                return
            except Exception as error:
                await self.context.set_status("Hue-SSE getrennt", str(error))
                await asyncio.sleep(delay)
                delay = min(60, delay * 2)

    async def _consume_sse_data(self, value):
        try:
            events = json.loads(value)
        except json.JSONDecodeError:
            return
        if not isinstance(events, list):
            return
        affected = set()
        removed_owners = set()
        for event in events:
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("type", "update"))
            for item in event.get("data", []) if isinstance(event.get("data"), list) else []:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                resource_id = str(item["id"])
                if event_type == "delete":
                    owner_id = self.resource_owner.get(resource_id)
                    if owner_id:
                        affected.add(owner_id)
                    if self.resources.get(resource_id, {}).get("type") in OWNER_TYPES:
                        removed_owners.add(resource_id)
                    self.resources.pop(resource_id, None)
                else:
                    self.resources[resource_id] = _deep_merge(self.resources.get(resource_id, {}), item)
                    owner_id = _owner_id(self.resources[resource_id])
                    if owner_id:
                        affected.add(owner_id)
                    if self.resources[resource_id].get("type") in OWNER_TYPES:
                        affected.add(resource_id)
        self._rebuild_index()
        for owner_id in affected:
            if owner_id in self.owner_resources:
                await self._publish_owner(owner_id)
        for owner_id in removed_owners:
            await self.context.remove_node(self.context.stable_node_id(f"hue:{owner_id}"))

    def _rebuild_index(self):
        owners = {key: value for key, value in self.resources.items() if value.get("type") in OWNER_TYPES}
        self.owner_resources = {key: [] for key in owners}
        self.resource_owner = {}
        for resource_id, resource in self.resources.items():
            if resource.get("type") not in SUPPORTED_TYPES:
                continue
            owner_id = _owner_id(resource)
            if owner_id in self.owner_resources:
                self.owner_resources[owner_id].append(resource_id)
                self.resource_owner[resource_id] = owner_id
        self.owner_resources = {key: value for key, value in self.owner_resources.items() if value}

    async def _publish_all(self):
        for owner_id in sorted(self.owner_resources):
            await self._publish_owner(owner_id)

    async def _publish_owner(self, owner_id):
        owner = self.resources.get(owner_id)
        resource_ids = self.owner_resources.get(owner_id, [])
        if not owner or not resource_ids:
            return
        node_id = self.context.stable_node_id(f"hue:{owner_id}")
        attributes = []
        self.controls = {key: value for key, value in self.controls.items() if value["node_id"] != node_id}
        for instance, resource_id in enumerate(sorted(resource_ids), start=1):
            resource = self.resources[resource_id]
            attributes.extend(self._resource_attributes(node_id, owner_id, resource, instance))
        identify = owner.get("identify") if isinstance(owner.get("identify"), dict) else {}
        identify_actions = identify.get("action_values") if isinstance(identify.get("action_values"), list) else []
        if owner.get("type") == "device" and "identify" in identify_actions:
            attribute_id = self.context.attribute_id(node_id, self._attribute_offset(owner_id, f"{owner_id}:identify"))
            attributes.append({
                "id": attribute_id, "node_id": node_id, "type": 1, "instance": len(resource_ids) + 1,
                "name": "Erkennungsmodus", "unit": "", "current_value": 0, "target_value": 0,
                "editable": True, "minimum": 0, "maximum": 1, "step_value": 1,
                "last_changed": time.time(),
            })
            self.controls[attribute_id] = {
                "node_id": node_id, "owner_id": owner_id, "resource_id": owner_id,
                "type": "device", "field": "identify", "minimum": 0, "maximum": 1,
            }
        if not attributes:
            return
        metadata = owner.get("metadata") if isinstance(owner.get("metadata"), dict) else {}
        product = owner.get("product_data") if isinstance(owner.get("product_data"), dict) else {}
        name = str(metadata.get("name") or product.get("product_name") or "Philips Hue")
        if owner.get("type") == "room":
            name = f"Raum: {name}"
        elif owner.get("type") == "zone":
            name = f"Zone: {name}"
        model = str(product.get("model_id") or product.get("product_name") or owner.get("type", ""))
        kinds = {self.resources[item].get("type") for item in resource_ids}
        image = "nodeicon_light" if kinds & {"light", "grouped_light"} else (
            "nodeicon_motion" if "motion" in kinds else "nodeicon_sensor"
        )
        await self.context.publish_node({
            "id": node_id, "integration_source": "server", "name": name,
            "note": f"Server · Philips Hue · {model}", "state": 1, "profile": 0, "protocol": 20,
            "image": image, "state_changed": time.time(), "attributes": attributes,
        })

    def _resource_attributes(self, node_id, owner_id, resource, instance):
        resource_id = str(resource["id"])
        kind = str(resource.get("type", ""))
        now = time.time()
        result = []

        def add(field, attribute_type, name, value, unit="", editable=False, minimum=None, maximum=None,
                step=None, data=None, choices=None):
            offset = self._attribute_offset(owner_id, f"{resource_id}:{field}")
            attribute_id = self.context.attribute_id(node_id, offset)
            item = {"id": attribute_id, "node_id": node_id, "type": attribute_type, "instance": instance,
                    "name": name, "unit": unit, "current_value": value, "editable": editable,
                    "last_changed": now}
            if editable:
                item["target_value"] = value
            if minimum is not None: item["minimum"] = minimum
            if maximum is not None: item["maximum"] = maximum
            if step is not None: item["step_value"] = step
            if data is not None: item["data"] = str(data)
            result.append(item)
            if editable:
                self.controls[attribute_id] = {"node_id": node_id, "owner_id": owner_id,
                    "resource_id": resource_id, "type": kind, "field": field,
                    "minimum": minimum, "maximum": maximum, "choices": choices or []}

        if kind in {"light", "grouped_light"}:
            on = resource.get("on") if isinstance(resource.get("on"), dict) else {}
            if on.get("on") is not None:
                add("on", 1, "An/Aus", 1 if on["on"] else 0, editable=True, minimum=0, maximum=1, step=1)
            dimming = resource.get("dimming") if isinstance(resource.get("dimming"), dict) else {}
            if dimming.get("brightness") is not None:
                add("brightness", 2, "Helligkeit", float(dimming["brightness"]), "%", True, 0, 100, 1)
            color_temperature = resource.get("color_temperature") if isinstance(resource.get("color_temperature"), dict) else {}
            mirek = color_temperature.get("mirek")
            schema = color_temperature.get("mirek_schema") if isinstance(color_temperature.get("mirek_schema"), dict) else {}
            if mirek:
                min_mirek = float(schema.get("mirek_minimum") or 153)
                max_mirek = float(schema.get("mirek_maximum") or 500)
                minimum, maximum = round(1_000_000 / max_mirek), round(1_000_000 / min_mirek)
                add("color_temperature", 42, "Farbtemperatur", round(1_000_000 / float(mirek)), "K",
                    True, minimum, maximum, 50)
            color = resource.get("color") if isinstance(resource.get("color"), dict) else {}
            xy = color.get("xy") if isinstance(color.get("xy"), dict) else {}
            if xy.get("x") is not None and xy.get("y") is not None:
                red, green, blue = _xy_to_rgb(float(xy["x"]), float(xy["y"]), float(dimming.get("brightness", 100)))
                add("color", 23, "Farbe", (red << 16) | (green << 8) | blue, editable=True,
                    minimum=0, maximum=0xFFFFFF, step=1)
            effects_v2 = resource.get("effects_v2") if isinstance(resource.get("effects_v2"), dict) else {}
            effect_action = effects_v2.get("action") if isinstance(effects_v2.get("action"), dict) else {}
            effect_status = effects_v2.get("status") if isinstance(effects_v2.get("status"), dict) else {}
            effect_choices = effect_action.get("effect_values") if isinstance(effect_action.get("effect_values"), list) else []
            current_effect = effect_status.get("effect")
            effect_field = "effect_v2"
            if not effect_choices:
                effects = resource.get("effects") if isinstance(resource.get("effects"), dict) else {}
                effect_choices = effects.get("effect_values") or effects.get("status_values") or []
                current_effect = effects.get("status") or effects.get("effect")
                effect_field = "effect"
            effect_choices = _unique_strings(effect_choices)
            if effect_choices:
                if str(current_effect or "") not in effect_choices:
                    effect_choices.insert(0, str(current_effect or "no_effect"))
                    effect_choices = _unique_strings(effect_choices)
                current_index = effect_choices.index(str(current_effect)) if str(current_effect) in effect_choices else 0
                add(effect_field, 45, "Lichteffekt", current_index, "text", True, 0,
                    len(effect_choices) - 1, 1, _choice_data(current_index, effect_choices), effect_choices)
        elif kind == "motion":
            motion = resource.get("motion") if isinstance(resource.get("motion"), dict) else {}
            if motion.get("motion") is not None: add("motion", 25, "Bewegung", 1 if motion["motion"] else 0)
        elif kind == "temperature":
            temperature = resource.get("temperature") if isinstance(resource.get("temperature"), dict) else {}
            if temperature.get("temperature") is not None:
                add("temperature", 5, "Temperatur", float(temperature["temperature"]), "°C")
        elif kind == "light_level":
            light = resource.get("light") if isinstance(resource.get("light"), dict) else {}
            if light.get("light_level") is not None:
                add("light_level", 11, "Helligkeit", _hue_light_level_to_lux(light["light_level"]), "lx")
        elif kind == "device_power":
            power = resource.get("power_state") if isinstance(resource.get("power_state"), dict) else {}
            if power.get("battery_level") is not None:
                add("battery", 8, "Batterie", float(power["battery_level"]), "%")
        elif kind == "contact":
            report = resource.get("contact_report") if isinstance(resource.get("contact_report"), dict) else {}
            if report.get("state") is not None:
                add("contact", 14, "Kontakt", 0 if report["state"] == "contact" else 1)
        elif kind == "button":
            button = resource.get("button") if isinstance(resource.get("button"), dict) else {}
            report = resource.get("button_report") if isinstance(resource.get("button_report"), dict) else {}
            event = button.get("last_event") or report.get("event")
            if event:
                add("button", 40, "Taster", _button_value(event), "text", data=_button_title(event))
        elif kind == "zigbee_connectivity":
            status = resource.get("status")
            if status is not None:
                add("connectivity", 222, "Verbindung", 1 if status == "connected" else 0, "text", data=status)
        return result

    def _attribute_offset(self, owner_id, key):
        storage_key = f"{owner_id}|{key}"
        if storage_key not in self.attribute_offsets:
            # Migration des ersten Hue-Adapterstands: bereits gespeicherte IDs bleiben erhalten.
            if key in self.attribute_offsets:
                offset = int(self.attribute_offsets[key])
            else:
                prefix = f"{owner_id}|"
                used = [int(value) for name, value in self.attribute_offsets.items() if str(name).startswith(prefix)]
                offset = max(used + [0]) + 1
            self.attribute_offsets[storage_key] = offset
            self.context.save_state({"attribute_offsets": self.attribute_offsets})
        return int(self.attribute_offsets[storage_key])

    async def _put_resource(self, kind, resource_id, body):
        await self._request("PUT", f"/clip/v2/resource/{kind}/{resource_id}", body)

    async def _request(self, method, path, body=None):
        response = await self.client.request(
            method, f"https://{self.host}{path}", headers=self._headers(), json=body,
        )
        response.raise_for_status()
        payload = response.json()
        errors = payload.get("errors", []) if isinstance(payload, dict) else []
        if errors:
            descriptions = [str(item.get("description") or item) for item in errors if isinstance(item, dict)]
            raise ValueError("Hue API: " + "; ".join(descriptions or [str(errors)]))
        return payload

    def _headers(self, extra=None):
        headers = {"hue-application-key": self.application_key}
        headers.update(extra or {})
        return headers


def _owner_id(resource):
    owner = resource.get("owner") if isinstance(resource.get("owner"), dict) else {}
    return str(owner.get("rid") or "")


def _deep_merge(current, update):
    result = dict(current) if isinstance(current, dict) else {}
    for key, value in update.items():
        result[key] = _deep_merge(result.get(key), value) if isinstance(value, dict) else value
    return result


def _hue_light_level_to_lux(value):
    return round(10 ** ((float(value) - 1) / 10000), 2)


def _button_value(value):
    return {"initial_press": 1, "repeat": 2, "short_release": 3, "long_release": 4}.get(str(value), 0)


def _button_title(value):
    return {"initial_press": "Gedrückt", "repeat": "Gehalten", "short_release": "Kurz losgelassen",
            "long_release": "Lang losgelassen"}.get(str(value), str(value))


def _unique_strings(values):
    result = []
    for value in values if isinstance(values, list) else []:
        text = str(value).strip()
        if text and text not in result:
            result.append(text)
    return result


def _choice_at(choices, value):
    try:
        index = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return choices[index] if 0 <= index < len(choices) else None


def _effect_title(value):
    labels = {
        "no_effect": "Kein Effekt", "none": "Kein Effekt", "candle": "Kerze",
        "fireplace": "Kamin", "prism": "Prisma", "opal": "Opal", "glisten": "Glitzern",
        "sparkle": "Funkeln", "cosmos": "Kosmos", "enchant": "Verzaubern",
        "sunbeam": "Sonnenstrahl", "underwater": "Unterwasser", "colorloop": "Farbwechsel",
    }
    text = str(value)
    return labels.get(text, text.replace("_", " ").strip().title())


def _choice_data(current, choices):
    return json.dumps({
        "label": _effect_title(choices[current]) if 0 <= current < len(choices) else "",
        "options": [{"value": index, "label": _effect_title(value)} for index, value in enumerate(choices)],
    }, ensure_ascii=False, separators=(",", ":"))


def _decimal_to_rgb(value):
    packed = max(0, min(0xFFFFFF, int(round(float(value)))))
    return (packed >> 16) & 255, (packed >> 8) & 255, packed & 255


def _rgb_to_xy(red, green, blue):
    def linear(channel):
        value = channel / 255.0
        return ((value + 0.055) / 1.055) ** 2.4 if value > 0.04045 else value / 12.92
    r, g, b = linear(red), linear(green), linear(blue)
    x_value = r * 0.664511 + g * 0.154324 + b * 0.162028
    y_value = r * 0.283881 + g * 0.668433 + b * 0.047685
    z_value = r * 0.000088 + g * 0.072310 + b * 0.986039
    total = x_value + y_value + z_value
    return (round(x_value / total, 6), round(y_value / total, 6)) if total else (0.0, 0.0)


def _xy_to_rgb(x, y, brightness=100):
    if y <= 0:
        return 0, 0, 0
    luminance = max(0.0, min(1.0, brightness / 100.0))
    x_value = luminance / y * x
    z_value = luminance / y * (1 - x - y)
    red = x_value * 1.656492 - luminance * 0.354851 - z_value * 0.255038
    green = -x_value * 0.707196 + luminance * 1.655397 + z_value * 0.036152
    blue = x_value * 0.051713 - luminance * 0.121364 + z_value * 1.011530
    maximum = max(red, green, blue, 1.0)
    values = [max(0.0, channel / maximum) for channel in (red, green, blue)]
    values = [12.92 * channel if channel <= 0.0031308 else 1.055 * channel ** (1 / 2.4) - 0.055 for channel in values]
    return tuple(max(0, min(255, round(channel * 255))) for channel in values)
