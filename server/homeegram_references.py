"""Typed, source-local HG references: storing a reference never executes it."""

from __future__ import annotations

import math
from typing import Literal
from urllib.parse import unquote

from pydantic import BaseModel, ConfigDict, Field, field_validator


ACTION_EVENTS = {"activate": 1, "deactivate": 2, "play": 3, "stop": 4}
TRIGGER_EVENTS = {"activated": 1, "deactivated": 2, "played": 3, "stopped": 4}
ACTION_LABELS = {1: "Aktivieren", 2: "Deaktivieren", 3: "Abspielen", 4: "Stoppen"}
TRIGGER_LABELS = {1: "Wird aktiviert", 2: "Wird deaktiviert", 3: "Wird ausgelöst", 4: "Wird gestoppt"}


class HomeegramAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["homeegram"]
    target_homeegram_id: int = Field(strict=True, gt=0)
    operation: Literal["activate", "deactivate", "play", "stop"]
    branch: Literal[0, 1, 2] = 1
    delay: float = Field(default=0, ge=0, le=604800, allow_inf_nan=False)

    @field_validator("branch", mode="before")
    @classmethod
    def strict_branch(cls, value):
        if type(value) is not int:
            raise ValueError("branch muss eine ganze Zahl sein")
        return value

    @field_validator("delay", mode="before")
    @classmethod
    def numeric_delay(cls, value):
        if type(value) not in (int, float):
            raise ValueError("delay muss eine endliche Zahl sein")
        return value


class HomeegramTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["homeegram"]
    source_homeegram_id: int = Field(strict=True, gt=0)
    event: Literal["activated", "deactivated", "played", "stopped"] = "played"


def validate_homeegram_reference(adapter, homeegram_id):
    """Resolve only native HG IDs from this exact adapter, never global SHB IDs."""
    if type(homeegram_id) is not int or homeegram_id <= 0:
        raise ValueError("Eine positive Original-homeegramm-ID ist erforderlich")
    resources = getattr(adapter, "resources", {})
    rows = resources.get("homeegrams") if isinstance(resources, dict) else None
    matches = [row for row in rows if isinstance(row, dict) and type(row.get("id")) is int
               and row["id"] == homeegram_id] if isinstance(rows, list) else []
    if len(matches) != 1:
        raise ValueError("Das referenzierte Homeegramm gehört nicht eindeutig zu dieser homee-Quelle")
    return matches[0]


def expand_homeegram_reference(adapter, section, row):
    """Safe read projection, preserving unknown codes without inventing behavior."""
    if section not in ("triggers", "actions"):
        raise ValueError("HG-Referenzen werden hier nur als Auslöser oder Aktion unterstützt")
    row = row if isinstance(row, dict) else {}
    fields = ("id", "source_homeegram_id", "target_homeegram_id", "homeegram_event", "branch", "delay")
    result = {key: row[key] for key in fields
              if type(row.get(key)) in (int, float) and math.isfinite(row[key])}
    field = "target_homeegram_id" if section == "actions" else "source_homeegram_id"
    identifier = result.get(field)
    try:
        target = validate_homeegram_reference(adapter, identifier)
    except ValueError:
        target = None
    fallback = "Homeegramm " + str(identifier) if identifier is not None else "Unbekanntes Homeegramm"
    raw_name = target.get("name") if target is not None else None
    name = unquote(raw_name) if isinstance(raw_name, str) and raw_name.strip() else fallback
    reference = {"id": identifier, "name": name, "missing": target is None}
    if target is not None and type(target.get("active")) is int and target["active"] in (0, 1):
        reference["active"] = bool(target["active"])
    result["target_homeegram" if section == "actions" else "source_homeegram"] = reference
    code = result.get("homeegram_event")
    label = (ACTION_LABELS if section == "actions" else TRIGGER_LABELS).get(code)
    result["event_label"] = label or "Unbekanntes HG-Ereignis " + str(code)
    if section == "actions":
        result["summary"] = "Homeegramm „" + name + "“ · " + result["event_label"]
    else:
        result["summary"] = "Wenn Homeegramm „" + name + "“ · " + result["event_label"]
    result["warnings"] = ["Das referenzierte Homeegramm fehlt oder ist in dieser homee-Quelle nicht eindeutig."] if target is None else []
    if label is None:
        result["warnings"].append("Unbekannter homeegram_event; nicht als unterstützten Befehl interpretieren.")
    return result
