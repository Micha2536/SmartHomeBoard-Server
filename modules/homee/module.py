import asyncio
import contextlib
import hashlib
import json
import logging
import re
import time
from collections import deque
from urllib.parse import parse_qs, quote

import httpx
import websockets


log = logging.getLogger("smarthomeboard.homee")


def manifest():
    return {
        "id": "homee",
        "name": "homee",
        "version": "1.6.0",
        "icon": "house.lodge",
        "description": "Dauerhafte lokale homee-WebSocket-Verbindung. Geräte und Werte werden auf dem Server gespeichert und an alle Apps verteilt.",
        "supportsDiscovery": False,
        "supportsMultipleInstances": True,
        "fields": [
            {"key": "host", "type": "text", "title": "IP-Adresse oder Hostname", "placeholder": "192.168.1.10", "required": True},
            {"key": "port", "type": "port", "title": "Port", "default": 7681, "minimum": 1, "maximum": 65535},
            {"key": "username", "type": "text", "title": "Benutzername", "required": True,
             "help": "Empfohlen: In homee einen eigenen Benutzer nur für SmartHomeBoard Server anlegen. So bleiben offizielle homee-App-Sitzungen vollständig getrennt."},
            {"key": "password", "type": "password", "title": "Passwort", "required": True},
        ],
    }


def create(configuration, context):
    return HomeeAdapter(configuration, context)


