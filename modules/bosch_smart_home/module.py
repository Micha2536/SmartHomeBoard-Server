import asyncio
import contextlib
import hashlib
import json
import os
import re
import tempfile
import time

try:
    from boschshcpy import SHCRegisterClient, SHCSession
except ImportError:  # Der Registry-Scan soll auch vor der Installation funktionieren.
    SHCRegisterClient = SHCSession = None


def manifest():
    return {
        "id": "bosch-smart-home",
        "name": "Bosch Smart Home",
        "version": "1.1.0",
        "icon": "house.and.flag",
        "description": (
            "Lokale Verbindung zum Bosch Smart Home Controller über die offizielle REST API. "
            "Geräte, Messwerte, Schalter, Heizungs-Sollwerte, Rollläden und Szenarien werden "
            "persistent an SmartHomeBoard übertragen. Keine Bosch-Cloud erforderlich."
        ),
        "supportsDiscovery": False,
        "supportsMultipleInstances": True,
        "fields": [
            {
                "key": "host", "type": "text", "title": "Controller-IP oder Hostname",
                "placeholder": "192.168.1.50", "required": True,
                "help": "Am zuverlässigsten ist eine feste DHCP-Zuordnung für den Bosch Smart Home Controller.",
            },
            {
                "key": "system_password", "type": "password", "title": "Controller-Systempasswort",
                "required": False,
                "help": (
                    "Nur für die einmalige Kopplung: Konfiguration speichern, die Taste am Controller drücken "
                    "und danach „Controller koppeln“ wählen. Anschließend wird das Passwort gelöscht."
                ),
            },
        ],
        "actions": [
            {"id": "pair", "title": "Controller koppeln", "icon": "link.badge.plus"},
            {"id": "refresh", "title": "Geräte neu einlesen", "icon": "arrow.clockwise"},
            {"id": "reset_pairing", "title": "Kopplung zurücksetzen", "icon": "trash"},
        ],
    }


def create(configuration, context):
    return BoschSmartHomeAdapter(configuration, context)


