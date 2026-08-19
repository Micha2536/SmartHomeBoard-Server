import asyncio
import contextlib
import json
import socket
import time

try:
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:
    padding = Cipher = algorithms = modes = None


DEVICE_TYPE = "10000000"
GATEWAY_PORT = 32100
REPORT_GROUP = "238.0.0.18"
REPORT_PORT = 32101


def manifest():
    return {
        "id": "motionblinds", "name": "MotionBlinds", "version": "1.0.0", "icon": "window.shade.closed",
        "description": (
            "Lokale UDP-Verbindung zum MotionBlinds WLAN-Gateway. Erkennt Rollläden, empfängt Live-Reports "
            "und steuert Position sowie Auf/Ab/Stopp ohne Cloud."
        ),
        "supportsDiscovery": False, "supportsMultipleInstances": False,
        "fields": [
            {"key": "bridge_ip", "type": "text", "title": "Gateway-IP", "required": True,
             "placeholder": "192.168.1.80"},
            {"key": "secret_key", "type": "password", "title": "Secret Key", "required": True},
            {"key": "response_port", "type": "port", "title": "Lokaler Antwort-Port", "default": 32200,
             "minimum": 1024, "maximum": 65535},
            {"key": "poll_seconds", "type": "duration", "title": "Abfrageintervall", "default": 60,
             "minimum": 10, "maximum": 3600, "unit": "s"},
        ],
        "actions": [{"id": "refresh", "title": "Geräte neu einlesen", "icon": "arrow.clockwise"}],
    }


def create(configuration, context):
    return MotionBlindsAdapter(configuration, context)


class DatagramReceiver(asyncio.DatagramProtocol):
    def __init__(self, callback):
        self.callback = callback

    def datagram_received(self, data, address):
        try:
            message = json.loads(data.decode("utf-8"))
            if isinstance(message, dict):
                self.callback(message.get("payload") if isinstance(message.get("payload"), dict) else message)
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass


