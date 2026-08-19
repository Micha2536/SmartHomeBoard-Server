import asyncio
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from server.automations import AutomationEngine
from server.database import Database
from server.runtime import Runtime


class DemoRegistry:
    def __init__(self):
        spec = importlib.util.spec_from_file_location("test_demo_module", ROOT / "modules/demo/module.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.modules = {"demo": {"manifest": self.module.manifest(), "factory": self.module.create}}

    def load(self): return [self.module.manifest()]
    def create(self, module_id, configuration, context): return self.modules[module_id]["factory"](configuration, context)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(self.temp.name)
        self.runtime = Runtime(self.database, DemoRegistry())
        self.database.save_integration({"id": "demo-1", "module_id": "demo", "name": "Server-Demo", "enabled": True,
                                        "configuration": {"initial_value": 20, "poll_seconds": 60}, "status": None, "error": None, "device_count": 0})
        await self.runtime.start()

    async def asyncTearDown(self):
        await self.runtime.shutdown()
        self.temp.cleanup()

    async def test_module_publishes_and_updates_node(self):
        nodes = self.database.nodes()
        self.assertEqual(len(nodes), 1)
        node = nodes[0]
        self.assertEqual(node["integration_source"], "server")
        self.assertEqual(node["integration_id"], "demo-1")
        self.assertEqual(node["integration_module"], "demo")
        self.assertEqual(node["integration_name"], "Server-Demo")
        target = next(item for item in node["attributes"] if item["editable"])
        await self.runtime.set_value(node["id"], target["id"], 23.5)
        updated = self.database.nodes()[0]
        changed = next(item for item in updated["attributes"] if item["id"] == target["id"])
        self.assertEqual(changed["current_value"], 23.5)
        self.assertEqual(changed["last_value"], 20)

    async def test_module_action_is_dispatched_to_active_adapter(self):
        calls = []

        async def action(action_id, payload):
            calls.append((action_id, payload))
            return {"accepted": True}

        self.runtime.adapters["demo-1"].action = action
        result = await self.runtime.integration_action("demo-1", "learn", {"seconds": 60})
        self.assertEqual(calls, [("learn", {"seconds": 60})])
        self.assertEqual(result, {"accepted": True})

    async def test_active_integration_test_reuses_existing_adapter(self):
        adapter = self.runtime.adapters["demo-1"]
        checks = []

        async def health_check():
            checks.append("checked")

        adapter.health_check = health_check
        instance = self.database.integration("demo-1")
        await self.runtime.test_instance(instance)
        self.assertIs(adapter, self.runtime.adapters["demo-1"])
        self.assertEqual(checks, ["checked"])

    async def test_scheduled_restart_returns_immediately_with_starting_status(self):
        gate = asyncio.Event()
        calls = []
        original_restart = self.runtime.restart_instance

        async def delayed_restart(instance):
            calls.append(instance["id"])
            await gate.wait()

        self.runtime.restart_instance = delayed_restart
        try:
            instance = self.database.integration("demo-1")
            task = self.runtime.schedule_restart(instance)
            await asyncio.sleep(0)
            self.assertEqual(self.database.integration("demo-1")["status"], "Verbindung wird aufgebaut")
            self.assertEqual(calls, ["demo-1"])
            self.assertFalse(task.done())
            gate.set()
            await task
        finally:
            self.runtime.restart_instance = original_restart

    async def test_automation_is_persisted(self):
        engine = AutomationEngine(self.runtime)
        rules = [{"id": "rule", "name": "Test", "isEnabled": True, "triggers": [], "conditions": [], "actions": []}]
        engine.replace(rules)
        self.assertEqual(self.database.setting("automations"), rules)
        status = engine.status()
        self.assertEqual(status["count"], 1)
        self.assertEqual(status["automations"][0]["id"], "rule")
        self.assertIsNotNone(status["synced_at"])

    async def test_server_owned_automation_survives_app_synchronization(self):
        engine = AutomationEngine(self.runtime)
        server_rule = {"id": "server-rule", "name": "Nur Server", "isEnabled": True,
                       "triggers": [], "conditions": [], "actions": []}
        app_rule = {"id": "app-rule", "name": "Vom iPad", "isEnabled": True,
                    "triggers": [], "conditions": [], "actions": []}
        engine.upsert_server(server_rule)
        engine.replace_from_app([app_rule])

        self.assertEqual({"server-rule", "app-rule"}, {rule["id"] for rule in engine.rules})
        origins = {item["id"]: item["origin"] for item in engine.status()["automations"]}
        self.assertEqual("server", origins["server-rule"])
        self.assertEqual("app", origins["app-rule"])

    async def test_separate_api_and_setup_engines_share_current_automation_state(self):
        api_engine = AutomationEngine(self.runtime)
        setup_engine = AutomationEngine(self.runtime)
        app_rule = {"id": "app-rule", "name": "Vom iPad", "isEnabled": True,
                    "triggers": [], "conditions": [], "actions": []}
        server_rule = {"id": "server-rule", "name": "Vom Webportal", "isEnabled": True,
                       "triggers": [], "conditions": [], "actions": []}

        api_engine.replace_from_app([app_rule])
        self.assertEqual(["app-rule"], [rule["id"] for rule in setup_engine.status()["automations"]])

        setup_engine.upsert_server(server_rule)
        api_engine.replace_from_app([{**app_rule, "name": "Vom iPad geändert"}])
        self.assertEqual(
            {"app-rule", "server-rule"},
            {rule["id"] for rule in setup_engine.status()["automations"]},
        )
        self.assertEqual("Vom iPad geändert", next(rule for rule in setup_engine.rules if rule["id"] == "app-rule")["name"])

    async def test_web_deleted_rule_is_not_recreated_by_next_app_sync(self):
        engine = AutomationEngine(self.runtime)
        rule = {"id": "shared-rule", "name": "Geteilt", "isEnabled": True,
                "triggers": [], "conditions": [], "actions": []}
        engine.replace_from_app([rule])
        self.assertTrue(engine.delete_server("shared-rule"))
        engine.replace_from_app([rule])
        self.assertEqual([], engine.rules)

    async def test_automation_threshold_and_not_equal_trigger_semantics(self):
        engine = AutomationEngine(self.runtime)
        self.assertTrue(engine._trigger_compare(9, 11, 10, "greater"))
        self.assertFalse(engine._trigger_compare(11, 12, 10, "greater"))
        self.assertTrue(engine._trigger_compare(11, 9, 10, "less"))
        self.assertFalse(engine._trigger_compare(9, 8, 10, "less"))
        self.assertTrue(engine._trigger_compare(9, 10, 10, "equal"))
        self.assertFalse(engine._trigger_compare(10, 10, 10, "equal"))
        self.assertTrue(engine._trigger_compare(11, 12, 10, "notEqual"))
        self.assertTrue(engine._trigger_compare(10, 11, 10, "notEqual"))
        self.assertFalse(engine._trigger_compare(11, 10, 10, "notEqual"))

    async def test_automation_toggle_accepts_only_editable_binary_attributes(self):
        engine = AutomationEngine(self.runtime)
        self.assertTrue(engine._is_toggleable({"type": 1, "editable": True}))
        self.assertTrue(engine._is_toggleable({"type": 999, "editable": True, "minimum": 0, "maximum": 1}))
        self.assertFalse(engine._is_toggleable({"type": 1, "editable": False}))
        self.assertFalse(engine._is_toggleable({"type": 6, "editable": True, "minimum": 5, "maximum": 35}))

    async def test_app_integration_action_is_forwarded_to_connected_app(self):
        engine = AutomationEngine(self.runtime)
        messages = []
        original_broadcast = self.runtime.broadcast

        async def capture(payload):
            messages.append(payload)

        self.runtime.broadcast = capture
        try:
            action = {"id": "action", "kind": "setAttribute", "nodeID": 123, "attributeID": 456, "value": 1}
            await engine._execute({"id": "rule", "name": "Hybrid"}, action, None, "rule:action")
        finally:
            self.runtime.broadcast = original_broadcast

        self.assertEqual(messages[0]["type"], "client_action")
        self.assertEqual(messages[0]["action"], action)

    async def test_roborock_automation_is_dispatched_as_one_compound_action(self):
        engine = AutomationEngine(self.runtime)
        node = self.database.nodes()[0]
        node["integration_module"] = "roborock"
        node["integration_id"] = "demo-1"
        self.database.save_node("demo-1", node)
        calls = []
        original_action = self.runtime.integration_action

        async def capture(integration_id, action_id, payload=None):
            calls.append((integration_id, action_id, payload))

        self.runtime.integration_action = capture
        action = {
            "id": "roborock-action",
            "kind": "roborockCleaning",
            "nodeID": node["id"],
            "roborockCleaningType": 2,
            "roborockSuction": 103,
            "roborockWater": 202,
            "roborockTarget": "room",
            "roborockTargetValue": 17,
        }
        try:
            await engine._execute({"id": "rule", "name": "Küche"}, action, None, "rule:roborock-action")
        finally:
            self.runtime.integration_action = original_action

        self.assertEqual(calls, [("demo-1", "automation_cleaning", action)])
        self.assertIn("atomar am Roborock ausgeführt", engine.events[-1]["message"])

    async def test_automation_can_stop_disable_enable_and_play_another_rule(self):
        engine = AutomationEngine(self.runtime)
        target = {
            "id": "target", "name": "Ziel", "isEnabled": True,
            "triggers": [], "conditions": [],
            "actions": [{"id": "wait", "kind": "showPopup", "delaySeconds": 60}],
        }
        engine.replace([target])
        await engine._trigger(target, {}, ignore_cooldown=True)
        self.assertTrue(engine.status()["automations"][0]["running"])

        stopped = await engine.control("target", "stop", source_rule_id="source")
        self.assertTrue(stopped["ok"])
        self.assertFalse(engine.status()["automations"][0]["running"])

        disabled = await engine.control("target", "disable", source_rule_id="source")
        self.assertTrue(disabled["ok"])
        self.assertFalse(target["isEnabled"])
        self.assertFalse((await engine.control("target", "play", source_rule_id="source"))["ok"])

        self.assertTrue((await engine.control("target", "enable", source_rule_id="source"))["ok"])
        self.assertTrue((await engine.control("target", "play", source_rule_id="source"))["ok"])
        engine._cancel_rule_tasks("target")

    async def test_push_action_passes_selected_recipients_and_device_value(self):
        engine = AutomationEngine(self.runtime)
        node = self.database.nodes()[0]
        attribute = node["attributes"][0]
        calls = []

        class Push:
            async def send(_self, title, message, recipients):
                calls.append((title, message, recipients))
                return 1

        engine.push_service = Push()
        action = {
            "id": "push", "kind": "serverPushNotification", "title": "Alarm {name}",
            "message": "{attribute}: {value} {unit}", "includeAttributeValue": True,
            "nodeID": node["id"], "attributeID": attribute["id"], "pushDeviceIDs": ["ipad-flur"],
        }
        await engine._execute({"id": "source", "name": "Quelle"}, action, {}, "source:push")
        self.assertEqual(["ipad-flur"], calls[0][2])
        self.assertIn(node["name"], calls[0][0])
        self.assertEqual(
            f"{attribute['name']}: {attribute['current_value']:g} {attribute['unit']}",
            calls[0][1],
        )


if __name__ == "__main__":
    unittest.main()
