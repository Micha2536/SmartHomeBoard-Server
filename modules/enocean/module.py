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
PACKET_RESPONSE = 0x02
PACKET_COMMON_COMMAND = 0x05
COMMON_COMMAND_READ_ID_BASE = 0x08
RORG_RPS = 0xF6
RORG_1BS = 0xD5
RORG_4BS = 0xA5


def manifest():
    return {
        "id": "enocean",
        "name": "EnOcean USB300",
        "version": "1.7.0",
        "icon": "antenna.radiowaves.left.and.right",
        "description": (
            "Lokales EnOcean-Backend über ESP3. Lernt Sender dauerhaft an und "
            "bildet Kontakte, Fenstergriffe, Taster, Sensoren und unterstützte "
            "bidirektionale Eltako-Rollladenaktoren auf Dashboard-Geräte ab."
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
                "title": "Standardprofil unbekannter F6-Empfangstelegramme (kein Anlernen)",
                "default": "F6-02-01",
                "options": [
                    {"value": "F6-02-01", "label": "Wandtaster · F6-02-01"},
                    {"value": "F6-02-02", "label": "Wandtaster invertiert · F6-02-02"},
                    {"value": "F6-10-00", "label": "Fenstergriff · F6-10-00"},
                ],
                "help": "Nur für unbekannte F6-Telegramme. Die vollständige, durchsuchbare EEP-Auswahl befindet sich unter EnOcean verwalten.",
            },
            {
                "key": "roller_runtime_seconds", "type": "integer",
                "title": "Rollladen-Laufzeit", "default": 60, "minimum": 1, "maximum": 255,
                "help": "Volle Fahrzeit in Sekunden für A5-3F-7F-Aktoren wie den Eltako FJ62NP-230V.",
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
        "actions": [],
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
        self.base_id = None
        self.write_lock = None
        self.response_waiter = None
        self.actor_confirmation_waiters = {}
        self.reader_task = None
        self.learning_task = None
        self.learning_until = 0.0
        self.learning_eep = ""
        self.learning_profile_id = ""
        self.learning_variant = ""
        self.learning_name = ""
        self.learning_tx_offset = 0
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
            self.base_id = await asyncio.to_thread(self._read_base_id)
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
        for waiter in self.actor_confirmation_waiters.values():
            if not waiter.done():
                waiter.cancel()
        self.actor_confirmation_waiters.clear()

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
            control_mode = str(profile.get("control_mode", ""))
            teach_payload = str(profile.get("teach_payload", "")).replace(" ", "")
            if teach_payload or control_mode == "rps_direction":
                self.learning_tx_offset = self._next_tx_offset()
            self._save_state(
                learning_until=time.time() + seconds,
                learning_eep=eep,
                learning_profile_id=profile_id,
            )
            if self.learning_task:
                self.learning_task.cancel()
            self.learning_task = asyncio.create_task(self._finish_learning_after(seconds))
            if control_mode == "rps_direction":
                try:
                    await self._teach_rps_direction_pushbutton(profile)
                except Exception:
                    await self._stop_learning()
                    raise
                sender_id = self._sender_id_for_offset(self.learning_tx_offset)
                device = {
                    "eep": eep,
                    "profile_id": profile_id,
                    "variant": "",
                    "control_mode": control_mode,
                    "rocker_pair": str(profile.get("rocker_pair", "A")).strip().upper(),
                    "tx_offset": self.learning_tx_offset,
                    "name": self.learning_name or "Eltako Rollladen",
                    "manufacturer": 0x00D,
                    "rssi": None,
                    "raw": "00",
                    "values": {"shutter_command": 2, "confirmation": "Virtueller Richtungstaster"},
                    "last_seen": time.time(),
                }
                self.devices[sender_id] = device
                self._save_state()
                await self._publish(sender_id, device)
                await self._stop_learning()
                await self.context.set_status(f"Richtungstaster angelernt · {sender_id}")
            elif teach_payload:
                try:
                    await self._send_4bs(bytes.fromhex(teach_payload), sender_offset=self.learning_tx_offset)
                except Exception:
                    await self._stop_learning()
                    raise
            if time.monotonic() < self.learning_until:
                await self.context.set_status(f"Anlernmodus · {eep} · noch {seconds} s")
            return self._management_state()
        if action_id == "stop_learning":
            await self._stop_learning()
            return self._management_state()
        if action_id == "test_device":
            sender_id = normalize_sender_id(payload.get("sender_id"))
            device = self.devices.get(sender_id)
            if not device:
                raise KeyError("EnOcean-Gerät wurde nicht gefunden")
            command_value = int(payload.get("command", 2))
            node_id = self.context.stable_node_id(sender_id)
            await self.set_value(node_id, self.context.attribute_id(node_id, 2), command_value)
            return {**self._management_state(), "message": "Tastertelegramm wurde vom USB300 bestätigt."}
        if action_id == "teach_device":
            sender_id = normalize_sender_id(payload.get("sender_id"))
            device = self.devices.get(sender_id)
            if not device or str(device.get("control_mode", "")) != "rps_direction":
                raise ValueError("Nur ein virtueller EnOcean-Richtungstaster kann erneut angelernt werden")
            rocker_pair = str(payload.get("rocker_pair", "A")).strip().upper()
            if rocker_pair not in {"A", "B"}:
                raise ValueError("Die EnOcean-Wippe muss A oder B sein")
            await self._teach_rps_direction_pushbutton(
                {"teach_repetitions": 4, "rocker_pair": rocker_pair},
                sender_offset=int(device.get("tx_offset", 0) or 0),
            )
            device["rocker_pair"] = rocker_pair
            device["last_seen"] = time.time()
            self.devices[sender_id] = device
            self._save_state()
            await self._publish(sender_id, device)
            return {
                **self._management_state(),
                "message": f"Vier Tastendrücke der EnOcean-Wippe {rocker_pair} wurden gesendet.",
            }
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
            device["control_mode"] = str(profile.get("control_mode", ""))
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
        match = next(
            ((sender_id, device) for sender_id, device in self.devices.items()
             if self.context.stable_node_id(sender_id) == int(node_id)),
            None,
        )
        if not match:
            raise ValueError("Das EnOcean-Gerät wurde nicht gefunden")
        sender_id, device = match
        if normalize_eep(device.get("eep")) != "A5-3F-7F":
            raise ValueError("Dieses EnOcean-Gerät unterstützt noch keine Serversteuerung")
        direction_id = self.context.attribute_id(int(node_id), 2)
        if int(attribute_id) != direction_id:
            raise ValueError("Dieses EnOcean-Attribut ist nicht schreibbar")
        command_value = int(round(float(value)))
        command = {0: 0x01, 1: 0x02, 2: 0x00}.get(command_value)
        if command is None:
            raise ValueError("Rollladenbefehl muss Öffnen, Schließen oder Stop sein")
        runtime = 0 if command == 0 else max(1, min(255, int(float(
            self.configuration.get("roller_runtime_seconds", 60)
        ))))
        sender_offset = int(device.get("tx_offset", 0) or 0)
        if str(device.get("control_mode", "")) == "rps_direction":
            up_rocker, down_rocker = self._rps_direction_payloads(device)
            if command == 0:
                current = int(dict(device.get("values", {})).get("shutter_command", 2) or 2)
                rocker = down_rocker if current == 4 else up_rocker
            else:
                rocker = up_rocker if command == 0x01 else down_rocker
            await self._send_rps_click(rocker, sender_offset=sender_offset)
        else:
            await self._send_4bs(
                bytes([0x00, runtime, command, 0x08]),
                sender_offset=sender_offset,
            )
        values = dict(device.get("values", {}))
        values.update({"shutter_command": {0: 3, 1: 4, 2: 2}[command_value], "runtime_seconds": runtime})
        device["values"] = values
        device["last_seen"] = time.time()
        self.devices[sender_id] = device
        self._save_state()
        await self._publish(sender_id, device)

    def _read_base_id(self):
        """Read the USB300 base ID before the background reader owns the port."""
        self.serial.write(encode_esp3_packet(PACKET_COMMON_COMMAND, bytes([COMMON_COMMAND_READ_ID_BASE])))
        parser = ESP3StreamParser()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            for packet_type, data, _optional in parser.feed(self.serial.read(512)):
                if packet_type == PACKET_RESPONSE and len(data) >= 5 and data[0] == 0:
                    return int.from_bytes(data[1:5], "big")
        raise TimeoutError("Die EnOcean-Basis-ID des USB300 konnte nicht gelesen werden")

    def _next_tx_offset(self):
        used = {
            int(device.get("tx_offset")) for device in self.devices.values()
            if device.get("tx_offset") is not None and normalize_eep(device.get("eep")) == "A5-3F-7F"
        }
        offset = next((value for value in range(128) if value not in used), None)
        if offset is None:
            raise RuntimeError("Der USB300-Sender-ID-Bereich für EnOcean-Aktoren ist vollständig belegt")
        return offset

    def _sender_id_for_offset(self, sender_offset):
        if self.base_id is None:
            raise ConnectionError("Der EnOcean USB300 ist nicht sendebereit")
        return f"{(int(self.base_id) + int(sender_offset)) & 0xFFFFFFFF:08X}"

    async def _teach_rps_direction_pushbutton(self, profile, sender_offset=None):
        repetitions = max(1, min(8, int(profile.get("teach_repetitions", 4) or 4)))
        rocker_pair = str(profile.get("rocker_pair", "A")).strip().upper()
        up_rocker = 0x30 if rocker_pair != "B" else 0x70
        if sender_offset is None:
            sender_offset = self.learning_tx_offset
        for index in range(repetitions):
            await self._send_rps_click(up_rocker, sender_offset=sender_offset)
            if index + 1 < repetitions:
                await asyncio.sleep(0.30)

    @staticmethod
    def _rps_direction_payloads(device):
        """Return up/down RPS payloads for the rocker pair used at teach-in."""
        pair = str(device.get("rocker_pair", "A")).strip().upper()
        return (0x70, 0x50) if pair == "B" else (0x30, 0x10)

    async def _send_rps_click(self, rocker, sender_offset):
        await self._send_rps(rocker, sender_offset=sender_offset, status=0x30)
        await asyncio.sleep(0.12)
        await self._send_rps(0x00, sender_offset=sender_offset, status=0x20)

    async def _send_rps(self, payload, destination=0xFFFFFFFF, sender_offset=None, status=0x30):
        if not self.serial or self.base_id is None:
            raise ConnectionError("Der EnOcean USB300 ist nicht sendebereit")
        if sender_offset is None:
            sender_offset = self.configuration.get("sender_id_offset", 0)
        offset = max(0, min(127, int(float(sender_offset))))
        sender_id = (int(self.base_id) + offset) & 0xFFFFFFFF
        data = bytes([RORG_RPS, int(payload) & 0xFF]) + sender_id.to_bytes(4, "big") + bytes([int(status) & 0xFF])
        optional = bytes([0x03]) + int(destination).to_bytes(4, "big") + bytes([0xFF, 0x00])
        packet = encode_esp3_packet(PACKET_RADIO_ERP1, data, optional)
        await self._write_radio_packet(packet, "EnOcean-Tastertelegramm")

    async def _send_4bs(self, payload, destination=0xFFFFFFFF, sender_offset=None):
        if not self.serial or self.base_id is None:
            raise ConnectionError("Der EnOcean USB300 ist nicht sendebereit")
        if len(payload) != 4:
            raise ValueError("Ein 4BS-Telegramm benötigt genau vier Datenbytes")
        if sender_offset is None:
            sender_offset = self.configuration.get("sender_id_offset", 0)
        if sender_offset is None:
            raise RuntimeError("Für diesen EnOcean-Aktor ist keine freie USB300-Sender-ID mehr verfügbar")
        offset = max(0, min(127, int(float(sender_offset))))
        sender_id = (int(self.base_id) + offset) & 0xFFFFFFFF
        data = bytes([RORG_4BS]) + bytes(payload) + sender_id.to_bytes(4, "big") + bytes([0x00])
        optional = bytes([0x03]) + int(destination).to_bytes(4, "big") + bytes([0xFF, 0x00])
        packet = encode_esp3_packet(PACKET_RADIO_ERP1, data, optional)
        await self._write_radio_packet(packet, "EnOcean-Telegramm")

    async def _write_radio_packet(self, packet, label):
        if self.write_lock is None:
            self.write_lock = asyncio.Lock()
        async with self.write_lock:
            loop = asyncio.get_running_loop()
            wait_for_response = bool(self.reader_task and not self.reader_task.done())
            waiter = loop.create_future() if wait_for_response else None
            self.response_waiter = waiter
            try:
                written = await asyncio.to_thread(self.serial.write, packet)
                if written != len(packet):
                    raise ConnectionError(f"Das {label} wurde nicht vollständig an den USB300 übertragen")
                if waiter is not None:
                    try:
                        response = await asyncio.wait_for(waiter, timeout=1.25)
                    except asyncio.TimeoutError as error:
                        raise ConnectionError(f"Der USB300 hat das {label} nicht bestätigt") from error
                    return_code = response[0] if response else 0xFF
                    if return_code != 0:
                        messages = {1: "Fehler", 2: "nicht unterstützt", 3: "ungültige Parameter", 4: "nicht erlaubt"}
                        raise ConnectionError(
                            f"Der USB300 hat das {label} abgelehnt: {messages.get(return_code, f'Code {return_code}') }"
                        )
            finally:
                if self.response_waiter is waiter:
                    self.response_waiter = None

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
        self.learning_tx_offset = 0
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
                    elif packet_type == PACKET_RESPONSE and self.response_waiter and not self.response_waiter.done():
                        self.response_waiter.set_result(bytes(data))
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
        if is_new_learning_device and not learning_matches_telegram(self.learning_eep, telegram):
            return

        eep = known.get("eep") if known else self.learning_eep or self.eep_overrides.get(sender_id)
        if not eep:
            eep = detect_eep(telegram, str(self.configuration.get("default_f6_eep", "F6-02-01")))
        if not known and not eep:
            # A 4BS data telegram without teach-in information cannot be decoded
            # safely. Keep it visible so the user can add an override.
            eep = f"{telegram['rorg']:02X}-00-00"

        if eep == "A5-3F-7F" and telegram["rorg"] == RORG_RPS:
            decoded = decode_fj62_confirmation(telegram["user_data"])
        elif is_teach_in(telegram["rorg"], telegram["user_data"]):
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
            "tx_offset": device.get("tx_offset", self.learning_tx_offset if is_new_learning_device and eep == "A5-3F-7F" else None),
            "name": device.get("name") or (self.learning_name if is_new_learning_device else "") or default_name(eep, sender_id),
            "manufacturer": telegram.get("manufacturer"),
            "rssi": telegram.get("rssi"),
            "raw": telegram["user_data"].hex().upper(),
            "values": decoded,
            "last_seen": time.time(),
        })
        if eep == "A5-3F-7F" and telegram["rorg"] == RORG_RPS:
            device["bidirectional_verified"] = True
            device["confirmation_rssi"] = telegram.get("rssi")
            device["confirmation_at"] = time.time()
        self.devices[sender_id] = device
        self._save_state()
        await self._publish(sender_id, device)
        confirmation_waiter = self.actor_confirmation_waiters.pop(sender_id, None)
        if confirmation_waiter and not confirmation_waiter.done():
            confirmation_waiter.set_result(dict(telegram))
        if is_new_learning_device:
            # Genau ein bislang unbekannter Sender pro Lernlauf. Das Stoppen
            # geschieht direkt nach dem persistenten Speichern der Sender-ID.
            await self._stop_learning()
            if eep == "A5-3F-7F" and self.reader_task and not self.reader_task.done():
                asyncio.create_task(self._verify_fj62_bidirectional(sender_id))
            else:
                await self.context.set_status(f"Angelernt · {sender_id} · {eep}")

    async def _verify_fj62_bidirectional(self, sender_id):
        """Request a second actuator confirmation after successful GFVS teach-in."""
        device = self.devices.get(sender_id)
        if not device:
            return
        loop = asyncio.get_running_loop()
        waiter = loop.create_future()
        previous = self.actor_confirmation_waiters.pop(sender_id, None)
        if previous and not previous.done():
            previous.cancel()
        self.actor_confirmation_waiters[sender_id] = waiter
        try:
            # Eltako documents 00000008 as "request confirmation telegram".
            await self._send_4bs(
                bytes.fromhex("00000008"),
                sender_offset=int(device.get("tx_offset", 0) or 0),
            )
            telegram = await asyncio.wait_for(waiter, timeout=5.0)
            current = self.devices.get(sender_id)
            if current:
                current["bidirectional_verified"] = True
                current["confirmation_rssi"] = telegram.get("rssi")
                current["confirmation_at"] = time.time()
                self.devices[sender_id] = current
                self._save_state()
                await self._publish(sender_id, current)
            await self.context.set_status(f"Bidirektional bestätigt · {sender_id}")
        except (asyncio.TimeoutError, ConnectionError) as error:
            log.warning("FJ62 %s antwortete nicht auf die Bestätigungsabfrage: %s", sender_id, error)
            await self.context.set_status(
                f"FJ62 angelernt · Statusabfrage ohne Antwort · {sender_id}",
                "Funkreichweite und aktivierte Bestätigungstelegramme prüfen",
            )
        finally:
            if self.actor_confirmation_waiters.get(sender_id) is waiter:
                self.actor_confirmation_waiters.pop(sender_id, None)

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
        if eep == "A5-3F-7F":
            direction = int(values.get("shutter_command", 2))
            add(2, 135, "Richtung", direction, data={2: "Gestoppt", 3: "Öffnet", 4: "Schließt"}.get(direction, "Unbekannt"))
            attributes[-1].update({"editable": True, "target_value": 2, "minimum": 0, "maximum": 2, "step_value": 1})
            if values.get("runtime_seconds") is not None:
                add(3, 104, "Letzte Fahrzeit", float(values["runtime_seconds"]), "s")
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