class BoschSmartHomeAdapter:
    def __init__(self, configuration, context):
        self.configuration = configuration
        self.context = context
        self.session = None
        self.loop = None
        self.temp_paths = []
        self.callback_tokens = []
        self.subscribed_services = set()
        self.controls = {}
        self.connect_lock = None
        self.publish_lock = None
        self.maintenance_task = None
        self.startup_status = "Verbunden"

    async def start(self):
        self.loop = asyncio.get_running_loop()
        self.connect_lock = asyncio.Lock()
        self.publish_lock = asyncio.Lock()
        if not self._has_credentials():
            self.startup_status = "Kopplung erforderlich"
            await self.context.set_status("Kopplung erforderlich")
            return
        await self._connect()

    async def stop(self):
        await self._disconnect()

    async def action(self, action_id, payload):
        if action_id == "pair":
            if self.session:
                return {"status": "already_paired", "devices": len(self.session.devices)}
            await self._pair()
            await self._connect()
            return {"status": "paired", "devices": len(self.session.devices)}
        if action_id == "refresh":
            if not self.session:
                raise ConnectionError("Der Bosch Smart Home Controller ist nicht verbunden")
            await self._publish_all()
            return {"status": "refreshed", "devices": len(self.session.devices)}
        if action_id == "reset_pairing":
            await self._disconnect()
            self.context.save_secret("certificate", "")
            self.context.save_secret("private_key", "")
            for node in list(self.context.nodes()):
                await self.context.remove_node(int(node["id"]))
            await self.context.set_status("Kopplung erforderlich")
            return {"status": "pairing_reset"}
        raise ValueError("Unbekannte Bosch-Smart-Home-Aktion")

    async def set_value(self, node_id, attribute_id, value):
        control = self.controls.get((int(node_id), int(attribute_id)))
        if not control:
            raise ValueError("Dieses Bosch-Smart-Home-Attribut ist nicht schreibbar")
        if control[0] == "scenario":
            await asyncio.to_thread(control[1].trigger)
            await self._publish_scenario(control[1])
            return
        service, key, conversion = control
        converted = _control_value(conversion, value)
        await asyncio.to_thread(service.put_state_element, key, converted)
        await asyncio.to_thread(service.short_poll)
        await self._publish_device_id(service.device_id)

    async def _pair(self):
        self._require_library()
        host = _host(self.configuration)
        password = str(self.configuration.get("system_password", ""))
        if not password:
            raise ValueError("Für die einmalige Kopplung fehlt das Controller-Systempasswort")
        await self.context.set_status("Kopplung läuft")
        client_id = "oss_shb_" + hashlib.sha256(str(self.context.integration_id).encode()).hexdigest()[:16]
        try:
            result = await asyncio.to_thread(
                SHCRegisterClient(host, password).register,
                client_id,
                "SmartHomeBoard_Server",
            )
        except Exception as error:
            await self.context.set_status("Kopplung fehlgeschlagen", _friendly_error(error))
            raise ValueError(
                "Kopplung fehlgeschlagen. Controller-Taste drücken, Systempasswort prüfen und erneut versuchen. "
                + _friendly_error(error)
            ) from error
        if not result or not result.get("cert") or not result.get("key"):
            raise ValueError("Der Bosch Controller hat keine Client-Zertifikate geliefert")
        self.context.save_secret("certificate", _text(result["cert"]))
        self.context.save_secret("private_key", _text(result["key"]))
        self.context.clear_configuration_value("system_password")

    async def _connect(self):
        self._require_library()
        async with self.connect_lock:
            if self.session:
                return
            certificate = self.context.load_secret("certificate", "")
            private_key = self.context.load_secret("private_key", "")
            if not certificate or not private_key:
                raise ValueError("Der Bosch Smart Home Controller ist noch nicht gekoppelt")
            cert_path = self._temporary_credential(certificate, ".crt")
            key_path = self._temporary_credential(private_key, ".key")
            await self.context.set_status("Verbindung wird aufgebaut")
            try:
                self.session = await asyncio.to_thread(
                    SHCSession, _host(self.configuration), cert_path, key_path
                )
                self._subscribe()
                await asyncio.to_thread(self.session.start_polling)
                await self._publish_all()
                self.maintenance_task = asyncio.create_task(self._maintenance_loop())
                await self.context.set_status("Verbunden")
            except Exception as error:
                await self._disconnect()
                await self.context.set_status("Nicht erreichbar", _friendly_error(error))
                raise

    async def _disconnect(self):
        if self.maintenance_task:
            self.maintenance_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.maintenance_task
            self.maintenance_task = None
        session, self.session = self.session, None
        if session:
            for service, token in self.callback_tokens:
                with contextlib.suppress(Exception):
                    service.unsubscribe_callback(token)
            self.callback_tokens.clear()
            self.subscribed_services.clear()
            with contextlib.suppress(Exception):
                await asyncio.to_thread(session.stop_polling)
        self.controls.clear()
        for path in self.temp_paths:
            with contextlib.suppress(OSError):
                os.unlink(path)
        self.temp_paths.clear()

    def _subscribe(self):
        for device in self.session.devices:
            for service in device.device_services:
                service_key = (str(device.id), str(service.id))
                if service_key in self.subscribed_services:
                    continue
                token = (id(self), str(device.id), str(service.id))

                def changed(device_id=str(device.id)):
                    if self.loop and self.session:
                        asyncio.run_coroutine_threadsafe(self._publish_device_id(device_id), self.loop)

                service.subscribe_callback(token, changed)
                self.callback_tokens.append((service, token))
                self.subscribed_services.add(service_key)

    async def _maintenance_loop(self):
        """Übernimmt neu am Controller angelegte Geräte, ohne dessen Zustände kurz abzufragen."""
        known_devices = {str(device.id) for device in self.session.devices}
        while self.session:
            await asyncio.sleep(15)
            current_devices = {str(device.id) for device in self.session.devices}
            new_devices = current_devices - known_devices
            self._subscribe()
            for device_id in new_devices:
                await self._publish_device_id(device_id)
            known_devices = current_devices

    async def _publish_device_id(self, device_id):
        if not self.session:
            return
        groups = self._logical_device_groups(list(self.session.devices))
        match = next((item for item in groups if any(str(device.id) == str(device_id) for device in item[1])), None)
        if not match:
            return
        if not self.publish_lock:
            self.publish_lock = asyncio.Lock()
        async with self.publish_lock:
            await self._publish_device(match[1], match[0])

    async def _publish_all(self):
        if not self.publish_lock:
            self.publish_lock = asyncio.Lock()
        async with self.publish_lock:
            self.controls.clear()
            active_node_ids = set()
            for external_id, devices in self._logical_device_groups(list(self.session.devices)):
                active_node_ids.add(await self._publish_device(devices, external_id))
            for scenario in list(self.session.scenarios):
                active_node_ids.add(await self._publish_scenario(scenario))
            for node in self.context.nodes():
                if int(node.get("id", -1)) not in active_node_ids:
                    await self.context.remove_node(int(node["id"]))

    async def _publish_device(self, devices, external_id=None):
        if not isinstance(devices, (list, tuple)):
            devices = [devices]
        external_id = external_id or ("device:" + str(devices[0].id))
        node_id = self.context.stable_node_id(external_id)
        self.controls = {key: value for key, value in self.controls.items() if key[0] != node_id}
        node = self._node(devices, external_id)
        await self.context.publish_node(node)
        return node["id"]

    def _logical_device_groups(self, devices):
        """Combines Bosch' virtual room controller with the thermostats in that room."""
        ordered = sorted(devices, key=lambda item: str(item.id))
        climate_by_room = {}
        thermostats_by_room = {}
        for device in ordered:
            room_id = str(getattr(device, "room_id", "") or "")
            if not room_id:
                continue
            if _is_room_climate_control(device):
                climate_by_room.setdefault(room_id, []).append(device)
            elif _is_heating_thermostat(device):
                thermostats_by_room.setdefault(room_id, []).append(device)

        consumed = set()
        groups = []
        for room_id in sorted(climate_by_room):
            climate_devices = climate_by_room[room_id]
            thermostat_devices = thermostats_by_room.get(room_id, [])
            merged = climate_devices + thermostat_devices
            consumed.update(str(item.id) for item in merged)
            # Keep the existing physical thermostat node id so dashboard references survive the merge.
            identity_device = thermostat_devices[0] if thermostat_devices else climate_devices[0]
            groups.append(("device:" + str(identity_device.id), merged))

        for device in ordered:
            if str(device.id) not in consumed:
                groups.append(("device:" + str(device.id), [device]))
        return groups

    async def _publish_scenario(self, scenario):
        node_id = self.context.stable_node_id("scenario:" + str(scenario.id))
        attribute_id = self.context.attribute_id(node_id, 1)
        self.controls[(node_id, attribute_id)] = ("scenario", scenario)
        now = time.time()
        await self.context.publish_node({
            "id": node_id, "integration_source": "server", "name": scenario.name,
            "note": "Server · Bosch Smart Home · Szenario", "state": 1,
            "profile": 0, "protocol": 21, "image": "play.circle.fill", "state_changed": now,
            "attributes": [{
                "id": attribute_id, "node_id": node_id, "type": 1, "name": "Szenario ausführen",
                "unit": "", "current_value": 0, "target_value": 0, "editable": True,
                "minimum": 0, "maximum": 1, "step_value": 1, "last_changed": now,
            }],
        })
        return node_id

    def _node(self, devices, external_id=None):
        if not isinstance(devices, (list, tuple)):
            devices = [devices]
        device = devices[0]
        external_id = external_id or ("device:" + str(device.id))
        node_id = self.context.stable_node_id(external_id)
        now = time.time()
        attributes = []
        attribute_keys = set()
        room = ""
        with contextlib.suppress(Exception):
            room = self.session.room(device.room_id).name if device.room_id else ""
        for source_device in devices:
            for service in sorted(source_device.device_services, key=lambda item: str(item.id)):
                state = service.state if isinstance(service.state, dict) else {}
                for key, value in sorted(state.items()):
                    service_key = (str(service.id), key)
                    if service_key in attribute_keys or key.startswith("@") or isinstance(value, (dict, list)) or value is None:
                        continue
                    attribute_keys.add(service_key)
                    descriptor = _descriptor(str(service.id), key, value)
                    offset = _attribute_offset(str(service.id), key)
                    attribute_id = self.context.attribute_id(node_id, offset)
                    converted, data = _display_value(value, descriptor)
                    item = {
                        "id": attribute_id, "node_id": node_id, "type": descriptor[1],
                        "name": descriptor[0], "unit": descriptor[2], "current_value": converted,
                        "editable": bool(descriptor[3]), "last_changed": now,
                    }
                    if data is not None:
                        item["data"] = data
                    if descriptor[3]:
                        item.update({
                            "target_value": converted, "minimum": descriptor[4],
                            "maximum": descriptor[5], "step_value": descriptor[6],
                        })
                        self.controls[(node_id, attribute_id)] = (service, key, descriptor[7])
                    attributes.append(item)
        statuses = {str(getattr(item, "status", "AVAILABLE")) for item in devices}
        models = list(dict.fromkeys(str(item.device_model or "") for item in devices if item.device_model))
        merged_climate = len(devices) > 1 and any(_is_room_climate_control(item) for item in devices)
        name = f"{room} Heizung" if merged_climate and room else str(device.name)
        return {
            "id": node_id, "integration_source": "server", "name": name,
            "note": " · ".join(filter(None, ["Server", "Bosch Smart Home", room, ", ".join(models)])),
            "state": 1 if statuses & {"AVAILABLE", "ONLINE", "ENABLED"} else 2,
            "profile": 0, "protocol": 21, "image": _device_image(devices),
            "state_changed": now, "attributes": attributes,
        }

    def _temporary_credential(self, content, suffix):
        handle = tempfile.NamedTemporaryFile(mode="w", prefix="shb_bosch_", suffix=suffix, delete=False)
        try:
            handle.write(content)
        finally:
            handle.close()
        os.chmod(handle.name, 0o600)
        self.temp_paths.append(handle.name)
        return handle.name

    def _has_credentials(self):
        return bool(self.context.load_secret("certificate", "") and self.context.load_secret("private_key", ""))

    @staticmethod
    def _require_library():
        if SHCSession is None:
            raise RuntimeError("Python-Paket boschshcpy fehlt; Server-Abhängigkeiten neu installieren")


