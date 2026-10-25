"""Read-only, source-local projections of native Homeegram group elements.

Group membership is derived from the same homee adapter's relationships, never
from SHB's merged/global device IDs. This is a description of cached metadata,
not an evaluation of whether a condition is currently true or a command ran.
"""
from __future__ import annotations

import math
from urllib.parse import unquote

from .homee_enums import ATTRIBUTE_TYPES


GROUP_ELEMENT_FIELDS = frozenset((
    "id", "group_id", "attribute_type", "operator", "operand", "value", "scope",
    "branch", "check_moment", "delay", "command", "source_attribute_id", "source_node_id", "operand_id",
))
ATTRIBUTE_NUMBERS = ("id", "type", "instance", "current_value", "minimum", "maximum",
                     "step_value", "last_changed", "modified")
ATTRIBUTE_NAMES = {
    1: "An/Aus", 2: "Dimmwert", 3: "Aktueller Verbrauch", 4: "Gesamtverbrauch",
    5: "Temperatur", 6: "Solltemperatur", 7: "Luftfeuchtigkeit", 8: "Batterie",
    10: "Fensterzustand", 11: "Helligkeit", 12: "Wasseralarm", 14: "Öffnen/Schließen",
    15: "Position", 16: "Rauchalarm", 25: "Bewegung", 76: "Anwesenheit",
    113: "Lamellenposition", 135: "Rollladen", 205: "Zuhause-Modus",
    243: "Wetter", 246: "Wetter",
}
# Native CAGroupScope, from HomeeEnums.swift and homee app.min.js.
SCOPES = {1: "Kein Gerät", 2: "Alle Geräte", 3: "Mindestens ein Gerät", 4: "Nicht alle Geräte"}
CONDITION_OPERATORS = {1: "=", 2: "≤", 3: "≥", 4: "<", 5: ">", 6: "≠",
                       7: "Text gleich", 8: "Text ungleich"}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _text(value, fallback):
    if not isinstance(value, str) or not value.strip():
        return fallback
    return "".join(character for character in unquote(value)[:500]
                   if character >= " " or character == "\t")


def _attribute_name(attribute_type):
    if attribute_type in ATTRIBUTE_NAMES:
        return ATTRIBUTE_NAMES[attribute_type]
    entry = ATTRIBUTE_TYPES.get(str(attribute_type), {})
    return entry.get("label") or ("Attributtyp " + str(attribute_type) if attribute_type is not None else "Attribut unbekannt")


def _value_label(attribute_type, value, unit="", *, action=False):
    if not _number(value):
        return "Wert unbekannt"
    # Import lazily: MCP UI imports this helper when building its read projection.
    # Reuse its shared weather/contact/motion catalog, without an import cycle.
    from .mcp_homeegram_ui import VALUE_LABELS
    if action and attribute_type in (14, 135):
        actions = {14: {0: "Schließen", 1: "Öffnen"},
                   135: {0: "Öffnen", 1: "Schließen", 2: "Stoppen"}}
        if value in actions[attribute_type]:
            return actions[attribute_type][value]
    key = str(int(value)) if value == int(value) else str(value)
    label = VALUE_LABELS.get(attribute_type, {}).get(key)
    if label is not None:
        return label
    return format(value, "g") + (" " + unit if unit else "")


def _target_label(section, row, unit=""):
    attribute_type = row.get("attribute_type")
    if section == "actions":
        command = row.get("command")
        if command == 3:
            return "Umschalten"
        if command == 2:
            source = row.get("source_attribute_id")
            if type(source) is int and source > 0:
                return "Aktuellen Wert von Attribut " + str(source)
            node = row.get("source_node_id")
            return "Aktuellen Wert von Gerät " + str(node) if type(node) is int and node != 0 else "Aktuellen Quellwert übernehmen"
        # The native app groups 4/6 as increase and 5/7 as decrease. Keep the
        # command code visible because their different native variants matter.
        if command in (4, 5, 6, 7):
            direction = "Erhöhen" if command in (4, 6) else "Verringern"
            return direction + " um " + _value_label(None, row.get("value"), unit) + " (Befehl " + str(command) + ")"
        if command != 1:
            return "Unbekannter Befehl " + str(command)
        return _value_label(attribute_type, row.get("value"), unit, action=True)
    operand = row.get("operand")
    if operand not in (None, 1):
        if operand == 2:
            operand_id = row.get("operand_id")
            return "Wert von Attribut " + str(operand_id) if type(operand_id) is int and operand_id > 0 else "Attributvergleich; Quellwert nicht aufgelöst"
        return "Unbekannter Operand " + str(operand)
    if section == "triggers" and row.get("operator") == 4:
        return _value_label(None, row.get("value"), unit)
    return _value_label(attribute_type, row.get("value"), unit)


