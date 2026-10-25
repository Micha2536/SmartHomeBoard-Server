"""Shared no-code server automation normalization; no application startup side effects."""

import datetime as dt
import math
import re
import uuid

from .automations import AutomationEngine


def validate_web_automation(source, nodes, database):
    """Validate and normalize the no-code automation editor payload."""
    if not isinstance(source, dict):
        raise ValueError("Die Automation ist kein gültiges JSON-Objekt")
    rule_id = str(source.get("id") or uuid.uuid4()).strip()
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", rule_id):
        raise ValueError("Die Automations-ID ist ungültig")
    name = str(source.get("name", "")).strip()
    if not name:
        raise ValueError("Der Name der Automation fehlt")
    node_map = {int(node.get("id", 0)): node for node in nodes}
    triggers = [
        _validate_web_condition(item, node_map, database, trigger=True)
        for item in source.get("triggers", [])
    ]
    conditions = [
        _validate_web_condition(item, node_map, database, trigger=False)
        for item in source.get("conditions", [])
    ]
    else_conditions = [
        _validate_web_condition(item, node_map, database, trigger=False)
        for item in source.get("elseConditions", [])
    ]
    actions = [_validate_web_action(item, node_map) for item in source.get("actions", [])]
    direct_else_actions = [_validate_web_action(item, node_map) for item in source.get("directElseActions", [])]
    else_actions = [_validate_web_action(item, node_map) for item in source.get("elseActions", [])]
    else_else_actions = [_validate_web_action(item, node_map) for item in source.get("elseElseActions", [])]
    prerequisites = [_validate_web_condition(item, node_map, database, trigger=False) for item in source.get("prerequisites", [])]
    always_actions = [_validate_web_action(item, node_map) for item in source.get("alwaysActions", [])]
    if not (actions or always_actions or direct_else_actions or else_actions or else_else_actions):
        raise ValueError("Mindestens eine Aktion ist erforderlich")
    validation = str(source.get("conditionValidation", "triggerTime"))
    if validation not in ("triggerTime", "executionTime", "both"):
        validation = "triggerTime"
    return {
        "id": rule_id,
        "name": name[:120],
        "isEnabled": bool(source.get("isEnabled", True)),
        "triggers": triggers,
        "conditions": conditions,
        "prerequisites": prerequisites,
        "alwaysActions": always_actions,
        "elseConditions": else_conditions,
        "actions": actions,
        "directElseActions": direct_else_actions,
        "elseActions": else_actions,
        "elseElseActions": else_else_actions,
        "cooldownSeconds": max(0, min(86400, _finite_number(source.get("cooldownSeconds", 30), "Mindestpause"))),
        "conditionValidation": validation,
    }


def _validate_web_condition(source, nodes, database, trigger):
    if not isinstance(source, dict):
        raise ValueError("Auslöser oder Bedingung ist ungültig")
    allowed = ("attribute", "attributeChanged", "attributeChangedBy", "groupAttribute", "timeDaily", "timeOnce") if trigger else ("attribute", "groupAttribute", "timeAfter", "timeBefore")
    kind = str(source.get("kind", "attribute"))
    if kind not in allowed:
        raise ValueError(f"{kind} wird vom Server nicht als {'Auslöser' if trigger else 'Bedingung'} unterstützt")
    result = {"id": str(source.get("id") or uuid.uuid4()), "kind": kind}
    result["holdSeconds"] = max(0, min(86400, _finite_number(source.get("holdSeconds", 0), "Mindestdauer")))
    if kind.startswith("time"):
        result["minuteOfDay"] = max(0, min(1439, int(_finite_number(source.get("minuteOfDay", 0), "Uhrzeit"))))
        if kind == "timeOnce":
            scheduled = str(source.get("scheduledAt", "")).strip()
            try:
                dt.datetime.fromisoformat(scheduled.replace("Z", "+00:00"))
            except ValueError as error:
                raise ValueError("Der einmalige Zeitpunkt ist ungültig") from error
            result.update({"scheduledAt": scheduled, "isConsumed": bool(source.get("isConsumed", False))})
        return result
    if kind == "attributeChanged":
        node_id, attribute_id, _attribute = _automation_attribute(source, nodes)
        result.update({"nodeID": node_id, "attributeID": attribute_id})
        return result
    comparison = str(source.get("comparison", "equal"))
    if comparison not in ("equal", "notEqual", "greater", "less"):
        raise ValueError("Der Vergleich ist ungültig")
    if kind == "groupAttribute":
        group_id = str(source.get("groupID", "")).strip()
        groups = database.setting("device_groups", []) or []
        if not any(str(item.get("id")) == group_id for item in groups):
            raise ValueError("Die gewählte Gerätegruppe ist nicht mehr verfügbar")
        aggregation = str(source.get("aggregation", "any"))
        if aggregation not in ("any", "none", "all", "atLeast", "atMost", "exactly"):
            raise ValueError("Die Gruppenauswertung ist ungültig")
        result.update({
            "groupID": group_id,
            "attributeType": int(_finite_number(source.get("attributeType", 0), "Attributtyp")),
            "attributeName": str(source.get("attributeName", ""))[:120],
            "aggregation": aggregation,
            "count": max(0, int(_finite_number(source.get("count", 1), "Geräteanzahl"))),
            "comparison": comparison,
            "value": _finite_number(source.get("value", 0), "Vergleichswert"),
        })
        return result
    node_id, attribute_id, _attribute = _automation_attribute(source, nodes)
    result.update({"nodeID": node_id, "attributeID": attribute_id, "comparison": comparison,
                   "value": _finite_number(source.get("value", 0), "Vergleichswert")})
    if kind == "attributeChangedBy":
        unit = str(source.get("changeUnit", "absolute"))
        result["changeUnit"] = unit if unit in ("absolute", "percent") else "absolute"
    return result