class HomeeAdapter:
    def __init__(self, configuration, context):
        self.configuration = configuration
        self.context = context
        self.socket = None
        self.task = None
        self.nodes = {int(node["id"]): node for node in context.nodes() if "id" in node}
        persisted = context.load_state({}) or {}
        self.resources = dict(persisted.get("resources", {})) if isinstance(persisted, dict) else {}
        self.client_id = str(persisted.get("client_id", "") if isinstance(persisted, dict) else "") or _client_id(context.integration_id)
        self.stopping = False
        self.connected_at = 0.0
        # In Python 3.9 muss das Lock innerhalb eines laufenden Event-Loops
        # erzeugt werden; deshalb erfolgt die Initialisierung beim ersten Login.
        self.connect_lock = None
        self.protocol_messages = deque(maxlen=100)
        self.last_all_request_at = 0.0
        self.history_requests = {}

    async def start(self):
        self.stopping = False
        await self._connect()
        self.task = asyncio.create_task(self._receive_forever())

    async def stop(self):
        self.stopping = True
        self._fail_history_requests(ConnectionError("homee-Verbindung wurde beendet"))
        if self.task:
            self.task.cancel()
        if self.socket:
            with contextlib.suppress(Exception):
                await self.socket.close()
        if self.task:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
        self.socket = None

    async def set_value(self, node_id, attribute_id, value):
        node = self.nodes.get(int(node_id))
        if not node or not any(int(item.get("id", -1)) == int(attribute_id) for item in node.get("attributes", [])):
            raise KeyError("Unbekanntes homee-Attribut")
        if not self.socket:
            raise ConnectionError("homee ist nicht verbunden")
        command = f"put:nodes/{int(node_id)}/attributes/{int(attribute_id)}?target_value={quote(_number_text(value))}"
        await self.socket.send(command)
        self._record_protocol("out", command)

    async def health_check(self):
        """Prüfe die vorhandene Sitzung, ohne einen zweiten homee-Login zu öffnen."""
        if not self.socket:
            raise ConnectionError("homee ist nicht verbunden")
        await self.socket.send("GET:nodes")
        self._record_protocol("out", "GET:nodes")

    async def attribute_history(self, node_id, attribute_id, from_timestamp, till_timestamp):
        node_id = int(node_id)
        attribute_id = int(attribute_id)
        from_timestamp = int(from_timestamp)
        till_timestamp = int(till_timestamp)
        node = self.nodes.get(node_id)
        if not node or not any(int(item.get("id", -1)) == attribute_id for item in node.get("attributes", [])):
            raise KeyError("Unbekanntes homee-Attribut")
        if not self.socket:
            raise ConnectionError("homee ist nicht verbunden")

        key = (node_id, attribute_id, from_timestamp, till_timestamp)
        if key in self.history_requests:
            return await asyncio.shield(self.history_requests[key])

        future = asyncio.get_running_loop().create_future()
        self.history_requests[key] = future
        command = (
            f"GET:nodes/{node_id}/attributes/{attribute_id}/history"
            f"?from={from_timestamp}&till={till_timestamp}"
        )
        try:
            await self.socket.send(command)
            self._record_protocol("out", command)
            return await asyncio.wait_for(asyncio.shield(future), timeout=12)
        finally:
            if self.history_requests.get(key) is future:
                self.history_requests.pop(key, None)

    async def action(self, action_id, payload):
        if action_id == "send_websocket":
            if not self.socket:
                raise ConnectionError("homee ist nicht verbunden")
            command = str(payload.get("command", "")).strip()
            if not command or len(command) > 2048:
                raise ValueError("Die WebSocket-Nachricht muss zwischen 1 und 2048 Zeichen lang sein")
            if "\n" in command or "\r" in command or not re.match(r"^(GET|PUT|POST|DELETE):", command, re.IGNORECASE):
                raise ValueError("Erlaubt sind einzelne homee-Befehle mit GET:, PUT:, POST: oder DELETE:")
            if command.upper() == "GET:ALL":
                if not await self._request_all():
                    raise ValueError("GET:all wurde bereits gesendet und ist zum Schutz des homee eine Minute gesperrt")
                return {"sent": command}
            await self.socket.send(command)
            self._record_protocol("out", command)
            return {"sent": command}
        if action_id == "protocol_log":
            category = str(payload.get("category", "")).strip().lower()
            try:
                limit = max(1, min(100, int(payload.get("limit", 50))))
            except (TypeError, ValueError):
                limit = 50
            messages = list(self.protocol_messages)
            if category and category != "all_messages":
                messages = [item for item in messages if item["category"] == category]
            return {"messages": messages[-limit:], "categories": list(_PROTOCOL_CATEGORIES)}
        raise ValueError("Unbekannte homee-Aktion")

    async def _connect(self):
        # Single-Flight: Parallele Auslöser warten hier. Sobald einer die
        # Verbindung hergestellt hat, führen alle nachfolgenden Aufrufer keinen
        # weiteren Login und keinen zweiten WebSocket-Handshake aus.
        if self.connect_lock is None:
            self.connect_lock = asyncio.Lock()
        async with self.connect_lock:
            if self.stopping or self.socket is not None:
                return False
            try:
                await self._connect_once()
                return True
            except Exception:
                await self._drop_socket()
                raise

    async def _connect_once(self):
        host = str(self.configuration.get("host", "")).strip()
        username = str(self.configuration.get("username", "")).strip()
        password = str(self.configuration.get("password", ""))
        if not host or not username or not password:
            raise ValueError("Host, Benutzername und Passwort sind erforderlich")
        port = int(float(self.configuration.get("port", 7681)))
        credentials_fingerprint = hashlib.sha256(
            f"{host}:{port}:{username}:{password}".encode("utf-8")
        ).hexdigest()
        if self.context.load_secret("credentials_fingerprint", "") != credentials_fingerprint:
            self.context.save_secret("access_token", "")
            self.context.save_secret("credentials_fingerprint", credentials_fingerprint)
        token = str(self.context.load_secret("access_token", "") or "")
        if token:
            try:
                await self._open_socket(host, port, token)
                await self.context.set_status("Verbunden")
                return
            except Exception:
                self.context.save_secret("access_token", "")
                if self.socket:
                    with contextlib.suppress(Exception):
                        await self.socket.close()
                self.socket = None

        password_hash = hashlib.sha512(password.encode("utf-8")).hexdigest()
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.post(
                f"http://{host}:{port}/access_token",
                auth=(username, password_hash),
                data={
                    "device_name": f"SmartHomeBoard Server {self.client_id[-8:]}",
                    "device_hardware_id": self.client_id,
                    "device_os": 5,
                    "device_type": 4,
                    "device_app": 0,
                },
            )
            response.raise_for_status()
        values = parse_qs(response.text.strip(), keep_blank_values=True)
        token = values.get("access_token", [""])[0]
        if not token:
            raise ValueError("homee hat keinen Access Token geliefert")
        self.context.save_secret("access_token", token)
        await self._open_socket(host, port, token)
        await self.context.set_status("Verbunden")

    async def _drop_socket(self):
        socket, self.socket = self.socket, None
        self.connected_at = 0.0
        self._fail_history_requests(ConnectionError("homee-Verbindung wurde unterbrochen"))
        if socket:
            with contextlib.suppress(Exception):
                await socket.close()

    async def _open_socket(self, host, port, token):
        self.socket = await websockets.connect(
            f"ws://{host}:{port}/connection?access_token={quote(token)}",
            subprotocols=["v2"],
            open_timeout=10,
            # Umfangreiche GET:all-Antworten überschreiten bei vielen Geräten
            # das websockets-Standardlimit von 1 MiB. Ein Close 1009 würde sonst
            # einen Reconnect mit erneutem GET:all auslösen.
            max_size=32 * 1024 * 1024,
            # Die funktionierende iOS-Verbindung versendet ebenfalls keine
            # zusätzlichen RFC-WebSocket-Pings. homee liefert seine laufenden
            # Ereignisse über das Anwendungsprotokoll.
            ping_interval=None,
        )
        self.connected_at = time.monotonic()
        if not await self._request_all(force=True):
            await self.socket.send("GET:nodes")
            self._record_protocol("out", "GET:nodes")

    async def _request_all(self, force=False):
        """Fordere den großen Snapshot höchstens einmal pro Minute an.

        Der initiale Aufruf nach einem echten Socket-Neuaufbau darf die Sperre
        bewusst umgehen. Daten- oder Reihenfolgeprobleme dürfen dagegen niemals
        eine GET:all-Schleife auslösen, die den homee überlastet.
        """
        if not self.socket:
            return False
        now = time.monotonic()
        # Auch ein neuer Socket darf den großen Abruf nicht in schneller Folge
        # wiederholen. `force` kennzeichnet nur den initialen Socket-Aufbau; es
        # umgeht niemals eine bereits laufende Schutzfrist.
        if self.last_all_request_at and now - self.last_all_request_at < 60:
            return False
        self.last_all_request_at = now
        await self.socket.send("GET:all")
        self._record_protocol("out", "GET:all")
        return True

    async def _receive_forever(self):
        delay = 2
        while not self.stopping:
            try:
                async for message in self.socket:
                    try:
                        await self._handle_message(message)
                    except asyncio.CancelledError:
                        raise
                    except Exception as processing_error:
                        # Eine fehlerhafte Nutzlast ist kein Verbindungsbruch.
                        # Insbesondere darf sie keinen Login + GET:all auslösen.
                        log.exception("homee-WebSocket-Nachricht konnte nicht verarbeitet werden")
                        self._record_protocol(
                            "in",
                            json.dumps({"error": {"message": str(processing_error), "source": "message_processing"}}),
                            {"error": {"message": str(processing_error)}},
                        )
                if not self.stopping:
                    raise ConnectionError("homee hat die WebSocket-Verbindung beendet")
            except asyncio.CancelledError:
                return
            except Exception as error:
                if self.stopping:
                    return
                connection_lifetime = time.monotonic() - self.connected_at if self.connected_at else 0
                # Erst jetzt gilt die Sitzung als beendet. Vor diesem Punkt kann
                # kein anderer Codepfad einen neuen Login beginnen.
                await self._drop_socket()
                # Erst eine wirklich stabile Sitzung setzt den Schutzabstand
                # zurück. Kurze Verbindungsabbrüche dürfen homee nicht durch eine
                # schnelle Login-/Reconnect-Schleife belasten.
                delay = 2 if connection_lifetime >= 60 else min(max(delay * 2, 5), 60)
                await self.context.set_status("Verbindung unterbrochen", str(error))
                await asyncio.sleep(delay)
                try:
                    await self._connect()
                except asyncio.CancelledError:
                    return
                except Exception as reconnect_error:
                    await self.context.set_status("Nicht erreichbar", str(reconnect_error))

    async def _handle_message(self, message):
        payload = _json_payload(message)
        self._record_protocol("in", message, payload)
        if not isinstance(payload, dict):
            return
        # GET:all antwortet nicht wie GET:nodes auf der obersten Ebene, sondern
        # mit {"all": {"nodes": [...], ...}}. Ohne dieses Entpacken bleibt die
        # Verbindung aktiv, aber kein einziges Gerät wird veröffentlicht.
        if isinstance(payload.get("all"), dict):
            payload = payload["all"]
        if isinstance(payload.get("attribute_history"), dict):
            self._resolve_history_request(payload["attribute_history"])
            return
        self._persist_resources(payload)
        if isinstance(payload.get("nodes"), list):
            incoming = {}
            for index, node in enumerate(payload["nodes"]):
                normalized = _normalize_node(node)
                if normalized:
                    incoming[normalized["id"]] = normalized
                    await self.context.publish_node(normalized)
                if index and index % 20 == 0:
                    await asyncio.sleep(0)
            for node_id in set(self.nodes) - set(incoming):
                await self.context.remove_node(node_id)
            self.nodes = incoming
            return
        for node in _records(payload, "node", "nodes"):
            normalized = _normalize_node(node)
            if normalized:
                self.nodes[normalized["id"]] = _merge_node(self.nodes.get(normalized["id"]), normalized)
                await self.context.publish_node(self.nodes[normalized["id"]])
        for attribute in _records(payload, "attribute", "attributes"):
            await self._merge_attribute(attribute)

    def _resolve_history_request(self, history):
        try:
            node_id = int(history.get("node_id"))
            attribute_id = int(history.get("attribute_id"))
        except (TypeError, ValueError):
            return
        candidates = [
            (key, future) for key, future in self.history_requests.items()
            if key[0] == node_id and key[1] == attribute_id
        ]
        if not candidates:
            return
        try:
            response_from = int(float(history.get("from")))
            response_till = int(float(history.get("till")))
            exact = next((item for item in candidates if item[0][2:] == (response_from, response_till)), None)
        except (TypeError, ValueError):
            exact = None
        key, future = exact or candidates[0]
        if not future.done():
            future.set_result(history)

    def _fail_history_requests(self, error):
        for future in self.history_requests.values():
            if not future.done():
                future.set_exception(error)
        self.history_requests.clear()

    async def _merge_attribute(self, attribute):
        if not isinstance(attribute, dict):
            return
        try:
            node_id = int(attribute.get("node_id"))
            attribute_id = int(attribute.get("id"))
        except (TypeError, ValueError):
            return
        node = self.nodes.get(node_id)
        if not node:
            await self._request_all()
            return
        attributes = list(node.get("attributes", []))
        previous = next((item for item in attributes if int(item.get("id", -1)) == attribute_id), {})
        merged = {**previous, **attribute, "id": attribute_id, "node_id": node_id}
        attributes = [merged if int(item.get("id", -1)) == attribute_id else item for item in attributes]
        if not previous:
            attributes.append(merged)
        node["attributes"] = attributes
        await self.context.publish_node(node)

    def _persist_resources(self, payload):
        """Persist everything from GET:all that is not already a runtime node.

        Complete plural collections replace the stored snapshot. Singular live
        events are merged into their collection by ID, so a WebSocket update
        cannot discard the other records received during GET:all.
        """
        changed = False
        for key, value in payload.items():
            if key in {"nodes", "node", "attributes", "attribute"}:
                continue
            collection_key = _SINGULAR_COLLECTIONS.get(key)
            if collection_key and isinstance(value, (dict, list)):
                records = value if isinstance(value, list) else [value]
                self.resources[collection_key] = _merge_records(
                    self.resources.get(collection_key, []), records
                )
            elif isinstance(value, dict) and isinstance(self.resources.get(key), dict):
                self.resources[key] = {**self.resources[key], **value}
            else:
                self.resources[key] = value
            changed = True
        if changed:
            self.context.save_state({
                "resources": self.resources,
                "resource_keys": sorted(self.resources),
                "updated_at": time.time(),
                "client_id": self.client_id,
            })

    def _record_protocol(self, direction, message, payload=None):
        text = message.decode("utf-8", errors="replace") if isinstance(message, bytes) else str(message)
        category = _protocol_category(payload) if direction == "in" else "command"
        maximum = 8000
        self.protocol_messages.append({
            "timestamp": time.time(),
            "direction": direction,
            "category": category,
            "message": text[:maximum],
            "size": len(text),
            "truncated": len(text) > maximum,
        })


