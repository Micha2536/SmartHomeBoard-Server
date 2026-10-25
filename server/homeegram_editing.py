"""Validated, explicit native Homeegram element patches shared by MCP/API.

The adapter owns durable backups and delivery idempotency. This layer owns
catalog authorization and validation; an element patch never replaces a whole
HG or silently drops native categories the editor cannot yet create.
"""

from copy import deepcopy
from datetime import datetime
import inspect
import math
import re
from typing import Annotated, Any, Literal
from urllib.parse import unquote
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .homeegram_revision import homeegram_revision


KINDS = {
    "triggers": ("time_triggers", "attribute_triggers", "switch_triggers", "webhook_triggers",
                 "homeegram_triggers", "celestial_triggers", "plan_triggers", "group_triggers", "user_triggers"),
    "conditions": ("time_conditions", "attribute_conditions", "homeegram_conditions", "celestial_conditions",
                   "plan_conditions", "group_conditions", "user_conditions"),
    "actions": ("attribute_actions", "tts_actions", "notification_actions", "group_actions",
                "webhook_actions", "homeegram_actions", "plan_actions", "user_actions"),
}
EDITABLE = {"switch_triggers", "time_triggers", "attribute_triggers", "time_conditions",
            "attribute_conditions", "attribute_actions", "homeegram_triggers", "homeegram_actions"}
FIELDS = {
    "switch_triggers": set(),
    "time_triggers": {"dtstart", "rrule"},
    "time_conditions": {"dtstart", "rrule", "duration", "branch", "check_moment"},
    "attribute_triggers": {"node_id", "attribute_id", "operator", "operand", "value"},
    "attribute_conditions": {"node_id", "attribute_id", "operator", "operand", "value", "branch", "check_moment"},
    "attribute_actions": {"node_id", "attribute_id", "command", "source_attribute_id", "value", "branch", "delay"},
    "homeegram_triggers": {"source_homeegram_id", "homeegram_event"},
    "homeegram_actions": {"target_homeegram_id", "homeegram_event", "branch", "delay"},
}


class HGElementChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["add", "update", "remove"]
    section: Literal["triggers", "conditions", "actions"]
    kind: str = Field(min_length=1, max_length=80)
    element_id: Annotated[int, Field(strict=True, gt=0)] | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def coherent_target(self):
        if self.kind not in KINDS[self.section]:
            raise ValueError("Elementart gehört nicht zu diesem HG-Bereich")
        if (self.operation == "add") != (self.element_id is None):
            raise ValueError("Nur update/remove benötigen eine positive Original-Element-ID")
        if self.operation == "remove" and self.parameters:
            raise ValueError("Beim Entfernen sind keine Parameter erlaubt")
        if self.operation != "remove" and self.kind not in EDITABLE:
            raise ValueError("Diese native Kategorie kann nur gezielt entfernt werden")
        if self.operation != "remove" and set(self.parameters) - FIELDS[self.kind]:
            raise ValueError("Unbekannte oder schreibgeschützte Elementfelder")
        return self


def _integer(value, name, allowed=None, minimum=None, maximum=None):
    if type(value) is not int or allowed is not None and value not in allowed:
        raise ValueError("Ungültiger ganzzahliger Wert: " + name)
    if minimum is not None and value < minimum or maximum is not None and value > maximum:
        raise ValueError("Wert außerhalb des Bereichs: " + name)
    return value


def _number(value, name):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("Ein endlicher Zahlenwert ist erforderlich: " + name)
    return value


def _date(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"\d{8}T\d{6}Z?", value):
        raise ValueError("Ungültiger homee-Zeitpunkt: " + name)
    try:
        datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
    except ValueError:
        raise ValueError("Ungültiger homee-Zeitpunkt: " + name) from None