# Name, homee-Typ, Einheit, editierbar, Minimum, Maximum, Schritt, Schreibkonvertierung
_DESCRIPTORS = {
    ("TemperatureLevel", "temperature"): ("Temperatur", 5, "°C", False, 0, 0, 0, None),
    ("HumidityLevel", "humidity"): ("Luftfeuchtigkeit", 7, "%", False, 0, 0, 0, None),
    ("RoomClimateControl", "setpointTemperature"): ("Solltemperatur", 6, "°C", True, 5, 30, 0.5, "number"),
    ("RoomClimateControl", "operationMode"): ("Betriebsmodus", 45, "choice", True, 0, 1, 1, "operation_mode"),
    ("RoomClimateControl", "boostMode"): ("Boost", 1, "", True, 0, 1, 1, "bool"),
    ("RoomClimateControl", "summerMode"): ("Sommermodus", 1, "", True, 0, 1, 1, "bool"),
    ("ShutterContact", "value"): ("Fenster/Tür", 10, "text", False, 0, 0, 0, None),
    ("PowerSwitch", "switchState"): ("Schalter", 1, "", True, 0, 1, 1, "on_off"),
    ("BinarySwitch", "on"): ("Schalter", 1, "", True, 0, 1, 1, "bool"),
    ("PowerMeter", "powerConsumption"): ("Leistung", 3, "W", False, 0, 0, 0, None),
    ("PowerMeter", "energyConsumption"): ("Energie", 4, "kWh", False, 0, 0, 0, None),
    ("MultiLevelSwitch", "level"): ("Helligkeit", 15, "%", True, 0, 100, 1, "number"),
    ("ShutterControl", "level"): ("Position", 15, "%", True, 0, 100, 1, "fraction_percent"),
    ("ShutterControl", "operationState"): ("Bewegung", 135, "text", False, 0, 0, 0, None),
    ("ValveTappet", "position"): ("Ventilöffnung", 17, "%", False, 0, 0, 0, None),
    ("LatestMotion", "latestMotionDetected"): ("Letzte Bewegung", 25, "text", False, 0, 0, 0, None),
    ("WaterLeakageSensor", "state"): ("Wasseralarm", 13, "text", False, 0, 0, 0, None),
    ("CommunicationQuality", "quality"): ("Signalqualität", 33, "text", False, 0, 0, 0, None),
    ("PresenceSimulationConfiguration", "enabled"): ("Anwesenheitssimulation", 1, "", True, 0, 1, 1, "bool"),
    ("PrivacyMode", "value"): ("Privatsphäre", 1, "", True, 0, 1, 1, "enabled_disabled"),
    ("CameraLight", "value"): ("Kameralicht", 1, "", True, 0, 1, 1, "on_off"),
    ("AirQualityLevel", "temperature"): ("Temperatur", 5, "°C", False, 0, 0, 0, None),
    ("AirQualityLevel", "humidity"): ("Luftfeuchtigkeit", 7, "%", False, 0, 0, 0, None),
    ("AirQualityLevel", "purity"): ("Luftreinheit", 65, "%", False, 0, 0, 0, None),
}