def _summary(section, row, group):
    name = "Gruppe „" + group["name"] + "“"
    attribute = group["attribute_name"]
    target = _target_label(section, row)
    if section == "actions":
        command = row.get("command")
        verb = "setzen auf " + target if command == 1 else target
        summary = "Geräte der " + name + ": " + attribute + " · " + verb
        if _number(row.get("delay")) and row["delay"] > 0:
            summary += " · nach " + format(row["delay"], "g") + " Sek."
        return summary
    if section == "conditions":
        scope = SCOPES.get(row.get("scope"), "Unbekannter Umfang " + str(row.get("scope")))
        operator = CONDITION_OPERATORS.get(row.get("operator"), "Unbekannter Operator " + str(row.get("operator")))
        return "Nur wenn " + scope.lower() + " der " + name + ": " + attribute + " " + operator + " " + target
    operator = row.get("operator")
    change = {1: "steigt über " + target, 2: "fällt unter " + target,
              3: "wechselt zu " + target, 4: "ändert sich um mehr als " + target}.get(operator)
    if operator == 4 and row.get("value") == 0:
        change = "ändert sich (jede Änderung)"
    if change is None:
        change = "Unbekannter Operator " + str(operator) + " · " + target
    return "Wenn bei mindestens einem Gerät der " + name + " " + attribute + " " + change


def _lookup(records, object_id):
    if not isinstance(records, list):
        return None
    matches = [record for record in records if isinstance(record, dict)
               and type(record.get("id")) is int and record["id"] == object_id]
    return matches[0] if len(matches) == 1 else None


def _safe_attribute(attribute, section, row):
    result = {key: attribute[key] for key in ATTRIBUTE_NUMBERS if _number(attribute.get(key))}
    attribute_type = row.get("attribute_type")
    result["name"] = _text(attribute.get("name"), _attribute_name(attribute_type))
    # Only known unit strings are metadata. Never project arbitrary string values.
    unit = attribute.get("unit")
    if (isinstance(unit, str) and len(unit) <= 30 and "://" not in unit
            and not unit.lower().startswith(("www.", "mailto:", "data:")) and all(character >= " " for character in unit)):
        result["unit"] = unit
    if type(attribute.get("editable")) is bool or type(attribute.get("editable")) is int and attribute["editable"] in (0, 1):
        result["editable"] = bool(attribute["editable"])
    result["missing"] = False
    result["applicable"] = section != "actions" or result.get("editable") is True
    result["current_value_label"] = _value_label(attribute_type, result.get("current_value"), result.get("unit", ""))
    result["target_value_label"] = _target_label(section, row, result.get("unit", ""))
    return result


