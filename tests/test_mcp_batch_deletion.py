"""MCP boundary tests; mocked deletion never talks to a real homee."""
import json
import tempfile
import unittest
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.database import Database
from server.homeegram_batch_deletion import HGDeletionTarget, HomeegramBatchDeletionService
from server.mcp_access import MCPAccess


class MCPBatchDeletionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = Database(self.folder.name)
        self.integration = dict(id="home", module_id="homee", name="Test", enabled=True,
                                configuration={"host": "192.0.2.1", "password": "private-password"})
        self.db.save_integration(self.integration)
        self.adapter = SimpleNamespace(nodes={}, resources={"homeegrams": []}, socket=object())
        self.runtime = SimpleNamespace(adapters={"home": self.adapter}, visible_nodes=lambda: [])
        self.access = MCPAccess(self.db, self.runtime, lambda: "admin")
        self.assertIsInstance(self.access.homeegram_batch_deletion, HomeegramBatchDeletionService)
        self.batch_delete = AsyncMock(return_value={
            "status": "completed", "deleted_count": 2, "results": [
                {"homeegram_id": 800, "name": "Licht Flur", "deleted": True, "status": "deleted"},
                {"homeegram_id": 801, "name": "Licht Küche", "deleted": True, "status": "deleted"}]})
        self.access.homeegram_batch_deletion.delete = self.batch_delete

        @asynccontextmanager
        async def lifespan(app):
            async with self.access.server.session_manager.run():
                yield

        app = FastAPI(lifespan=lifespan)
        app.include_router(self.access.router)
        app.mount("/mcp", self.access.http_app)
        self.client = TestClient(app).__enter__()
        self.admin = {"Authorization": "Bearer admin"}
        self.configure(enabled=True)
        token = self.client.post("/api/v1/mcp/rotate-token", headers=self.admin).json()["token"]
        self.headers = {"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.db._connection.close()
        self.folder.cleanup()

    def configure(self, **overrides):
        response = self.client.put("/api/v1/mcp", headers=self.admin, json={"enabled": True, **overrides})
        self.assertEqual(200, response.status_code, response.text)

    def rpc(self, method, params=None, headers=None):
        return self.client.post("/mcp/", headers=headers if headers is not None else self.headers,
                                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})

    def arguments(self, **overrides):
        return {"integration_id": "home", "homeegrams": [
            {"homeegram_id": 800, "confirmation_name": "Licht Flur"},
            {"homeegram_id": 801, "confirmation_name": "Licht Küche"}],
            "request_id": str(uuid.uuid4()), **overrides}

    def delete(self, arguments):
        response = self.rpc("tools/call", {"name": "delete_homeegrams", "arguments": arguments})
        self.assertEqual(200, response.status_code, response.text)
        return response.json()["result"]

    def test_tool_discovery_schema_annotations_and_explicit_selection(self):
        tools = self.rpc("tools/list").json()["result"]["tools"]
        tool = next(tool for tool in tools if tool["name"] == "delete_homeegrams")
        self.assertFalse(tool["annotations"]["readOnlyHint"])
        self.assertTrue(tool["annotations"]["destructiveHint"])
        self.assertTrue(tool["annotations"]["idempotentHint"])
        self.assertFalse(tool["annotations"]["openWorldHint"])
        schema = tool["inputSchema"]
        self.assertEqual({"integration_id", "homeegrams", "request_id"}, set(schema["required"]))
        targets = schema["properties"]["homeegrams"]
        self.assertEqual(1, targets["minItems"])
        self.assertEqual(50, targets["maxItems"])
        target_schema = schema["$defs"]["HGDeletionTarget"]
        self.assertEqual({"homeegram_id", "confirmation_name"}, set(target_schema["required"]))
        self.assertFalse(target_schema["additionalProperties"])
        self.assertEqual(0, target_schema["properties"]["homeegram_id"]["exclusiveMinimum"])
        self.assertIn("JEDES HG", tool["description"])
        self.assertIn("bestätigten Löschreihenfolge", tool["description"])
        self.assertIn("Platzhaltern", tool["description"])
        self.assertIn("outputSchema", tool)

    def test_existing_delete_grant_is_required_and_not_implicitly_enabled(self):
        refused = self.delete(self.arguments())
        self.assertTrue(refused["isError"])
        self.batch_delete.assert_not_awaited()
        status = self.access.status()
        self.assertFalse(status["allow_homee_deletion"])
        self.configure(allow_drafts=True, allow_homee_creation=True, allow_device_control=True,
                       allow_homee_activation=True, allow_homee_editing=True)
        self.assertTrue(self.delete(self.arguments())["isError"])
        self.batch_delete.assert_not_awaited()

    def test_explicit_list_order_and_uuid_are_forwarded_to_shared_service(self):
        self.configure(allow_homee_deletion=True)
        arguments = self.arguments()
        result = self.delete(arguments)
        self.assertFalse(result.get("isError", False), result)
        self.assertEqual(self.batch_delete.return_value, result["structuredContent"])
        self.batch_delete.assert_awaited_once()
        integration_id, targets, request_id = self.batch_delete.await_args.args
        self.assertEqual("home", integration_id)
        self.assertEqual(arguments["request_id"], request_id)
        self.assertTrue(all(isinstance(target, HGDeletionTarget) for target in targets))
        self.assertEqual(arguments["homeegrams"], [target.model_dump() for target in targets])
        self.assertTrue(self.batch_delete.await_args.kwargs["authorize"](self.adapter))
        audit = str(self.db.setting("mcp_audit"))
        self.assertIn("2/2 bestätigt", audit)
        self.assertNotIn("Licht Küche", audit)
        self.assertNotIn(arguments["request_id"], audit)

    def test_empty_oversized_wildcard_and_non_strict_target_ids_are_rejected_on_wire(self):
        self.configure(allow_homee_deletion=True)
        arguments = self.arguments()
        invalid = [[], [{"homeegram_id": i + 1, "confirmation_name": "HG"} for i in range(51)],
                   "all", ["*"], [{"homeegram_id": 800, "confirmation_name": "HG", "all": True}]]
        invalid.extend([{"homeegram_id": target_id, "confirmation_name": "HG"}]
                       for target_id in (0, -1, True, 800.0, "800", "*"))
        invalid.extend([{"homeegram_id": 800, "confirmation_name": name}]
                       for name in ("", " ", "x" * 501))
        for targets in invalid:
            with self.subTest(targets=targets):
                self.assertTrue(self.delete({**arguments, "homeegrams": targets})["isError"])
        self.batch_delete.assert_not_awaited()

    def test_source_and_integration_grants_are_checked_before_shared_service(self):
        self.configure(allow_homee_deletion=True)
        self.db.save_integration(dict(id="other", module_id="shelly", name="Other", enabled=True, configuration={}))
        self.runtime.adapters["other"] = self.adapter
        for source in ("missing", "other"):
            self.assertTrue(self.delete(self.arguments(integration_id=source))["isError"])
        self.db.save_integration({**self.integration, "enabled": False})
        self.assertTrue(self.delete(self.arguments())["isError"])
        self.batch_delete.assert_not_awaited()

    def test_callback_rechecks_permission_source_and_current_adapter(self):
        self.configure(allow_homee_deletion=True)
        self.assertFalse(self.delete(self.arguments()).get("isError", False))
        authorize = self.batch_delete.await_args.kwargs["authorize"]
        config = self.db.setting("mcp_access")
        self.assertTrue(authorize(self.adapter))
        self.assertFalse(authorize(SimpleNamespace(socket=object())))
        for override in ({"enabled": False}, {"allow_homee_deletion": False}):
            self.db.set_setting("mcp_access", {**config, **override})
            self.assertFalse(authorize(self.adapter))
        self.db.set_setting("mcp_access", config)
        self.db.save_integration({**self.integration, "enabled": False})
        self.assertFalse(authorize(self.adapter))
        self.db.save_integration(self.integration)
        self.runtime.adapters["home"] = SimpleNamespace(socket=object())
        self.assertFalse(authorize(self.adapter))

    def test_partial_result_is_structured_not_reported_as_total_success(self):
        self.configure(allow_homee_deletion=True)
        self.batch_delete.return_value = {
            "status": "partial", "deleted_count": 1, "results": [
                {"homeegram_id": 800, "deleted": True, "status": "deleted"},
                {"homeegram_id": 801, "deleted": False, "status": "uncertain",
                 "error_code": "selection_changed"}]}
        result = self.delete(self.arguments())
        self.assertFalse(result.get("isError", False), result)
        self.assertEqual("partial", result["structuredContent"]["status"])
        self.assertEqual(1, result["structuredContent"]["deleted_count"])
        self.assertEqual("selection_changed", result["structuredContent"]["results"][1]["error_code"])
        self.assertIn("1/2 bestätigt", str(self.db.setting("mcp_audit")))
        self.assertIn("Abbruch HG 801: selection_changed", str(self.db.setting("mcp_audit")))

    def test_backend_errors_and_private_backups_are_not_exposed(self):
        self.configure(allow_homee_deletion=True)
        self.batch_delete.side_effect = RuntimeError("private-webhook-url private-password private-backup")
        result = self.delete(self.arguments())
        self.assertTrue(result["isError"])
        combined = json.dumps(result) + str(self.db.setting("mcp_audit"))
        self.assertNotIn("private-webhook-url", combined)
        self.assertNotIn("private-password", combined)
        self.assertNotIn("private-backup", combined)
        self.assertIn("dieselbe request_id", str(result))
        self.assertIn("2 HGs", str(self.db.setting("mcp_audit")))

    def test_mcp_authentication_cannot_be_bypassed_for_batch_delete(self):
        arguments = {"name": "delete_homeegrams", "arguments": self.arguments()}
        for headers in ({}, self.admin):
            self.assertEqual(401, self.rpc("tools/call", arguments, headers).status_code)
        self.assertEqual(403, self.rpc("tools/call", arguments,
                         {**self.headers, "Origin": "https://evil.example"}).status_code)
        self.configure(enabled=False, allow_homee_deletion=True)
        self.assertEqual(403, self.rpc("tools/call", arguments).status_code)
        self.batch_delete.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