def _json_payload(message):
    if isinstance(message, bytes):
        message = message.decode("utf-8", errors="replace")
    text = str(message)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


def _records(payload, singular, plural):
    value = payload.get(singular)
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return value
    value = payload.get(plural)
    return value if isinstance(value, list) else []


_SINGULAR_COLLECTIONS = {
    "homeegram": "homeegrams",
    "group": "groups",
    "plan": "plans",
    "user": "users",
    "relationship": "relationships",
    "scenario": "scenarios",
    "notification": "notifications",
    "cube": "cubes",
}

_PROTOCOL_CATEGORIES = (
    "node", "user", "homeegram", "attribute", "settings", "all",
    "warning", "code", "error", "other", "command",
)


def _protocol_category(payload):
    """Ordne Singular-, Plural- und optionale payload-Umschläge gleich ein."""
    if not isinstance(payload, dict) or not payload:
        return "other"
    wrapped = payload.get("payload")
    if isinstance(wrapped, dict) and wrapped:
        payload = wrapped
    first_key = str(next(iter(payload))).lower()
    aliases = {
        "nodes": "node",
        "users": "user",
        "homeegrams": "homeegram",
        "attributes": "attribute",
        "warnings": "warning",
        "codes": "code",
        "errors": "error",
    }
    category = aliases.get(first_key, first_key)
    return category if category in _PROTOCOL_CATEGORIES else "other"


