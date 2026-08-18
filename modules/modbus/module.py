import asyncio
import json
import logging
import math
import os
import struct
import time
from pathlib import Path

from pymodbus.client import AsyncModbusTcpClient

PROFILE_DIR = Path(__file__).parent / "profiles"
log = logging.getLogger("smarthomeboard.modbus")


def _profiles():
    result = {}
    directories = [PROFILE_DIR, Path(os.getenv("SHB_DATA_DIR", "/data")) / "modbus-profiles"]
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
        for path in sorted(directory.glob("*.json")):
            try:
                profile = json.loads(path.read_text(encoding="utf-8"))
                result[profile["id"]] = profile
            except Exception:
                log.exception("Modbus-Profil %s konnte nicht geladen werden", path)
    # Kompatibilität mit Server 0.1/0.2: bestehende Integrationen behalten ihre alte Profil-ID.
    current_mennekes = result.get("mennekes.amtron-4you500-4business700.v1_5")
    if current_mennekes:
        result.setdefault("mennekes.amtron.v1_5", current_mennekes)
    return result


def manifest():
    profiles = _profiles()
    visible_profiles = {item["id"]: item for item in profiles.values()}
    return {
        "id": "modbus-tcp", "name": "Modbus TCP", "version": "2.0.0", "icon": "point.3.connected.trianglepath.dotted",
        "description": "Universeller Modbus-TCP-Adapter. Weitere Geräte werden ausschließlich als JSON-Datei im Ordner modules/modbus/profiles ergänzt.",
        "supportsDiscovery": False, "supportsMultipleInstances": True,
        "fields": [
            {"key": "profile", "type": "select", "title": "Geräteprofil", "required": True,
             "options": [{"value": item["id"], "label": f"{item.get('manufacturer', '')} · {item['model']}"} for item in sorted(visible_profiles.values(), key=lambda value: (value.get("manufacturer", ""), value["model"]))]},
            {"key": "host", "type": "text", "title": "IP-Adresse oder Hostname", "required": True},
            {"key": "port", "type": "port", "title": "Port", "default": 502},
            {"key": "unit_id", "type": "integer", "title": "Unit-ID", "default": 1},
            {"key": "poll_seconds", "type": "duration", "title": "Abfrageintervall", "default": 10, "minimum": 2, "unit": "s"}
        ]
    }


def create(configuration, context):
    profile = _profiles().get(str(configuration.get("profile", "")))
    if not profile:
        raise ValueError("Das gewählte Modbus-Profil ist nicht vorhanden")
    return ModbusAdapter(configuration, context, profile)


