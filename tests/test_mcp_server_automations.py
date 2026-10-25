"""Authenticated MCP coverage for the real server automation engine.

Only a temporary SQLite database is written. There are no adapter connections,
timers, APNs requests or hardware commands in these tests.
"""
from contextlib import asynccontextmanager
from copy import deepcopy
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.automations import AutomationEngine
from server.database import Database
from server.mcp_access import MCPAccess


class ServerAutomationMCPTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = Database(self.folder.name)
        self.nodes = [
            {"id": 12, "name": "Flurlicht", "integration_id": "home", "integration_source": "server",
             "integration_module": "homee", "attributes": [
                 {"id": 55, "name": "An/Aus", "type": 1, "current_value": 0,
                  "minimum": 0, "maximum": 1, "editable": True},
                 {"id": 56, "name": "Leistung", "type": 3, "current_value": 12.5,
                  "minimum": 0, "maximum": 5000, "editable": False}]},
            {"id": 20, "name": "Saugroboter", "integration_id": "vacuum", "integration_source": "server",
             "integration_module": "roborock", "attributes": []},
            {"id": 30, "name": "Sonos Küche", "integration_id": "speaker", "integration_source": "server",
             "integration_module": "sonos", "attributes": []},
        ]
        for integration_id, module_id in (("home", "homee"), ("vacuum", "roborock"), ("speaker", "sonos")):
            self.db.save_integration({"id": integration_id, "module_id": module_id, "name": integration_id,
                "enabled": True, "configuration": {"password": "private-module-password"}})
        for node in self.nodes:
            self.db.save_node(node["integration_id"], node)
        self.db.set_setting("device_groups", [{"id": "lights", "name": "Flurlichter", "node_ids": [12]}])
        self.db.set_setting("push_devices", [{"id": "phone", "device_name": "Test-iPhone",
            "device_token": "private-apns-token", "environment": "production"}])
        self.runtime = SimpleNamespace(database=self.db, adapters={
            "home": SimpleNamespace(socket=object(), nodes={12: self.nodes[0]}, resources={"homeegrams": []}),
            "vacuum": SimpleNamespace(), "speaker": SimpleNamespace()},
            visible_nodes=self.db.nodes, set_value=AsyncMock(), integration_action=AsyncMock(), broadcast=AsyncMock())
        self.engine = AutomationEngine(self.runtime)
        self.engine.push_service = SimpleNamespace(send=AsyncMock(), status=lambda: {
            "configured": True, "devices": [{"id": "phone", "name": "Test-iPhone"}]})
        self.engine.test = AsyncMock()
        self.engine._trigger = AsyncMock()
        self.runtime.automation_engine = self.engine
        self.target_id = str(uuid.uuid4())
        self.engine.upsert_server({"id": self.target_id, "name": "Zielautomation", "isEnabled": False,
            "triggers": [], "conditions": [], "actions": [
                {"id": "target-action", "kind": "setAttribute", "nodeID": 12, "attributeID": 55, "value": 0}]})
        self.access = MCPAccess(self.db, self.runtime, lambda: "admin")

        @asynccontextmanager
        async def lifespan(app):
            async with self.access.server.session_manager.run():
                yield

        self.app = FastAPI(lifespan=lifespan)
        self.app.include_router(self.access.router)
        self.app.mount("/mcp", self.access.http_app)
        self.client = TestClient(self.app).__enter__()
        self.admin = {"Authorization": "Bearer admin"}
        self.configure()
        token = self.client.post("/api/v1/mcp/rotate-token", headers=self.admin).json()["token"]
        self.headers = {"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.db._connection.close()
        self.folder.cleanup()

    def configure(self, **grants):
        response = self.client.put("/api/v1/mcp", headers=self.admin, json={"enabled": True, **grants})
        self.assertEqual(200, response.status_code, response.text)

    def request(self, method, params=None, headers=None):
        return self.client.post("/mcp/", headers=headers or self.headers,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})

    def rpc(self, name, arguments=None):
        response = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        self.assertEqual(200, response.status_code, response.text)
        return response.json()["result"]

    def payload(self, result):
        self.assertFalse(result.get("isError", False), result)
        return result.get("structuredContent") or json.loads(result["content"][0]["text"])

    def rule(self, **changes):
        return {"name": "MCP Flurlicht", "triggers": [],
            "actions": [{"kind": "setAttribute", "nodeID": 12, "attributeID": 55, "value": 1}], **changes}

    def create(self, automation=None, request_id=None):
        return self.rpc("create_server_automation", {"automation": automation or self.rule(),
            "request_id": request_id or str(uuid.uuid4())})

    def get(self, automation_id):
        return self.payload(self.rpc("get_server_automation", {"automation_id": automation_id}))

    def activate_args(self, automation_id, active, **overrides):
        current = self.get(automation_id)
        return {"automation_id": automation_id, "confirmation_name": current["automation"]["name"],
            "active": active, "expected_revision": current["revision"], "request_id": str(uuid.uuid4()), **overrides}

    def assert_no_execution(self):
        self.runtime.set_value.assert_not_awaited()
        self.runtime.integration_action.assert_not_awaited()
        self.runtime.broadcast.assert_not_awaited()
        self.engine.push_service.send.assert_not_awaited()
        self.engine.test.assert_not_awaited()
        self.engine._trigger.assert_not_awaited()
        self.assertIsNone(self.engine.timer_task)

    def test_discovery_exposes_five_tools_with_separate_mutation_annotations(self):
        tools = {tool["name"]: tool for tool in self.request("tools/list").json()["result"]["tools"]}
        self.assertEqual(23, len(tools))
        for name in ("get_server_automation_capabilities", "list_server_automations", "get_server_automation"):
            self.assertTrue(tools[name]["annotations"]["readOnlyHint"])
        for name in ("create_server_automation", "set_server_automation_active"):
            self.assertFalse(tools[name]["annotations"]["readOnlyHint"])
            self.assertTrue(tools[name]["annotations"]["idempotentHint"])
        required = tools["set_server_automation_active"]["inputSchema"]["required"]
        self.assertEqual({"automation_id", "confirmation_name", "active", "expected_revision", "request_id"}, set(required))
        self.assertEqual({"automation", "request_id"}, set(tools["create_server_automation"]["inputSchema"]["required"]))
        status = self.client.get("/api/v1/mcp", headers=self.admin).json()
        self.assertTrue(status["server_automations_supported"])
        self.assertFalse(status["allow_server_automation_creation"])
        self.assertFalse(status["allow_server_automation_activation"])

    def test_read_and_capabilities_need_no_write_grant_and_do_not_leak_secrets(self):
        capabilities = self.payload(self.rpc("get_server_automation_capabilities"))
        self.assertTrue(capabilities["creation_always_disabled"])
        self.assertEqual({"setAttribute", "toggleAttribute", "roborockCleaning", "sonosSpeak",
            "controlAutomation", "serverPushNotification"}, set(capabilities["action_kinds"]))
        self.assertEqual({"attribute", "attributeChanged", "attributeChangedBy", "groupAttribute", "timeDaily", "timeOnce"},
            set(capabilities["trigger_kinds"]))
        self.assertEqual({"attribute", "groupAttribute", "timeAfter", "timeBefore"}, set(capabilities["condition_kinds"]))
        current = self.get(self.target_id)
        self.assertEqual("server", current["origin"])
        self.assertEqual("Zielautomation", current["automation"]["name"])
        self.assertRegex(current["revision"], r"^[a-f0-9]{64}$")
        listing = self.payload(self.rpc("list_server_automations"))
        self.assertEqual(1, listing["total"])
        for data in (capabilities, current, listing):
            self.assertNotIn("private-apns-token", str(data))
            self.assertNotIn("private-module-password", str(data))
        self.assertIn("Test-iPhone", str(capabilities))
        self.assertIn("Flurlichter", str(capabilities))
        self.assert_no_execution()

    def test_creation_requires_own_grant_and_never_inherits_hg_or_device_grants(self):
        self.configure(allow_drafts=True, allow_homee_creation=True, allow_homee_activation=True,
            allow_homee_editing=True, allow_device_control=True)
        self.assertTrue(self.create()["isError"])
        self.assertEqual(1, len(self.engine.rules))
        self.configure(allow_server_automation_creation=True)
        result = self.payload(self.create())
        automation_id = result["automation"]["id"]
        self.assertFalse(result["automation"]["isEnabled"])
        self.assertIn(automation_id, self.engine.server_owned_ids)
        self.assertEqual([], result["automation"]["triggers"])
        self.assertEqual(result["automation"], self.get(automation_id)["automation"])
        self.assertEqual(2, len(self.db.setting("automations")))
        self.assertFalse(self.access.status()["allow_server_automation_activation"])
        self.assert_no_execution()

    def test_older_settings_client_preserves_new_grants_but_explicit_false_revokes(self):
        self.configure(allow_server_automation_creation=True, allow_server_automation_activation=True)
        self.configure(allow_drafts=False)
        self.assertTrue(self.access.status()["allow_server_automation_creation"])
        self.assertTrue(self.access.status()["allow_server_automation_activation"])
        self.configure(allow_server_automation_creation=False, allow_server_automation_activation=False)
        self.assertFalse(self.access.status()["allow_server_automation_creation"])
        self.assertFalse(self.access.status()["allow_server_automation_activation"])
        self.assertTrue(self.create()["isError"])
        self.assert_no_execution()

    def test_full_native_branches_all_action_and_trigger_kinds_are_preserved(self):
        self.configure(allow_server_automation_creation=True)
        group = {"kind": "groupAttribute", "groupID": "lights", "attributeType": 1,
            "attributeName": "An/Aus", "comparison": "equal", "value": 1, "aggregation": "atLeast", "count": 1}
        condition = {"kind": "attribute", "nodeID": 12, "attributeID": 55, "comparison": "equal", "value": 1}
        source = self.rule(
            triggers=[dict(condition, holdSeconds=2), {"kind": "attributeChanged", "nodeID": 12, "attributeID": 55},
                {"kind": "attributeChangedBy", "nodeID": 12, "attributeID": 56, "comparison": "greater", "value": 10,
                 "changeUnit": "percent"}, group, {"kind": "timeDaily", "minuteOfDay": 420},
                {"kind": "timeOnce", "scheduledAt": "2027-01-01T12:30:00+01:00"}],
            prerequisites=[dict(condition, holdSeconds=1.5)],
            conditions=[{"kind": "timeAfter", "minuteOfDay": 400}, {"kind": "timeBefore", "minuteOfDay": 1300}],
            elseConditions=[dict(group, aggregation="exactly", count=0)],
            alwaysActions=[{"kind": "toggleAttribute", "nodeID": 12, "attributeID": 55, "delaySeconds": 2.5}],
            actions=[{"kind": "setAttribute", "nodeID": 12, "attributeID": 55, "value": 1, "delaySeconds": 3}],
            directElseActions=[{"kind": "roborockCleaning", "nodeID": 20, "roborockTarget": "room", "roborockTargetValue": 3,
                "roborockCleaningType": 1, "roborockSuction": 2, "roborockWater": 3, "delaySeconds": 4}],
            elseActions=[{"kind": "sonosSpeak", "nodeID": 30, "sonosText": "Flurlicht {value}",
                "sonosVolume": 35, "sonosRestorePlayback": False, "delaySeconds": 5}],
            elseElseActions=[{"kind": "controlAutomation", "automationID": self.target_id, "automationOperation": "play"},
                {"kind": "serverPushNotification", "title": "Hinweis", "message": "Leistung {value}",
                 "pushDeviceIDs": ["phone"], "includeAttributeValue": True, "nodeID": 12, "attributeID": 56}],
            cooldownSeconds=42.5, conditionValidation="both")
        result = self.payload(self.create(source))["automation"]
        for key in ("triggers", "prerequisites", "conditions", "elseConditions", "alwaysActions", "actions",
                    "directElseActions", "elseActions", "elseElseActions"):
            self.assertEqual(len(source[key]), len(result[key]), key)
            for expected, actual in zip(source[key], result[key]):
                self.assertTrue(actual["id"])
                for field, value in expected.items():
                    self.assertEqual(value, actual[field], (key, field))
        self.assertEqual(42.5, result["cooldownSeconds"])
        self.assertEqual("both", result["conditionValidation"])
        self.assertFalse(result["isEnabled"])
        self.assert_no_execution()

    def test_create_uuid_replay_is_durable_and_payload_collision_is_rejected(self):
        self.configure(allow_server_automation_creation=True)
        request_id = str(uuid.uuid4())
        first = self.payload(self.create(request_id=request_id))
        second = self.payload(self.create(request_id=request_id.upper()))
        self.assertEqual(first["automation"]["id"], second["automation"]["id"])
        self.assertTrue(second["replayed"])
        self.assertEqual(2, len(self.engine.rules))
        self.assertTrue(self.create(self.rule(name="Andere Regel"), request_id)["isError"])
        self.assertTrue(self.create(request_id="not-a-uuid")["isError"])
        self.assertEqual(2, len(self.engine.rules))
        self.assert_no_execution()

    def test_enable_disable_requires_separate_grant_name_revision_and_never_plays(self):
        self.configure(allow_server_automation_creation=True)
        created = self.payload(self.create())
        automation_id = created["automation"]["id"]
        args = self.activate_args(automation_id, True)
        self.assertTrue(self.rpc("set_server_automation_active", args)["isError"])
        self.assertFalse(self.get(automation_id)["automation"]["isEnabled"])
        self.configure(allow_server_automation_activation=True)
        for override in ({"confirmation_name": "Andere Automation"}, {"expected_revision": "0" * 64},
                         {"active": "true"}, {"active": 1}, {"active": None}, {"request_id": "invalid"}):
            self.assertTrue(self.rpc("set_server_automation_active", {**args, **override})["isError"], override)
        enabled = self.payload(self.rpc("set_server_automation_active", args))
        self.assertTrue(enabled["active"])
        self.assertTrue(self.get(automation_id)["automation"]["isEnabled"])
        self.assertTrue(self.payload(self.rpc("set_server_automation_active", args))["replayed"])
        stale = {**args, "request_id": str(uuid.uuid4()), "active": False}
        self.assertTrue(self.rpc("set_server_automation_active", stale)["isError"])
        disabled = self.payload(self.rpc("set_server_automation_active", self.activate_args(automation_id, False)))
        self.assertFalse(disabled["active"])
        self.assertFalse(self.get(automation_id)["automation"]["isEnabled"])
        self.assert_no_execution()

    def test_app_owned_rules_are_readable_but_cannot_be_activated(self):
        app_id = str(uuid.uuid4())
        self.engine.replace_from_app([{**self.rule(), "id": app_id, "isEnabled": False}])
        self.assertEqual("app", self.get(app_id)["origin"])
        self.configure(allow_server_automation_creation=True, allow_server_automation_activation=True)
        self.assertTrue(self.rpc("set_server_automation_active", self.activate_args(app_id, True))["isError"])
        self.assertFalse(self.get(app_id)["automation"]["isEnabled"])
        self.assert_no_execution()

    def test_deactivate_cancels_only_target_waits_and_never_runs_pending_actions(self):
        self.configure(allow_server_automation_creation=True, allow_server_automation_activation=True)
        automation_id = self.payload(self.create())["automation"]["id"]
        self.payload(self.rpc("set_server_automation_active", self.activate_args(automation_id, True)))
        target_wait = SimpleNamespace(cancel=Mock())
        unrelated_wait = SimpleNamespace(cancel=Mock())
        self.engine.action_tasks = {automation_id + ":waiting": target_wait,
                                    self.target_id + ":waiting": unrelated_wait}
        self.engine.action_timing = {automation_id + ":waiting": {"started_at": 1, "deadline": 100},
                                     self.target_id + ":waiting": {"started_at": 1, "deadline": 100}}
        self.payload(self.rpc("set_server_automation_active", self.activate_args(automation_id, False)))
        target_wait.cancel.assert_called_once()
        unrelated_wait.cancel.assert_not_called()
        self.assertNotIn(automation_id + ":waiting", self.engine.action_tasks)
        self.assertIn(self.target_id + ":waiting", self.engine.action_tasks)
        self.assertNotIn(automation_id + ":waiting", self.engine.action_timing)
        self.assert_no_execution()

    def test_create_does_not_cancel_unrelated_pending_automation(self):
        self.configure(allow_server_automation_creation=True)
        unrelated_wait = SimpleNamespace(cancel=Mock())
        key = self.target_id + ":waiting"
        self.engine.action_tasks[key] = unrelated_wait
        self.engine.action_timing[key] = {"started_at": 1, "deadline": 100}
        self.payload(self.create())
        unrelated_wait.cancel.assert_not_called()
        self.assertIn(key, self.engine.action_tasks)
        self.assertIn(key, self.engine.action_timing)
        self.assert_no_execution()

    def test_reject_unknown_client_kinds_root_activation_and_strict_references(self):
        self.configure(allow_server_automation_creation=True)
        bad = [self.rule(isEnabled=True), self.rule(id=str(uuid.uuid4())), self.rule(actions=[]),
            self.rule(actions=[{"kind": "showPopup", "message": "client-only"}]),
            self.rule(triggers=[{"kind": "webhook", "url": "https://example.test/"}]),
            self.rule(conditions=[{"kind": "timeDaily", "minuteOfDay": 0}]),
            self.rule(conditionValidation="guess")]
        for node_id in (True, "12", 12.5, 999):
            bad.append(self.rule(actions=[{"kind": "setAttribute", "nodeID": node_id, "attributeID": 55, "value": 1}]))
        bad += [self.rule(actions=[{"kind": "setAttribute", "nodeID": 12, "attributeID": 56, "value": 1}]),
            self.rule(actions=[{"kind": "setAttribute", "nodeID": 12, "attributeID": 55, "value": 2}]),
            self.rule(actions=[{"kind": "sonosSpeak", "nodeID": 12, "sonosText": "wrong-module"}]),
            self.rule(actions=[{"kind": "roborockCleaning", "nodeID": 12, "roborockTarget": "complete"}]),
            self.rule(actions=[{"kind": "controlAutomation", "automationID": "missing", "automationOperation": "play"}]),
            self.rule(actions=[{"kind": "serverPushNotification", "pushDeviceIDs": ["missing"], "message": "Test"}])]
        for source in bad:
            self.assertTrue(self.create(source)["isError"], source)
        self.assertEqual(1, len(self.engine.rules))
        self.assert_no_execution()

    def test_missing_disabled_integration_and_missing_adapter_are_not_client_fallbacks(self):
        self.configure(allow_server_automation_creation=True)
        integration = self.db.integration("home")
        self.db.save_integration({**integration, "enabled": False})
        self.assertTrue(self.create()["isError"])
        self.db.save_integration({**integration, "enabled": True})
        adapter = self.runtime.adapters.pop("home")
        self.assertTrue(self.create()["isError"])
        self.runtime.adapters["home"] = adapter
        self.payload(self.create())
        self.assertEqual(2, len(self.engine.rules))
        self.assert_no_execution()

    def test_pagination_and_name_search_return_only_matching_summaries(self):
        self.configure(allow_server_automation_creation=True)
        self.payload(self.create(self.rule(name="Flur A")))
        self.payload(self.create(self.rule(name="Flur B")))
        first = self.payload(self.rpc("list_server_automations", {"query": "Flur", "offset": 0, "limit": 1}))
        second = self.payload(self.rpc("list_server_automations", {"query": "Flur", "offset": 1, "limit": 1}))
        self.assertEqual(2, first["total"])
        self.assertEqual(1, len(first["automations"]))
        self.assertEqual(1, len(second["automations"]))
        self.assertNotEqual(first["automations"][0]["id"], second["automations"][0]["id"])
        self.assertEqual({"Flur A", "Flur B"}, {first["automations"][0]["name"], second["automations"][0]["name"]})
        self.assertEqual([], self.payload(self.rpc("list_server_automations", {"query": "not-existing"}))["automations"])
        self.assert_no_execution()

    def test_read_sanitizes_unknown_stored_fields_and_requires_mcp_bearer(self):
        rule = next(rule for rule in self.engine.rules if rule["id"] == self.target_id)
        self.engine.upsert_server({**deepcopy(rule), "private": "private-rule-secret"})
        self.assertNotIn("private-rule-secret", str(self.get(self.target_id)))
        self.assertTrue(self.rpc("get_server_automation", {"automation_id": "missing"})["isError"])
        response = self.request("tools/call", {"name": "list_server_automations", "arguments": {}}, headers=self.admin)
        self.assertEqual(401, response.status_code)
        self.assertEqual(401, self.client.get("/api/v1/mcp").status_code)
        self.assert_no_execution()


if __name__ == "__main__":
    unittest.main()