_LABELS = {
    "OPEN": "Offen", "CLOSED": "Geschlossen", "ON": "Ein", "OFF": "Aus",
    "ENABLED": "Aktiv", "DISABLED": "Inaktiv", "AUTOMATIC": "Automatisch", "MANUAL": "Manuell",
    "GOOD": "Gut", "MEDIUM": "Mittel", "BAD": "Schlecht", "UNKNOWN": "Unbekannt",
    "NO_LEAKAGE": "Trocken", "LEAKAGE_DETECTED": "Wasser erkannt", "AVAILABLE": "Verfügbar",
    "STOPPED": "Gestoppt", "MOVING": "Bewegt sich", "CALIBRATING": "Kalibrierung",
}


def _descriptor(service, key, value):
    known = _DESCRIPTORS.get((service, key))
    if known:
        return known
    name = re.sub(r"(?<!^)(?=[A-Z])", " ", key).replace("_", " ").strip().capitalize()
    if isinstance(value, bool):
        return (name, 1, "", False, 0, 1, 1, None)
    if isinstance(value, (int, float)):
        return (name, 0, "", False, 0, 0, 0, None)
    return (name, 0, "text", False, 0, 0, 0, None)


def _display_value(value, descriptor):
    if descriptor[7] == "operation_mode":
        selected = 0 if str(value).upper() == "AUTOMATIC" else 1
        label = "Automatisch" if selected == 0 else "Manuell"
        return selected, json.dumps({
            "label": label,
            "options": [
                {"value": 0, "label": "Automatisch"},
                {"value": 1, "label": "Manuell"},
            ],
        }, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, bool):
        return (1 if value else 0), None
    if isinstance(value, (int, float)):
        if descriptor[7] == "fraction_percent":
            return round(float(value) * 100, 1), None
        return value, None
    text = _LABELS.get(str(value), str(value).replace("_", " ").title())
    if descriptor[7] in ("on_off", "enabled_disabled"):
        return (1 if str(value) in ("ON", "ENABLED") else 0), None
    digest = int.from_bytes(hashlib.sha256(str(value).encode()).digest()[:4], "big")
    return digest, text


