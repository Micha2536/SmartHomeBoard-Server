import asyncio
import datetime as dt
import logging
import time
from zoneinfo import ZoneInfo

log = logging.getLogger("smarthomeboard.automations")


class AutomationEngine:
    def __init__(self, runtime, timezone="Europe/Berlin"):
        self.runtime = runtime
        self.timezone = ZoneInfo(timezone)
        self.rules = runtime.database.setting("automations", [])
        self.server_owned_ids = set(runtime.database.setting("automation_server_owned_ids", []))
        self.deleted_ids = set(runtime.database.setting("automation_deleted_ids", []))
        self.last_triggered = runtime.database.setting("automation_last_triggered", {})
        self.synced_at = runtime.database.setting("automations_synced_at", None)
        stored_events = runtime.database.setting("automation_events", [])
        self.events = stored_events[-50:]
        if len(stored_events) != len(self.events):
            runtime.database.set_setting("automation_events", self.events)
        self.condition_states = {}
        self.action_tasks = {}
        self.timer_task = None

    async def start(self):
        self.timer_task = asyncio.create_task(self._timer())

    async def stop(self):
        if self.timer_task:
            self.timer_task.cancel()
        for task in self.action_tasks.values():
            task.cancel()

    def replace(self, rules):
        """Replace every rule (kept for imports/tests and explicit administration)."""
        self.rules = rules
        self.server_owned_ids.intersection_update(str(rule.get("id", "")) for rule in rules)
        self._persist_rules("Automationen vollständig ersetzt")

    def replace_from_app(self, rules):
        """Synchronize app-owned rules without overwriting web-owned rules."""
        preserved = [rule for rule in self.rules if str(rule.get("id", "")) in self.server_owned_ids]
        incoming = [
            rule for rule in rules
            if str(rule.get("id", "")) not in self.server_owned_ids
            and str(rule.get("id", "")) not in self.deleted_ids
        ]
        self.rules = preserved + incoming
        self._persist_rules(f"{len(incoming)} App-Automationen synchronisiert; {len(preserved)} Serverregeln beibehalten")

    def upsert_server(self, rule):
        rule_id = str(rule.get("id", ""))
        previous = next((item for item in self.rules if str(item.get("id", "")) == rule_id), None)
        self.rules = [item for item in self.rules if str(item.get("id", "")) != rule_id]
        self.rules.append(rule)
        self.server_owned_ids.add(rule_id)
        self.deleted_ids.discard(rule_id)
        self._cancel_rule_tasks(rule_id)
        self._persist_rules(f"Serverautomation {'aktualisiert' if previous else 'angelegt'}: {rule.get('name', 'Automation')}")

    def delete_server(self, rule_id):
        rule_id = str(rule_id)
        previous = next((item for item in self.rules if str(item.get("id", "")) == rule_id), None)
        if not previous:
            return False
        self.rules = [item for item in self.rules if str(item.get("id", "")) != rule_id]
        self.server_owned_ids.discard(rule_id)
        self.deleted_ids.add(rule_id)
        if len(self.deleted_ids) > 500:
            self.deleted_ids = set(list(self.deleted_ids)[-500:])
        self._cancel_rule_tasks(rule_id)
        self._persist_rules(f"Serverautomation gelöscht: {previous.get('name', 'Automation')}")
        return True

    def _cancel_rule_tasks(self, rule_id):
        for key in [key for key in self.action_tasks if key.startswith(f"{rule_id}:")]:
            self.action_tasks.pop(key).cancel()

    def _persist_rules(self, message):
        self.synced_at = time.time()
        self.runtime.database.set_setting("automations", self.rules)
        self.runtime.database.set_setting("automations_synced_at", self.synced_at)
        self.runtime.database.set_setting("automation_server_owned_ids", sorted(self.server_owned_ids))
        self.runtime.database.set_setting("automation_deleted_ids", sorted(self.deleted_ids))
        for task in self.action_tasks.values():
            task.cancel()
        self.action_tasks.clear()
        self._record(None, "info", message)

    def status(self):
        return {
            "count": len(self.rules),
            "synced_at": self.synced_at,
            "automations": [
                {
                    "id": str(rule.get("id", "")),
                    "name": rule.get("name", "Automation"),
                    "enabled": bool(rule.get("isEnabled", True)),
                    "trigger_count": len(rule.get("triggers", [])),
                    "condition_count": len(rule.get("conditions", [])),
                    "action_count": len(rule.get("actions", [])),
                    "last_triggered_at": self.last_triggered.get(str(rule.get("id", ""))),
                    "origin": "server" if str(rule.get("id", "")) in self.server_owned_ids else "app",
                }
                for rule in self.rules
            ],
            "recent_events": list(reversed(self.events[-100:])),
        }

    def _record(self, rule, level, message):
        event = {
            "timestamp": time.time(),
            "rule_id": str(rule.get("id", "")) if rule else None,
            "rule_name": rule.get("name", "Automation") if rule else "System",
            "level": level,
            "message": message,
        }
        self.events.append(event)
        if len(self.events) > 50:
            self.events = self.events[-50:]
        self.runtime.database.set_setting("automation_events", self.events)

    async def test(self, rule_id):
        rule = next((item for item in self.rules if str(item.get("id", "")) == str(rule_id)), None)
        if not rule:
            return {"ok": False, "message": "Automation wurde auf dem Server nicht gefunden."}
        if not rule.get("isEnabled", True):
            self._record(rule, "warning", "Manueller Test abgelehnt: Automation ist deaktiviert")
            return {"ok": False, "message": "Die Automation ist deaktiviert."}
        ok, message = await self._trigger(
            rule,
            {"device_name": "Manueller Test", "attribute_name": "Play", "value": "Test"},
            ignore_cooldown=True,
            source="Manueller Test",
        )
        return {"ok": ok, "message": message}

    async def node_changed(self, previous, node):
        old = {a["id"]: a for a in (previous or {}).get("attributes", [])}
        events = []
        for attribute in node.get("attributes", []):
            before = old.get(attribute["id"], {}).get("current_value")
            after = attribute.get("current_value")
            if before is not None and after is not None and before != after:
                events.append({"node_id": node["id"], "attribute_id": attribute["id"], "previous": before, "value": after,
                               "device_name": node.get("name", "Gerät"), "attribute_name": attribute.get("name", "Wert")})
        for rule in self.rules:
            if not rule.get("isEnabled", True):
                continue
            for event in events:
                if any(self._event_trigger(trigger, event) for trigger in rule.get("triggers", [])):
                    await self._trigger(rule, event)
                    break

    async def _timer(self):
        while True:
            now = dt.datetime.now(self.timezone)
            for rule in self.rules:
                if not rule.get("isEnabled", True):
                    continue
                for trigger in rule.get("triggers", []):
                    key = f"{rule.get('id')}:{trigger.get('id')}"
                    matches = self._time_trigger(trigger, now)
                    if matches and not self.condition_states.get(key, False):
                        await self._trigger(rule, {"device_name": "Zeitplan", "attribute_name": "Zeit", "value": now.strftime("%H:%M")})
                    self.condition_states[key] = matches
            await asyncio.sleep(1)

    async def _trigger(self, rule, event, ignore_cooldown=False, source="Auslöser"):
        now = dt.datetime.now(self.timezone).timestamp()
        cooldown = max(0, float(rule.get("cooldownSeconds", 30)))
        if not ignore_cooldown and now - self.last_triggered.get(str(rule.get("id", "")), 0) < cooldown:
            message = f"{source} erkannt, aber Mindestpause ist noch aktiv"
            self._record(rule, "info", message)
            return False, message
        validation = rule.get("conditionValidation", "triggerTime")
        if validation in ("triggerTime", "both") and not self._conditions_match(rule):
            message = f"{source} erkannt, aber UND-Bedingungen sind nicht erfüllt"
            self._record(rule, "warning", message)
            return False, message
        rule_id = str(rule.get("id", ""))
        self.last_triggered[rule_id] = now
        self.runtime.database.set_setting("automation_last_triggered", self.last_triggered)
        message = f"{source} angenommen – {len(rule.get('actions', []))} Aktion(en) gestartet"
        self._record(rule, "success", message)
        for action in rule.get("actions", []):
            key = f"{rule.get('id')}:{action.get('id')}"
            previous = self.action_tasks.pop(key, None)
            if previous:
                previous.cancel()
            self.action_tasks[key] = asyncio.create_task(self._execute(rule, action, event, key))
        return True, message

    async def _execute(self, rule, action, event, key):
        try:
            delay = max(0, float(action.get("delaySeconds", 0)))
            if delay:
                self._record(rule, "info", f"Aktion {action.get('kind', 'unbekannt')} wartet {delay:g} Sekunden")
                await asyncio.sleep(delay)
            if rule.get("conditionValidation") in ("executionTime", "both") and not self._conditions_match(rule):
                self._record(rule, "warning", "Verzögerte Aktion übersprungen: Bedingungen nicht mehr erfüllt")
                return
            kind = action.get("kind")
            if kind == "setAttribute":
                if self._attribute(action):
                    await self.runtime.set_value(int(action.get("nodeID", 0)), int(action.get("attributeID", 0)), float(action.get("value", 0)))
                    result = "am Server ausgeführt"
                else:
                    await self.runtime.broadcast({"type": "client_action", "action": action, "context": event})
                    result = "an die App weitergeleitet"
            elif kind == "toggleAttribute":
                attribute = self._attribute(action)
                if attribute and not self._is_toggleable(attribute):
                    raise ValueError("Das ausgewählte Attribut ist kein schaltbarer 0/1-Wert")
                if attribute:
                    current = attribute.get("current_value")
                    if current is None:
                        current = attribute.get("target_value", 0)
                    value = 0 if float(current or 0) >= 0.5 else 1
                    await self.runtime.set_value(int(action.get("nodeID", 0)), int(action.get("attributeID", 0)), value)
                    result = "am Server ausgeführt"
                else:
                    await self.runtime.broadcast({"type": "client_action", "action": action, "context": event})
                    result = "an die App weitergeleitet"
            elif kind == "roborockCleaning":
                node = self._node(int(action.get("nodeID", 0)))
                if not node or node.get("integration_module") != "roborock":
                    raise ValueError("Der ausgewählte Roborock ist nicht als aktive Serverintegration verfügbar")
                integration_id = node.get("integration_id")
                if not integration_id:
                    raise ValueError("Dem Roborock fehlt die Server-Integrations-ID")
                await self.runtime.integration_action(integration_id, "automation_cleaning", action)
                result = "atomar am Roborock ausgeführt"
            else:
                await self.runtime.broadcast({"type": "client_action", "action": action, "context": event})
                result = "an die App weitergeleitet"
            self._record(rule, "success", f"Aktion {kind} {result}")
        except asyncio.CancelledError:
            self._record(rule, "info", "Vorheriger Zeitablauf durch erneuten Auslöser abgebrochen")
        except Exception as error:
            self._record(rule, "error", f"Aktion fehlgeschlagen: {error}")
            log.exception("Automation %s konnte nicht ausgeführt werden", rule.get("name"))
        finally:
            self.action_tasks.pop(key, None)

    def _conditions_match(self, rule):
        return all(self._condition(condition) for condition in rule.get("conditions", []))

    def _condition(self, condition):
        kind = condition.get("kind")
        now = dt.datetime.now(self.timezone)
        if kind == "timeAfter":
            return now.hour * 60 + now.minute >= int(condition.get("minuteOfDay", 0))
        if kind == "timeBefore":
            return now.hour * 60 + now.minute <= int(condition.get("minuteOfDay", 0))
        if kind == "attribute":
            value = self._attribute_value(condition)
            return value is not None and self._compare(value, float(condition.get("value", 0)), condition.get("comparison", "equal"))
        return True

    def _event_trigger(self, trigger, event):
        if int(trigger.get("nodeID", 0)) != event["node_id"] or int(trigger.get("attributeID", 0)) != event["attribute_id"]:
            return False
        kind = trigger.get("kind")
        if kind == "attributeChangedBy":
            change = abs(event["value"] - event["previous"])
            if trigger.get("changeUnit") == "percent":
                change = 100 if event["previous"] == 0 and change else (change / abs(event["previous"]) * 100 if event["previous"] else 0)
            return self._compare(change, abs(float(trigger.get("value", 0))), trigger.get("comparison", "greater"))
        if kind == "attribute":
            return self._trigger_compare(
                event["previous"],
                event["value"],
                float(trigger.get("value", 0)),
                trigger.get("comparison", "equal"),
            )
        return False

    @staticmethod
    def _time_trigger(trigger, now):
        kind = trigger.get("kind")
        if kind == "timeDaily":
            return now.hour * 60 + now.minute == int(trigger.get("minuteOfDay", -1)) and now.second < 2
        if kind == "timeOnce" and not trigger.get("isConsumed", False):
            try:
                target = dt.datetime.fromisoformat(trigger.get("scheduledAt", "").replace("Z", "+00:00"))
                return abs((dt.datetime.now(dt.timezone.utc) - target.astimezone(dt.timezone.utc)).total_seconds()) < 2
            except Exception:
                return False
        return False

    def _attribute_value(self, condition):
        attribute = self._attribute(condition)
        return attribute.get("current_value") if attribute else None

    def _attribute(self, reference):
        node = self._node(int(reference.get("nodeID", 0)))
        if node:
            for attribute in node.get("attributes", []):
                if attribute["id"] == int(reference.get("attributeID", 0)):
                    return attribute
        return None

    def _node(self, node_id):
        return next((node for node in self.runtime.database.nodes() if node["id"] == node_id), None)

    @staticmethod
    def _is_toggleable(attribute):
        if not attribute.get("editable", False):
            return False
        if int(attribute.get("type") or 0) == 1:
            return True
        try:
            return abs(float(attribute.get("minimum"))) < 0.000001 and abs(float(attribute.get("maximum")) - 1) < 0.000001
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _compare(left, right, comparison):
        epsilon = 0.000001
        if comparison == "equal": return abs(left - right) <= epsilon
        if comparison == "notEqual": return abs(left - right) > epsilon
        if comparison == "greater": return left > right
        if comparison == "less": return left < right
        return False

    @staticmethod
    def _trigger_compare(previous, current, target, comparison):
        epsilon = 0.000001
        previous_equal = abs(previous - target) <= epsilon
        current_equal = abs(current - target) <= epsilon
        if comparison == "equal": return not previous_equal and current_equal
        if comparison == "notEqual": return not current_equal
        if comparison == "greater": return previous <= target + epsilon and current > target + epsilon
        if comparison == "less": return previous >= target - epsilon and current < target - epsilon
        return False