def _schedule(row, condition):
    _date(row.get("dtstart"), "dtstart")
    rule = row.get("rrule")
    if not isinstance(rule, str) or len(rule) > 1024:
        raise ValueError("Ungültige Wiederholungsregel")
    fields = {}
    for part in rule.split(";") if rule else []:
        pair = part.split("=")
        if len(pair) != 2 or pair[0] in fields:
            raise ValueError("Ungültige oder doppelte Wiederholungsregel")
        key, value = pair
        fields[key] = value
        if key == "FREQ":
            if value not in {"MINUTELY", "HOURLY", "DAILY", "WEEKLY", "MONTHLY", "YEARLY"}:
                raise ValueError("Unbekanntes Zeitintervall")
        elif key == "UNTIL":
            _date(value, "UNTIL")
        elif key == "BYDAY":
            if not re.fullmatch(r"(?:[+-]?[1-5])?(?:MO|TU|WE|TH|FR|SA|SU)(?:,(?:[+-]?[1-5])?(?:MO|TU|WE|TH|FR|SA|SU))*", value):
                raise ValueError("Ungültige Wochentage")
        elif key == "WKST":
            if value not in {"MO", "TU", "WE", "TH", "FR", "SA", "SU"}:
                raise ValueError("Ungültiger Wochenbeginn")
        elif key in {"INTERVAL", "COUNT", "BYHOUR", "BYMINUTE", "BYSECOND", "BYMONTHDAY", "BYMONTH", "BYSETPOS"}:
            limits = {"INTERVAL": (1, 10000), "COUNT": (1, 1000000), "BYHOUR": (0, 23),
                      "BYMINUTE": (0, 59), "BYSECOND": (0, 59), "BYMONTHDAY": (-31, 31),
                      "BYMONTH": (1, 12), "BYSETPOS": (-366, 366)}
            if not re.fullmatch(r"-?\d+(?:,-?\d+)*", value) or len(value.split(",")) > 366:
                raise ValueError("Ungültige Zeitregelwerte")
            numbers = [int(v) for v in value.split(",")]
            lo, hi = limits[key]
            if any(v < lo or v > hi or key in {"BYMONTHDAY", "BYSETPOS"} and v == 0 for v in numbers):
                raise ValueError("Zeitregelwert außerhalb des Bereichs")
            if key in {"INTERVAL", "COUNT"} and len(numbers) != 1:
                raise ValueError("Einzelner Intervallwert erforderlich")
        else:
            raise ValueError("Nicht unterstütztes Zeitregelfeld: " + key)
    if rule and "FREQ" not in fields:
        raise ValueError("FREQ fehlt in der Wiederholungsregel")
    if "UNTIL" in fields and "COUNT" in fields:
        raise ValueError("UNTIL und COUNT dürfen nicht kombiniert werden")
    if condition:
        duration = row.get("duration")
        if not isinstance(duration, str) or not re.fullmatch(r"P(?=\d|T\d)(?:\d+D)?(?:T(?=\d)(?:\d+H)?(?:\d+M)?(?:\d+(?:\.\d+)?S)?)?", duration):
            raise ValueError("Eine gültige ISO-Zeitfensterdauer ist erforderlich")
        if not any(float(v) > 0 for v in re.findall(r"\d+(?:\.\d+)?", duration)):
            raise ValueError("Das Zeitfenster muss länger als null sein")