def encode_esp3_packet(packet_type, data, optional=b""):
    data, optional = bytes(data), bytes(optional)
    header = len(data).to_bytes(2, "big") + bytes([len(optional), int(packet_type)])
    payload = data + optional
    return b"\x55" + header + bytes([crc8(header)]) + payload + bytes([crc8(payload)])


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
    if eep == "A5-3F-7F" and len(payload) == 4:
        runtime = ((payload[0] << 8) | payload[1]) / 10.0 if payload[3] & 0x02 else float(payload[1])
        command = payload[2]
        return {
            "shutter_command": {0x00: 2, 0x01: 3, 0x02: 4}.get(command, 2),
            "runtime_seconds": runtime,
            "locked": 1 if payload[3] & 0x04 else 0,
        }
    if eep in ("A5-30-01", "A5-30-02", "A5-30-03") and len(payload) == 4:
        return {"alarm": 1 if any(payload[:3]) else 0, "raw_value": int.from_bytes(payload, "big")}
    return {"raw_value": int.from_bytes(payload, "big")}


def decode_fj62_confirmation(payload):
    """Decode the RPS confirmation emitted by FJ62 actuators after GFVS teach-in."""
    value = payload[0] if payload else 0
    if value in (0x70, 0x30):
        return {"shutter_command": 3, "confirmation": "Auf"}
    if value in (0x50, 0x10):
        return {"shutter_command": 4, "confirmation": "Ab"}
    return {"shutter_command": 2, "confirmation": "Stopp", "raw_value": value}