def _validate_web_action(source, nodes):
    if not isinstance(source, dict):
        raise ValueError("Eine Aktion ist ungültig")
    kind = str(source.get("kind", "setAttribute"))
    result = {
        "id": str(source.get("id") or uuid.uuid4()), "kind": kind,
        "delaySeconds": max(0, min(86400, _finite_number(source.get("delaySeconds", 0), "Verzögerung"))),
    }
    if kind in ("setAttribute", "toggleAttribute"):
        node_id, attribute_id, attribute = _automation_attribute(source, nodes)
        if not attribute.get("editable", False):
            raise ValueError(f"{attribute.get('name', 'Attribut')} ist nicht steuerbar")
        result.update({"nodeID": node_id, "attributeID": attribute_id})
        if kind == "setAttribute":
            value = _finite_number(source.get("value", 0), "Zielwert")
            minimum, maximum = attribute.get("minimum"), attribute.get("maximum")
            if minimum is not None and value < float(minimum):
                raise ValueError(f"Zielwert für {attribute.get('name')} liegt unter dem Minimum")
            if maximum is not None and value > float(maximum):
                raise ValueError(f"Zielwert für {attribute.get('name')} liegt über dem Maximum")
            result["value"] = value
        elif not AutomationEngine._is_toggleable(attribute):
            raise ValueError("Umschalten ist nur bei einem schreibbaren 0/1-Attribut möglich")
        return result
    if kind == "roborockCleaning":
        node_id = _integer_id(source.get("nodeID", 0), "Roborock")
        node = nodes.get(node_id)
        if not node or node.get("integration_module") != "roborock":
            raise ValueError("Der gewählte Roborock läuft nicht als lokales Servermodul")
        target = str(source.get("roborockTarget", "complete"))
        if target not in ("complete", "room", "routine", "spot"):
            raise ValueError("Das Roborock-Ziel ist ungültig")
        result.update({
            "nodeID": node_id, "roborockTarget": target,
            "roborockTargetValue": _finite_number(source.get("roborockTargetValue", -1), "Reinigungsziel"),
        })
        for key in ("roborockCleaningType", "roborockSuction", "roborockWater"):
            if source.get(key) not in (None, ""):
                result[key] = _finite_number(source[key], key)
        return result
    if kind == "sonosSpeak":
        node_id = _integer_id(source.get("nodeID", 0), "Sonos-Lautsprecher")
        node = nodes.get(node_id)
        if not node or node.get("integration_module") != "sonos":
            raise ValueError("Der gewählte Sonos-Lautsprecher läuft nicht als lokales Servermodul")
        text = str(source.get("sonosText") or "").strip()
        if not text:
            raise ValueError("Für die Sonos-Sprachausgabe fehlt der Text")
        if len(text) > 500:
            raise ValueError("Die Sonos-Sprachausgabe darf höchstens 500 Zeichen enthalten")
        result.update({"nodeID": node_id, "sonosText": text,
                       "sonosRestorePlayback": source.get("sonosRestorePlayback", True) is not False})
        if source.get("sonosVolume") not in (None, ""):
            result["sonosVolume"] = max(0, min(100, _finite_number(source["sonosVolume"], "Ansagelautstärke")))
        return result
    if kind == "controlAutomation":
        automation_id = str(source.get("automationID", "")).strip()
        if not automation_id:
            raise ValueError("Für die Automationssteuerung muss eine Zielautomation gewählt werden")
        operation = str(source.get("automationOperation", "play"))
        if operation not in ("play", "stop", "enable", "disable"):
            raise ValueError("Der Automationsbefehl ist ungültig")
        result.update({"automationID": automation_id, "automationOperation": operation})
        return result
    if kind == "serverPushNotification":
        result.update({
            "title": str(source.get("title") or "SmartHomeBoard")[:180],
            "message": str(source.get("message") or "Automation wurde ausgeführt.")[:1500],
            "includeAttributeValue": bool(source.get("includeAttributeValue", False)),
            "pushDeviceIDs": [str(item) for item in source.get("pushDeviceIDs", []) if str(item)],
        })
        if result["includeAttributeValue"]:
            node_id, attribute_id, _attribute = _automation_attribute(source, nodes)
            result.update({"nodeID": node_id, "attributeID": attribute_id})
        return result
    raise ValueError("Diese Automationsaktion wird vom Webportal nicht unterstützt")


def _automation_attribute(source, nodes):
    node_id = _integer_id(source.get("nodeID", 0), "Gerät")
    attribute_id = _integer_id(source.get("attributeID", 0), "Attribut")
    node = nodes.get(node_id)
    attribute = next((item for item in (node or {}).get("attributes", []) if int(item.get("id", 0)) == attribute_id), None)
    if not node or not attribute:
        raise ValueError("Das gewählte Gerät oder Attribut ist nicht mehr verfügbar")
    return node_id, attribute_id, attribute


def _integer_id(value, title):
    # Native SHB IDs can exceed the lossless integer range of a float. Keep
    # legacy form/string coercion without rounding an already integer JSON ID.
    return value if type(value) is int else int(_finite_number(value, title))


def _finite_number(value, title):
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{title} ist keine Zahl") from error
    if not math.isfinite(number):
        raise ValueError(f"{title} ist ungültig")
    return number