class NativeHomeegramEditorService:
    def __init__(self, database, runtime):
        self.db = database
        self.runtime = runtime

    def source(self, integration_id):
        integration = self.db.integration(integration_id)
        adapter = self.runtime.adapters.get(integration_id)
        if (not integration or integration.get("module_id") != "homee" or not integration.get("enabled")
                or adapter is None or getattr(adapter, "socket", None) is None):
            raise ValueError("Eine aktive, verbundene Original-homee-Quelle ist erforderlich")
        return adapter

    def snapshot(self, integration_id, homeegram_id, expected_name=None):
        _integer(homeegram_id, "homeegram_id", minimum=1)
        adapter = self.source(integration_id)
        rows = [hg for hg in adapter.resources.get("homeegrams", [])
                if isinstance(hg, dict) and type(hg.get("id")) is int and hg["id"] == homeegram_id]
        if len(rows) != 1:
            raise ValueError("Original-Homeegramm nicht eindeutig gefunden")
        original = rows[0]
        if not all(isinstance(original.get(s), dict) for s in KINDS):
            raise ValueError("Das vollständige Original-Homeegramm wird benötigt")
        if expected_name is not None and (not isinstance(expected_name, str) or not expected_name.strip()
                or unquote(str(original.get("name", ""))) != expected_name):
            raise ValueError("Der vollständige dekodierte Homeegramm-Name stimmt nicht überein")
        return deepcopy(original), homeegram_revision(original)

    async def _authorize(self, integration_id, adapter, authorize):
        if self.source(integration_id) is not adapter:
            raise ValueError("Die homee-Quelle wurde geändert")
        if authorize is not None:
            allowed = authorize()
            if inspect.isawaitable(allowed):
                allowed = await allowed
            if allowed is not True:
                raise ValueError("Diese Homeegramm-Änderung ist nicht freigegeben")
        return True

    @staticmethod
    def _request(request_id):
        if not isinstance(request_id, str):
            raise ValueError("request_id muss eine UUID sein")
        try:
            return str(UUID(request_id))
        except ValueError:
            raise ValueError("request_id muss eine UUID sein") from None

    @staticmethod
    def _public(result, integration_id, homeegram_id, expected_name, request_id, expected_active, editing=False):
        if (not isinstance(result, dict) or result.get("homeegram_id") != homeegram_id
                or result.get("name") != expected_name or result.get("active") != expected_active
                or result.get("status") != ("edited_disabled" if editing else "active" if expected_active else "inactive")):
            raise ValueError("homee hat die gewünschte Änderung nicht bestätigt")
        fields = ("homeegram_id", "name", "active", "status", "request_id", "backup_saved", "replayed", "updated", "revision", "changed")
        return {**{k: result[k] for k in fields if k in result}, "integration_id": integration_id, "request_id": request_id}

    async def set_active(self, integration_id, homeegram_id, expected_name, active, request_id, authorize=None):
        if type(active) is not bool:
            raise ValueError("active muss ein boolescher Wert sein")
        request_id = self._request(request_id)
        adapter = self.source(integration_id)
        self.snapshot(integration_id, homeegram_id, expected_name)
        await self._authorize(integration_id, adapter, authorize)
        result = await adapter.set_homeegram_active_confirmed(homeegram_id, expected_name, active,
            request_id, authorize=lambda: self._authorize(integration_id, adapter, authorize))
        return self._public(result, integration_id, homeegram_id, expected_name, request_id, int(active))

    def validate_changes(self, adapter, original, changes):
        if not isinstance(changes, list) or not 1 <= len(changes) <= 120:
            raise ValueError("1 bis 120 gezielte Änderungen sind erforderlich")
        result, targets = [], set()
        for value in changes:
            change = value if isinstance(value, HGElementChange) else HGElementChange.model_validate(value)
            item = change.model_dump(exclude_none=True)
            params = deepcopy(change.parameters)
            existing = {}
            if change.operation != "add":
                target = (change.section, change.element_id)
                if target in targets:
                    raise ValueError("Ein HG-Element darf nur einmal pro Anfrage geändert werden")
                targets.add(target)
                found = [(kind, row) for kind, rows in original[change.section].items() for row in rows
                         if isinstance(row, dict) and row.get("id") == change.element_id]
                if len(found) != 1 or found[0][0] != change.kind:
                    raise ValueError("Element-ID gehört nicht eindeutig zu diesem HG und Typ")
                existing = found[0][1]
            if change.operation == "remove":
                item.pop("parameters", None)
                result.append(item)
                continue
            if change.operation == "add":
                if change.section in {"conditions", "actions"} and "branch" not in params:
                    raise ValueError("Neue Bedingungen und Aktionen benötigen einen expliziten branch")
                if change.section == "conditions":
                    params.setdefault("check_moment", 1)
                if change.kind.startswith("attribute_"):
                    if change.section == "actions":
                        params.setdefault("delay", 0)
                        params.setdefault("source_attribute_id", 0)
                    else:
                        params.setdefault("operand", 1)
                if change.kind == "homeegram_actions":
                    params.setdefault("delay", 0)
            row = {**existing, **params}
            if change.section == "conditions":
                _integer(row.get("branch"), "branch", allowed=(0, 1))
                _integer(row.get("check_moment"), "check_moment", allowed=(1, 2, 3))
            elif change.section == "actions":
                # Old HG-reference actions may predate branch support. An
                # event-only update must retain that absence, not invent a
                # new always/then path. New additions remain explicit above.
                legacy_reference = (change.operation == "update" and change.kind == "homeegram_actions"
                                    and "branch" not in row)
                if not legacy_reference:
                    _integer(row.get("branch"), "branch", allowed=(0, 1, 2))
            if change.kind.startswith("time_"):
                _schedule(row, change.section == "conditions")
            if change.kind in {"homeegram_triggers", "homeegram_actions"}:
                reference = "source_homeegram_id" if change.section == "triggers" else "target_homeegram_id"
                identifier = _integer(row.get(reference), reference, minimum=1)
                _integer(row.get("homeegram_event"), "homeegram_event", allowed=(1, 2, 3, 4))
                matches = [hg for hg in adapter.resources.get("homeegrams", [])
                           if isinstance(hg, dict) and type(hg.get("id")) is int and hg["id"] == identifier]
                if len(matches) != 1:
                    raise ValueError("Das referenzierte Homeegramm gehört nicht eindeutig zu diesem Original-homee")
                if change.section == "actions":
                    delay = _number(row.get("delay", 0), "delay")
                    if not 0 <= delay <= 604800:
                        raise ValueError("Verzögerung außerhalb des Bereichs")
            if change.kind.startswith("attribute_"):
                _integer(row.get("node_id"), "node_id")
                _integer(row.get("attribute_id"), "attribute_id", minimum=1)
                node = adapter.nodes.get(row["node_id"])
                attrs = [a for a in (node or {}).get("attributes", []) if a.get("id") == row["attribute_id"]]
                if len(attrs) != 1:
                    raise ValueError("Gerät und Attribut gehören nicht zu diesem Original-homee")
                attr = attrs[0]
                if change.section == "actions":
                    if attr.get("editable") not in (True, 1):
                        raise ValueError("Aktionsattribut ist nicht schreibbar")
                    command = _integer(row.get("command"), "command", allowed=(1, 3))
                    _integer(row.get("source_attribute_id", 0), "source_attribute_id", allowed=(0,))
                    delay = _number(row.get("delay", 0), "delay")
                    if not 0 <= delay <= 604800:
                        raise ValueError("Verzögerung außerhalb des Bereichs")
                    if command == 3:
                        if attr.get("type") != 1:
                            raise ValueError("Umschalten ist nur für An/Aus-Schalter zulässig")
                        params.pop("value", None)
                    else:
                        number = _number(row.get("value"), "value")
                        if attr.get("type") == 1 and number not in (0, 1):
                            raise ValueError("Ein An/Aus-Schalter benötigt 0 oder 1")
                        for bound, invalid in (("minimum", lambda n: number < n), ("maximum", lambda n: number > n)):
                            limit = attr.get(bound)
                            if type(limit) in (int, float) and math.isfinite(limit) and invalid(limit):
                                raise ValueError("Aktionswert außerhalb des Gerätebereichs")
                else:
                    operator = _integer(row.get("operator"), "operator", allowed=(1, 2, 3, 4) if change.section == "triggers" else (1, 2, 3, 4, 5, 6))
                    _integer(row.get("operand"), "operand", allowed=(1,))
                    number = _number(row.get("value"), "value")
                    if change.section == "triggers" and operator == 4 and number < 0:
                        raise ValueError("Änderungsschwelle muss mindestens null sein")
            item["parameters"] = params
            result.append(item)
        return result

    async def edit(self, integration_id, homeegram_id, expected_name, expected_revision, changes, request_id, authorize=None):
        if not isinstance(expected_revision, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_revision):
            raise ValueError("Ungültige Original-Revision")
        request_id = self._request(request_id)
        adapter = self.source(integration_id)
        original, revision = self.snapshot(integration_id, homeegram_id, expected_name)
        previous = getattr(adapter, "homeegram_mutations", {}).get(request_id)
        if not previous and expected_revision != revision:
            raise ValueError("Das Homeegramm wurde geändert; bitte erneut laden")
        # Replays validate against the retained original, not already removed
        # element IDs or newly edited values. The adapter checks fingerprint.
        baseline = previous.get("backup", original) if previous else original
        wire = self.validate_changes(adapter, baseline, changes)
        await self._authorize(integration_id, adapter, authorize)
        result = await adapter.edit_homeegram_confirmed(homeegram_id, expected_name, wire, request_id,
            authorize=lambda: self._authorize(integration_id, adapter, authorize), expected_revision=expected_revision)
        return self._public(result, integration_id, homeegram_id, expected_name, request_id, 0, editing=True)