class MotionBlindsAdapter:
    def __init__(self, configuration, context):
        self.configuration, self.context = configuration, context
        self.transport = None
        self.report_transport = None
        self.task = None
        self.access_token = ""
        self.inbox = []
        self.inbox_event = None
        self.request_lock = None
        self.mac_by_node = {}
        self.details_by_mac = {}
        self.summary_by_mac = {}
        self.refresh_tasks = {}

    async def start(self):
        self._require_configuration()
        self.inbox_event = asyncio.Event()
        self.request_lock = asyncio.Lock()
        await self._open_sockets()
        await self._discover()
        self.task = asyncio.create_task(self._loop())
        await self.context.set_status("Verbunden")

    async def stop(self):
        if self.task:
            self.task.cancel()
        for task in self.refresh_tasks.values():
            task.cancel()
        if self.task:
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        if self.transport:
            self.transport.close()
        if self.report_transport:
            self.report_transport.close()

    async def action(self, action_id, payload):
        if action_id != "refresh":
            raise ValueError("Unbekannte MotionBlinds-Aktion")
        count = await self._discover()
        return {"status": "refreshed", "devices": count}

    async def set_value(self, node_id, attribute_id, value):
        node_id = int(node_id)
        mac = self.mac_by_node.get(node_id)
        if not mac:
            raise ValueError("Das MotionBlinds-Gerät ist nicht mehr bekannt")
        if not self.access_token:
            await self._discover()
        offset = int(attribute_id) - self.context.attribute_id(node_id, 0)
        target = None
        if offset == 2:
            operation = int(round(float(value)))
            data = {"operation": 2 if operation == 2 else (0 if operation == 1 else 1)}
        elif offset == 1:
            target = min(100, max(0, round(float(value))))
            data = {"targetPosition": target}
        else:
            raise ValueError("Dieses MotionBlinds-Attribut ist nicht schreibbar")
        await self._send({"msgType": "WriteDevice", "mac": mac, "deviceType": DEVICE_TYPE,
                          "msgID": self._message_id(), "accessToken": self.access_token, "data": data})
        old = self.refresh_tasks.pop(node_id, None)
        if old:
            old.cancel()
        self.refresh_tasks[node_id] = asyncio.create_task(self._follow_command(node_id, mac, target))

    async def _loop(self):
        while True:
            try:
                await asyncio.sleep(max(10, min(3600, int(float(self.configuration.get("poll_seconds", 60))))))
                for mac in list(self.summary_by_mac):
                    with contextlib.suppress(Exception):
                        await self._read_device(mac)
                await self.context.set_status("Verbunden")
            except asyncio.CancelledError:
                return
            except Exception as error:
                await self.context.set_status("Nicht erreichbar", str(error))

    async def _follow_command(self, node_id, mac, target):
        try:
            for delay in [0.35] + [1.0] * 30:
                await asyncio.sleep(delay)
                reply = await self._read_device(mac)
                data = reply.get("data") if isinstance(reply.get("data"), dict) else reply
                current = _number(data.get("currentPosition", data.get("position")))
                if target is not None and current is not None and abs(current - target) <= 1:
                    break
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        finally:
            self.refresh_tasks.pop(node_id, None)

    async def _discover(self):
        reply = None
        for attempt in range(3):
            try:
                reply = await self._request({"msgType": "GetDeviceList", "msgID": self._message_id()},
                                            lambda msg: msg.get("msgType") == "GetDeviceListAck", 2.5)
                break
            except TimeoutError:
                if attempt < 2:
                    await asyncio.sleep(0.7)
        if reply is None:
            raise TimeoutError("Das MotionBlinds Gateway hat nicht rechtzeitig geantwortet")
        entries = reply.get("data")
        if not isinstance(entries, list):
            raise ValueError("Die MotionBlinds-Geräteliste ist ungültig")
        if reply.get("token"):
            self.access_token = _access_token(str(reply["token"]), str(self.configuration["secret_key"]))
        found = set()
        for summary in entries:
            mac = _string(summary.get("mac")) if isinstance(summary, dict) else None
            if not mac or _string(summary.get("deviceType")) != DEVICE_TYPE:
                continue
            node_id = self.context.stable_node_id(mac.lower())
            found.add(node_id)
            self.mac_by_node[node_id] = mac
            self.summary_by_mac[mac] = summary
            try:
                await self._read_device(mac)
            except Exception:
                self.details_by_mac.setdefault(mac, summary)
                await self._publish(mac)
        for node in list(self.context.nodes()):
            if int(node["id"]) not in found:
                await self.context.remove_node(int(node["id"]))
        return len(found)

    async def _read_device(self, mac):
        if not self.access_token:
            return {}
        reply = await self._request({"msgType": "ReadDevice", "mac": mac, "deviceType": DEVICE_TYPE,
                                    "msgID": self._message_id(), "accessToken": self.access_token},
                                   lambda msg: msg.get("msgType") == "ReadDeviceAck" and _string(msg.get("mac")) == mac, 4)
        await self._apply_message(reply)
        return reply

    async def _open_sockets(self):
        loop = asyncio.get_running_loop()
        port = int(float(self.configuration.get("response_port", 32200)))
        self.transport, _ = await loop.create_datagram_endpoint(
            lambda: DatagramReceiver(self._received), local_addr=("0.0.0.0", port), reuse_port=True
        )
        try:
            report_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            report_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            report_socket.bind(("", REPORT_PORT))
            report_socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                     socket.inet_aton(REPORT_GROUP) + socket.inet_aton("0.0.0.0"))
            report_socket.setblocking(False)
            self.report_transport, _ = await loop.create_datagram_endpoint(
                lambda: DatagramReceiver(self._received), sock=report_socket
            )
        except OSError:
            if 'report_socket' in locals():
                report_socket.close()

    def _received(self, message):
        message_type = message.get("msgType")
        if message_type in ("GetDeviceListAck", "ReadDeviceAck"):
            self.inbox.append(message)
            if len(self.inbox) > 100:
                del self.inbox[:-100]
            self.inbox_event.set()
        if message_type in ("WriteDeviceAck", "Report"):
            asyncio.create_task(self._apply_message(message))

    async def _apply_message(self, message):
        mac = _string(message.get("mac"))
        if not mac or mac not in self.summary_by_mac:
            return
        incoming = message.get("data") if isinstance(message.get("data"), dict) else {}
        current = dict(self.details_by_mac.get(mac, {}))
        current_data = current.get("data") if isinstance(current.get("data"), dict) else {}
        current_data = dict(current_data)
        if message.get("msgType") == "WriteDeviceAck":
            incoming = {key: value for key, value in incoming.items() if key != "targetPosition"}
        current_data.update(incoming)
        current.update(message)
        current["data"] = current_data
        self.details_by_mac[mac] = current
        await self._publish(mac)

    async def _publish(self, mac):
        summary = self.summary_by_mac.get(mac, {})
        details = self.details_by_mac.get(mac, summary)
        data = details.get("data") if isinstance(details.get("data"), dict) else details
        node_id = self.context.stable_node_id(mac.lower())
        self.mac_by_node[node_id] = mac
        position = _number(data.get("currentPosition", data.get("position", data.get("targetPosition"))), 0)
        raw_battery = _number(data.get("batteryLevel", data.get("battery", summary.get("batteryLevel"))))
        name = _string(data.get("name") or summary.get("name") or summary.get("deviceName")) or f"MotionBlinds {mac[-5:]}"
        now, base = time.time(), self.context.attribute_id(node_id, 0)
        attributes = [
            {"id": base + 1, "node_id": node_id, "type": 15, "instance": 1, "name": "Position", "unit": "%",
             "current_value": position, "target_value": position, "editable": True, "minimum": 0,
             "maximum": 100, "step_value": 1, "last_changed": now},
            {"id": base + 2, "node_id": node_id, "type": 135, "instance": 1, "name": "Richtung", "unit": "",
             "current_value": 2, "target_value": 2, "editable": True, "minimum": 0,
             "maximum": 2, "step_value": 1, "last_changed": now},
        ]
        if raw_battery is not None:
            voltage = round(raw_battery) / 100 if raw_battery > 100 else round(raw_battery * 100) / 100
            attributes.append({"id": base + 3, "node_id": node_id, "type": 195, "instance": 1,
                               "name": "Batteriespannung", "unit": "V", "current_value": voltage,
                               "editable": False, "minimum": 0, "maximum": 16, "step_value": 0.01,
                               "last_changed": now})
        await self.context.publish_node({"id": node_id, "integration_source": "server", "name": name,
            "note": f"Server · MotionBlinds · {mac}", "state": 1, "profile": 2004, "protocol": 20,
            "image": "nodeicon_shutter", "state_changed": now, "attributes": attributes})

    async def _request(self, packet, predicate, timeout):
        async with self.request_lock:
            await self._send(packet)
            deadline = asyncio.get_running_loop().time() + timeout
            expected_id = _string(packet.get("msgID"))
            while True:
                for index, message in enumerate(self.inbox):
                    received_id = _string(message.get("msgID"))
                    if predicate(message) and (not expected_id or not received_id or received_id == expected_id):
                        return self.inbox.pop(index)
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError("MotionBlinds-Zeitüberschreitung")
                self.inbox_event.clear()
                try:
                    await asyncio.wait_for(self.inbox_event.wait(), remaining)
                except asyncio.TimeoutError as error:
                    raise TimeoutError("MotionBlinds-Zeitüberschreitung") from error

    async def _send(self, packet):
        host = str(self.configuration.get("bridge_ip", "")).strip()
        self.transport.sendto(json.dumps(packet, separators=(",", ":")).encode(), (host, GATEWAY_PORT))

    def _message_id(self):
        return str(int(time.time() * 1000))

    def _require_configuration(self):
        if not str(self.configuration.get("bridge_ip", "")).strip() or not str(self.configuration.get("secret_key", "")):
            raise ValueError("MotionBlinds Gateway-IP oder Secret Key fehlt")
        if Cipher is None:
            raise RuntimeError("Das Python-Paket cryptography ist nicht installiert")


def _access_token(token, secret_key):
    if Cipher is None:
        raise RuntimeError("Das Python-Paket cryptography ist nicht installiert")
    key = secret_key.encode()[:16].ljust(16, b"\0")
    padder = padding.PKCS7(128).padder()
    padded = padder.update(token.encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return (encryptor.update(padded) + encryptor.finalize()).hex().upper()[:32]


def _string(value):
    return str(value) if value is not None else None


def _number(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
