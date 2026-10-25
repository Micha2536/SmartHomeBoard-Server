"""Permission-gated On/Off commands through the normal audited runtime path."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import uuid
from collections.abc import Callable
from typing import Literal


class DeviceControl:
    """Persist command reservations before sending, including uncertain outcomes.

    A retained request UUID never sends twice, even after a restart. The bounded
    journal deliberately refuses new requests when full instead of removing old
    IDs and making a subsequent toggle replay possible.
    """

    MAX_RECORDS = 100000

    def __init__(self, database, runtime, allowed_callable: Callable[[], bool]):
        self.db = database
        self.runtime = runtime
        self.allowed = allowed_callable
        self._lock = asyncio.Lock()
        with self.db._lock, self.db._connection:
            self.db._connection.execute("""
                CREATE TABLE IF NOT EXISTS mcp_device_commands (
                    request_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    result TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)

    async def control(
        self, node_id: int, attribute_id: int,
        operation: Literal["on", "off", "toggle"], request_id: str,
    ) -> dict:
        if not self.allowed():
            raise ValueError("Geräte schalten ist für MCP nicht freigegeben")
        if type(node_id) is not int or type(attribute_id) is not int:
            raise ValueError("Geräte- und Attribut-ID müssen ganze SHB-IDs sein")
        if operation not in ("on", "off", "toggle"):
            raise ValueError("Nur Einschalten, Ausschalten und Umschalten sind erlaubt")
        try:
            request_id = str(uuid.UUID(str(request_id)))
        except (ValueError, AttributeError, TypeError):
            raise ValueError("request_id muss eine UUID sein") from None
        fingerprint = hashlib.sha256(json.dumps(
            [node_id, attribute_id, operation], separators=(",", ":")
        ).encode()).hexdigest()

        async with self._lock:
            # Permissions can change while another request holds the lock.
            if not self.allowed():
                raise ValueError("Geräte schalten ist für MCP nicht freigegeben")
            previous_request = self._previous(request_id, fingerprint)
            if previous_request is not None:
                return previous_request
            node = next((n for n in self.runtime.visible_nodes() if n.get("id") == node_id), None)
            if node is None:
                raise ValueError("Sichtbares SHB-Gerät nicht gefunden")
            attribute = next((a for a in node.get("attributes", []) if a.get("id") == attribute_id), None)
            if attribute is None:
                raise ValueError("Attribut gehört nicht zu diesem SHB-Gerät")
            if type(attribute.get("type")) is not int or attribute["type"] != 1:
                raise ValueError("Nur An/Aus-Schalter werden unterstützt")
            if attribute.get("editable") not in (True, 1):
                raise ValueError("Dieses Schalterattribut ist nicht schreibbar")
            integration_id = str(node.get("integration_id", ""))
            integration = self.db.integration(integration_id)
            if (not integration or not integration.get("enabled")
                    or self.runtime.adapters.get(integration_id) is None):
                raise ValueError("Gerät gehört zu keiner aktiven Serverintegration")

            previous = self._finite_value(attribute.get("current_value"))
            if operation == "toggle" and previous not in (0, 1):
                raise ValueError("Umschalten benötigt einen bekannten An/Aus-Cachewert (0 oder 1)")
            value = (1 - int(previous)) if operation == "toggle" else int(operation == "on")
            result = {
                "request_id": request_id, "node_id": node_id, "attribute_id": attribute_id,
                "operation": operation, "requested_value": value, "previous_value": previous,
                "previous_value_source": "cache", "confirmed": False,
                "command_sent": None, "status": "pending", "replayed": False,
                "retry_allowed": False,
            }
            if operation == "toggle":
                result["toggle_basis"] = "cached_current_value"
            # A second service/process may have reserved the same UUID between
            # lookup and validation. The transaction returns its record instead.
            existing = self._reserve(request_id, fingerprint, result)
            if existing is not None:
                return existing

            try:
                await self.runtime.set_value(
                    node_id, attribute_id, value, source="mcp",
                    source_detail="MCP: " + {"on": "Einschalten", "off": "Ausschalten", "toggle": "Umschalten"}[operation],
                    client_id="mcp", metadata={"request_id": request_id, "operation": operation,
                        "previous_value_source": "cache"},
                )
            except BaseException as error:
                # An adapter exception does not prove that the device received
                # nothing. Persist uncertainty, never retry an ambiguous toggle.
                result.update(status="uncertain", command_sent=None,
                    error="Versand nicht bestätigt; Gerätezustand vor einer neuen Anfrage prüfen",
                    error_type=type(error).__name__)
                self._store(request_id, result)
                if not isinstance(error, Exception):
                    raise
                return result

            result.update(status="sent", command_sent=True)
            self._store(request_id, result)
            return result

    @staticmethod
    def _finite_value(value):
        if not isinstance(value, (int, float)):
            return None
        try:
            return value if math.isfinite(value) else None
        except OverflowError:
            return None

    @staticmethod
    def _replay(row, fingerprint):
        if row["fingerprint"] != fingerprint:
            raise ValueError("Diese Anfrage-ID wurde bereits für einen anderen Gerätebefehl verwendet")
        return {**json.loads(row["result"]), "replayed": True}

    def _previous(self, request_id, fingerprint):
        with self.db._lock:
            row = self.db._connection.execute(
                "SELECT fingerprint,result FROM mcp_device_commands WHERE request_id=?", (request_id,)
            ).fetchone()
        return self._replay(row, fingerprint) if row else None

    def _reserve(self, request_id, fingerprint, result):
        with self.db._lock, self.db._connection:
            self.db._connection.execute("BEGIN IMMEDIATE")
            row = self.db._connection.execute(
                "SELECT fingerprint,result FROM mcp_device_commands WHERE request_id=?", (request_id,)
            ).fetchone()
            if row:
                return self._replay(row, fingerprint)
            count = self.db._connection.execute("SELECT COUNT(*) FROM mcp_device_commands").fetchone()[0]
            if count >= self.MAX_RECORDS:
                raise ValueError(f"MCP-Befehlsjournal ist voll ({self.MAX_RECORDS} Anfragen); keine weiteren Gerätebefehle möglich")
            self.db._connection.execute(
                "INSERT INTO mcp_device_commands(request_id,fingerprint,result) VALUES(?,?,?)",
                (request_id, fingerprint, json.dumps(result, ensure_ascii=False, allow_nan=False)),
            )
        return None

    def _store(self, request_id, result):
        with self.db._lock, self.db._connection:
            self.db._connection.execute(
                "UPDATE mcp_device_commands SET result=? WHERE request_id=?",
                (json.dumps(result, ensure_ascii=False, allow_nan=False), request_id),
            )
