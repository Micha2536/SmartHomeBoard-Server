"""EnOcean USB300 integration using the standardized ESP3 serial protocol.

The first version deliberately keeps transmission disabled. It receives ERP1
telegrams, persists learned sender IDs/EEPs and publishes homee-compatible
nodes. Unknown EEPs stay visible as diagnostic nodes instead of disappearing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("smarthomeboard.enocean")

PACKET_RADIO_ERP1 = 0x01
RORG_RPS = 0xF6
RORG_1BS = 0xD5
RORG_4BS = 0xA5


def manifest():
    return {
        "id": "enocean",
        "name": "EnOcean USB300",
        "version": "1.3.0",
        "icon": "antenna.radiowaves.left.and.right",
        "description": (
            "Lokales EnOcean-Backend über ESP3. Lernt Sender dauerhaft an und "
            "bildet Kontakte, Fenstergriffe, Taster sowie verbreitete Klima-, "
            "Helligkeits- und Bewegungssensoren auf Dashboard-Geräte ab."
        ),
        "supportsDiscovery": True,
        "supportsMultipleInstances": False,
        "fields": [
            {
                "key": "serial_port",
                "type": "text",
                "title": "Serielle Schnittstelle",
                "default": "/dev/enocean",
                "placeholder": "/dev/enocean",
                "required": True,
                "help": "Im Docker-Setup wird der stabile USB300-Gerätepfad als /dev/enocean eingebunden.",
            },
            {
                "key": "default_f6_eep",
                "type": "select",
                "title": "Standardprofil für batterielose F6-Sender",
                "default": "F6-02-01",
                "options": [
                    {"value": "F6-02-01", "label": "Wandtaster · F6-02-01"},
                    {"value": "F6-02-02", "label": "Wandtaster invertiert · F6-02-02"},
                    {"value": "F6-10-00", "label": "Fenstergriff · F6-10-00"},
                ],
                "help": "F6-Telegramme übertragen ihr EEP nicht. Das Profil gilt beim Anlernen; Ausnahmen können unten zugeordnet werden.",
            },
            {
                "key": "eep_overrides",
                "type": "multiline",
                "title": "Abweichende EEP-Zuordnungen",
                "placeholder": "019ADAA0=F6-10-00",
                "default": "",
                "help": "Optional eine Zeile je Sender: achtstellige Sender-ID=EEP, beispielsweise 019ADAA0=F6-10-00.",
            },
        ],
        "actions": [
            {"id": "start_learning", "title": "60 Sekunden anlernen", "icon": "dot.radiowaves.left.and.right"},
            {"id": "stop_learning", "title": "Anlernen beenden", "icon": "stop.circle"},
        ],
    }


def create(configuration, context):
    return EnOceanAdapter(configuration, context)


class ESP3StreamParser:
    """Incremental ESP3 parser with synchronization and both CRC checks."""

    def __init__(self):
        self.buffer = bytearray()

    def feed(self, chunk):
        self.buffer.extend(chunk)
        packets = []
        while True:
            try:
                sync = self.buffer.index(0x55)
            except ValueError:
                self.buffer.clear()
                break
            if sync:
                del self.buffer[:sync]
            if len(self.buffer) < 6:
                break
            header = bytes(self.buffer[1:5])
            if crc8(header) != self.buffer[5]:
                del self.buffer[0]
                continue
            data_length = (header[0] << 8) | header[1]
            optional_length = header[2]
            payload_length = data_length + optional_length
            frame_length = 7 + payload_length
            if data_length > 4096 or optional_length > 255:
                del self.buffer[0]
                continue
            if len(self.buffer) < frame_length:
                break
            payload = bytes(self.buffer[6 : 6 + payload_length])
            if crc8(payload) != self.buffer[6 + payload_length]:
                del self.buffer[0]
                continue
            packets.append((header[3], payload[:data_length], payload[data_length:]))
            del self.buffer[:frame_length]
        return packets


class EnOceanAdapter:
    def __init__(self, configuration, context):
        self.configuration = configuration
        self.context = context
        self.serial = None
        self.reader_task = None
        self.learning_task = None
        self.learning_until = 0.0
        self.learning_eep = ""
        self.learning_profile_id = ""
        self.learning_variant = ""
        self.learning_name = ""
        self.parser = ESP3StreamParser()
        state = context.load_state({"devices": {}}) or {"devices": {}}
        self.devices = state.get("devices", {}) if isinstance(state, dict) else {}
        self.eep_overrides = parse_eep_overrides(configuration.get("eep_overrides", ""))

    async def start(self):
        port = str(self.configuration.get("serial_port", "/dev/enocean")).strip()
        if not port:
            raise ValueError("Serielle EnOcean-Schnittstelle fehlt")
        try:
            import serial
            self.serial = serial.Serial(port=port, baudrate=57600, bytesize=8, parity="N", stopbits=1, timeout=1)
            self.serial.reset_input_buffer()
        except ImportError as error:
            raise RuntimeError("Python-Paket pyserial fehlt; Container bitte neu bauen") from error
        except Exception as error:
            raise ConnectionError(f"EnOcean USB300 konnte unter {port} nicht geöffnet werden: {error}") from error

        migrated = False
        for sender_id, device in list(self.devices.items()):
            try:
                if device.get("eep") == "A5-14-09" and "window_position" not in device.get("values", {}):
                    raw = bytes.fromhex(str(device.get("raw", "")))
                    if len(raw) == 4:
                        device["values"] = decode_eep("A5-14-09", raw)
                        self.devices[sender_id] = device
                        migrated = True
                await self._publish(sender_id, device)
            except Exception:
                log.exception("Gespeichertes EnOcean-Gerät %s konnte nicht veröffentlicht werden", sender_id)
        if migrated:
            self._save_state()
        self.reader_task = asyncio.create_task(self._reader_loop())
        await self.context.set_status("Verbunden")

    async def stop(self):
        for task in (self.reader_task, self.learning_task):
            if task:
                task.cancel()
        if self.serial:
            with contextlib.suppress(Exception):
                self.serial.close()
        self.serial = None

    async def action(self, action_id, payload):
        if action_id == "get_management":
            return self._management_state()
        if action_id == "start_learning":
            profile_id = normalize_eep(payload.get("eep"))
            profile = profile_by_id(profile_id)
            if not profile:
                raise ValueError("Vor dem Anlernen muss ein verfügbares EnOcean-EEP-Profil ausgewählt werden")
            eep = normalize_eep(profile.get("eep") or profile_id)
            seconds = min(300, max(10, int(payload.get("seconds", 60))))
            self.learning_until = time.monotonic() + seconds
            self.learning_eep = eep
            self.learning_profile_id = profile_id
            self.learning_variant = str(profile.get("variant", ""))
            self.learning_name = str(payload.get("name", "")).strip()[:120]
            self._save_state(
                learning_until=time.time() + seconds,
                learning_eep=eep,
                learning_profile_id=profile_id,
            )
            if self.learning_task:
                self.learning_task.cancel()
            self.learning_task = asyncio.create_task(self._finish_learning_after(seconds))
            await self.context.set_status(f"Anlernmodus · {eep} · noch {seconds} s")
            return self._management_state()
        if action_id == "stop_learning":
            await self._stop_learning()
            return self._management_state()
        if action_id == "update_device":
            sender_id = normalize_sender_id(payload.get("sender_id"))
            device = self.devices.get(sender_id)
            if not device:
                raise KeyError("EnOcean-Gerät wurde nicht gefunden")
            name = str(payload.get("name", "")).strip()
            profile_id = normalize_eep(payload.get("eep"))
            profile = profile_by_id(profile_id)
            if not profile:
                raise ValueError("Bitte ein verfügbares EnOcean-Geräteprofil auswählen")
            eep = normalize_eep(profile.get("eep") or profile_id)
            if name:
                device["name"] = name[:120]
            device["eep"] = eep
            device["profile_id"] = profile_id
            device["variant"] = str(profile.get("variant", ""))
            raw = bytes.fromhex(str(device.get("raw", "")))
            if raw:
                device["values"] = decode_eep(eep, raw, device["variant"])
            self.devices[sender_id] = device
            self._save_state()
            await self._publish(sender_id, device)
            return self._management_state()
        if action_id == "delete_device":
            sender_id = normalize_sender_id(payload.get("sender_id"))
            if sender_id not in self.devices:
                raise KeyError("EnOcean-Gerät wurde nicht gefunden")
            del self.devices[sender_id]
            self._save_state()
            await self.context.remove_node(self.context.stable_node_id(sender_id))
            return self._management_state()
        raise ValueError("Unbekannte EnOcean-Modulaktion")

    async def set_value(self, node_id, attribute_id, value):
        raise ValueError("Die erste EnOcean-Version unterstützt zunächst nur empfangende Geräte")

    async def _finish_learning_after(self, seconds):
        try:
            await asyncio.sleep(seconds)
            await self._stop_learning()
        except asyncio.CancelledError:
            return

    async def _stop_learning(self):
        self.learning_until = 0.0
        self.learning_eep = ""
        self.learning_profile_id = ""
        self.learning_variant = ""
        self.learning_name = ""
        self._save_state(learning_until=0, learning_eep="", learning_profile_id="")
        if self.learning_task and self.learning_task is not asyncio.current_task():
            self.learning_task.cancel()
        self.learning_task = None
        await self.context.set_status("Verbunden")

    async def _reader_loop(self):
        while self.serial:
            try:
                chunk = await asyncio.to_thread(self.serial.read, 512)
                for packet_type, data, optional in self.parser.feed(chunk):
                    if packet_type == PACKET_RADIO_ERP1:
                        await self._handle_radio(data, optional)
            except asyncio.CancelledError:
                return
            except Exception as error:
                log.exception("Fehler beim Lesen des EnOcean USB300")
                await self.context.set_status("Nicht erreichbar", str(error))
                await asyncio.sleep(2)

    async def _handle_radio(self, data, optional):
        telegram = parse_radio_erp1(data, optional)
        if not telegram:
            return
        sender_id = telegram["sender_id"]
        known = self.devices.get(sender_id)
        learning = time.monotonic() < self.learning_until
        if not known and not learning:
            return

        # Gespeicherte IDs bleiben für weitere Anlernvorgänge gesperrt. Ihre
        # Zustände werden aktualisiert, sie dürfen aber keinen Lernlauf beenden.
        is_new_learning_device = known is None and learning
        if is_new_learning_device and not eep_matches_rorg(self.learning_eep, telegram["rorg"]):
            return

        eep = known.get("eep") if known else self.learning_eep or self.eep_overrides.get(sender_id)
        if not eep:
            eep = detect_eep(telegram, str(self.configuration.get("default_f6_eep", "F6-02-01")))
        if not known and not eep:
            # A 4BS data telegram without teach-in information cannot be decoded
            # safely. Keep it visible so the user can add an override.
            eep = f"{telegram['rorg']:02X}-00-00"

        if is_teach_in(telegram["rorg"], telegram["user_data"]):
            decoded = dict((known or {}).get("values", {}))
            # A5-14-09 enthält selbst im 4BS-Teach-in-Telegramm bereits die
            # Kontaktbits. So erscheint der ausgewählte Fenstergriff sofort
            # als homee-kompatibler 0/1/2-Zustand statt nur als Rohwert.
            if not decoded and eep == "A5-14-09":
                decoded = decode_eep(eep, telegram["user_data"], (known or {}).get("variant", self.learning_variant))
            if not decoded:
                decoded = {"raw_value": int.from_bytes(telegram["user_data"], "big")}
        else:
            decoded = decode_eep(eep, telegram["user_data"], (known or {}).get("variant", self.learning_variant))
        if eep.startswith("F6-02-") and known:
            # Jede RPS-Nachricht beschreibt das Ereignis einer Wippe. Der
            # zuletzt bekannte Zustand der anderen Wippe bleibt erhalten.
            decoded = {**dict(known.get("values", {})), **decoded}
        device = dict(known or {})
        device.update({
            "eep": eep,
            "profile_id": device.get("profile_id") or (self.learning_profile_id if is_new_learning_device else eep),
            "variant": device.get("variant") or (self.learning_variant if is_new_learning_device else profile_variant(eep)),
            "name": device.get("name") or (self.learning_name if is_new_learning_device else "") or default_name(eep, sender_id),
            "manufacturer": telegram.get("manufacturer"),
            "rssi": telegram.get("rssi"),
            "raw": telegram["user_data"].hex().upper(),
            "values": decoded,
            "last_seen": time.time(),
        })
        self.devices[sender_id] = device
        self._save_state()
        await self._publish(sender_id, device)
        if is_new_learning_device:
            # Genau ein bislang unbekannter Sender pro Lernlauf. Das Stoppen
            # geschieht direkt nach dem persistenten Speichern der Sender-ID.
            await self._stop_learning()
            await self.context.set_status(f"Angelernt · {sender_id} · {eep}")

    def _save_state(self, learning_until=None, learning_eep=None, learning_profile_id=None):
        previous = self.context.load_state({}) or {}
        until = previous.get("learning_until", 0) if learning_until is None else learning_until
        selected = previous.get("learning_eep", "") if learning_eep is None else learning_eep
        selected_profile = previous.get("learning_profile_id", "") if learning_profile_id is None else learning_profile_id
        self.context.save_state({
            "devices": self.devices,
            "learning_until": until,
            "learning_eep": selected,
            "learning_profile_id": selected_profile,
        })

    def _management_state(self):
        remaining = max(0, int(self.learning_until - time.monotonic()))
        devices = []
        for sender_id, device in sorted(self.devices.items(), key=lambda item: str(item[1].get("name", item[0])).casefold()):
            devices.append({
                "sender_id": sender_id,
                "name": str(device.get("name") or sender_id),
                "eep": str(device.get("eep") or ""),
                "rssi": device.get("rssi"),
                "last_seen": device.get("last_seen"),
                "raw": str(device.get("raw") or ""),
            })
        return {
            "learning": remaining > 0,
            "learning_seconds": remaining,
            "selected_eep": self.learning_profile_id if remaining else "",
            "devices": devices,
            "profiles": profile_catalog(),
        }

    async def _publish(self, sender_id, device):
        node_id = self.context.stable_node_id(sender_id)
        values = device.get("values", {})
        now = float(device.get("last_seen") or time.time())
        eep = str(device.get("eep", "Unbekannt"))
        profile, image = node_presentation(eep)
        attributes = []

        def add(offset, kind, name, value, unit="", data=None, instance=None):
            item = {
                "id": self.context.attribute_id(node_id, offset),
                "node_id": node_id,
                "type": kind,
                "name": name,
                "unit": unit,
                "current_value": value,
                "editable": False,
                "last_changed": now,
            }
            if data is not None:
                item["data"] = data
            if instance is not None:
                item["instance"] = instance
            attributes.append(item)

        if "open" in values:
            add(1, 14, "Geöffnet", values["open"], data="Offen" if values["open"] else "Geschlossen")
        if "window_position" in values:
            position = int(values["window_position"])
            add(1, 10, "Fensterposition", position, data={0: "Geschlossen", 1: "Offen", 2: "Gekippt"}.get(position, "Unbekannt"))
        if eep.startswith("F6-02-"):
            variant = str(device.get("variant") or profile_variant(device.get("profile_id") or eep))
            add(1, 40, "Wippe 1", values.get("rocker_1", 0), data=values.get("rocker_1_name", "Nicht betätigt"), instance=1)
            if variant != "single":
                add(2, 40, "Wippe 2", values.get("rocker_2", 0), data=values.get("rocker_2_name", "Nicht betätigt"), instance=2)
            energy_bow = int(values.get("energy_bow", 0) or 0)
            add(
                3, 0, "Energy Harvesting", energy_bow,
                data="Energy Bow gedrückt" if energy_bow else "Energy Bow losgelassen",
                instance=3,
            )
        elif "rocker_1" in values:
            add(1, 40, "Wippe 1", values["rocker_1"], data=values.get("rocker_1_name"), instance=1)
        if "rocker_2" in values and not eep.startswith("F6-02-"):
            add(2, 40, "Wippe 2", values["rocker_2"], data=values.get("rocker_2_name"), instance=2)
        if "button" in values and "rocker_1" not in values:
            add(1, 40, "Taster", values["button"], data=values.get("button_name"))
        if "temperature" in values:
            add(2, 5, "Temperatur", round(values["temperature"], 2), "°C")
        if "humidity" in values:
            add(3, 7, "Luftfeuchtigkeit", round(values["humidity"], 1), "%")
        if "motion" in values:
            add(4, 25, "Bewegung", values["motion"], data="Bewegung" if values["motion"] else "Keine Bewegung")
        if "smoke" in values:
            add(4, 16, "Rauchalarm", values["smoke"], data="Rauch erkannt" if values["smoke"] else "Kein Rauch")
        if "battery_low" in values:
            add(8, 69, "Batteriewarnung", values["battery_low"], data="Batterie schwach" if values["battery_low"] else "Batterie in Ordnung")
        if "alarm" in values:
            add(9, 27, "Alarm", values["alarm"], data="Alarm" if values["alarm"] else "Kein Alarm")
        if "brightness" in values:
            add(5, 11, "Helligkeit", round(values["brightness"], 1), "lx")
        if "supply_voltage" in values:
            add(6, 51, "Versorgungsspannung", round(values["supply_voltage"], 2), "V")
        if "raw_value" in values or not attributes:
            add(7, 0, "Rohwert", values.get("raw_value", int(device.get("raw") or "0", 16)), data=device.get("raw"))
        quality = link_quality(device.get("rssi"))
        add(90, 33, "Empfangsqualität", quality, data=f"{quality}/3 · {device.get('rssi', '?')} dBm")

        await self.context.publish_node({
            "id": node_id,
            "integration_source": "server",
            "name": device.get("name") or default_name(eep, sender_id),
            "note": f"Server · EnOcean · {sender_id} · EEP {eep}",
            "state": 1,
            "profile": profile,
            "protocol": 3,
            "cube_type": 3,
            "image": image,
            "state_changed": now,
            "attributes": attributes,
        })


def crc8(data):
    value = 0
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = ((value << 1) ^ 0x07) & 0xFF if value & 0x80 else (value << 1) & 0xFF
    return value


def parse_radio_erp1(data, optional=b""):
    if len(data) < 7:
        return None
    rorg = data[0]
    sender = data[-5:-1].hex().upper()
    user_data = bytes(data[1:-5])
    if rorg == RORG_RPS and len(user_data) != 1:
        return None
    if rorg == RORG_1BS and len(user_data) != 1:
        return None
    if rorg == RORG_4BS and len(user_data) != 4:
        return None
    result = {
        "rorg": rorg,
        "sender_id": sender,
        "user_data": user_data,
        "status": data[-1],
        "rssi": -int(optional[5]) if len(optional) >= 6 else None,
    }
    if rorg == RORG_4BS and is_teach_in(rorg, user_data) and user_data[3] & 0x80:
        db3, db2, db1, _db0 = user_data
        function = (db3 >> 2) & 0x3F
        kind = ((db3 & 0x03) << 5) | ((db2 >> 3) & 0x1F)
        result["eep"] = f"A5-{function:02X}-{kind:02X}"
        result["manufacturer"] = ((db2 & 0x07) << 8) | db1
    return result


def is_teach_in(rorg, user_data):
    if rorg in (RORG_1BS, RORG_4BS) and user_data:
        return (user_data[-1] & 0x08) == 0
    return False


def detect_eep(telegram, default_f6_eep):
    if telegram.get("eep"):
        return telegram["eep"]
    if telegram["rorg"] == RORG_RPS:
        return normalize_eep(default_f6_eep)
    if telegram["rorg"] == RORG_1BS:
        return "D5-00-01"
    return None


def decode_eep(eep, payload, variant=""):
    eep = normalize_eep(eep)
    if not payload:
        return {"raw_value": 0}
    if eep == "D5-00-01":
        contact_closed = payload[0] & 0x01
        return {"open": 0 if contact_closed else 1}
    if eep == "F6-10-00":
        position = {0xF0: 0, 0xE0: 1, 0xD0: 2}.get(payload[0], 3)
        return {"window_position": position}
    if eep.startswith("F6-02-"):
        raw = payload[0]
        pressed = (raw >> 4) & 0x01
        rocker = (raw >> 5) & 0x07
        second = (raw >> 1) & 0x07
        second_pressed = raw & 0x01
        code = rocker + 1 if pressed else (second + 9 if second_pressed else 0)
        names = {0: "Losgelassen", 1: "AI", 2: "AO", 3: "BI", 4: "BO", 5: "CI", 6: "CO", 7: "DI", 8: "DO"}
        double_rocker = variant != "single"
        result = {
            "button": code,
            "button_name": names.get(code, f"Taste {code}"),
            "energy_bow": 1 if pressed else 0,
            "raw_value": raw,
        }

        def apply_rocker(action, is_pressed):
            group = action // 2 + 1
            if group > (2 if double_rocker else 1):
                return
            is_i_side = action % 2 == 0
            value = (1 if is_i_side else 2) if is_pressed else (3 if is_i_side else 4)
            result[f"rocker_{group}"] = value
            result[f"rocker_{group}_name"] = {
                1: "I gedrückt",
                2: "O gedrückt",
                3: "I losgelassen",
                4: "O losgelassen",
            }[value]

        apply_rocker(rocker, bool(pressed))
        if second_pressed:
            apply_rocker(second, True)
            result["energy_bow"] = 1
        return result
    if eep == "F6-05-01":
        return {"alarm": 1 if payload[0] else 0, "raw_value": payload[0]}
    if eep == "F6-05-02":
        return {
            "smoke": 1 if payload[0] == 0x10 else 0,
            "battery_low": 1 if payload[0] == 0x30 else 0,
            "raw_value": payload[0],
        }
    if eep.startswith("F6-04-"):
        return {"button": 1 if payload[0] else 0, "button_name": "Karte eingesetzt" if payload[0] else "Karte entfernt", "raw_value": payload[0]}
    if eep.startswith("A5-02-") and len(payload) == 4:
        minimum, maximum = temperature_range(eep)
        if minimum is not None:
            return {"temperature": maximum - payload[2] * (maximum - minimum) / 255.0}
    if eep in ("A5-04-01", "A5-04-02") and len(payload) == 4:
        minimum, maximum = (0.0, 40.0) if eep.endswith("01") else (-20.0, 60.0)
        return {
            "humidity": min(100.0, payload[1] * 100.0 / 250.0),
            "temperature": minimum + min(250, payload[2]) * (maximum - minimum) / 250.0,
        }
    if eep == "A5-06-02" and len(payload) == 4:
        return {"brightness": payload[2] * 1020.0 / 255.0}
    if eep in ("A5-07-01", "A5-07-02", "A5-07-03") and len(payload) == 4:
        result = {"motion": 1 if payload[2] > 0 else 0, "supply_voltage": payload[0] * 5.0 / 250.0}
        if eep == "A5-07-03":
            result["brightness"] = payload[1] * 1000.0 / 255.0
        return result
    if eep in ("A5-08-01", "A5-08-02", "A5-08-03") and len(payload) == 4:
        factor = {"A5-08-01": 2.0, "A5-08-02": 4.0, "A5-08-03": 6.0}[eep]
        minimum, maximum = (-30.0, 50.0) if eep.endswith("03") else (0.0, 51.0)
        return {
            "brightness": payload[0] * factor,
            "temperature": minimum + payload[1] * (maximum - minimum) / 255.0,
            "motion": 1 if payload[2] > 0 else 0,
        }
    if eep == "A5-14-09" and len(payload) == 4:
        # EEP: DB0.2..DB0.1 = 00 geschlossen, 01 gekippt,
        # 10 reserviert, 11 geöffnet. Andere DB0-Flags dürfen die
        # Positionserkennung nicht beeinflussen.
        contact = (payload[3] >> 1) & 0x03
        position = {0: 0, 1: 2, 3: 1}.get(contact, 3)
        return {"window_position": position, "supply_voltage": payload[0] * 5.0 / 250.0}
    if eep in ("A5-30-01", "A5-30-02", "A5-30-03") and len(payload) == 4:
        return {"alarm": 1 if any(payload[:3]) else 0, "raw_value": int.from_bytes(payload, "big")}
    return {"raw_value": int.from_bytes(payload, "big")}


def temperature_range(eep):
    kind = int(eep.split("-")[2], 16)
    ranges = {
        **{index: (-40.0 + (index - 1) * 10.0, 0.0 + (index - 1) * 10.0) for index in range(1, 12)},
        **{index: (-60.0 + (index - 0x10) * 10.0, 20.0 + (index - 0x10) * 10.0) for index in range(0x10, 0x1C)},
    }
    return ranges.get(kind, (None, None))


def normalize_eep(value):
    return str(value or "").strip().upper().replace(".", "-").replace(":", "-")


def profile_catalog():
    try:
        records = json.loads(Path(__file__).with_name("profiles.json").read_text(encoding="utf-8"))
        return records if isinstance(records, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def profile_ids():
    return {str(item.get("id", "")) for item in profile_catalog()}


def profile_by_id(profile_id):
    normalized = normalize_eep(profile_id)
    return next((item for item in profile_catalog() if normalize_eep(item.get("id")) == normalized), None)


def profile_variant(profile_id):
    profile = profile_by_id(profile_id)
    return str(profile.get("variant", "")) if profile else ""


def eep_matches_rorg(eep, rorg):
    try:
        return int(str(eep).split("-", 1)[0], 16) == rorg
    except (TypeError, ValueError):
        return False


def parse_eep_overrides(source):
    result = {}
    for line in str(source or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        sender, eep = (part.strip() for part in line.split("=", 1))
        sender = sender.replace(":", "").replace("-", "").upper()
        eep = normalize_eep(eep)
        if len(sender) == 8 and len(eep) == 8:
            result[sender] = eep
    return result


def normalize_sender_id(value):
    sender = str(value or "").replace(":", "").replace("-", "").strip().upper()
    if len(sender) != 8 or any(character not in "0123456789ABCDEF" for character in sender):
        raise ValueError("Die EnOcean-Sender-ID ist ungültig")
    return sender


def node_presentation(eep):
    if eep == "D5-00-01":
        return 2000, "sensor.contact"
    if eep == "F6-10-00":
        return 2001, "window.casement"
    if eep == "A5-14-09":
        return 2001, "window.casement"
    if eep.startswith("F6-02-"):
        return 24, "switch.2"
    if eep.startswith("A5-02-"):
        return 3009, "thermometer.medium"
    if eep.startswith("A5-04-"):
        return 3001, "humidity"
    if eep.startswith("A5-06-"):
        return 1000, "sun.max"
    if eep.startswith("A5-07-") or eep.startswith("A5-08-"):
        return 4015, "figure.walk.motion"
    if eep == "F6-05-02":
        return 4012, "smoke.fill"
    if eep == "F6-05-01" or eep.startswith("A5-30-"):
        return 27, "exclamationmark.triangle"
    return 0, "sensor"


def default_name(eep, sender_id):
    if eep == "D5-00-01":
        kind = "Fensterkontakt"
    elif eep == "F6-10-00":
        kind = "Fenstergriff"
    elif eep == "A5-14-09":
        kind = "Fenstergriff"
    elif eep.startswith("F6-02-"):
        kind = "Wandtaster"
    elif eep.startswith("A5-02-"):
        kind = "Temperatursensor"
    elif eep.startswith("A5-04-"):
        kind = "Klimasensor"
    elif eep.startswith("A5-06-"):
        kind = "Helligkeitssensor"
    elif eep.startswith("A5-07-") or eep.startswith("A5-08-"):
        kind = "Bewegungsmelder"
    elif eep == "F6-05-02":
        kind = "Rauchmelder"
    elif eep == "F6-05-01" or eep.startswith("A5-30-"):
        kind = "Alarmsensor"
    else:
        kind = "Gerät"
    return f"EnOcean {kind} {sender_id[-4:]}"


def link_quality(rssi):
    if rssi is None:
        return 0
    if rssi >= -60:
        return 3
    if rssi >= -75:
        return 2
    if rssi >= -90:
        return 1
    return 0
