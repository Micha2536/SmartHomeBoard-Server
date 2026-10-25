"""Portable MCP Apps view and durable, jointly editable Homeegram workspaces.

The iframe never receives credentials. It uses the authenticated host bridge to
call the same tools as the assistant. Only the explicitly named deployment tool
can send a new, disabled Homeegram to homee; edits here only update local drafts.
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import unquote

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from .homeegram_groups import expand_group_element
from .homeegram_references import (ACTION_EVENTS, TRIGGER_EVENTS, HomeegramAction,
                                   HomeegramTrigger, expand_homeegram_reference)


RESOURCE_URI = "ui://shb/homeegram/v1.html"
RESOURCE_MIME = "text/html;profile=mcp-app"


class WorkspaceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: int = Field(strict=True)
    attribute_id: int = Field(strict=True)
    value: float = Field(allow_inf_nan=False)


class WorkspaceTrigger(WorkspaceReference):
    comparison: Literal["equal", "greater", "less", "changed"] = "equal"


class WorkspaceCondition(WorkspaceReference):
    comparison: Literal["equal", "notEqual", "greater", "less", "greaterOrEqual", "lessOrEqual"] = "equal"
    branch: Literal[0, 1] = 1
    check_moment: Literal[1, 2, 3] = 1


class WorkspaceAction(WorkspaceReference):
    operation: Literal["set", "toggle"] = "set"
    branch: Literal[0, 1, 2] = 1
    delay: float = Field(default=0, ge=0, le=604800, allow_inf_nan=False)


class WorkspaceDraft(BaseModel):
    """An unfinished local draft may intentionally have no action yet."""
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    manual_play: bool = True
    triggers: list[WorkspaceTrigger | HomeegramTrigger] = Field(default_factory=list, max_length=40)
    conditions: list[WorkspaceCondition] = Field(default_factory=list, max_length=40)
    actions: list[WorkspaceAction | HomeegramAction] = Field(default_factory=list, max_length=40)


VALUE_LABELS = {
    1: {"0": "Aus", "1": "An"},
    10: {"0": "Geschlossen", "1": "Offen", "2": "Gekippt"},
    14: {"0": "Geschlossen", "1": "Offen"},
    25: {"0": "Keine Bewegung", "1": "Bewegung erkannt"},
    76: {"0": "Keine Bewegung", "1": "Bewegung erkannt"},
    135: {"0": "Geöffnet", "1": "Geschlossen", "2": "Gestoppt", "3": "Öffnet", "4": "Schließt"},
    205: {"0": "Zuhause", "1": "Schlafend", "2": "Abwesend", "3": "Urlaub"},
}
WEATHER_LABELS = dict(enumerate(("Sonnig", "Klar", "Regen", "Schnee", "Schneeregen", "Windig", "Nebel",
                               "Bewölkt", "Teilweise bewölkt (Tag)", "Teilweise bewölkt (Nacht)",
                               "Hagel", "Gewitter", "Tornado")))
for _weather_type in (243, 246):
    VALUE_LABELS[_weather_type] = {str(key): value for key, value in WEATHER_LABELS.items()}


def _request_id(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("request_id muss eine UUID sein") from None


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _public_homeegram(homeegram, *, decode_name=True, adapter=None):
    """Keep shapes and placeholders, but never expose private special payloads."""
    from .mcp_access import SAFE_ELEMENT
    result = {key: homeegram[key] for key in ("id", "active", "play") if key in homeegram}
    name = str(homeegram.get("name", ""))
    result["name"] = unquote(name) if decode_name else name
    warnings = []
    for section in ("triggers", "conditions", "actions"):
        result[section] = {}
        groups = homeegram.get(section) or {}
        if not isinstance(groups, dict):
            warnings.append("Ungültige Elementgruppe: " + section)
            continue
        for kind, rows in groups.items():
            if not isinstance(rows, list):
                continue
            if kind in ("attribute_" + section, "time_" + section, "switch_triggers"):
                result[section][kind] = [{key: copy.deepcopy(value) for key, value in row.items() if key in SAFE_ELEMENT}
                                         for row in rows if isinstance(row, dict)]
            elif kind == "group_" + section and adapter is not None:
                result[section][kind] = [expand_group_element(adapter, section, row)
                                         for row in rows if isinstance(row, dict)]
            elif section in ("triggers", "actions") and kind == "homeegram_" + section and adapter is not None:
                result[section][kind] = [expand_homeegram_reference(adapter, section, row)
                                         for row in rows if isinstance(row, dict)]
            elif rows:
                # IDs, paths and delays explain the original flow without leaking
                # webhook URLs, notification text, user details or credentials.
                result[section][kind] = [
                    {**{key: row[key] for key in ("id", "branch", "delay")
                         if key in row and isinstance(row[key], (int, float))}, "unsupported": True}
                    for row in rows if isinstance(row, dict)]
                warnings.append(kind + ": Spezialelement wird als nicht bearbeitbarer Platzhalter dargestellt")
    return result, warnings


def _draft_from_native(homeegram):
    """Recover older SHB drafts only when they can be represented losslessly."""
    data = {"name": str(homeegram.get("name", "")), "manual_play": False,
            "triggers": [], "conditions": [], "actions": []}
    trigger_operators = {1: "greater", 2: "less", 3: "equal", 4: "changed"}
    condition_operators = {1: "equal", 2: "lessOrEqual", 3: "greaterOrEqual", 4: "less", 5: "greater", 6: "notEqual"}
    for section in ("triggers", "conditions", "actions"):
        groups = homeegram.get(section) or {}
        if not isinstance(groups, dict):
            return None
        for kind, rows in groups.items():
            if not rows:
                continue
            if section == "triggers" and kind == "switch_triggers":
                if not isinstance(rows, list) or len(rows) != 1:
                    return None
                data["manual_play"] = True
                continue
            if section in ("triggers", "actions") and kind == "homeegram_" + section:
                allowed = {"id", "homeegram_id", "homeegram_event", "type"}
                allowed |= {"target_homeegram_id", "branch", "delay"} if section == "actions" else {"source_homeegram_id"}
                if not isinstance(rows, list):
                    return None
                for row in rows:
                    if not isinstance(row, dict) or set(row) - allowed:
                        return None
                    if "type" in row and row["type"] != (6 if section == "actions" else 5):
                        return None
                    codes = ACTION_EVENTS if section == "actions" else TRIGGER_EVENTS
                    event = next((name for name, code in codes.items()
                                  if type(row.get("homeegram_event")) is int and row["homeegram_event"] == code), None)
                    if event is None:
                        return None
                    if section == "actions":
                        data[section].append({"type": "homeegram", "target_homeegram_id": row.get("target_homeegram_id"),
                                              "operation": event, "branch": row.get("branch", 0), "delay": row.get("delay", 0)})
                    else:
                        data[section].append({"type": "homeegram", "source_homeegram_id": row.get("source_homeegram_id"),
                                              "event": event})
                continue
            if kind != "attribute_" + section or not isinstance(rows, list):
                return None
            for row in rows:
                if not isinstance(row, dict):
                    return None
                allowed = {"id", "homeegram_id", "node_id", "attribute_id", "value", "operator", "operand",
                           "branch", "check_moment", "command", "delay", "source_attribute_id"}
                if set(row) - allowed:
                    return None
                item = {key: row.get(key) for key in ("node_id", "attribute_id", "value")}
                if section == "actions":
                    if row.get("command") not in (1, 3) or row.get("source_attribute_id", 0) != 0:
                        return None
                    item.update(operation="toggle" if row["command"] == 3 else "set",
                                delay=row.get("delay", 0), branch=row.get("branch", 0))
                else:
                    operators = trigger_operators if section == "triggers" else condition_operators
                    if row.get("operator") not in operators or row.get("operand", 1) != 1:
                        return None
                    item["comparison"] = operators[row["operator"]]
                    if section == "conditions":
                        item.update(branch=row.get("branch", 0), check_moment=row.get("check_moment", 1))
                data[section].append(item)
    try:
        return WorkspaceDraft.model_validate(data)
    except ValueError:
        return None


class HomeegramWorkspaceService:
    def __init__(self, access):
        self.access = access
        self.db = access.db

    def _sources(self):
        return [{"id": item["id"], "name": item["name"], "status": item.get("status")}
                for item in self.db.integrations() if item.get("module_id") == "homee"]

    def _source_id(self, integration_id):
        if integration_id:
            return integration_id
        sources = self._sources()
        if len(sources) == 1:
            return sources[0]["id"]
        if len(sources) > 1:
            raise ValueError("Bitte eine homee-Quelle auswählen; mehrere Quellen vorhanden")
        return ""

    def _permission(self, field, integration_id):
        status = self.access.status()
        if not status.get("enabled") or not status.get(field):
            raise ValueError("HG-Entwürfe sind nicht freigegeben" if field == "allow_drafts" else
                             "HG-Anlage auf homee ist nicht freigegeben")
        integration, adapter = self.access.source(integration_id)
        if not integration.get("enabled") or self.access.runtime.adapters.get(integration_id) is not adapter:
            raise ValueError("Diese homee-Integration ist nicht aktiviert")

    def _record(self, draft_id):
        try:
            draft_id = str(uuid.UUID(draft_id))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Ungültige Entwurf-ID") from None
        record = next((row for row in self.access.drafts() if row.get("id") == draft_id), None)
        if record is None:
            raise ValueError("Entwurf nicht gefunden oder inzwischen entfernt")
        return record

    def _catalog(self, adapter, integration_id):
        from .mcp_access import safe_node
        nodes = []
        for original in sorted(adapter.nodes.values(), key=lambda row: row["id"]):
            node = safe_node(original)
            # Unknown/currently absent values do not make a real characteristic
            # disappear from the editor. Keep the same safe metadata whitelist.
            visible_ids = {attribute["id"] for attribute in node["attributes"]}
            for attribute in original.get("attributes", []):
                if attribute.get("id") not in visible_ids and isinstance(attribute.get("id"), int):
                    safe = {key: attribute[key] for key in ("id", "type", "name", "unit", "minimum", "maximum", "step_value", "editable") if key in attribute}
                    node["attributes"].append(safe)
            for attribute in node["attributes"]:
                labels = VALUE_LABELS.get(attribute.get("type"))
                if labels:
                    attribute["value_labels"] = labels
                    attribute["action_labels"] = ({"0": "Ausschalten", "1": "Einschalten"} if attribute["type"] == 1
                                                  else {"0": "Öffnen", "1": "Schließen", "2": "Stoppen"} if attribute["type"] == 135
                                                  else labels)
            nodes.append(node)
        homeegrams = [{"id": hg["id"], "name": unquote(str(hg.get("name", ""))), "active": bool(hg.get("active"))}
                     for hg in adapter.resources.get("homeegrams", []) if isinstance(hg, dict) and "id" in hg]
        drafts = [{"id": row["id"], "name": str(row.get("homeegram", {}).get("name", "Entwurf")),
                   "revision": row.get("workspace_revision", 1), "status": row.get("status", "needs_review")}
                  for row in self.access.drafts() if row.get("integration_id") == integration_id]
        return {"nodes": nodes, "homeegrams": homeegrams, "drafts": drafts}

    def get(self, integration_id="", homeegram_id=None, draft_id=None):
        if homeegram_id is not None and draft_id is not None:
            raise ValueError("Entweder ein Original-HG oder einen Entwurf auswählen")
        sources = self._sources()
        record = self._record(draft_id) if draft_id else None
        if record:
            if integration_id and integration_id != record["integration_id"]:
                raise ValueError("Entwurf gehört nicht zu dieser homee-Quelle")
            integration_id = record["integration_id"]
        else:
            # An empty entrypoint must still render a useful source picker when
            # several homee hubs are connected; writes never choose one silently.
            integration_id = self._source_id(integration_id) if integration_id or homeegram_id is not None or len(sources) <= 1 else ""
        status = self.access.status()
        permissions = {key: bool(status.get(key)) for key in ("allow_drafts", "allow_homee_creation")}
        workspace = {"integration_id": integration_id, "source_name": "", "mode": "browser", "homeegram_id": None,
                     "draft_id": None, "revision": None, "name": "Homeegramme", "active": False,
                     "homeegram": {}, "draft": None, "editable": False, "warnings": []}
        if not integration_id:
            return {"workspace": workspace, "catalog": {"nodes": [], "homeegrams": [], "drafts": []},
                    "permissions": permissions, "sources": sources, "cached": True}
        integration, adapter = self.access.source(integration_id)
        workspace["source_name"] = integration["name"]
        if record:
            model = WorkspaceDraft.model_validate(record["workspace_draft"]) if record.get("workspace_draft") else _draft_from_native(record["homeegram"])
            public, warnings = _public_homeegram(record["homeegram"], decode_name=False, adapter=adapter)
            deployment = record.get("workspace_deployment")
            locked = bool(deployment) or record.get("status") == "on_homee"
            workspace.update(mode="draft", draft_id=record["id"], revision=record.get("workspace_revision", 1),
                             name=public["name"], homeegram=public, draft=model.model_dump() if model else None,
                             editable=bool(model and permissions["allow_drafts"] and not locked), warnings=warnings,
                             status=record.get("status", "needs_review"))
            if model is None:
                workspace["warnings"].append("Dieser Entwurf enthält noch nicht unterstützte Elemente. In SHB bearbeiten; nichts wird stillschweigend entfernt.")
            if deployment:
                workspace["deployment"] = {key: deployment[key] for key in ("request_id", "status", "homeegram_id") if key in deployment}
        elif homeegram_id is not None:
            if type(homeegram_id) is not int or homeegram_id <= 0:
                raise ValueError("Positive Original-homeegramm-ID erforderlich")
            original = next((hg for hg in adapter.resources.get("homeegrams", []) if hg.get("id") == homeegram_id), None)
            if original is None:
                raise ValueError("Homeegramm nicht im Cache dieser Quelle vorhanden")
            public, warnings = _public_homeegram(original, adapter=adapter)
            workspace.update(mode="view", homeegram_id=homeegram_id, name=public["name"],
                             active=bool(original.get("active")), homeegram=public, warnings=warnings)
        # Explicit flow semantics help both a headless model and the web client.
        hg = workspace["homeegram"]
        rows = lambda section: [row for items in hg.get(section, {}).values() for row in items]
        workspace["flow"] = {"new_paths": any(row.get("branch", 0) == 1 for row in rows("conditions")) or
                              any(row.get("branch", 0) in (1, 2) for row in rows("actions")),
                             "trigger_join": "OR", "condition_join": "AND", "nested_conditions": False,
                             "conditions": {"prerequisites": 0, "if": 1}, "actions": {"always": 0, "then": 1, "else": 2}}
        self.access.audit("Homeegramm-Arbeitsansicht gelesen")
        return {"workspace": workspace, "catalog": self._catalog(adapter, integration_id),
                "permissions": permissions, "sources": sources, "cached": True}

    def create(self, integration_id, request_id, name="Neues Homeegramm"):
        from .mcp_access import now
        integration_id = self._source_id(integration_id)
        self._permission("allow_drafts", integration_id)
        request_id = _request_id(request_id)
        model = WorkspaceDraft(name=name)
        native = self.access.build_homeegram(integration_id, model)
        fingerprint = _fingerprint(["workspace_create", integration_id, model.model_dump()])
        with self.db._lock:
            if request_id in self.db.setting("mcp_removed_drafts", []):
                raise ValueError("Dieser Entwurf wurde entfernt; eine neue Anfrage-ID verwenden")
            rows = self.access.drafts()
            previous = next((row for row in rows if row["id"] == request_id), None)
            if previous:
                if previous.get("fingerprint") != fingerprint:
                    raise ValueError("Diese Anfrage-ID wurde bereits für einen anderen Entwurf verwendet")
            else:
                if len(rows) >= 100:
                    raise ValueError("100 Entwürfe vorhanden. Bitte in SHB zuerst aufräumen")
                integration, _ = self.access.source(integration_id)
                rows.insert(0, {"id": request_id, "fingerprint": fingerprint, "created_at": now(), "updated_at": now(),
                                "integration_id": integration_id, "source_name": integration["name"],
                                "source_host": str(integration.get("configuration", {}).get("host", "")),
                                "homeegram": native, "workspace_draft": model.model_dump(), "workspace_revision": 1,
                                "status": "needs_review", "workspace_requests": {}})
                self.db.set_setting("mcp_drafts", rows)
                self.access.audit("Interaktiver HG-Entwurf angelegt: " + request_id)
        return self.get(draft_id=request_id)

    def update(self, draft_id, expected_revision, request_id, draft):
        from .mcp_access import now
        request_id = _request_id(request_id)
        model = draft if isinstance(draft, WorkspaceDraft) else WorkspaceDraft.model_validate(draft)
        fingerprint = _fingerprint(["workspace_update", draft_id, expected_revision, model.model_dump()])
        with self.db._lock:
            record = self._record(draft_id)
            self._permission("allow_drafts", record["integration_id"])
            previous = record.get("workspace_requests", {}).get(request_id)
            if previous:
                if previous.get("fingerprint") != fingerprint:
                    raise ValueError("Diese Anfrage-ID gehört zu einer anderen Änderung")
                return self.get(draft_id=record["id"])
            if record.get("workspace_deployment") or record.get("status") == "on_homee":
                raise ValueError("Dieser Entwurf wurde gesendet oder wird gerade gesendet und ist schreibgeschützt")
            if record.get("workspace_revision", 1) != expected_revision:
                raise ValueError("Versionskonflikt: Entwurf wurde zwischenzeitlich geändert. Neu laden und Änderungen abgleichen")
            if record.get("workspace_draft") is None and _draft_from_native(record["homeegram"]) is None:
                raise ValueError("Nicht unterstützte Elemente dürfen nicht durch einen Teilentwurf ersetzt werden")
            native = self.access.build_homeegram(record["integration_id"], model)
            record.update(homeegram=native, workspace_draft=model.model_dump(),
                          workspace_revision=expected_revision + 1, updated_at=now())
            requests = record.setdefault("workspace_requests", {})
            requests[request_id] = {"fingerprint": fingerprint, "revision": record["workspace_revision"]}
            record["workspace_requests"] = dict(list(requests.items())[-100:])
            rows = self.access.drafts()
            self.db.set_setting("mcp_drafts", [record if row["id"] == record["id"] else row for row in rows])
            self.access.audit("Interaktiver HG-Entwurf bearbeitet: " + record["id"])
        return self.get(draft_id=record["id"])

    async def deploy(self, draft_id, expected_revision, request_id):
        from .mcp_access import now
        request_id = _request_id(request_id)
        fingerprint = _fingerprint(["workspace_deploy", draft_id, expected_revision])
        with self.db._lock:
            record = self._record(draft_id)
            self._permission("allow_homee_creation", record["integration_id"])
            prior = record.get("workspace_deployment")
            if prior:
                if prior.get("request_id") != request_id or prior.get("fingerprint") != fingerprint:
                    raise ValueError("Entwurf bereits gesendet oder Sendestatus unklar. Bestehende request_id verwenden; nicht erneut anlegen")
                if prior.get("status") == "created_disabled":
                    result = self.get(draft_id=record["id"])
                    result["deployment"] = {"homeegram_id": prior["homeegram_id"], "active": 0, "play": 0,
                                            "status": "created_disabled", "replayed": True}
                    return result
            elif record.get("status") == "on_homee":
                raise ValueError("Dieser Entwurf wurde bereits auf homee angelegt")
            if record.get("workspace_revision", 1) != expected_revision:
                raise ValueError("Versionskonflikt: Vor dem Senden den aktuellen Entwurf neu laden")
            # Final validation rejects empty actions and unsupported/malformed
            # payloads before recording any possibly-sent request.
            native = self.access.validate_edited_homeegram(record["integration_id"], record["homeegram"])
            integration_id = record["integration_id"]
            record["workspace_deployment"] = {"request_id": request_id, "fingerprint": fingerprint,
                                               "status": "pending", "revision": expected_revision}
            rows = self.access.drafts()
            self.db.set_setting("mcp_drafts", [record if row["id"] == record["id"] else row for row in rows])
        try:
            deployed = await self.access.create_on_homee(integration_id, request_id, native)
            if not isinstance(deployed, dict) or not isinstance(deployed.get("homeegram_id"), int) or deployed.get("active", 0) != 0:
                raise ValueError("homee hat die deaktivierte Anlage nicht eindeutig bestätigt")
        except Exception:
            with self.db._lock:
                rows = self.access.drafts()
                for row in rows:
                    if row["id"] == record["id"]:
                        row["workspace_deployment"]["status"] = "uncertain"
                self.db.set_setting("mcp_drafts", rows)
            raise ValueError("Anlage nicht bestätigt. Entwurf bleibt gesperrt; mit derselben request_id prüfen, keine neue Anlage blind senden") from None
        with self.db._lock:
            rows = self.access.drafts()
            for row in rows:
                if row["id"] == record["id"]:
                    row.update(status="on_homee", homeegram_id=deployed["homeegram_id"], deployed_at=now(),
                               workspace_revision=expected_revision + 1)
                    row["workspace_deployment"].update(status="created_disabled", homeegram_id=deployed["homeegram_id"])
            self.db.set_setting("mcp_drafts", rows)
        result = self.get(draft_id=record["id"])
        result["deployment"] = {key: deployed[key] for key in ("homeegram_id", "active", "play", "status", "replayed") if key in deployed}
        return result


def register_homeegram_ui(access):
    """Register the real HTML resource and independent render/data tools."""
    service = HomeegramWorkspaceService(access)
    access.homeegram_workspace = service
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    visibility = {"ui": {"visibility": ["model", "app"]}}

    @access.server.resource(RESOURCE_URI, name="homeegram-editor", title="Homeegramme · Ablauf und Entwurf",
                            description="Interaktiver SHB-Ablaufbaum. Originale ansehen, Entwürfe gemeinsam mit der KI bearbeiten.",
                            mime_type=RESOURCE_MIME, meta={
                                "ui": {"csp": {"connectDomains": [], "resourceDomains": [], "frameDomains": []}, "prefersBorder": True},
                                "openai/widgetDescription": "SHB Homeegramm-Ablauf mit gemeinsamem, versioniertem Entwurf; erst nach ausdrücklicher Bestätigung deaktiviert auf homee anlegen.",
                                "openai/widgetPrefersBorder": True,
                                "openai/widgetCSP": {"connect_domains": [], "resource_domains": [], "frame_domains": []},
                                "openai/ui": {"availableDisplayModes": ["inline", "fullscreen"], "preferredDisplayMode": "fullscreen"},
                            })
    def homeegram_editor_resource() -> str:
        return (Path(__file__).parent / "web" / "homeegram.html").read_text(encoding="utf-8")

    @access.server.tool(annotations=read, structured_output=True, meta={"ui": {"resourceUri": RESOURCE_URI, "visibility": ["model", "app"]},
                                              "openai/ui": {"entrypoints": [{"type": "thread"}]},
                                              "openai/outputTemplate": RESOURCE_URI})
    async def show_homeegram(integration_id: str = "", homeegram_id: Annotated[int | None, Field(strict=True, gt=0)] = None,
                            draft_id: str | None = None) -> dict[str, Any]:
        """Öffnet die INTERAKTIVE grafische SHB-Homeegramm-Ansicht (MCP App).
        Ohne ID: Übersicht. Mit homeegram_id: Original ansehen, ohne es zu ändern.
        Mit draft_id: gemeinsamen SHB-Entwurf anzeigen. Original-homee-IDs verwenden.
        Zum gemeinsamen Erstellen create_homeegram_workspace, danach show_homeegram mit draft_id.
        KI-Änderungen mit update_homeegram_workspace; Entwürfe bleiben lokal und deaktiviert.
        Nur ein MCP-Apps-fähiger Client zeigt die Webansicht; structuredContent funktioniert auch ohne UI.
        """
        return service.get(integration_id, homeegram_id, draft_id)

    @access.server.tool(annotations=read, structured_output=True, meta=visibility)
    async def get_homeegram_workspace(integration_id: str = "", homeegram_id: Annotated[int | None, Field(strict=True, gt=0)] = None,
                                     draft_id: str | None = None) -> dict[str, Any]:
        """Lädt aktuellen Entwurf/Original, versionierten Zustand und sicheren Original-homee-Katalog.
        Datenwerkzeug ohne neue Webansicht. Vor KI-Änderungen neu lesen und revision übernehmen.
        """
        return service.get(integration_id, homeegram_id, draft_id)

    @access.server.tool(annotations=write, structured_output=True, meta=visibility)
    async def create_homeegram_workspace(integration_id: str, request_id: str, name: str = "Neues Homeegramm") -> dict[str, Any]:
        """Neuer gemeinsamer LEERER Homeegramm-Entwurf, nur lokal in SHB. Keine Geräteaktion/kein Versand.
        Benötigt Entwurfsfreigabe. request_id neue UUID, bei Retry dieselbe.
        Anschließend show_homeegram(draft_id) für die interaktive Ansicht aufrufen.
        """
        return service.create(integration_id, request_id, name)

    @access.server.tool(annotations=write, structured_output=True, meta=visibility)
    async def update_homeegram_workspace(draft_id: str, expected_revision: Annotated[int, Field(strict=True, ge=1)],
                                         request_id: str, draft: WorkspaceDraft) -> dict[str, Any]:
        """Ersetzt den vollständigen lokalen Entwurf mit Konfliktschutz, KEIN Versand an homee.
        Vorher get_homeegram_workspace lesen, nur gewollte Felder ändern, alle übrigen Elemente behalten.
        expected_revision aus dem gelesenen Zustand. Bei Konflikt neu lesen, nicht blind überschreiben.
        Original-homee-Geräte/Attribute aus catalog.nodes. conditions: branch 0=Immer-Voraussetzung für ALLE Aktionen,
        branch 1=eine gemeinsame Wenn-Reihe (UND). actions: branch 0=Immer NACH Voraussetzungen,
        1=Dann (Wenn wahr), 2=Sonst (Wenn unwahr). Keine verschachtelten homee-Bedingungen.
        HG-Aktionen: type=homeegram, target_homeegram_id aus catalog.homeegrams, operation=activate/deactivate/play/stop.
        HG-Auslöser: type=homeegram, source_homeegram_id, event=activated/deactivated/played/stopped.
        Konfiguration allein führt kein referenziertes HG aus; manual_play betrifft nur das HG selbst.
        request_id neue UUID, bei Retry dieselbe. Leere/unfertige Entwürfe sind ausdrücklich erlaubt.
        """
        return service.update(draft_id, expected_revision, request_id, draft)

    @access.server.tool(annotations=write, structured_output=True, meta=visibility)
    async def deploy_homeegram_workspace(draft_id: str, expected_revision: Annotated[int, Field(strict=True, ge=1)], request_id: str) -> dict[str, Any]:
        """Legt den geprüften Entwurf auf ausdrücklichen Nutzerauftrag als neues DEAKTIVIERTES HG auf homee an.
        Benötigt separate HG-Anlagefreigabe und mindestens eine gültige Aktion. Nie automatisch nach einer Änderung senden.
        Aktuelle revision übernehmen; request_id neue UUID, bei unklarem Ergebnis genau dieselbe wiederverwenden.
        Kein Aktivieren, kein Play, kein Ändern vorhandener HGs. Erfolg enthält bestätigte homeegram_id.
        """
        return await service.deploy(draft_id, expected_revision, request_id)

    return service