def expand_group_element(adapter, section, row):
    """Return safe native fields plus resolved, read-only group/member metadata.

    ``section`` is triggers/conditions/actions. Unknown enums stay visibly
    unknown. HG members are listed separately, never treated as device targets.
    Only attributes with the element's native attribute_type are included.
    """
    if section not in ("triggers", "conditions", "actions"):
        raise ValueError("Unbekannter Homeegramm-Bereich")
    row = row if isinstance(row, dict) else {}
    result = {key: row[key] for key in GROUP_ELEMENT_FIELDS if _number(row.get(key))}
    group_id = result.get("group_id")
    resources = getattr(adapter, "resources", {})
    resources = resources if isinstance(resources, dict) else {}
    original_group = _lookup(resources.get("groups"), group_id)
    warnings = []
    group = {"id": group_id, "name": _text((original_group or {}).get("name"), "Gruppe " + str(group_id)),
             "attribute_name": _attribute_name(result.get("attribute_type")), "missing": original_group is None,
             "members": [], "homeegrams": [], "warnings": warnings, "membership_complete": True,
             "missing_relationships": not isinstance(resources.get("relationships"), list)}
    if group["missing"]:
        warnings.append("Gruppe fehlt oder ist nicht eindeutig in dieser homee-Quelle vorhanden.")
        group["membership_complete"] = False
    relationships = resources.get("relationships")
    if not isinstance(relationships, list):
        warnings.append("Gruppenmitglieder unbekannt: Relationships wurden nicht geladen.")
        group["membership_complete"] = False
        relationships = []
    relationships = [(index, relation) for index, relation in enumerate(relationships)
                     if isinstance(relation, dict) and type(relation.get("group_id")) is int and relation["group_id"] == group_id]
    relationships.sort(key=lambda item: (item[1]["order"] if _number(item[1].get("order")) else item[0], item[0]))
    nodes = getattr(adapter, "nodes", {})
    nodes = list(nodes.values()) if isinstance(nodes, dict) else nodes if isinstance(nodes, list) else []
    seen_nodes, seen_homeegrams = set(), set()
    for _, relation in relationships:
        node_id, homeegram_id = relation.get("node_id"), relation.get("homeegram_id")
        if type(node_id) is int and node_id != 0 and node_id not in seen_nodes:
            seen_nodes.add(node_id)
            node = _lookup(nodes, node_id)
            member = {"node_id": node_id, "name": _text((node or {}).get("name"), "Gerät " + str(node_id)),
                      "missing": node is None, "attributes": [], "applicable": False, "missing_attribute": False}
            attributes = (node or {}).get("attributes")
            attributes = attributes if isinstance(attributes, list) else []
            seen_attributes = set()
            for attribute in attributes:
                if not isinstance(attribute, dict) or type(attribute.get("type")) is not int or attribute["type"] != result.get("attribute_type"):
                    continue
                attribute_id = attribute.get("id")
                if type(attribute_id) is not int or attribute_id in seen_attributes:
                    continue
                seen_attributes.add(attribute_id)
                member["attributes"].append(_safe_attribute(attribute, section, result))
            if not member["attributes"]:
                member["missing_attribute"] = True
                member["attributes"].append({"id": None, "type": result.get("attribute_type"),
                    "name": group["attribute_name"], "missing": True, "applicable": False,
                    "current_value_label": "Nicht verfügbar", "target_value_label": _target_label(section, result)})
                warnings.append("Gerät " + str(node_id) + ": passendes Attribut fehlt; kein aufgelöstes Geräte-Ziel.")
            member["applicable"] = any(attribute["applicable"] for attribute in member["attributes"])
            if node is None:
                warnings.append("Gerät " + str(node_id) + " fehlt in dieser homee-Quelle.")
                group["membership_complete"] = False
            if section == "actions" and not member["applicable"] and not member["missing_attribute"]:
                warnings.append("Gerät " + str(node_id) + ": passendes Attribut ist nicht als schreibbar bekannt.")
            group["members"].append(member)
        if type(homeegram_id) is int and homeegram_id > 0 and homeegram_id not in seen_homeegrams:
            seen_homeegrams.add(homeegram_id)
            homeegram = _lookup(resources.get("homeegrams"), homeegram_id)
            group["homeegrams"].append({"homeegram_id": homeegram_id,
                "name": _text((homeegram or {}).get("name"), "Homeegramm " + str(homeegram_id)),
                "missing": homeegram is None, "applicable": False})
            if homeegram is None:
                warnings.append("Homeegramm " + str(homeegram_id) + " fehlt in dieser homee-Quelle.")
                group["membership_complete"] = False
    if group["homeegrams"]:
        warnings.append("HG-Mitglieder sind keine Geräte und werden nicht als Ziel dieses Geräteattributs ausgewiesen.")
    if section == "conditions" and result.get("scope") not in SCOPES:
        warnings.append("Unbekannter Gruppenumfang: " + str(result.get("scope")) + ".")
    if section != "actions" and result.get("operand") not in (None, 1):
        warnings.append("Der Vergleich nutzt keinen aufgelösten festen Wert; Operand " + str(result.get("operand")) + ".")
    result["group"] = group
    result["summary"] = _summary(section, result, group)
    return result


def project_homeegram_groups(adapter, homeegram):
    """Group-only read projection for callers merging it into a sanitized HG.

    It intentionally returns no other raw HG fields or special element payloads.
    """
    result = {}
    homeegram = homeegram if isinstance(homeegram, dict) else {}
    for section in ("triggers", "conditions", "actions"):
        contents = homeegram.get(section)
        contents = contents if isinstance(contents, dict) else {}
        rows = contents.get("group_" + section)
        rows = rows if isinstance(rows, list) else []
        result[section] = {"group_" + section: [expand_group_element(adapter, section, row)
                           for row in rows if isinstance(row, dict)]}
    return result
