import asyncio
import contextlib
import hashlib
import inspect
import json
import logging
import math
import re
import time
import uuid
from collections import deque
from urllib.parse import parse_qs, quote, unquote

import httpx
import websockets

from server.homeegram_revision import homeegram_revision as _homeegram_mutation_revision


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
        self.homeegram_create_lock = None
        self.homeegram_creations = dict(persisted.get("homeegram_creations", {})) if isinstance(persisted, dict) else {}
        self.homeegram_delete_lock = None
        self.homeegram_deletions = dict(persisted.get("homeegram_deletions", {})) if isinstance(persisted, dict) else {}
        self.homeegram_mutations = dict(persisted.get("homeegram_mutations", {})) if isinstance(persisted, dict) else {}
        self.homeegram_snapshot_revision = 0
        self.homeegram_response_revision = 0
        self.homeegram_full_item_revisions = {}
        self.homeegram_wait_seconds = 15

    async def set_homeegram_active_confirmed(self, homeegram_id, expected_name, active,
                                           idempotency_key, authorize=None):
        """Change only the active flag, with a durable original and confirmation.

        The request UUID is reserved before writing. An uncertain request can
        subsequently be checked but is never sent again, including after restart.
        In particular this method never writes the play flag or executes an HG.
        """
        if type(active) is not bool:
            raise ValueError("Aktiv muss ein boolescher Wert sein")
        return await self._mutate_homeegram_confirmed(
            homeegram_id, expected_name, idempotency_key, "active", active,
            authorize=authorize)

    async def edit_homeegram_confirmed(self, homeegram_id, expected_name, changes,
                                      idempotency_key, authorize=None, expected_revision=None):
        """Apply explicit element patches, retaining every unmentioned element.

        Existing HGs are disabled and confirmed before the first element write;
        successful edits remain disabled. This is deliberately not a complete
        replacement API, so unsupported native elements cannot be lost merely
        because a client cannot display them.
        """
        normalized = _homeegram_edit_changes(changes)
        if expected_revision is not None and (not isinstance(expected_revision, str)
                or not re.fullmatch(r"[a-f0-9]{64}", expected_revision)):
            raise ValueError("Ungültiger Homeegramm-Revisionswert")
        return await self._mutate_homeegram_confirmed(
            homeegram_id, expected_name, idempotency_key, "edit", normalized,
            authorize=authorize, expected_revision=expected_revision)

    async def _mutate_homeegram_confirmed(self, homeegram_id, expected_name, idempotency_key,
                                         mutation, values, authorize=None, expected_revision=None):
        if type(homeegram_id) is not int or homeegram_id <= 0:
            raise ValueError("Eine positive Original-homeegramm-ID ist erforderlich")
        if not isinstance(expected_name, str) or not expected_name.strip():
            raise ValueError("Der genaue Homeegramm-Name muss bestätigt werden")
        try:
            if not isinstance(idempotency_key, str):
                raise ValueError()
            key = str(uuid.UUID(idempotency_key))
        except (ValueError, AttributeError, TypeError):
            raise ValueError("Die Anfrage-ID für die HG-Änderung muss eine UUID sein") from None
        fingerprint = hashlib.sha256(json.dumps(
            [homeegram_id, expected_name, mutation, values, expected_revision],
            sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        # Deletion and editing share one native mutation lane. A DELETE must
        # not race an edit that already backed up the same HG.
        if self.homeegram_delete_lock is None:
            self.homeegram_delete_lock = asyncio.Lock()
        async with self.homeegram_delete_lock:
            await self._authorize_homeegram_mutation(authorize)
            previous = self.homeegram_mutations.get(key)
            if previous and previous.get("fingerprint") != fingerprint:
                raise ValueError("Diese Anfrage-ID wurde bereits für eine andere HG-Änderung verwendet")
            if not previous and len(self.homeegram_mutations) >= 1000:
                raise ValueError("Das lokale HG-Änderungsjournal ist voll (1000 Einträge)")
            socket = self.socket
            self._check_homeegram_connection(socket)
            await self._refresh_homeegram_snapshot(socket)
            original = self._fresh_homeegram_for_mutation(homeegram_id, expected_name)
            if previous:
                if not _homeegram_mutation_matches(original, previous["desired"], mutation):
                    raise ValueError(f"Die frühere Änderung an HG {homeegram_id} ist nicht vollständig bestätigt. "
                                     "Sie wird nicht erneut gesendet; bitte auf homee prüfen.")
                previous.update(status="complete", confirmed_at=time.time(),
                                revision=_homeegram_mutation_revision(original))
                previous.pop("error", None)
                self._save_state()
                return _mutated_homeegram_result(previous, key, replayed=True)
            if expected_revision is not None and _homeegram_mutation_revision(original) != expected_revision:
                raise ValueError("Das Homeegramm wurde zwischenzeitlich geändert. Bitte neu laden und die Änderungen erneut prüfen.")
            backup = json.loads(json.dumps(original, ensure_ascii=False, allow_nan=False))
            if mutation == "active":
                desired = {**backup, "active": int(values)}
                commands = []
                changed = original.get("active") != int(values)
            else:
                desired, commands = _prepare_homeegram_edit(backup, values)
                changed = bool(commands) or original.get("active") != 0
                _validate_homeegram_references(self.resources.get("homeegrams", []),
                                                _changed_homeegram_references(desired, values))
            operation = {"fingerprint": fingerprint, "homeegram_id": homeegram_id,
                         "name": expected_name, "mutation": mutation, "desired": desired,
                         "created_at": time.time(), "status": "reserved", "backup": backup,
                         "changed": changed, "updated": len(values) if mutation == "edit" else 0,
                         "commands_reserved": 0}
            if mutation == "edit":
                operation["changes"] = values
            self.homeegram_mutations[key] = operation
            # Failure to persist the full original prevents every native write.
            self._save_state()
            try:
                if mutation == "active":
                    if changed:
                        before = self.homeegram_response_revision
                        await self._send_homeegram_mutation_command(socket,
                            f"PUT:homeegrams/{homeegram_id}?active={int(values)}", operation, authorize)
                        original = await self._confirm_homeegram_mutation(socket, operation, before)
                else:
                    if original.get("active") != 0:
                        before = self.homeegram_response_revision
                        await self._send_homeegram_mutation_command(socket,
                            f"PUT:homeegrams/{homeegram_id}?active=0", operation, authorize)
                        # Before touching elements only disabling is expected.
                        disabled = {**backup, "active": 0}
                        await self._confirm_homeegram_mutation(socket, operation, before,
                                                              desired=disabled, mutation="active")
                    for command in commands:
                        current = self._fresh_homeegram_for_mutation(homeegram_id, expected_name)
                        if current.get("active") != 0:
                            raise ValueError("Das HG wurde zwischenzeitlich aktiviert. Die Bearbeitung wird abgebrochen.")
                        _validate_homeegram_references(self.resources.get("homeegrams", []),
                                                        _changed_homeegram_references(desired, values))
                        before = self.homeegram_response_revision
                        await self._send_homeegram_mutation_command(socket, command, operation, authorize)
                    # A complete direct element response confirms the final
                    # graph without waiting for a redundant GET. Otherwise
                    # the confirmation helper asks for a fresh full list.
                    if commands:
                        original = await self._confirm_homeegram_mutation(socket, operation, before)
                    else:
                        original = self._fresh_homeegram_for_mutation(homeegram_id, expected_name)
                        if not _homeegram_mutation_matches(original, desired, mutation):
                            raise ValueError("Der vollständige deaktivierte HG-Zustand wurde nicht bestätigt")
                operation.update(status="complete", confirmed_at=time.time(),
                                 revision=_homeegram_mutation_revision(original))
                operation.pop("error", None)
                self._save_state()
                return _mutated_homeegram_result(operation, key, replayed=False)
            except BaseException as error:
                operation.update(status="uncertain_or_partial", error=str(error)[:500])
                self._save_state()
                raise

    def _fresh_homeegram_for_mutation(self, homeegram_id, expected_name):
        matches = [row for row in self.resources.get("homeegrams", [])
                   if isinstance(row, dict) and type(row.get("id")) is int and row["id"] == homeegram_id]
        if len(matches) != 1:
            raise ValueError("Das Homeegramm ist nicht eindeutig in der frischen homee-Liste vorhanden")
        original = matches[0]
        if unquote(str(original.get("name", ""))) != expected_name:
            raise ValueError("Der bestätigte Name stimmt nicht exakt mit dem aktuellen Homeegramm überein")
        if not all(isinstance(original.get(section), dict) for section in ("triggers", "conditions", "actions")):
            raise ValueError("homee hat kein vollständiges Homeegramm für das Backup geliefert; keine Änderung möglich")
        if type(original.get("active")) is not int or original["active"] not in (0, 1):
            raise ValueError("homee hat keinen eindeutigen Aktiv-Zustand geliefert")
        for section in ("triggers", "conditions", "actions"):
            for rows in original[section].values():
                if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                    raise ValueError("homee hat unvollständige HG-Elemente geliefert")
        return original

    @staticmethod
    async def _authorize_homeegram_mutation(authorize):
        if authorize is None:
            return
        allowed = authorize()
        if inspect.isawaitable(allowed):
            allowed = await allowed
        if allowed is not True:
            raise ValueError("Das Ändern von Homeegrammen ist nicht freigegeben")

    async def _send_homeegram_mutation_command(self, socket, command, operation, authorize):
        self._check_homeegram_connection(socket)
        await self._authorize_homeegram_mutation(authorize)
        self._check_homeegram_connection(socket)
        if operation["mutation"] == "edit":
            _validate_homeegram_references(self.resources.get("homeegrams", []),
                                            _changed_homeegram_references(operation["desired"], operation["changes"]))
        operation["commands_reserved"] += 1
        operation["status"] = "sending"
        # Reserve each send before it can reach homee; retries never replay it.
        self._save_state()
        await self._send_homeegram_command(socket, command)

    async def _confirm_homeegram_mutation(self, socket, operation, before, desired=None, mutation=None):
        wanted = desired if desired is not None else operation["desired"]
        kind = mutation if mutation is not None else operation["mutation"]
        deadline = time.monotonic() + self.homeegram_wait_seconds
        while True:
            self._check_homeegram_connection(socket)
            actual = self._fresh_homeegram_for_mutation(operation["homeegram_id"], operation["name"])
            if (self.homeegram_full_item_revisions.get(operation["homeegram_id"], 0) > before
                    and _homeegram_mutation_matches(actual, wanted, kind)):
                return actual
            if time.monotonic() >= deadline:
                raise TimeoutError(f"homee hat die Änderung von HG {operation['homeegram_id']} nicht vollständig bestätigt. "
                                   "Das Original wurde gesichert. Dieselbe Anfrage prüft nur und sendet nicht erneut.")
            # A full direct PUT response is accepted above without another GET.
            await self._refresh_homeegram_snapshot(socket)
            await asyncio.sleep(0.05)

    async def delete_homeegram_confirmed(self, homeegram_id, expected_name, idempotency_key, authorize=None):
        """Back up and delete one explicitly identified HG on the live session.

        The full original stays in the local journal, never in the result sent
        to an MCP client. Reserved request UUIDs never send DELETE twice, even
        if the first reply was lost or homee subsequently reuses that HG ID.
        """
        if type(homeegram_id) is not int or homeegram_id <= 0:
            raise ValueError("Eine positive Original-homeegramm-ID ist erforderlich")
        if not isinstance(expected_name, str) or not expected_name.strip():
            raise ValueError("Der genaue Homeegramm-Name muss bestätigt werden")
        # The caller confirms the displayed name; decode only the raw homee
        # field below. Decoding the confirmation again would corrupt literal
        # percent sequences such as a visible "%20" in a legitimate HG name.
        name = expected_name
        try:
            if not isinstance(idempotency_key, str):
                raise ValueError("Anfrage-ID muss eine UUID sein")
            key = str(uuid.UUID(idempotency_key))
        except (ValueError, AttributeError, TypeError):
            raise ValueError("Die Anfrage-ID für das Löschen muss eine UUID sein") from None
        fingerprint = hashlib.sha256(json.dumps([homeegram_id, name], ensure_ascii=False).encode()).hexdigest()
        if self.homeegram_delete_lock is None:
            self.homeegram_delete_lock = asyncio.Lock()
        async with self.homeegram_delete_lock:
            await self._authorize_homeegram_deletion(authorize)
            previous = self.homeegram_deletions.get(key)
            if previous and previous.get("fingerprint") != fingerprint:
                raise ValueError("Diese Anfrage-ID wurde bereits für einen anderen Löschauftrag verwendet")
            if previous and previous.get("status") == "deleted":
                return _deleted_homeegram_result(previous, key, replayed=True)
            if not previous and len(self.homeegram_deletions) >= 1000:
                raise ValueError("Das lokale HG-Löschjournal ist voll (1000 Einträge); keine weitere Löschung möglich")
            socket = self.socket
            self._check_homeegram_connection(socket)
            await self._refresh_homeegram_snapshot(socket)
            matches = [row for row in self.resources.get("homeegrams", [])
                       if isinstance(row, dict) and type(row.get("id")) is int and row["id"] == homeegram_id]
            if previous:
                # A send that failed or timed out may nevertheless have reached
                # homee. Retrying is a read-only confirmation, never a new delete.
                if matches:
                    raise ValueError(f"Der frühere Löschauftrag für HG {homeegram_id} ist nicht bestätigt. "
                                     "Es wird nicht erneut gelöscht; bitte auf homee prüfen.")
                previous.update(status="deleted", confirmed_at=time.time())
                previous.pop("error", None)
                self._save_state()
                return _deleted_homeegram_result(previous, key, replayed=True)
            if len(matches) != 1:
                raise ValueError("Das Homeegramm ist nicht eindeutig in der frischen homee-Liste vorhanden")
            original = matches[0]
            if unquote(str(original.get("name", ""))) != name:
                raise ValueError("Der bestätigte Name stimmt nicht exakt mit dem aktuellen Homeegramm überein")
            if not all(isinstance(original.get(section), dict) for section in ("triggers", "conditions", "actions")):
                raise ValueError("homee hat kein vollständiges Homeegramm für das Backup geliefert; es wird nicht gelöscht")
            backup = json.loads(json.dumps(original, ensure_ascii=False, allow_nan=False))
            operation = {"fingerprint": fingerprint, "homeegram_id": homeegram_id, "name": name,
                         "created_at": time.time(), "status": "delete_reserved", "backup": backup}
            self.homeegram_deletions[key] = operation
            # A failed backup write must prevent the destructive command.
            self._save_state()
            try:
                command = f"DELETE:homeegrams/{homeegram_id}"

                async def send_authorized():
                    self._check_homeegram_connection(socket)
                    await self._authorize_homeegram_deletion(authorize)
                    self._check_homeegram_connection(socket)
                    # homee may return the complete HG list directly for
                    # DELETE, before send() resumes. Keep the pre-send revision
                    # so that this reply is not lost behind another GET.
                    revision = self.homeegram_snapshot_revision
                    await socket.send(command)
                    return revision

                delete_revision = await asyncio.wait_for(send_authorized(), timeout=self.homeegram_wait_seconds)
                self._record_protocol("out", command)
                self._check_homeegram_connection(socket)
                deadline = time.monotonic() + self.homeegram_wait_seconds
                while True:
                    self._check_homeegram_connection(socket)
                    if self.homeegram_snapshot_revision > delete_revision and not any(
                            isinstance(row, dict) and row.get("id") == homeegram_id
                            for row in self.resources.get("homeegrams", [])):
                        operation.update(status="deleted", confirmed_at=time.time())
                        self._save_state()
                        return _deleted_homeegram_result(operation, key, replayed=False)
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"homee hat das Löschen von HG {homeegram_id} noch nicht bestätigt. "
                                           "Das Original wurde lokal gesichert. Dieselbe Anfrage prüft nur den Zustand und löscht nicht erneut.")
                    # No confirming complete reply yet: request a fresh list.
                    # A delayed DELETE reply also satisfies this wait.
                    await self._refresh_homeegram_snapshot(socket)
                    if any(isinstance(row, dict) and row.get("id") == homeegram_id
                           for row in self.resources.get("homeegrams", [])):
                        await asyncio.sleep(0.1)
            except BaseException as error:
                operation.update(status="uncertain", error=str(error)[:500])
                self._save_state()
                raise

    @staticmethod
    async def _authorize_homeegram_deletion(authorize):
        if authorize is None:
            return
        allowed = authorize()
        if inspect.isawaitable(allowed):
            allowed = await allowed
        if allowed is not True:
            raise ValueError("Das Löschen von Homeegrammen ist nicht freigegeben")

    async def create_homeegram_disabled(self, payload, idempotency_key):
        """Create a new, confirmed disabled HG on the existing homee session.

        This deliberately is not a raw-command action. Callers must additionally
        authorize the operation and validate its original homee device IDs.
        An uncertain/partial request is recorded before sending, so retrying its
        key can only confirm the same HG, never create another or replay actions.
        """
        desired, elements = _disabled_homeegram_payload(payload)
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 128:
            raise ValueError("Eine eindeutige Anfrage-ID mit höchstens 128 Zeichen ist erforderlich")
        key = idempotency_key.strip()
        fingerprint = hashlib.sha256(json.dumps(desired, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if self.homeegram_create_lock is None:
            self.homeegram_create_lock = asyncio.Lock()
        async with self.homeegram_create_lock:
            socket = self.socket
            self._check_homeegram_connection(socket)
            previous = self.homeegram_creations.get(key)
            if previous and previous.get("fingerprint") != fingerprint:
                raise ValueError("Diese Anfrage-ID wurde bereits für ein anderes Homeegramm verwendet")
            if previous:
                await self._refresh_homeegram_snapshot(socket)
                existing = self._find_created_homeegram(previous)
                if not existing or not _homeegram_matches(existing, desired, elements):
                    identifier = previous.get("homeegram_id")
                    location = f" (ID {identifier})" if identifier else ""
                    raise ValueError("Die frühere Anlage" + location + " ist nicht vollständig als deaktiviertes HG bestätigt. "
                                     "Bitte auf homee prüfen; dieselbe Anfrage erzeugt keine zweite Kopie.")
                previous.update(status="complete", homeegram_id=int(existing["id"]))
                self._save_state()
                return _created_homeegram_result(existing, created=False)
            if len(self.homeegram_creations) >= 1000:
                raise ValueError("Die Grenze für gespeicherte HG-Anfragen ist erreicht")

            # Only a fresh complete list can distinguish the new ID from cached
            # or same-named existing HGs. Never open another connection/login.
            await self._refresh_homeegram_snapshot(socket)
            _validate_homeegram_references(self.resources.get("homeegrams", []),
                                            [(kind, values) for _, kind, values in elements])
            operation = {"fingerprint": fingerprint, "name": desired["name"],
                         "baseline_ids": [int(h["id"]) for h in self.resources.get("homeegrams", [])
                                          if isinstance(h, dict) and "id" in h],
                         "status": "creation_sent", "created_at": time.time(), "elements_sent": 0}
            self.homeegram_creations[key] = operation
            self._save_state()
            try:
                header = {k: v for k, v in desired.items() if k not in {"triggers", "conditions", "actions", "play"}}
                await self._send_homeegram_command(socket, "POST:homeegrams?" + _homeegram_query(header))
                created = await self._wait_for_created_homeegram(socket, operation)
                homeegram_id = int(created["id"])
                operation.update(homeegram_id=homeegram_id, status="adding_elements")
                self._save_state()
                for index, (section, kind, parameters) in enumerate(elements):
                    current = self._find_created_homeegram(operation)
                    if current and (current.get("active") != 0 or current.get("play", 0) != 0):
                        raise ValueError("Das neue HG wurde zwischenzeitlich aktiviert oder abgespielt. Die Anlage wird abgebrochen.")
                    _validate_homeegram_references(self.resources.get("homeegrams", []),
                                                    [(k, v) for _, k, v in elements])
                    # Persist intent before send; after a crash/timeout the same
                    # key must not append an element that may already exist.
                    operation["elements_sent"] = index + 1
                    self._save_state()
                    values = dict(parameters)
                    values["type"] = _HG_NATIVE_TYPES[section][kind]
                    if kind == "attribute_triggers":
                        values["logicType"] = 0
                    elif kind == "attribute_conditions":
                        values["logicType"] = 1
                    await self._send_homeegram_command(socket,
                        f"POST:homeegrams/{homeegram_id}/{section}?" + _homeegram_query(values))
                operation["status"] = "confirming"
                self._save_state()
                confirmed = await self._confirm_created_homeegram(socket, operation, desired, elements)
                operation["status"] = "complete"
                operation.pop("error", None)
                self._save_state()
                return _created_homeegram_result(confirmed, created=True)
            except BaseException as error:
                operation["status"] = "uncertain_or_partial"
                operation["error"] = str(error)[:500]
                self._save_state()
                raise

    def _check_homeegram_connection(self, socket):
        if socket is None or self.socket is not socket or self.stopping:
            raise ConnectionError("Die homee-Verbindung wurde unterbrochen; der HG-Vorgang wird nicht automatisch wiederholt")

    async def _send_homeegram_command(self, socket, command):
        self._check_homeegram_connection(socket)
        await asyncio.wait_for(socket.send(command), timeout=self.homeegram_wait_seconds)
        self._record_protocol("out", command)
        self._check_homeegram_connection(socket)

    async def _refresh_homeegram_snapshot(self, socket):
        revision = self.homeegram_snapshot_revision
        await self._send_homeegram_command(socket, "GET:homeegrams")
        deadline = time.monotonic() + self.homeegram_wait_seconds
        while self.homeegram_snapshot_revision <= revision:
            self._check_homeegram_connection(socket)
            if time.monotonic() >= deadline:
                raise TimeoutError("homee hat die aktuelle Homeegramm-Liste nicht bestätigt")
            await asyncio.sleep(0.05)
        self._check_homeegram_connection(socket)

    def _find_created_homeegram(self, operation):
        rows = self.resources.get("homeegrams", [])
        if operation.get("homeegram_id"):
            return next((h for h in rows if isinstance(h, dict) and h.get("id") == operation["homeegram_id"]), None)
        known = set(operation["baseline_ids"])
        candidates = [h for h in rows if isinstance(h, dict) and h.get("id") not in known
                      and unquote(str(h.get("name", ""))) == operation["name"]]
        if len(candidates) > 1:
            raise ValueError("Mehrere neue gleichnamige HGs gefunden; keine eindeutige Zuordnung. Bitte auf homee prüfen.")
        return candidates[0] if candidates else None

    async def _wait_for_created_homeegram(self, socket, operation):
        deadline = time.monotonic() + self.homeegram_wait_seconds
        while True:
            self._check_homeegram_connection(socket)
            created = self._find_created_homeegram(operation)
            if created:
                if int(created.get("active", -1)) != 0 or int(created.get("play", 0)) != 0:
                    raise ValueError("homee hat das neue HG nicht als deaktiviert bestätigt. Bitte auf homee prüfen.")
                return created
            if time.monotonic() >= deadline:
                raise TimeoutError("homee hat die Anlage nicht bestätigt. Die Anfrage bleibt gespeichert; bitte nicht mit neuer Anfrage-ID wiederholen.")
            await self._refresh_homeegram_snapshot(socket)
            await asyncio.sleep(0.05)

    async def _confirm_created_homeegram(self, socket, operation, desired, elements):
        deadline = time.monotonic() + self.homeegram_wait_seconds
        while True:
            await self._refresh_homeegram_snapshot(socket)
            actual = self._find_created_homeegram(operation)
            if actual and _homeegram_matches(actual, desired, elements):
                return actual
            if time.monotonic() >= deadline:
                raise TimeoutError(f"homee hat HG {operation['homeegram_id']} noch nicht vollständig deaktiviert bestätigt. "
                                   "Es wird nicht aktiviert. Bitte auf homee prüfen; dieselbe Anfrage erzeugt keine zweite Kopie.")
            await asyncio.sleep(0.1)

    def connection_status(self):
        host = str(self.configuration.get("host", "")).strip()
        port = int(float(self.configuration.get("port", 7681)))
        return [{
            "id": "homee", "protocol": "WebSocket", "direction": "Ausgehend",
            "endpoint": f"ws://{host}:{port}/connection" if host else "homee WebSocket",
            "peer": "homee", "state": "Verbunden" if self.socket is not None else "Verbindung wird aufgebaut",
            "error": "",
        }]

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
        self.context.report_connection("homee", state="Beendet")

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
                await self._request_group_snapshot()
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
        self.context.report_connection("homee", state="Getrennt")

    async def _open_socket(self, host, port, token):
        endpoint = f"ws://{host}:{port}/connection"
        self.context.report_connection(
            "homee", protocol="WebSocket", direction="Ausgehend", endpoint=endpoint,
            peer="homee", state="Verbindung wird aufgebaut", error="",
        )
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
        self.context.report_connection("homee", state="Verbunden", error="")
        if not await self._request_all(force=True):
            await self.socket.send("GET:nodes")
            self._record_protocol("out", "GET:nodes")
        await self._request_group_snapshot()

    async def _request_group_snapshot(self):
        """Load membership metadata once per session / explicit full refresh.

        Some homee versions omit relationships from GET:all. These small,
        separate snapshots supply group names and memberships without another
        large GET:all or a recurring polling task.
        """
        socket = self.socket
        if socket is None:
            raise ConnectionError("homee ist nicht verbunden")
        for command in ("GET:groups", "GET:relationships"):
            if self.socket is not socket:
                raise ConnectionError("Die homee-Verbindung wurde während des Abrufs geändert")
            await socket.send(command)
            self._record_protocol("out", command)

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
                    self.context.report_connection("homee", message_received=True)
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
            if key == "homeegrams" and isinstance(value, list):
                self.homeegram_snapshot_revision += 1
            if key in {"homeegram", "homeegrams"}:
                records = value if isinstance(value, list) else [value]
                self.homeegram_response_revision += 1
                for row in records:
                    if (isinstance(row, dict) and type(row.get("id")) is int
                            and all(isinstance(row.get(section), dict)
                                    for section in ("triggers", "conditions", "actions"))):
                        self.homeegram_full_item_revisions[row["id"]] = self.homeegram_response_revision
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
            self._save_state()

    def _save_state(self):
        self.context.save_state({
            "resources": self.resources,
            "resource_keys": sorted(self.resources),
            "updated_at": time.time(),
            "client_id": self.client_id,
            "homeegram_creations": self.homeegram_creations,
            "homeegram_deletions": self.homeegram_deletions,
            "homeegram_mutations": self.homeegram_mutations,
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


def _deleted_homeegram_result(operation, request_id, replayed):
    return {"homeegram_id": operation["homeegram_id"], "name": operation["name"],
            "deleted": True, "status": "deleted", "backup_saved": True,
            "request_id": request_id, "replayed": replayed}


_HG_NATIVE_TYPES = {
    "triggers": {"time_triggers": 1, "attribute_triggers": 2, "switch_triggers": 3,
                 "webhook_triggers": 4, "homeegram_triggers": 5, "celestial_triggers": 6,
                 "plan_triggers": 7, "group_triggers": 8, "user_triggers": 9},
    "conditions": {"time_conditions": 1, "attribute_conditions": 2, "homeegram_conditions": 3,
                   "celestial_conditions": 4, "plan_conditions": 5, "group_conditions": 6,
                   "user_conditions": 7},
    "actions": {"attribute_actions": 1, "tts_actions": 2, "notification_actions": 3,
                "group_actions": 4, "webhook_actions": 5, "homeegram_actions": 6,
                "plan_actions": 7, "user_actions": 8},
}
_HG_ELEMENT_PARAMETERS = frozenset({
    "node_id", "attribute_id", "group_id", "attribute_type", "source_attribute_id",
    "source_homeegram_id", "target_homeegram_id", "operator", "operand", "command", "value",
    "branch", "check_moment", "delay", "scope", "homeegram_event", "dtstart", "rrule",
    "duration", "event", "message", "body", "url", "style", "critical", "user_ids", "method",
    "content_type", "source_plan_id", "target_plan_id", "plan_event", "user_id", "time_of_day", "offset",
})


def _validate_homeegram_reference(kind, row, allow_legacy_missing_branch=False):
    """Validate a stored HG reference, never an executable HG command."""
    if kind not in {"homeegram_triggers", "homeegram_actions"}:
        return None
    reference = "source_homeegram_id" if kind == "homeegram_triggers" else "target_homeegram_id"
    identifier = row.get(reference)
    event = row.get("homeegram_event")
    if type(identifier) is not int or identifier <= 0:
        raise ValueError("Eine positive Original-homeegramm-ID ist erforderlich: " + reference)
    if type(event) is not int or event not in (1, 2, 3, 4):
        raise ValueError("Ungültiges Homeegramm-Ereignis; 1 bis 4 erforderlich")
    if kind == "homeegram_actions":
        legacy_branch = allow_legacy_missing_branch and "branch" not in row
        if not legacy_branch and (type(row.get("branch")) is not int or row["branch"] not in (0, 1, 2)):
            raise ValueError("Ein expliziter Homeegramm-Aktionspfad ist erforderlich")
        delay = row.get("delay", 0)
        if type(delay) not in (int, float) or not math.isfinite(delay) or not 0 <= delay <= 604800:
            raise ValueError("Ungültige Aktionsverzögerung")
    return identifier


def _validate_homeegram_references(homeegrams, elements):
    for kind, row in elements:
        identifier = _validate_homeegram_reference(kind, row,
            allow_legacy_missing_branch=type(row.get("id")) is int and row["id"] > 0)
        if identifier is not None and len([hg for hg in homeegrams
                if isinstance(hg, dict) and type(hg.get("id")) is int and hg["id"] == identifier]) != 1:
            raise ValueError("Das referenzierte Homeegramm gehört nicht eindeutig zu diesem Original-homee")


def _changed_homeegram_references(desired, changes):
    """Unedited legacy references may remain, including missing devices/HGs."""
    references = []
    for index, change in enumerate(changes):
        if change["operation"] == "remove" or change["kind"] not in {"homeegram_triggers", "homeegram_actions"}:
            continue
        identifier = -(index + 1) if change["operation"] == "add" else change["element_id"]
        row = next(row for row in desired[change["section"]][change["kind"]] if row["id"] == identifier)
        references.append((change["kind"], row))
    return references


def _homeegram_edit_changes(changes):
    """Basic transport validation; source/value authorization belongs to caller."""
    if not isinstance(changes, list) or not 1 <= len(changes) <= 120:
        raise ValueError("Eine Liste mit 1 bis 120 HG-Änderungen ist erforderlich")
    result, targets = [], set()
    for change in changes:
        if not isinstance(change, dict) or set(change) - {"operation", "section", "kind", "element_id", "parameters"}:
            raise ValueError("Ungültige HG-Änderung")
        operation, section, kind = (change.get(key) for key in ("operation", "section", "kind"))
        if operation not in ("add", "update", "remove") or section not in _HG_NATIVE_TYPES or kind not in _HG_NATIVE_TYPES[section]:
            raise ValueError("Nicht unterstützte HG-Elementart oder Operation")
        identifier = change.get("element_id")
        normalized = {"operation": operation, "section": section, "kind": kind}
        if operation == "add":
            if identifier is not None:
                raise ValueError("Neue HG-Elemente dürfen keine bestehende ID verwenden")
        else:
            if type(identifier) is not int or identifier <= 0:
                raise ValueError("Eine positive Original-Element-ID ist erforderlich")
            target = (section, identifier)
            if target in targets:
                raise ValueError("Ein HG-Element darf pro Anfrage nur einmal geändert werden")
            targets.add(target)
            normalized["element_id"] = identifier
        parameters = change.get("parameters", {})
        if not isinstance(parameters, dict) or set(parameters) - _HG_ELEMENT_PARAMETERS:
            raise ValueError("Nicht unterstützte HG-Elementfelder")
        if operation == "remove" and parameters:
            raise ValueError("Beim Entfernen dürfen keine Parameter angegeben werden")
        for key, value in parameters.items():
            if key == "user_ids":
                if not isinstance(value, list) or len(value) > 200 or not all(type(item) is int and item > 0 for item in value):
                    raise ValueError("Ungültige Benutzer-IDs")
            elif type(value) in (int, float):
                if not math.isfinite(value):
                    raise ValueError("HG-Zahlenwerte müssen endlich sein")
            elif not isinstance(value, str) or len(value) > 16000 or any(ord(char) < 32 and char not in "\n\t\r" for char in value):
                raise ValueError("Ungültiger HG-Parameter: " + key)
            if key == "branch" and (type(value) is not int or section == "triggers"
                    or value not in ((0, 1) if section == "conditions" else (0, 1, 2))):
                raise ValueError("Ungültiger Bedingungs- oder Aktionspfad")
            if key == "check_moment" and (type(value) is not int or section != "conditions" or value not in (1, 2, 3)):
                raise ValueError("Ungültiger Bedingungs-Prüfzeitpunkt")
        if operation != "remove":
            normalized["parameters"] = json.loads(json.dumps(parameters, allow_nan=False))
        result.append(normalized)
    return result


def _prepare_homeegram_edit(original, changes):
    desired = json.loads(json.dumps(original, ensure_ascii=False, allow_nan=False))
    desired.update(active=0, play=0)
    commands = []
    homeegram_id = original["id"]
    for index, change in enumerate(changes):
        section, kind, operation = (change[key] for key in ("section", "kind", "operation"))
        rows = desired[section].setdefault(kind, [])
        parameters = dict(change.get("parameters", {}))
        if operation == "add":
            if section in {"conditions", "actions"} and "branch" not in parameters:
                raise ValueError("Neue Bedingungen und Aktionen benötigen einen expliziten Pfad")
            row = {**parameters, "id": -(index + 1), "homeegram_id": homeegram_id}
            if kind == "homeegram_actions":
                row.setdefault("delay", 0)
                parameters.setdefault("delay", 0)
            _validate_homeegram_reference(kind, row)
            rows.append(row)
            values = {**parameters, "type": _HG_NATIVE_TYPES[section][kind]}
            if kind == "attribute_triggers":
                values["logicType"] = 0
            elif kind == "attribute_conditions":
                values["logicType"] = 1
            commands.append(f"POST:homeegrams/{homeegram_id}/{section}?" + _homeegram_element_query(values))
            continue
        identifier = change["element_id"]
        matches = [(candidate_kind, position, row) for candidate_kind, candidates in desired[section].items()
                   for position, row in enumerate(candidates) if row.get("id") == identifier]
        if len(matches) != 1 or matches[0][0] != kind:
            raise ValueError("Die Element-ID ist nicht eindeutig im bestätigten HG und Typ vorhanden")
        _, position, old = matches[0]
        if operation == "remove":
            rows.pop(position)
            commands.append(f"DELETE:homeegrams/{homeegram_id}/{section}/{identifier}")
        else:
            updated = {**old, **parameters}
            _validate_homeegram_reference(kind, updated, allow_legacy_missing_branch=True)
            if kind == "attribute_actions" and updated.get("command") == 3:
                updated.pop("value", None)
            if updated == old:
                continue
            rows[position] = updated
            # Unknown future fields are retained in desired/backup, not invented
            # as write parameters. Never drop branch/check_moment during PUT.
            values = {key: value for key, value in updated.items() if key in _HG_ELEMENT_PARAMETERS}
            commands.append(f"PUT:homeegrams/{homeegram_id}/{section}/{identifier}?" + _homeegram_element_query(values))
    return desired, commands


def _homeegram_element_query(values):
    return _homeegram_query({key: ",".join(str(item) for item in value) if key == "user_ids" else value
                            for key, value in values.items()})


def _homeegram_mutation_matches(actual, desired, mutation):
    if (actual.get("id") != desired.get("id") or actual.get("active") != desired.get("active")
            or unquote(str(actual.get("name", ""))) != unquote(str(desired.get("name", "")))):
        return False
    if mutation == "active":
        # Only active may change; an unrelated concurrent edit must not be
        # reported as a confirmed activation of the backed-up configuration.
        a = {**actual, "active": 0}
        b = {**desired, "active": 0}
        return _homeegram_mutation_revision(a) == _homeegram_mutation_revision(b)
    if actual.get("play", 0) != 0:
        return False
    for section in ("triggers", "conditions", "actions"):
        received = actual.get(section)
        if not isinstance(received, dict):
            return False
        expected = desired.get(section, {})
        if set(key for key, rows in received.items() if rows) != set(key for key, rows in expected.items() if rows):
            return False
        for kind, rows in expected.items():
            current = received.get(kind, [])
            if not isinstance(current, list) or len(current) != len(rows):
                return False
            # homee may reorder rows. Keep existing IDs exact and match new
            # server-assigned IDs as a multiset, never reuse one received row
            # to "confirm" two identical additions.
            if not all(isinstance(row, dict) and type(row.get("id")) is int and row["id"] > 0
                       for row in current) or len({row["id"] for row in current}) != len(current):
                return False
            unmatched = list(current)
            existing_ids = {row["id"] for row in rows if row.get("id", 0) > 0}
            for wanted in sorted(rows, key=lambda row: row.get("id", 0) <= 0):
                def matches(found):
                    if wanted.get("id", 0) > 0:
                        if found["id"] != wanted["id"]:
                            return False
                    elif found["id"] in existing_ids:
                        return False
                    return all(found.get(key) == value for key, value in wanted.items()
                               if key not in {"id", "homeegram_id"}
                               and not (key == "value" and wanted.get("command") == 3))
                index = next((i for i, found in enumerate(unmatched) if matches(found)), None)
                if index is None:
                    return False
                unmatched.pop(index)
    ignored = {"triggers", "conditions", "actions", "state", "play", "last_triggered"}
    return {key: value for key, value in actual.items() if key not in ignored} == {
        key: value for key, value in desired.items() if key not in ignored}


def _mutated_homeegram_result(operation, request_id, replayed):
    active = operation["desired"]["active"]
    result = {"homeegram_id": operation["homeegram_id"], "name": operation["name"],
              "status": "edited_disabled" if operation["mutation"] == "edit" else "active" if active else "inactive",
              "active": active, "request_id": request_id, "replayed": replayed,
              "backup_saved": True, "changed": operation["changed"], "revision": operation["revision"]}
    if operation["mutation"] == "edit":
        result.update(play=0, updated=operation["updated"])
    return result


def _disabled_homeegram_payload(payload):
    """Accept only the native kinds currently supported by MCP HG creation."""
    if not isinstance(payload, dict):
        raise ValueError("Homeegramm muss ein Objekt sein")
    allowed = {"id", "name", "image", "active", "play", "visible", "favorite", "order", "triggers", "conditions", "actions"}
    if set(payload) - allowed:
        raise ValueError("Nicht unterstützte Homeegramm-Felder: " + ", ".join(sorted(set(payload) - allowed)))
    if payload.get("id", 0) != 0 or payload.get("active", 0) != 0 or payload.get("play", 0) != 0:
        raise ValueError("Es dürfen nur neue deaktivierte Homeegramme ohne Abspielen angelegt werden")
    name = unquote(str(payload.get("name", ""))).strip()
    if not name or len(name) > 120 or any(ord(char) < 32 for char in name):
        raise ValueError("Ein Homeegramm-Name mit 1 bis 120 Zeichen ist erforderlich")
    desired = {"name": name, "image": str(payload.get("image", "default")), "active": 0,
               "play": 0, "visible": payload.get("visible", 1)}
    if desired["visible"] not in (0, 1):
        raise ValueError("Ungültige Homeegramm-Sichtbarkeit")
    for key in ("favorite", "order"):
        if key in payload:
            if not isinstance(payload[key], int) or payload[key] < 0:
                raise ValueError("Ungültiges Feld: " + key)
            desired[key] = payload[key]
    elements = []
    for section, kinds in (("triggers", ("switch_triggers", "attribute_triggers", "homeegram_triggers")),
                           ("conditions", ("attribute_conditions",)),
                           ("actions", ("attribute_actions", "homeegram_actions"))):
        collections = payload.get(section, {})
        if not isinstance(collections, dict) or set(collections) - set(kinds):
            raise ValueError("Nicht unterstützte Homeegramm-Elemente in " + section)
        desired[section] = {}
        for kind in kinds:
            rows = collections.get(kind, [])
            if not isinstance(rows, list) or len(rows) > 40:
                raise ValueError("Ungültige Anzahl von Homeegramm-Elementen")
            normalized = []
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("Homeegramm-Element muss ein Objekt sein")
                metadata = {"id", "homeegram_id"}
                if kind == "homeegram_triggers":
                    fields = {"source_homeegram_id", "homeegram_event"}
                elif kind == "homeegram_actions":
                    fields = {"target_homeegram_id", "homeegram_event", "branch", "delay"}
                else:
                    fields = set() if kind == "switch_triggers" else {"node_id", "attribute_id", "value", "operator", "operand"} if section == "triggers" else {"node_id", "attribute_id", "value", "operator", "operand", "branch", "check_moment"} if section == "conditions" else {"node_id", "attribute_id", "value", "command", "branch", "delay", "source_attribute_id"}
                if set(row) - fields - metadata:
                    raise ValueError("Nicht unterstützte Felder in " + kind)
                if row.get("homeegram_id", 0) != 0:
                    raise ValueError("Elemente dürfen keinem bestehenden Homeegramm gehören")
                values = {key: value for key, value in row.items() if key in fields}
                if kind in {"homeegram_triggers", "homeegram_actions"}:
                    if kind == "homeegram_actions":
                        values.setdefault("delay", 0)
                    _validate_homeegram_reference(kind, values)
                elif kind != "switch_triggers":
                    for key in ("node_id", "attribute_id"):
                        if type(values.get(key)) is not int or (key == "attribute_id" and values[key] <= 0):
                            raise ValueError("Ungültige Original-homee-ID: " + key)
                    if section == "actions":
                        values.setdefault("command", 1)
                        values.setdefault("branch", 1)
                        values.setdefault("delay", 0)
                        values.setdefault("source_attribute_id", 0)
                        if values["command"] not in (1, 3) or values["branch"] not in (0, 1, 2) or values["source_attribute_id"] != 0:
                            raise ValueError("Ungültiger Homeegramm-Aktionsbefehl oder Zweig")
                        if type(values["delay"]) not in (int, float) or not math.isfinite(values["delay"]) or not 0 <= values["delay"] <= 604800:
                            raise ValueError("Ungültige Aktionsverzögerung")
                        if values["command"] == 3:
                            values.pop("value", None)
                    else:
                        values.setdefault("operator", 3 if section == "triggers" else 1)
                        values.setdefault("operand", 1)
                        if values["operator"] not in (range(1, 5) if section == "triggers" else range(1, 9)) or values["operand"] != 1:
                            raise ValueError("Ungültiger Vergleich oder Operand")
                        if section == "conditions":
                            values.setdefault("branch", 1)
                            values.setdefault("check_moment", 1)
                            if values["branch"] not in (0, 1) or values["check_moment"] not in (1, 2, 3):
                                raise ValueError("Ungültige Bedingungsart oder Prüfzeitpunkt")
                    if not (section == "actions" and values["command"] == 3):
                        if type(values.get("value")) not in (int, float) or not math.isfinite(values["value"]):
                            raise ValueError("Ein endlicher Zahlenwert ist erforderlich")
                normalized.append(values)
                elements.append((section, kind, values))
            desired[section][kind] = normalized
    if not any(desired["actions"].values()):
        raise ValueError("Mindestens eine Geräte- oder Homeegramm-Aktion ist erforderlich")
    return desired, elements


def _homeegram_query(values):
    return "&".join(quote(str(key), safe="") + "=" + quote(_number_text(value) if type(value) in (int, float) else str(value), safe="")
                    for key, value in sorted(values.items()))


def _homeegram_matches(actual, desired, expected_elements):
    if actual.get("active") != 0 or actual.get("play", 0) != 0 or unquote(str(actual.get("name", ""))) != desired["name"]:
        return False
    actual_elements = []
    for section in ("triggers", "conditions", "actions"):
        collections = actual.get(section, {})
        if not isinstance(collections, dict):
            return False
        for kind, rows in collections.items():
            if not isinstance(rows, list):
                return False
            actual_elements.extend((section, kind, row) for row in rows)
    if len(actual_elements) != len(expected_elements):
        return False
    for section, kind, values in expected_elements:
        match = next((index for index, (received_section, received_kind, received) in enumerate(actual_elements)
                      if received_section == section and received_kind == kind and isinstance(received, dict)
                      and all(received.get(key) == value for key, value in values.items())), None)
        if match is None:
            return False
        actual_elements.pop(match)
    return True


def _created_homeegram_result(homeegram, created):
    return {"homeegram_id": int(homeegram["id"]), "homeegram": json.loads(json.dumps(homeegram)),
            "created": created, "active": 0, "play": 0, "status": "created_disabled"}


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