def learning_matches_telegram(eep, telegram):
    normalized = normalize_eep(eep)
    if normalized == "A5-3F-7F":
        return telegram.get("rorg") == RORG_RPS and bytes(telegram.get("user_data", b""))[:1] in (
            b"\x70", b"\x30", b"\x50", b"\x10"
        )
    return eep_matches_rorg(normalized, telegram.get("rorg"))


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
        return [_profile_with_instructions(item) for item in records] if isinstance(records, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _profile_with_instructions(profile):
    item = dict(profile) if isinstance(profile, dict) else {}
    configured = item.get("instructions")
    if isinstance(configured, list) and configured:
        item["instructions"] = [str(step).strip() for step in configured if str(step).strip()]
        return item
    eep = normalize_eep(item.get("eep") or item.get("id"))
    support = str(item.get("support", "catalog"))
    steps = [f"Im SHB das Profil {item.get('id', eep)} auswählen und einen eindeutigen Gerätenamen eintragen."]
    if eep.startswith("F6-"):
        steps.extend([
            "Anlernen starten und anschließend die gewünschte Taste beziehungsweise Wippe einmal vollständig drücken und loslassen.",
            "Prüfen, ob Sender-ID und Empfangsstärke erscheinen und die betätigte Taste im Livezustand wechselt.",
        ])
    elif eep == "D5-00-01":
        steps.extend([
            "Anlernen starten und den Fenster-/Türkontakt einmal öffnen und wieder schließen; falls vorhanden alternativ die Lerntaste kurz betätigen.",
            "Prüfen, ob der SHB-Zustand anschließend zwischen Offen und Geschlossen wechselt.",
        ])
    elif eep.startswith("A5-"):
        steps.extend([
            "Anlernen starten und am Gerät das Teach-in-Telegramm auslösen – üblicherweise über die Lerntaste oder den in der Geräteanleitung beschriebenen Vorgang.",
            "Nach erfolgreichem Empfang die angelegten Werte einmal durch eine Zustandsänderung kontrollieren.",
        ])
    else:
        steps.extend([
            "Anlernen starten und am Gerät den vom Hersteller beschriebenen Einlernvorgang auslösen.",
            "Nach dem ersten Telegramm Sender-ID, Profil und Werte kontrollieren.",
        ])
    if support != "decoded":
        steps.append("Dieses Profil ist noch nicht vollständig dekodiert; nach dem Anlernen deshalb die angezeigten Rohdaten prüfen.")
    item["instructions"] = steps
    return item


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
    if eep == "A5-3F-7F":
        return 2004, "nodeicon_shutter"
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
    elif eep == "A5-3F-7F":
        kind = "Rollladenaktor"
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
