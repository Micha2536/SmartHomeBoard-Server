"""Wire-level group visibility in both headless MCP and the MCP Apps view."""

from contextlib import asynccontextmanager
import hashlib
import json
import tempfile
from types import SimpleNamespace
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.database import Database
from server.mcp_access import MCPAccess


class MCPGroupVisibilityTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = Database(self.folder.name)
        self.db.save_integration(dict(id="home", module_id="homee", name="Test homee", enabled=True,
                                      configuration={"password": "private-source-password"}))
        self.hg = {"id": 877, "name": "Gruppen%20Test", "active": 0,
            "triggers": {"group_triggers": [{"id": 1, "group_id": 7, "attribute_type": 25,
                                             "operator": 3, "operand": 1, "value": 1}]},
            "conditions": {"group_conditions": [{"id": 2, "group_id": 7, "attribute_type": 1,
                                                   "operator": 1, "operand": 1, "value": 0,
                                                   "scope": 3, "branch": 1, "check_moment": 3}]},
            "actions": {"group_actions": [{"id": 3, "group_id": 7, "attribute_type": 1,
                                             "command": 1, "value": 1, "branch": 1, "delay": 30,
                                             "url": "private-group-url"}],
                        "webhook_actions": [{"id": 4, "url": "private-webhook-url"}]}}
        self.adapter = SimpleNamespace(socket=object(), resources={
            "homeegrams": [self.hg], "groups": [{"id": 7, "name": "Küche%20und%20Flur", "password": "private-group"}],
            "relationships": [{"id": 1, "group_id": 7, "node_id": 12, "homeegram_id": 0, "order": 0},
                              {"id": 2, "group_id": 7, "node_id": 13, "homeegram_id": 0, "order": 1}]},
            nodes={12: {"id": 12, "name": "Küchenlicht", "attributes": [
                {"id": 55, "type": 1, "current_value": 0, "editable": True, "data": "private-attribute"}]},
                   13: {"id": 13, "name": "Flur%20BWM", "attributes": [
                {"id": 57, "type": 25, "current_value": 1, "editable": False}]}})
        foreign = SimpleNamespace(nodes={12: {"id": 12, "name": "Foreign-device-do-not-use", "attributes": []}},
                                  resources={"groups": [{"id": 7, "name": "Foreign-group-do-not-use"}]})
        runtime = SimpleNamespace(adapters={"home": self.adapter, "foreign": foreign}, visible_nodes=lambda: [])
        self.access = MCPAccess(self.db, runtime, lambda: "admin")
        self.db.set_setting("mcp_access", {"enabled": True, "token_hash": hashlib.sha256(b"read-only-test").hexdigest()})

        @asynccontextmanager
        async def lifespan(app):
            async with self.access.server.session_manager.run():
                yield

        app = FastAPI(lifespan=lifespan)
        app.mount("/mcp", self.access.http_app)
        self.client = TestClient(app).__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.db._connection.close()
        self.folder.cleanup()

    def call(self, name):
        response = self.client.post("/mcp/", headers={"Authorization": "Bearer read-only-test",
                                                     "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": name, "arguments": {"integration_id": "home", "homeegram_id": 877}}})
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()["result"]
        self.assertFalse(result.get("isError"), result)
        if result.get("structuredContent"):
            return result["structuredContent"]
        return json.loads(next(item["text"] for item in result["content"] if item["type"] == "text"))

    def test_headless_hg_keeps_group_semantics_and_resolved_members(self):
        result = self.call("get_homeegram")
        hg = result["homeegram"]
        trigger = hg["triggers"]["group_triggers"][0]
        condition = hg["conditions"]["group_conditions"][0]
        action = hg["actions"]["group_actions"][0]
        self.assertEqual("Küche und Flur", trigger["group"]["name"])
        self.assertEqual(["Küchenlicht", "Flur BWM"], [m["name"] for m in trigger["group"]["members"]])
        self.assertEqual("Bewegung erkannt", trigger["group"]["members"][1]["attributes"][0]["target_value_label"])
        self.assertEqual((3, 1, 3), (condition["scope"], condition["branch"], condition["check_moment"]))
        self.assertIn("mindestens ein", condition["summary"].lower())
        self.assertEqual(55, action["group"]["members"][0]["attributes"][0]["id"])
        self.assertEqual("Aus", action["group"]["members"][0]["attributes"][0]["current_value_label"])
        payload = json.dumps(result)
        for secret in ("private-source-password", "private-group", "private-attribute", "private-group-url",
                       "private-webhook-url", "Foreign-device-do-not-use", "Foreign-group-do-not-use"):
            self.assertNotIn(secret, payload)
        self.assertEqual(["webhook_actions"], result["omitted_categories"])

    def test_mcp_apps_projection_has_same_group_information_and_remains_read_only(self):
        read = self.call("get_homeegram")["homeegram"]
        workspace = self.call("show_homeegram")["workspace"]
        self.assertFalse(workspace["editable"])
        for section in ("triggers", "conditions", "actions"):
            self.assertEqual(read[section]["group_" + section], workspace["homeegram"][section]["group_" + section])
            self.assertNotIn("unsupported", workspace["homeegram"][section]["group_" + section][0])
        self.assertTrue(workspace["flow"]["new_paths"])

    def test_missing_relationships_are_unknown_not_an_empty_known_group(self):
        self.adapter.resources.pop("relationships")
        row = self.call("get_homeegram")["homeegram"]["actions"]["group_actions"][0]
        self.assertTrue(row["group"]["missing_relationships"])
        self.assertFalse(row["group"]["membership_complete"])
        self.assertTrue(row["group"]["warnings"])


if __name__ == "__main__":
    unittest.main()