def _merge_records(existing, updates):
    result = [dict(item) if isinstance(item, dict) else item for item in existing] if isinstance(existing, list) else []
    positions = {
        str(item["id"]): index
        for index, item in enumerate(result)
        if isinstance(item, dict) and "id" in item
    }
    for update in updates:
        if not isinstance(update, dict):
            if update not in result:
                result.append(update)
            continue
        identifier = str(update.get("id", ""))
        if identifier and identifier in positions:
            index = positions[identifier]
            result[index] = {**result[index], **update}
        else:
            if identifier:
                positions[identifier] = len(result)
            result.append(dict(update))
    return result


def _normalize_node(node):
    if not isinstance(node, dict):
        return None
    try:
        result = dict(node)
        result["id"] = int(result["id"])
    except (KeyError, TypeError, ValueError):
        return None
    result["attributes"] = [dict(item) for item in result.get("attributes", []) if isinstance(item, dict)]
    for attribute in result["attributes"]:
        attribute.setdefault("node_id", result["id"])
    return result


def _merge_node(previous, current):
    if not previous:
        return current
    result = {**previous, **current}
    old_attributes = {int(item.get("id", -1)): item for item in previous.get("attributes", [])}
    if current.get("attributes"):
        merged = []
        seen = set()
        for item in current["attributes"]:
            attribute_id = int(item.get("id", -1))
            merged.append({**old_attributes.get(attribute_id, {}), **item})
            seen.add(attribute_id)
        merged.extend(item for attribute_id, item in old_attributes.items() if attribute_id not in seen)
        result["attributes"] = merged
    elif previous.get("attributes"):
        result["attributes"] = previous["attributes"]
    return result


def _number_text(value):
    number = float(value)
    return str(int(number)) if number.is_integer() else format(number, ".12g")


def _client_id(integration_id):
    digest = hashlib.sha256(f"SmartHomeBoard Server:{integration_id}".encode("utf-8")).hexdigest()
    return f"shb-server-{digest[:24]}"