def _control_value(conversion, value):
    active = float(value) >= 0.5
    if conversion == "bool":
        return active
    if conversion == "on_off":
        return "ON" if active else "OFF"
    if conversion == "enabled_disabled":
        return "ENABLED" if active else "DISABLED"
    if conversion == "fraction_percent":
        return max(0.0, min(1.0, float(value) / 100.0))
    if conversion == "operation_mode":
        return "AUTOMATIC" if float(value) < 0.5 else "MANUAL"
    return float(value)


def _attribute_offset(service, key):
    return 100 + int.from_bytes(hashlib.sha256(f"{service}:{key}".encode()).digest()[:4], "big") % 900_000


def _service_ids(device):
    return {str(item.id) for item in getattr(device, "device_services", [])}


def _is_room_climate_control(device):
    return "RoomClimateControl" in _service_ids(device) or str(getattr(device, "id", "")).startswith("roomClimateControl_")


def _is_heating_thermostat(device):
    services = _service_ids(device)
    if not services & {"TemperatureLevel", "HumidityLevel"}:
        return False
    if services & {"Thermostat", "ValveTappet", "TemperatureOffset", "SilentMode"}:
        return True
    model = str(getattr(device, "device_model", "")).upper()
    if model in {"TRV", "TRV_GEN2", "TRV_GEN2_DUAL", "THB", "BWTH", "BWTH24", "RTH2_BAT", "RTH2_230"}:
        return True
    hint = " ".join([str(getattr(device, "name", "")), model]).lower()
    return any(token in hint for token in ("thermostat", "radiator", "trv", "heizkörper"))


def _device_image(devices):
    if not isinstance(devices, (list, tuple)):
        devices = [devices]
    services = set().union(*(_service_ids(device) for device in devices))
    if "ShutterContact" in services:
        return "door.left.hand.open"
    if "RoomClimateControl" in services or "TemperatureLevel" in services:
        return "thermometer.medium"
    if "ShutterControl" in services:
        return "window.shade.closed"
    if "PowerSwitch" in services:
        return "powerplug.fill"
    if "LatestMotion" in services:
        return "figure.walk.motion"
    if "WaterLeakageSensor" in services:
        return "drop.triangle.fill"
    return "sensor.fill"


def _host(configuration):
    host = str(configuration.get("host", "")).strip()
    host = re.sub(r"^https?://", "", host).split("/", 1)[0].split(":", 1)[0]
    if not host:
        raise ValueError("Controller-IP oder Hostname fehlt")
    return host


def _friendly_error(error):
    return re.sub(r"\s+", " ", str(error)).strip()[:400]


def _text(value):
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)