class ModbusAdapter:
    def __init__(self, configuration, context, profile):
        self.configuration, self.context, self.profile = configuration, context, profile
        self.node_id = context.stable_node_id(f"{configuration.get('host')}:{configuration.get('unit_id')}:{profile['id']}")
        self.client = None
        self.task = None

    async def start(self):
        self.client = AsyncModbusTcpClient(str(self.configuration.get("host", "")), port=int(float(self.configuration.get("port", 502))), timeout=7)
        if not await self.client.connect():
            raise ConnectionError("Modbus-TCP-Verbindung fehlgeschlagen")
        await self.poll()
        self.task = asyncio.create_task(self.loop())

    async def stop(self):
        if self.task: self.task.cancel()
        if self.client: self.client.close()

    async def loop(self):
        while True:
            await asyncio.sleep(max(2, int(self.profile.get("minimum_poll_seconds", 2)), int(float(self.configuration.get("poll_seconds", 10)))))
            try:
                await self.poll()
                await self.context.set_status("Verbunden")
            except asyncio.CancelledError:
                return
            except Exception as error:
                await self.context.set_status("Nicht erreichbar", str(error))

    async def poll(self):
        attributes = []
        unit = int(float(self.configuration.get("unit_id", self.profile.get("default_unit_id", 1))))
        now = time.time()
        for index, mapping in enumerate(self.profile.get("registers", []), start=1):
            count = {"int16": 1, "uint16": 1, "int32": 2, "uint32": 2, "float32": 2, "int64": 4, "uint64": 4, "float64": 4}.get(mapping.get("data_type"), 1)
            method = self.client.read_input_registers if mapping.get("register_type") == "input" else self.client.read_holding_registers
            response = await self._read(method, int(mapping["address"]), count, unit)
            if response.isError():
                raise IOError(f"Modbus-Register {mapping['address']}: {response}")
            value = _decode(response.registers, mapping)
            if not math.isfinite(value) and mapping.get("unavailable_value_policy") == "sma": value = 0
            elif not math.isfinite(value): continue
            value = value * float(mapping.get("scale", 1)) + float(mapping.get("offset", 0))
            attribute = {"id": self.node_id * 100 + index, "node_id": self.node_id, "type": int(mapping.get("attribute_type", 222)),
                         "name": mapping["name"], "unit": mapping.get("unit", ""), "current_value": value,
                         "editable": bool(mapping.get("writable", False)), "last_changed": now}
            if attribute["editable"]:
                attribute["target_value"] = value
                for source, target in (("minimum", "minimum"), ("maximum", "maximum"), ("step", "step_value")):
                    if source in mapping: attribute[target] = mapping[source]
            labels = mapping.get("value_labels", {})
            label = labels.get(str(round(value)), labels.get(round(value)))
            if label is not None: attribute["data"] = str(label)
            attributes.append(attribute)
        if not attributes:
            attributes.append({"id": self.node_id * 100 + 1, "node_id": self.node_id, "type": 213, "name": "Verbindung", "unit": "text",
                               "current_value": 1, "data": "Verbunden", "editable": False, "last_changed": now})
        await self.context.publish_node({"id": self.node_id, "integration_source": "server", "name": self.context.integration_name,
            "note": f"Server · Modbus TCP · {self.profile.get('manufacturer', '')} {self.profile['model']}", "state": 1,
            "profile": int(self.profile.get("node_profile", 0)), "protocol": 20, "image": self.profile.get("icon", "server.rack"),
            "state_changed": now, "attributes": attributes})

    async def set_value(self, node_id, attribute_id, value):
        index = attribute_id - node_id * 100 - 1
        mappings = self.profile.get("registers", [])
        if node_id != self.node_id or not 0 <= index < len(mappings): raise KeyError("Unbekanntes Modbus-Attribut")
        mapping = mappings[index]
        if not mapping.get("writable"): raise ValueError("Modbus-Register ist nur lesbar")
        raw = (float(value) - float(mapping.get("offset", 0))) / float(mapping.get("scale", 1))
        unit = int(float(self.configuration.get("unit_id", self.profile.get("default_unit_id", 1))))
        words = _encode(raw, mapping)
        response = await self._write(int(mapping["address"]), words, unit)
        if response.isError(): raise IOError(str(response))
        log.info("Modbus-Schreibbefehl bestätigt: %s · Unit %s · Register %s · Wert %s", self.context.integration_name, unit, mapping["address"], value)
        if mapping.get("id") == "gx-relay-1":
            await asyncio.sleep(0.3)
            confirmation = await self._read(self.client.read_holding_registers, int(mapping["address"]), 1, unit)
            if confirmation.isError():
                raise IOError(f"Cerbo-Relais konnte nach dem Schreiben nicht zurückgelesen werden: {confirmation}")
            confirmed = _decode(confirmation.registers, mapping) * float(mapping.get("scale", 1)) + float(mapping.get("offset", 0))
            if abs(confirmed - float(value)) > 0.001:
                raise IOError("Cerbo GX hat den Relaiswert nicht übernommen. Relais 1 unter Einstellungen → Integrationen → Relais auf Funktion 'Manuell' stellen und beim Modbus-TCP-Server 'Schreiben erlaubt' wählen.")
        await self.poll()

    async def _read(self, method, address, count, unit):
        try:
            return await method(address=address, count=count, device_id=unit)
        except TypeError:
            return await method(address=address, count=count, slave=unit)

    async def _write(self, address, words, unit):
        method = self.client.write_register if len(words) == 1 else self.client.write_registers
        arguments = {"address": address, "value": words[0]} if len(words) == 1 else {"address": address, "values": words}
        try:
            return await method(**arguments, device_id=unit)
        except TypeError:
            return await method(**arguments, slave=unit)


def _decode(registers, mapping):
    data_type = mapping.get("data_type", "uint16")
    if mapping.get("word_order") == "swappedWords" and len(registers) > 1: registers = list(reversed(registers))
    raw = b"".join(int(word).to_bytes(2, "big") for word in registers)
    if mapping.get("unavailable_value_policy") == "sma" and _is_sma_unavailable(raw, data_type): return math.nan
    code = {"int16": ">h", "uint16": ">H", "int32": ">i", "uint32": ">I", "float32": ">f", "int64": ">q", "uint64": ">Q", "float64": ">d"}[data_type]
    return struct.unpack(code, raw)[0]


def _encode(value, mapping):
    data_type = mapping.get("data_type", "uint16")
    code = {"int16": ">h", "uint16": ">H", "int32": ">i", "uint32": ">I", "float32": ">f", "int64": ">q", "uint64": ">Q", "float64": ">d"}[data_type]
    number = float(value) if data_type.startswith("float") else round(value)
    raw = struct.pack(code, number)
    words = [int.from_bytes(raw[index:index + 2], "big") for index in range(0, len(raw), 2)]
    return list(reversed(words)) if mapping.get("word_order") == "swappedWords" and len(words) > 1 else words


def _is_sma_unavailable(raw, data_type):
    values = {"int16": 0x8000, "uint16": 0xFFFF, "int32": 0x80000000, "int64": 0x8000000000000000, "uint64": 0xFFFFFFFFFFFFFFFF}
    number = int.from_bytes(raw, "big")
    if data_type == "uint32": return number in (0xFFFFFFFF, 0x00FFFFFD)
    return data_type in values and number == values[data_type]
