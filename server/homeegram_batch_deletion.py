"""Explicit, bounded HG deletion lists built on the confirmed single deleter.

There is deliberately no 'all' selector. A persisted batch pins the exact
ordered selection and each child's UUID before any destructive command. The
adapter remains responsible for private backups and never resending a DELETE.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import inspect
import json
from urllib.parse import unquote
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .homeegram_revision import homeegram_revision


MAX_BATCH_SIZE = 50
JOURNAL_SETTING = "mcp_homeegram_deletion_batches"
_HG_REFERENCES = (("triggers", "homeegram_triggers", "source_homeegram_id"),
                  ("conditions", "homeegram_conditions", "target_homeegram_id"),
                  ("actions", "homeegram_actions", "target_homeegram_id"))


class _BatchDeletionConflict(ValueError):
    def __init__(self, message, code="selection_changed"):
        super().__init__(message)
        self.code = code


def _deletion_pin(original):
    """Keep edit protection, without treating homee's order as HG identity.

    Reference rows are stored as hashes, not as another copy of private HG
    contents. A later comparison can allow their exact removal, but never an
    edit or new reference, after this batch has confirmed the referenced HG's
    deletion. The editor's shared revision deliberately remains unchanged.
    """
    value = deepcopy(original)
    value.pop("order", None)
    references = []
    for section, kind, field in _HG_REFERENCES:
        group = value.get(section)
        if not isinstance(group, dict) or not isinstance(group.get(kind), list):
            continue
        rows = []
        for row in group[kind]:
            encoded = json.dumps(row, sort_keys=True, ensure_ascii=False,
                                 allow_nan=False, separators=(",", ":")).encode()
            target = row.get(field) if isinstance(row, dict) else None
            rows.append({"revision": hashlib.sha256(encoded).hexdigest(),
                         "target_id": target if type(target) is int and target > 0 else None})
        references.append({"section": section, "kind": kind, "rows": rows})
        # Keep the collection's presence/shape protected by the envelope hash.
        group[kind] = []
    return {"revision": homeegram_revision(value), "references": references}


def _matches_deletion_pin(original, pin, deleted_ids):
    actual = _deletion_pin(original)
    if actual["revision"] != pin.get("revision"):
        return False
    expected_lists = pin.get("references", [])
    if len(actual["references"]) != len(expected_lists):
        return False
    for expected, current in zip(expected_lists, actual["references"]):
        if (expected["section"], expected["kind"]) != (current["section"], current["kind"]):
            return False
        remaining = iter(current["rows"])
        candidate = next(remaining, None)
        for row in expected["rows"]:
            if candidate is not None and row == candidate:
                candidate = next(remaining, None)
            elif row["target_id"] not in deleted_ids:
                return False
        if candidate is not None:
            return False
    return True


class HGDeletionTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    homeegram_id: int = Field(strict=True, gt=0)
    confirmation_name: str = Field(min_length=1, max_length=500)

    @field_validator("confirmation_name")
    @classmethod
    def exact_visible_name(cls, value):
        if not value.strip():
            raise ValueError("Den vollständigen dekodierten Homeegramm-Namen bestätigen")
        # Do not decode twice or strip significant spaces / literal % sequences.
        return value


class HomeegramBatchDeletionService:
    def __init__(self, database, runtime):
        self.db, self.runtime = database, runtime
        self._lock = None

    def _source(self, integration_id, expected_adapter=None):
        integration = self.db.integration(integration_id)
        if not integration or integration.get("module_id") != "homee":
            raise ValueError("homee-Integration nicht gefunden")
        if not integration.get("enabled"):
            raise ValueError("Die homee-Integration ist deaktiviert")
        adapter = self.runtime.adapters.get(integration_id)
        if adapter is None or getattr(adapter, "socket", None) is None:
            raise ConnectionError("Die homee-Serverintegration ist nicht verbunden")
        if expected_adapter is not None and adapter is not expected_adapter:
            raise ConnectionError("Die homee-Quelle wurde während des Auftrags geändert")
        if not callable(getattr(adapter, "delete_homeegram_confirmed", None)):
            raise ValueError("Das homee-Servermodul muss aktualisiert werden")
        return adapter

    async def _authorize(self, integration_id, adapter, authorize):
        self._source(integration_id, adapter)
        if authorize is not None:
            allowed = authorize(adapter)
            if inspect.isawaitable(allowed):
                allowed = await allowed
            if allowed is not True:
                raise ValueError("Homeegramme löschen ist nicht freigegeben")
        self._source(integration_id, adapter)

    @staticmethod
    def _rows(adapter):
        rows = getattr(adapter, "resources", {}).get("homeegrams")
        if not isinstance(rows, list):
            raise ValueError("homee hat keine vollständige Homeegramm-Liste geliefert")
        return rows

    @classmethod
    def _original(cls, adapter, target):
        rows = [row for row in cls._rows(adapter) if isinstance(row, dict)
                and type(row.get("id")) is int and row["id"] == target.homeegram_id]
        if len(rows) != 1:
            raise _BatchDeletionConflict("Ein ausgewähltes HG ist nicht eindeutig vorhanden; Liste erneut prüfen")
        original = rows[0]
        if unquote(str(original.get("name", ""))) != target.confirmation_name:
            raise _BatchDeletionConflict("Ein bestätigter HG-Name passt nicht zur ID; Liste erneut prüfen")
        if not all(isinstance(original.get(key), dict) for key in ("triggers", "conditions", "actions")):
            raise _BatchDeletionConflict("Ein ausgewähltes HG ist unvollständig; keine sichere Löschung möglich",
                                         "incomplete_homeegram")
        # Fail the initial preflight for any original that cannot be backed up.
        json.dumps(original, allow_nan=False)
        return original

    def _save(self, key, record):
        # Atomic read/modify/write relative to other database settings clients.
        with self.db._lock:
            journal = self.db.setting(JOURNAL_SETTING, {})
            if not isinstance(journal, dict):
                raise ValueError("Das lokale HG-Sammellöschjournal ist ungültig")
            journal[key] = deepcopy(record)
            self.db.set_setting(JOURNAL_SETTING, journal)

    @staticmethod
    def _public(record, *, replayed):
        results = deepcopy(record["results"])
        deleted = sum(row.get("deleted") is True for row in results)
        status = "completed" if deleted == len(results) else "partial" if deleted else "stopped"
        return {"integration_id": record["integration_id"], "request_id": record["request_id"],
                "status": status, "deleted_count": deleted, "total_count": len(results),
                "replayed": replayed, "results": results}

    async def delete(self, integration_id, homeegrams, request_id, authorize=None):
        try:
            if not isinstance(request_id, str):
                raise ValueError()
            key = str(uuid.UUID(request_id))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("request_id muss eine UUID sein") from None
        if not isinstance(homeegrams, list) or not 1 <= len(homeegrams) <= MAX_BATCH_SIZE:
            raise ValueError("Eine ausdrückliche Liste mit 1 bis 50 Homeegrammen ist erforderlich")
        targets = [HGDeletionTarget.model_validate(item) for item in homeegrams]
        if len({item.homeegram_id for item in targets}) != len(targets):
            raise ValueError("Eine Homeegramm-ID darf in der Auswahl nur einmal vorkommen")
        selection = [target.model_dump() for target in targets]
        fingerprint = hashlib.sha256(json.dumps([integration_id, selection], ensure_ascii=False,
                                                sort_keys=True).encode()).hexdigest()
        if self._lock is None:
            self._lock = asyncio.Lock()
        # Sequential execution also avoids overwhelming the homee websocket.
        async with self._lock:
            adapter = self._source(integration_id)
            await self._authorize(integration_id, adapter, authorize)
            journal = self.db.setting(JOURNAL_SETTING, {})
            if not isinstance(journal, dict):
                raise ValueError("Das lokale HG-Sammellöschjournal ist ungültig")
            previous = journal.get(key)
            if previous is not None:
                if not isinstance(previous, dict) or previous.get("fingerprint") != fingerprint:
                    raise ValueError("Diese Anfrage-ID gehört zu einer anderen HG-Auswahl; die ursprüngliche Liste beibehalten")
                record = deepcopy(previous)
                if all(row.get("deleted") is True for row in record["results"]):
                    return self._public(record, replayed=True)
            else:
                if len(journal) >= 1000:
                    raise ValueError("Das lokale HG-Sammellöschjournal ist voll; keine weitere Sammellöschung möglich")
                refresh = getattr(adapter, "_refresh_homeegram_snapshot", None)
                if not callable(refresh):
                    raise ValueError("Das homee-Servermodul muss aktualisiert werden")
                await refresh(adapter.socket)
                await self._authorize(integration_id, adapter, authorize)
                # Validate ALL names, identities and backup shapes before the
                # first DELETE. A typo in item 50 must not delete items 1–49.
                originals = [self._original(adapter, target) for target in targets]
                children = [str(uuid.uuid5(uuid.UUID(key), "homeegram:" + str(target.homeegram_id)))
                            for target in targets]
                record = {"request_id": key, "integration_id": integration_id, "fingerprint": fingerprint,
                          "created_at": datetime.now(timezone.utc).isoformat(),
                          "targets": [{**target.model_dump(), "revision": homeegram_revision(original),
                                       "deletion_pin": _deletion_pin(original)}
                                      for target, original in zip(targets, originals)],
                          "results": [{"homeegram_id": target.homeegram_id, "name": target.confirmation_name,
                                       "request_id": child, "deleted": False, "status": "not_attempted"}
                                      for target, child in zip(targets, children)]}
                self._save(key, record)

            for index, target in enumerate(targets):
                row = record["results"][index]
                if row.get("deleted") is True:
                    continue
                child_id = row["request_id"]
                # Capture this BEFORE invoking the adapter. For a new child,
                # check its pinned identity again after the adapter's fresh GET
                # and immediately before sending. Retried children only confirm
                # their existing journal entry, including an already absent HG.
                child_journal = getattr(adapter, "homeegram_deletions", {})
                reserved_before = isinstance(child_journal, dict) and child_id in child_journal

                async def authorized_child(target=target, index=index, reserved_before=reserved_before):
                    await self._authorize(integration_id, adapter, authorize)
                    if not reserved_before:
                        original = self._original(adapter, target)
                        pinned = record["targets"][index]
                        deleted_ids = {item["homeegram_id"] for item in record["results"][:index]
                                       if item.get("deleted") is True}
                        if "deletion_pin" in pinned:
                            matches = _matches_deletion_pin(original, pinned["deletion_pin"], deleted_ids)
                        else:
                            # Old journals contain only a hash. They cannot be
                            # safely re-pinned from possibly modified live data.
                            matches = homeegram_revision(original) == pinned["revision"]
                        if not matches:
                            raise _BatchDeletionConflict("Ein ausgewähltes HG wurde seit der Vorprüfung geändert; nicht gelöscht")
                    return True

                row.update(status="pending", deleted=False)
                row.pop("error", None)
                row.pop("error_code", None)
                self._save(key, record)
                try:
                    if not reserved_before:
                        # DELETE may return a metadata-only list of the other
                        # HGs. Refresh before calling any content-aware guard,
                        # including the adapter's initial authorize callback.
                        await self._authorize(integration_id, adapter, authorize)
                        await adapter._refresh_homeegram_snapshot(adapter.socket)
                    await authorized_child()
                    result = await adapter.delete_homeegram_confirmed(
                        target.homeegram_id, target.confirmation_name, child_id, authorize=authorized_child)
                    if (not isinstance(result, dict) or result.get("deleted") is not True
                            or result.get("status") != "deleted"
                            or result.get("homeegram_id") != target.homeegram_id
                            or result.get("name") != target.confirmation_name):
                        raise ValueError("Die Einzellöschung wurde nicht eindeutig bestätigt")
                    # Never expose original backup contents or arbitrary errors.
                    row.update(deleted=True, status="deleted", backup_saved=result.get("backup_saved") is True,
                               replayed=result.get("replayed") is True)
                except Exception as error:
                    code = (error.code if isinstance(error, _BatchDeletionConflict) else
                            "confirmation_timeout" if isinstance(error, TimeoutError) else
                            "connection_unavailable" if isinstance(error, ConnectionError) else
                            "deletion_not_confirmed")
                    reason = {
                        "selection_changed": "HG fehlt, wurde umbenannt oder seit der Vorprüfung inhaltlich geändert; nicht gelöscht.",
                        "incomplete_homeegram": "homee hat kein vollständiges HG für die sichere Vorprüfung geliefert.",
                        "confirmation_timeout": "homee hat die Löschung nicht rechtzeitig eindeutig bestätigt.",
                        "connection_unavailable": "Die homee-Verbindung oder Quelle ist nicht mehr verfügbar.",
                    }.get(code, "Löschung abgelehnt oder nicht eindeutig bestätigt.")
                    row.update(status="uncertain", deleted=False,
                               error_code=code,
                               error=reason + " Zustand prüfen; dieselbe Sammelanfrage-ID und unveränderte Auswahl verwenden.")
                    self._save(key, record)
                    break
                self._save(key, record)
            return self._public(record, replayed=previous is not None)
