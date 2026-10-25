"""Typed MCP access to the real, server-owned SHB automation engine.

Configuration is never a test run: creation persists a disabled rule, and the
separately authorized activation tool only changes its enabled flag. Device
commands occur later through the existing engine, not through this service.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import re
from typing import Annotated, Literal
from urllib.parse import unquote
from uuid import UUID, NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator

from .automation_validation import validate_web_automation
from .automations import AutomationEngine
from .push import PushService


Number = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Seconds = Annotated[float, Field(strict=True, ge=0, le=86400, allow_inf_nan=False)]
Identifier = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._:-]+$")]
NodeID = Annotated[int, Field(strict=True, ge=-(2**63), lt=2**63)]
Comparison = Literal["equal", "notEqual", "greater", "less"]


class NativeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ConditionBase(NativeInput):
    id: Identifier | None = None
    holdSeconds: Seconds = 0


class AttributeCondition(ConditionBase):
    kind: Literal["attribute"]
    nodeID: NodeID
    attributeID: NodeID
    comparison: Comparison = "equal"
    value: Number


class AttributeChangedTrigger(ConditionBase):
    kind: Literal["attributeChanged"]
    nodeID: NodeID
    attributeID: NodeID


class AttributeChangedByTrigger(ConditionBase):
    kind: Literal["attributeChangedBy"]
    nodeID: NodeID
    attributeID: NodeID
    comparison: Comparison = "greater"
    value: Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
    changeUnit: Literal["absolute", "percent"] = "absolute"


class GroupAttributeCondition(ConditionBase):
    kind: Literal["groupAttribute"]
    groupID: Identifier
    attributeType: Annotated[int, Field(strict=True, ge=0, le=2147483647)]
    attributeName: Annotated[str, Field(max_length=120)] = ""
    aggregation: Literal["any", "none", "all", "atLeast", "atMost", "exactly"] = "any"
    count: Annotated[int, Field(strict=True, ge=0, le=100000)] = 1
    comparison: Comparison = "equal"
    value: Number


class DailyTrigger(ConditionBase):
    kind: Literal["timeDaily"]
    minuteOfDay: Annotated[int, Field(strict=True, ge=0, le=1439)]


class OnceTrigger(ConditionBase):
    kind: Literal["timeOnce"]
    scheduledAt: Annotated[str, Field(min_length=16, max_length=64)]

    @field_validator("scheduledAt")
    @classmethod
    def aware_time(cls, value):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("scheduledAt muss ein ISO-8601-Zeitpunkt sein") from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("scheduledAt benötigt eine explizite Zeitzone, z. B. +02:00 oder Z")
        if parsed.utcoffset().total_seconds() % 60:
            raise ValueError("scheduledAt benötigt eine Zeitzone mit ganzen Minuten")
        # The app's ISO8601DateFormatter expects Internet date-time syntax,
        # unlike Python, which also accepts spaces/compact dates on input.
        return parsed.isoformat()


class AfterCondition(ConditionBase):
    kind: Literal["timeAfter"]
    minuteOfDay: Annotated[int, Field(strict=True, ge=0, le=1439)]


class BeforeCondition(ConditionBase):
    kind: Literal["timeBefore"]
    minuteOfDay: Annotated[int, Field(strict=True, ge=0, le=1439)]


TriggerInput = Annotated[
    AttributeCondition | AttributeChangedTrigger | AttributeChangedByTrigger
    | GroupAttributeCondition | DailyTrigger | OnceTrigger,
    Field(discriminator="kind"),
]
ConditionInput = Annotated[
    AttributeCondition | GroupAttributeCondition | AfterCondition | BeforeCondition,
    Field(discriminator="kind"),
]


class ActionBase(NativeInput):
    id: Identifier | None = None
    delaySeconds: Seconds = 0


class SetAttributeAction(ActionBase):
    kind: Literal["setAttribute"]
    nodeID: NodeID
    attributeID: NodeID
    value: Number


class ToggleAttributeAction(ActionBase):
    kind: Literal["toggleAttribute"]
    nodeID: NodeID
    attributeID: NodeID


class RoborockAction(ActionBase):
    kind: Literal["roborockCleaning"]
    nodeID: NodeID
    roborockTarget: Literal["complete", "room", "routine", "spot"] = "complete"
    roborockTargetValue: Number = -1
    roborockCleaningType: Number | None = None
    roborockSuction: Number | None = None
    roborockWater: Number | None = None


class SonosAction(ActionBase):
    kind: Literal["sonosSpeak"]
    nodeID: NodeID
    sonosText: Annotated[str, Field(min_length=1, max_length=500)]
    sonosVolume: Annotated[float, Field(strict=True, ge=0, le=100, allow_inf_nan=False)] | None = None
    sonosRestorePlayback: Annotated[bool, Field(strict=True)] = True


class ControlAutomationAction(ActionBase):
    kind: Literal["controlAutomation"]
    automationID: Identifier
    automationOperation: Literal["play", "stop", "enable", "disable"] = "play"


class PushAction(ActionBase):
    kind: Literal["serverPushNotification"]
    title: Annotated[str, Field(min_length=1, max_length=180)] = "SmartHomeBoard"
    message: Annotated[str, Field(min_length=1, max_length=1500)] = "Automation wurde ausgeführt."
    includeAttributeValue: Annotated[bool, Field(strict=True)] = False
    pushDeviceIDs: Annotated[list[Annotated[str, Field(min_length=1, max_length=200)]], Field(max_length=50)] = Field(default_factory=list)
    nodeID: NodeID | None = None
    attributeID: NodeID | None = None

    @model_validator(mode="after")
    def selected_value(self):
        if self.includeAttributeValue and (self.nodeID is None or self.attributeID is None):
            raise ValueError("includeAttributeValue benötigt nodeID und attributeID")
        if not self.includeAttributeValue and (self.nodeID is not None or self.attributeID is not None):
            raise ValueError("nodeID/attributeID sind nur mit includeAttributeValue zulässig")
        if len(self.pushDeviceIDs) != len(set(self.pushDeviceIDs)):
            raise ValueError("Push-Empfänger dürfen nicht doppelt vorkommen")
        return self


ActionInput = Annotated[
    SetAttributeAction | ToggleAttributeAction | RoborockAction | SonosAction
    | ControlAutomationAction | PushAction,
    Field(discriminator="kind"),
]

TRIGGER_KINDS = ("attribute", "attributeChanged", "attributeChangedBy", "groupAttribute", "timeDaily", "timeOnce")
CONDITION_KINDS = ("attribute", "groupAttribute", "timeAfter", "timeBefore")
ACTION_KINDS = ("setAttribute", "toggleAttribute", "roborockCleaning", "sonosSpeak", "controlAutomation", "serverPushNotification")
CONDITION_SECTIONS = ("triggers", "prerequisites", "conditions", "elseConditions")
ACTION_SECTIONS = ("alwaysActions", "actions", "directElseActions", "elseActions", "elseElseActions")
SECTIONS = CONDITION_SECTIONS + ACTION_SECTIONS


class ServerAutomationInput(NativeInput):
    """Complete native engine definition. It is always created disabled.

    Node/attribute IDs are SHB server IDs, not native homee IDs. No ID or
    isEnabled root field is accepted; activation uses its separate tool/grant.
    Triggers are OR, all conditions within a section are AND. No trigger is
    required for rules called later by another automation.
    """
    name: Annotated[str, Field(min_length=1, max_length=120)]
    triggers: Annotated[list[TriggerInput], Field(max_length=100)] = Field(default_factory=list)
    prerequisites: Annotated[list[ConditionInput], Field(max_length=100)] = Field(default_factory=list)
    conditions: Annotated[list[ConditionInput], Field(max_length=100)] = Field(default_factory=list)
    elseConditions: Annotated[list[ConditionInput], Field(max_length=100)] = Field(default_factory=list)
    alwaysActions: Annotated[list[ActionInput], Field(max_length=100)] = Field(default_factory=list)
    actions: Annotated[list[ActionInput], Field(max_length=100)] = Field(default_factory=list)
    directElseActions: Annotated[list[ActionInput], Field(max_length=100)] = Field(default_factory=list)
    elseActions: Annotated[list[ActionInput], Field(max_length=100)] = Field(default_factory=list)
    elseElseActions: Annotated[list[ActionInput], Field(max_length=100)] = Field(default_factory=list)
    cooldownSeconds: Seconds = 30
    conditionValidation: Literal["triggerTime", "executionTime", "both"] = "triggerTime"

    @model_validator(mode="after")
    def coherent_rule(self):
        if not any(getattr(self, key) for key in ACTION_SECTIONS):
            raise ValueError("Mindestens eine Aktion ist erforderlich")
        if any(item.holdSeconds > 0 and item.kind not in ("attribute", "groupAttribute") for item in self.triggers):
            raise ValueError("holdSeconds wird bei Auslösern nur für attribute und groupAttribute unterstützt")
        ids = [item.id for section in SECTIONS for item in getattr(self, section) if item.id]
        if len(ids) != len(set(ids)):
            raise ValueError("Auslöser-, Bedingungs- und Aktions-IDs müssen eindeutig sein")
        return self


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _request(value):
    if not isinstance(value, str):
        raise ValueError("request_id muss eine UUID sein")
    try:
        return str(UUID(value))
    except ValueError:
        raise ValueError("request_id muss eine UUID sein") from None


class ServerAutomationMCP:
    MAX_RECORDS = 100000

    def __init__(self, database, runtime, allowed_callable):
        self.db = database
        self.runtime = runtime
        self.allowed = allowed_callable
        self._lock = asyncio.Lock()

    def _engine(self):
        engine = getattr(self.runtime, "automation_engine", None)
        if engine is None:
            raise ValueError("Der Server-Automationsdienst ist nicht verfügbar")
        engine.refresh_from_database()
        return engine

    def _require(self, permission):
        if not self.allowed(permission):
            raise ValueError("Diese Serverautomations-Aktion ist nicht freigegeben: " + permission)

    @contextmanager
    def _transaction(self):
        try:
            with self.db.settings_transaction():
                yield
        except BaseException:
            # Engine writes mutate memory before persisting. Rollback must also
            # reload that state, even when the old synced_at value was None.
            engine = getattr(self.runtime, "automation_engine", None)
            if engine is not None:
                engine.synced_at = object()
                engine.refresh_from_database()
            raise

    def _nodes(self):
        result = []
        for node in self.runtime.visible_nodes():
            integration_id = str(node.get("integration_id", ""))
            integration = self.db.integration(integration_id)
            if (integration and integration.get("enabled")
                    and self.runtime.adapters.get(integration_id) is not None
                    and node.get("integration_source", "server") == "server"
                    and node.get("integration_module", integration["module_id"]) == integration["module_id"]):
                result.append(node)
        return result

    def _push(self):
        engine = self._engine()
        service = getattr(engine, "push_service", None)
        if service is not None:
            status = service.status()
            return {"configured": bool(status.get("configured")), "devices": [
                {"id": str(item["id"]), "name": str(item.get("name") or "iPhone/iPad")[:120]}
                for item in status.get("devices", []) if item.get("id")
            ]}
        # Same stable IDs as PushService, never expose APNs tokens/configuration.
        return {"configured": False, "devices": [
            {"id": PushService._device_id(item), "name": str(item.get("device_name") or "iPhone/iPad")[:120]}
            for item in self.db.setting("push_devices", []) or []
        ]}

    def _groups(self, nodes=None):
        nodes = self._nodes() if nodes is None else nodes
        node_map = {int(node["id"]): node for node in nodes}
        result = []
        for group in self.db.setting("device_groups", []) or []:
            members = []
            for node_id in group.get("node_ids", []):
                node = node_map.get(node_id)
                members.append({"id": node_id, "name": unquote(str(node.get("name", ""))) if node else "Nicht verfügbar",
                                "selectable": node is not None})
            result.append({"id": str(group.get("id", "")), "name": str(group.get("name", ""))[:120],
                           "members": members, "selectable": bool(members) and all(item["selectable"] for item in members)})
        return result

    def capabilities(self):
        engine = self._engine()
        return {
            "schema_version": 1, "execution_location": "server", "creation_always_disabled": True,
            "trigger_kinds": list(TRIGGER_KINDS), "condition_kinds": list(CONDITION_KINDS),
            "action_kinds": list(ACTION_KINDS), "native_schema": ServerAutomationInput.model_json_schema(),
            "permissions": {key: bool(self.allowed(key)) for key in (
                "allow_server_automation_creation", "allow_server_automation_activation")},
            "timezone": str(engine.timezone), "ids": "SHB server node/attribute IDs, not native homee IDs",
            "device_catalog_tool": "list_devices", "groups": self._groups(), "push": self._push(),
            "branches": {
                "triggers": "OR; no trigger is allowed for a rule invoked by another automation",
                "prerequisites": "AND; false blocks every action including alwaysActions",
                "alwaysActions": "Runs only after prerequisites pass, regardless of the conditions branch",
                "conditions": "AND; true selects actions, false selects directElseActions and the else branch",
                "actions": "DANN / true branch; also used if conditions is empty",
                "directElseActions": "Runs whenever conditions is false, independent of elseConditions",
                "elseConditions": "AND; if present, true selects elseActions and false selects elseElseActions",
                "elseActions": "SONST-WENN true, or SONST when elseConditions is empty",
                "elseElseActions": "SONST-WENN false",
            },
            "not_supported": ["iOS-only devices/actions", "smartwidget triggers", "arbitrary scripts", "direct MCP play/test/delete"],
            "runtime_limits": {
                "trigger_holdSeconds": "Positive holds only for attribute and groupAttribute; changed/time triggers require 0",
                "condition_holdSeconds": "0..86400 seconds; checked by the native engine when evaluating the condition",
                "time_triggers": "Daily clock time or one ISO-8601 timestamp with timezone; no RRULE/week/month recurrence",
                "conditionValidation": "triggerTime, executionTime or both, with existing native per-action semantics",
            },
        }

    @staticmethod
    def _safe_rule(rule):
        # A strict allowlist protects legacy/app rules with private extension
        # payloads. Unsupported kinds remain visible as explicit placeholders.
        fields = {
            "attribute": {"nodeID", "attributeID", "comparison", "value"},
            "attributeChanged": {"nodeID", "attributeID"},
            "attributeChangedBy": {"nodeID", "attributeID", "comparison", "value", "changeUnit"},
            "groupAttribute": {"groupID", "attributeType", "attributeName", "aggregation", "count", "comparison", "value"},
            "timeDaily": {"minuteOfDay"}, "timeOnce": {"minuteOfDay", "scheduledAt", "isConsumed"},
            "timeAfter": {"minuteOfDay"}, "timeBefore": {"minuteOfDay"},
            "setAttribute": {"nodeID", "attributeID", "value"}, "toggleAttribute": {"nodeID", "attributeID"},
            "roborockCleaning": {"nodeID", "roborockTarget", "roborockTargetValue", "roborockCleaningType", "roborockSuction", "roborockWater"},
            "sonosSpeak": {"nodeID", "sonosText", "sonosVolume", "sonosRestorePlayback"},
            "controlAutomation": {"automationID", "automationOperation"},
            "serverPushNotification": {"title", "message", "includeAttributeValue", "pushDeviceIDs", "nodeID", "attributeID"},
        }
        result = {key: deepcopy(rule[key]) for key in ("id", "name", "isEnabled", "cooldownSeconds", "conditionValidation") if key in rule}
        warnings = []
        for section in SECTIONS:
            result[section] = []
            allowed_kinds = TRIGGER_KINDS if section == "triggers" else CONDITION_KINDS if section in CONDITION_SECTIONS else ACTION_KINDS
            for item in rule.get(section, []) or []:
                if not isinstance(item, dict):
                    warnings.append(f"Ungültiges Element in {section}")
                    continue
                kind = item.get("kind", "")
                allowed = {"id", "kind"} | (fields.get(kind, set()) if kind in allowed_kinds else set())
                allowed.add("holdSeconds" if section in CONDITION_SECTIONS else "delaySeconds")
                result[section].append({key: deepcopy(item[key]) for key in allowed if key in item})
                if kind not in allowed_kinds:
                    warnings.append(f"{section}: {kind} ist nicht serverseitig unterstützt")
        return result, warnings

    def _find(self, automation_id):
        if not isinstance(automation_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", automation_id):
            raise ValueError("Die Automations-ID ist ungültig")
        engine = self._engine()
        matches = [rule for rule in engine.rules if str(rule.get("id", "")) == automation_id]
        if len(matches) != 1:
            raise ValueError("Automation wurde auf dem Server nicht eindeutig gefunden")
        return engine, matches[0]

    def get(self, automation_id):
        engine, rule = self._find(automation_id)
        safe, warnings = self._safe_rule(rule)
        return {"automation": safe, "origin": "server" if automation_id in engine.server_owned_ids else "app",
                "revision": _digest(rule), "cached": True, "warnings": warnings}

    def list(self, offset=0, limit=50, query=""):
        engine = self._engine()
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("offset muss positiv und limit zwischen 1 und 100 sein")
        if not isinstance(query, str) or len(query) > 120:
            raise ValueError("Der Suchtext ist ungültig")
        needle = query.strip().casefold()
        rows = [rule for rule in engine.rules if not needle or needle in str(rule.get("name", "")).casefold() or needle in str(rule.get("id", "")).casefold()]
        items = []
        for rule in rows[offset:offset + limit]:
            rule_id = str(rule.get("id", ""))
            enabled = bool(rule.get("isEnabled", True))
            items.append({"id": rule_id, "name": str(rule.get("name", "Automation")), "isEnabled": enabled,
                          "enabled": enabled, "origin": "server" if rule_id in engine.server_owned_ids else "app",
                          "revision": _digest(rule), "trigger_count": len(rule.get("triggers", [])),
                          "condition_count": sum(len(rule.get(key, [])) for key in CONDITION_SECTIONS if key != "triggers"),
                          "action_count": sum(len(rule.get(key, [])) for key in ACTION_SECTIONS)})
        return {"automations": items, "total": len(rows), "offset": offset, "limit": limit,
                "next_offset": offset + limit if offset + limit < len(rows) else None, "cached": True}

    def _validate(self, model, rule_id):
        nodes = self._nodes()
        source = model.model_dump(exclude_none=True)
        source.update(id=rule_id, isEnabled=False)
        # IDs are stable even if a process dies before persisting the rule.
        for section in SECTIONS:
            for index, item in enumerate(source[section]):
                item.setdefault("id", str(uuid5(UUID(rule_id), f"{section}:{index}")))
        rule = validate_web_automation(source, nodes, self.db)
        self._validate_references(rule, nodes)
        return rule

    def _validate_references(self, rule, nodes=None):
        nodes = self._nodes() if nodes is None else nodes
        node_map = {int(node["id"]): node for node in nodes}
        groups = {str(group.get("id")): group for group in self.db.setting("device_groups", []) or []}
        existing = {str(item.get("id", "")) for item in self._engine().rules}
        recipient_ids = {item["id"] for item in self._push()["devices"]}
        for section in SECTIONS:
            for item in rule.get(section, []) or []:
                kind = item["kind"]
                if kind == "groupAttribute":
                    group = groups.get(item["groupID"])
                    if not group or not group.get("node_ids"):
                        raise ValueError("Die Gerätegruppe ist nicht verfügbar oder leer")
                    if any(member not in node_map for member in group["node_ids"]):
                        raise ValueError("Die Gerätegruppe enthält versteckte oder nicht aktive Servergeräte")
                    if not any(any(attribute.get("type") == item["attributeType"] for attribute in node_map[member].get("attributes", [])) for member in group["node_ids"]):
                        raise ValueError("Das ausgewählte Gruppenattribut ist in keinem Mitglied vorhanden")
                if kind == "controlAutomation":
                    if item["automationID"] == rule["id"] or item["automationID"] not in existing:
                        raise ValueError("Die Zielautomation fehlt oder ist dieselbe Automation")
                if kind == "serverPushNotification":
                    if any(value not in recipient_ids for value in item.get("pushDeviceIDs", [])):
                        raise ValueError("Ein ausgewählter Push-Empfänger ist nicht mehr registriert")
                if "nodeID" not in item:
                    continue
                node = node_map.get(item["nodeID"])
                if node is None:
                    raise ValueError("Das Gerät ist kein sichtbares Gerät eines aktiven Servermoduls")
                if kind == "roborockCleaning" and node.get("integration_module") != "roborock":
                    raise ValueError("Das Gerät ist kein aktives Roborock-Servergerät")
                if kind == "sonosSpeak" and node.get("integration_module") != "sonos":
                    raise ValueError("Das Gerät ist kein aktives Sonos-Servergerät")
                if "attributeID" in item:
                    attribute = next((a for a in node.get("attributes", []) if a.get("id") == item["attributeID"]), None)
                    if attribute is None:
                        raise ValueError("Das Attribut gehört nicht zu diesem Servergerät")
                    if kind in ("setAttribute", "toggleAttribute"):
                        if not attribute.get("editable", False):
                            raise ValueError("Das Attribut ist nicht steuerbar")
                        if kind == "toggleAttribute" and not AutomationEngine._is_toggleable(attribute):
                            raise ValueError("Umschalten erfordert ein binäres, schreibbares Attribut")
                        if kind == "setAttribute":
                            for key, comparison in (("minimum", lambda a, b: a < b), ("maximum", lambda a, b: a > b)):
                                if attribute.get(key) is not None and comparison(item["value"], float(attribute[key])):
                                    raise ValueError("Der Zielwert liegt außerhalb des Attributbereichs")

    @staticmethod
    def _journal_key(request_id):
        return "mcp_server_automation_mutation:" + request_id

    def _reserve(self, key, record):
        # Unique settings keys give concurrent MCP service instances/processes
        # one durable winner. An unresolved request is never blindly replayed.
        with self.db._lock:
            count = self.db._connection.execute(
                "SELECT COUNT(*) FROM settings WHERE key LIKE 'mcp_server_automation_mutation:%'"
            ).fetchone()[0]
            if count >= self.MAX_RECORDS:
                raise ValueError(f"MCP-Automationsjournal ist voll ({self.MAX_RECORDS} Anfragen); alte Anfrage-IDs werden nicht verworfen")
            inserted = self.db._connection.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO NOTHING",
                (key, json.dumps(record)),
            ).rowcount
        return bool(inserted)

    def _replay(self, key, fingerprint):
        record = self.db.setting(key)
        if not record:
            return None
        if record.get("fingerprint") != fingerprint:
            raise ValueError("request_id wurde bereits für einen anderen Automationsauftrag verwendet")
        if record.get("status") != "complete":
            # A crash after persist but before journal completion is recoverable
            # by observing the exact intended definition, without writing again.
            try:
                current = self.get(record["automation_id"])
            except ValueError:
                current = None
            intended = record.get("intended_rule")
            same = current and intended and current["origin"] == "server"
            if same:
                actual = deepcopy(current["automation"])
                expected = deepcopy(intended)
                if record["operation"] == "create":
                    actual.pop("isEnabled", None)
                    expected.pop("isEnabled", None)
                same = actual == expected
            if not same:
                raise ValueError("Der frühere Automationsauftrag ist unbestätigt; Zustand erst prüfen, nicht blind erneut senden")
            result = self._result(record["operation"], intended, record["request_id"])
            record.update(status="complete", result=result)
            self.db.set_setting(key, record)
        result = deepcopy(record["result"])
        result.update(replayed=True, result_is_historical=True)
        try:
            current = self.get(record["automation_id"])
            result["current"] = {"present": True, "active": bool(current["automation"].get("isEnabled", True)), "revision": current["revision"]}
        except ValueError:
            result["current"] = {"present": False}
        return result

    @staticmethod
    def _result(operation, rule, request_id, changed=True):
        safe, warnings = ServerAutomationMCP._safe_rule(rule)
        return {"status": "created" if operation == "create" else "updated" if changed else "unchanged",
                "automation_id": str(rule["id"]), "id": str(rule["id"]), "name": rule["name"],
                "active": bool(rule.get("isEnabled", True)), "automation": safe, "origin": "server",
                "revision": _digest(rule), "request_id": request_id, "replayed": False, "warnings": warnings}

    async def create(self, automation: ServerAutomationInput, request_id: str):
        permission = "allow_server_automation_creation"
        self._require(permission)
        if not isinstance(automation, ServerAutomationInput):
            automation = ServerAutomationInput.model_validate(automation)
        request_id = _request(request_id)
        fingerprint = _digest({"operation": "create", "automation": automation.model_dump(exclude_none=True)})
        key = self._journal_key(request_id)
        async with self._lock:
            with self._transaction():
                self._require(permission)
                replay = self._replay(key, fingerprint)
                if replay is not None:
                    return replay
                rule_id = str(uuid5(NAMESPACE_URL, "https://smarthomeboard.local/mcp/server-automation/" + request_id))
                engine = self._engine()
                if rule_id in engine.deleted_ids or any(str(item.get("id")) == rule_id for item in engine.rules):
                    raise ValueError("Die neue Automations-ID existiert bereits oder wurde gelöscht")
                rule = self._validate(automation, rule_id)
                record = {"operation": "create", "status": "pending", "request_id": request_id,
                          "fingerprint": fingerprint, "automation_id": rule_id, "intended_rule": rule}
                if not self._reserve(key, record):
                    return self._replay(key, fingerprint)
                self._require(permission)
                engine.upsert_server(deepcopy(rule))
                observed = self.get(rule_id)
                if observed["origin"] != "server" or observed["revision"] != _digest(rule):
                    raise ValueError("Die neue Serverautomation konnte nicht bestätigt werden")
                result = self._result("create", rule, request_id)
                record.update(status="complete", result=result)
                self.db.set_setting(key, record)
                return result

    async def set_active(self, automation_id, confirmation_name, active, expected_revision, request_id):
        permission = "allow_server_automation_activation"
        self._require(permission)
        if type(active) is not bool:
            raise ValueError("active muss ein boolescher Wert sein")
        if not isinstance(expected_revision, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_revision):
            raise ValueError("Die vollständige aktuelle revision ist erforderlich")
        if not isinstance(confirmation_name, str) or not 1 <= len(confirmation_name) <= 120:
            raise ValueError("Der genaue Bestätigungsname ist erforderlich")
        request_id = _request(request_id)
        fingerprint = _digest({"operation": "set_active", "automation_id": automation_id,
                               "confirmation_name": confirmation_name, "active": active, "revision": expected_revision})
        key = self._journal_key(request_id)
        async with self._lock:
            with self._transaction():
                self._require(permission)
                replay = self._replay(key, fingerprint)
                if replay is not None:
                    return replay
                engine, original = self._find(automation_id)
                if automation_id not in engine.server_owned_ids:
                    raise ValueError("Nur echte Serverautomationen dürfen hier aktiviert oder deaktiviert werden; diese Regel stammt aus der App")
                if original.get("name") != confirmation_name:
                    raise ValueError("Der Bestätigungsname stimmt nicht mit der Automation überein")
                if _digest(original) != expected_revision:
                    raise ValueError("Die Automation wurde geändert; erneut laden und bestätigen")
                if active:
                    # Reject stale or app-only configurations before enabling them.
                    model_source = {key: value for key, value in original.items() if key not in {"id", "isEnabled"}}
                    check = deepcopy(model_source)
                    for item in check.get("triggers", []):
                        if item.get("kind") == "timeOnce":
                            item.pop("isConsumed", None)
                            item.pop("minuteOfDay", None)
                    ServerAutomationInput.model_validate(check)
                    self._validate_references(original)
                intended = deepcopy(original)
                changed = bool(original.get("isEnabled", True)) != active
                intended["isEnabled"] = active
                record = {"operation": "set_active", "status": "pending", "request_id": request_id,
                          "fingerprint": fingerprint, "automation_id": automation_id, "intended_rule": intended}
                if not self._reserve(key, record):
                    return self._replay(key, fingerprint)
                self._require(permission)
                if changed or not active:
                    # Disabling also stops a stray pending wait when the flag
                    # was already false. Replayed requests return above and
                    # never repeat this control operation.
                    response = await engine.control(automation_id, "enable" if active else "disable")
                    if not response.get("ok"):
                        raise ValueError("Der Server konnte den Aktivierungszustand nicht ändern")
                observed = self.get(automation_id)
                if observed["origin"] != "server" or observed["revision"] != _digest(intended):
                    raise ValueError("Die Aktivierung/Deaktivierung konnte nicht bestätigt werden")
                result = self._result("set_active", intended, request_id, changed)
                record.update(status="complete", result=result)
                self.db.set_setting(key, record)
                return result
