import tempfile
import json
import unittest
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from server.database import Database
from server.mcp_access import MCPAccess, DraftInput, safe_node


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = Database(self.folder.name)
        self.db.save_integration(dict(id="home", module_id="homee", name="Test", enabled=True, configuration={"host": "192.0.2.1", "password": "secret"}))
        self.node = {"id": 12, "name": "Licht", "attributes": [
            {"id": 55, "type": 1, "current_value": 0, "minimum": 0, "maximum": 1, "editable": True},
            {"id": 56, "type": 3, "current_value": 12.5, "unit": "W", "editable": False},
            {"id": 57, "data": "secret", "current_value": "secret"}]}
        self.adapter = SimpleNamespace(nodes={12: self.node}, resources={"homeegrams": []}, socket=object())
        self.adapter.create_homeegram_disabled = AsyncMock(return_value={"homeegram_id": 800, "active": 0, "play": 0, "status": "created_disabled"})
        self.adapter.homeegram_deletions = {}
        self.adapter.delete_homeegram_confirmed = AsyncMock(return_value={
            "homeegram_id": 800, "name": "Licht Flur", "deleted": True,
            "status": "deleted", "backup_saved": True, "replayed": False})
        runtime = SimpleNamespace(adapters={"home": self.adapter}, visible_nodes=lambda: [self.node])
        self.node["integration_id"] = "home"
        self.db.save_node("home", self.node)
        runtime.set_value = AsyncMock()
        self.access = MCPAccess(self.db, runtime, lambda: "admin")
        @asynccontextmanager
        async def lifespan(app):
            async with self.access.server.session_manager.run(): yield
        self.app = FastAPI(lifespan=lifespan)
        self.app.include_router(self.access.router)
        self.app.mount("/mcp", self.access.http_app)
        self.client = TestClient(self.app).__enter__()
        self.admin = {"Authorization": "Bearer admin"}
        self.client.put("/api/v1/mcp", headers=self.admin, json={"enabled": True, "allow_drafts": True})
        self.token = self.client.post("/api/v1/mcp/rotate-token", headers=self.admin).json()["token"]
        self.headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json, text/event-stream"}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.db._connection.close()
        self.folder.cleanup()

    def rpc(self, method, params=None):
        return self.client.post("/mcp/", headers=self.headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})

    def draft(self, **overrides):
        return DraftInput.model_validate({"name": "Test", "conditions": [{"node_id": 12, "attribute_id": 55, "value": 0, "branch": 0}],
            "actions": [{"node_id": 12, "attribute_id": 55, "value": 1, "branch": 1}], **overrides})

    def deletion_arguments(self, **overrides):
        self.adapter.resources["homeegrams"] = [{"id": 800, "name": "Licht%20Flur", "active": 0,
            "triggers": {"switch_triggers": [{"id": 1}]}, "conditions": {},
            "actions": {"webhook_actions": [{"url": "private-backup-secret"}]}}]
        return {"integration_id": "home", "homeegram_id": 800, "confirmation_name": "Licht Flur",
                "request_id": str(uuid.uuid4()), **overrides}

    def deletion_rpc(self, arguments):
        return self.rpc("tools/call", {"name": "delete_homeegram", "arguments": arguments}).json()["result"]

    def enable_deletion(self):
        response = self.client.put("/api/v1/mcp", headers=self.admin,
            json={"enabled": True, "allow_homee_deletion": True})
        self.assertEqual(200, response.status_code, response.text)

    def deletion_record(self, arguments, **overrides):
        return {"homeegram_id": arguments["homeegram_id"], "name": arguments["confirmation_name"],
                "created_at": 1789650000, "confirmed_at": 1789650001, "status": "deleted",
                "fingerprint": "private-fingerprint", "backup": self.adapter.resources["homeegrams"][0],
                **overrides}

    def test_initialize_and_tool_discovery(self):
        response = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}})
        self.assertEqual(200, response.status_code)
        self.assertEqual("SmartHomeBoard", response.json()["result"]["serverInfo"]["name"])
        names = {x["name"] for x in self.rpc("tools/list").json()["result"]["tools"]}
        self.assertEqual({"list_devices", "get_device_state", "list_homee_sources", "get_homee_catalog", "list_homeegrams", "get_homeegram", "create_homeegram_draft", "create_homeegram", "control_device", "delete_homeegram", "delete_homeegrams", "set_homeegram_active", "edit_homeegram", "show_homeegram", "get_homeegram_workspace", "create_homeegram_workspace", "update_homeegram_workspace", "deploy_homeegram_workspace", "get_server_automation_capabilities", "list_server_automations", "get_server_automation", "create_server_automation", "set_server_automation_active"}, names)

    def test_deletion_capability_and_separate_permission_default(self):
        status = self.client.get("/api/v1/mcp", headers=self.admin).json()
        self.assertTrue(status["homee_deletion_supported"])
        self.assertFalse(status["allow_homee_deletion"])
        self.enable_deletion()
        self.assertTrue(self.client.get("/api/v1/mcp", headers=self.admin).json()["allow_homee_deletion"])
        self.client.put("/api/v1/mcp", headers=self.admin, json={"enabled": True})
        self.assertFalse(self.client.get("/api/v1/mcp", headers=self.admin).json()["allow_homee_deletion"])

    def test_deletion_tool_annotations_and_confirmation_schema(self):
        tool = next(t for t in self.rpc("tools/list").json()["result"]["tools"] if t["name"] == "delete_homeegram")
        self.assertFalse(tool["annotations"]["readOnlyHint"])
        self.assertTrue(tool["annotations"]["destructiveHint"])
        self.assertTrue(tool["annotations"]["idempotentHint"])
        self.assertEqual(0, tool["inputSchema"]["properties"]["homeegram_id"]["exclusiveMinimum"])
        self.assertEqual({"integration_id", "homeegram_id", "confirmation_name", "request_id"}, set(tool["inputSchema"]["required"]))
        self.assertIn("ausdrücklichem", tool["description"])
        self.assertIn("keine automatische Auswahl", tool["description"])

    def test_deletion_requires_permission_and_returns_only_public_metadata(self):
        arguments = self.deletion_arguments()
        refused = self.deletion_rpc(arguments)
        self.assertTrue(refused["isError"])
        self.adapter.delete_homeegram_confirmed.assert_not_awaited()
        self.enable_deletion()
        self.adapter.delete_homeegram_confirmed.return_value.update(
            request_id=arguments["request_id"], backup=self.adapter.resources["homeegrams"][0])
        accepted = self.deletion_rpc(arguments)
        self.assertFalse(accepted.get("isError", False), accepted)
        self.assertNotIn("private-backup-secret", str(accepted))
        data = accepted.get("structuredContent") or json.loads(accepted["content"][0]["text"])
        self.assertTrue(data["deleted"])
        self.assertNotIn("backup", data)
        self.assertEqual((800, "Licht Flur", arguments["request_id"]), self.adapter.delete_homeegram_confirmed.await_args.args)
        self.assertTrue(self.adapter.delete_homeegram_confirmed.await_args.kwargs["authorize"]())
        self.assertIn("HG-Löschung bestätigt", str(self.db.setting("mcp_audit")))

    def test_deletion_rejects_bad_ids_uuid_missing_or_mismatched_name_before_adapter(self):
        arguments = self.deletion_arguments()
        self.enable_deletion()
        for override in ({"homeegram_id": 0}, {"homeegram_id": -800}, {"homeegram_id": True},
                         {"homeegram_id": 800.5}, {"homeegram_id": "800"}, {"homeegram_id": 999},
                         {"request_id": "not-a-uuid"}, {"confirmation_name": ""},
                         {"confirmation_name": "  "}, {"confirmation_name": "Anderes HG"},
                         {"confirmation_name": "Licht%20Flur"}):
            result = self.deletion_rpc({**arguments, **override})
            self.assertTrue(result["isError"], override)
        self.adapter.delete_homeegram_confirmed.assert_not_awaited()

    def test_literal_percent_in_display_name_is_not_decoded_twice(self):
        arguments = self.deletion_arguments(confirmation_name="Licht%20Flur")
        self.adapter.resources["homeegrams"][0]["name"] = "Licht%2520Flur"
        self.enable_deletion()
        self.assertFalse(self.deletion_rpc(arguments).get("isError", False))
        self.adapter.delete_homeegram_confirmed.assert_awaited_once()
        self.assertEqual("Licht%20Flur", self.adapter.delete_homeegram_confirmed.await_args.args[1])
        self.adapter.delete_homeegram_confirmed.reset_mock()
        self.assertTrue(self.deletion_rpc({**arguments, "confirmation_name": "Licht Flur"})["isError"])
        self.adapter.delete_homeegram_confirmed.assert_not_awaited()

    def test_deletion_uuid_is_canonical_and_existing_id_conflicts_are_rejected(self):
        arguments = self.deletion_arguments()
        self.enable_deletion()
        upper = {**arguments, "request_id": arguments["request_id"].upper()}
        self.assertFalse(self.deletion_rpc(upper).get("isError", False))
        self.assertEqual(arguments["request_id"], self.adapter.delete_homeegram_confirmed.await_args.args[2])
        self.adapter.delete_homeegram_confirmed.reset_mock()
        self.adapter.homeegram_deletions[arguments["request_id"]] = self.deletion_record(arguments, homeegram_id=801)
        self.assertTrue(self.deletion_rpc(arguments)["isError"])
        self.adapter.delete_homeegram_confirmed.assert_not_awaited()

    def test_confirmed_replay_survives_reused_id_and_disconnected_socket(self):
        arguments = self.deletion_arguments()
        self.enable_deletion()
        record = self.deletion_record(arguments)
        self.adapter.homeegram_deletions[arguments["request_id"]] = record
        # A successful adapter replay returns its retained result; it must not
        # mistake a subsequently reused homee ID for a new deletion request.
        self.adapter.resources["homeegrams"][0]["name"] = "Neues%20HG"
        self.adapter.socket = None
        delete_sends = []
        async def replay(homeegram_id, confirmation_name, request_id, authorize):
            self.assertTrue(authorize())
            previous = self.adapter.homeegram_deletions.get(request_id)
            if previous and previous["status"] == "deleted":
                return {"homeegram_id": homeegram_id, "name": previous["name"], "deleted": True,
                        "status": "deleted", "request_id": request_id, "backup_saved": True, "replayed": True}
            delete_sends.append(homeegram_id)
            raise AssertionError("Replay darf keine neue Löschung senden")
        self.adapter.delete_homeegram_confirmed.side_effect = replay
        result = self.deletion_rpc(arguments)
        self.assertFalse(result.get("isError", False), result)
        data = result.get("structuredContent") or json.loads(result["content"][0]["text"])
        self.assertTrue(data["replayed"])
        self.assertEqual([], delete_sends)

    def test_deletion_never_routes_to_missing_wrong_or_disabled_integration(self):
        arguments = self.deletion_arguments()
        self.enable_deletion()
        self.db.save_integration(dict(id="other", module_id="shelly", name="Other", enabled=True, configuration={}))
        self.access.runtime.adapters["other"] = self.adapter
        for integration_id in ("missing", "other"):
            self.assertTrue(self.deletion_rpc({**arguments, "integration_id": integration_id})["isError"])
        current = self.db.integration("home")
        current["enabled"] = False
        self.db.save_integration(current)
        self.assertTrue(self.deletion_rpc(arguments)["isError"])
        self.adapter.delete_homeegram_confirmed.assert_not_awaited()

    def test_authorize_callback_rechecks_mcp_permission_integration_and_adapter(self):
        arguments = self.deletion_arguments()
        self.enable_deletion()
        self.assertFalse(self.deletion_rpc(arguments).get("isError", False))
        authorize = self.adapter.delete_homeegram_confirmed.await_args.kwargs["authorize"]
        self.assertTrue(authorize())
        config = self.db.setting("mcp_access")
        for change in ({"enabled": False}, {"allow_homee_deletion": False}):
            self.db.set_setting("mcp_access", {**config, **change})
            self.assertFalse(authorize())
        self.db.set_setting("mcp_access", config)
        current = self.db.integration("home")
        self.db.save_integration({**current, "enabled": False})
        self.assertFalse(authorize())
        self.db.save_integration(current)
        self.access.runtime.adapters["home"] = SimpleNamespace(socket=object())
        self.assertFalse(authorize())

    def test_deletion_backend_error_never_exposes_backup_in_response_or_audit(self):
        arguments = self.deletion_arguments()
        self.enable_deletion()
        self.adapter.delete_homeegram_confirmed.side_effect = RuntimeError(json.dumps(self.adapter.resources["homeegrams"][0]))
        result = self.deletion_rpc(arguments)
        self.assertTrue(result["isError"])
        self.assertNotIn("private-backup-secret", str(result))
        audit = str(self.db.setting("mcp_audit"))
        self.assertIn("HG-Löschung abgelehnt oder nicht bestätigt", audit)
        self.assertNotIn("private-backup-secret", audit)

    def test_live_deletion_backups_are_admin_only_and_metadata_omits_payload(self):
        arguments = self.deletion_arguments()
        self.adapter.homeegram_deletions[arguments["request_id"]] = self.deletion_record(arguments)
        base = "/api/v1/mcp/homeegram-deletions"
        backup = base + "/" + arguments["request_id"] + "/backup?integration_id=home"
        for path in (base + "?integration_id=home", backup):
            self.assertEqual(401, self.client.get(path, headers=self.headers).status_code)
            self.assertEqual(401, self.client.get(path).status_code)
        listed = self.client.get(base + "?integration_id=home", headers=self.admin)
        self.assertEqual(200, listed.status_code)
        self.assertNotIn("private-backup-secret", listed.text)
        self.assertNotIn("private-fingerprint", listed.text)
        self.assertTrue(listed.json()["deletions"][0]["backup_available"])
        restored = self.client.get(backup, headers=self.admin)
        self.assertEqual(200, restored.status_code)
        self.assertEqual(self.adapter.resources["homeegrams"][0], restored.json())
        self.assertIn("attachment", restored.headers["content-disposition"])
        self.assertEqual("no-store", restored.headers["cache-control"])

    def test_admin_backup_can_be_loaded_offline_and_cannot_cross_integrations(self):
        arguments = self.deletion_arguments()
        self.db.set_setting("module_state:home", {"homeegram_deletions": {
            arguments["request_id"]: self.deletion_record(arguments)}})
        self.access.runtime.adapters.pop("home")
        base = "/api/v1/mcp/homeegram-deletions"
        self.assertEqual(200, self.client.get(base + "?integration_id=home", headers=self.admin).status_code)
        backup = base + "/" + arguments["request_id"] + "/backup"
        self.assertEqual(200, self.client.get(backup + "?integration_id=home", headers=self.admin).status_code)
        self.db.save_integration(dict(id="other", module_id="homee", name="Other", enabled=True, configuration={}))
        self.assertEqual(404, self.client.get(backup + "?integration_id=other", headers=self.admin).status_code)
        self.assertEqual(404, self.client.get(backup + "?integration_id=missing", headers=self.admin).status_code)
        self.assertEqual(400, self.client.get(base + "/invalid/backup?integration_id=home", headers=self.admin).status_code)

    def test_hg_names_are_decoded_once_consistently_for_confirmation(self):
        self.adapter.resources["homeegrams"] = [{"id": 800, "name": "Flur%20%2520", "active": 0}]
        listed = self.rpc("tools/call", {"name": "list_homeegrams", "arguments": {"integration_id": "home"}}).json()["result"]
        data = listed.get("structuredContent") or json.loads(listed["content"][0]["text"])
        self.assertEqual("Flur %20", data["homeegrams"][0]["name"])
        detail = self.rpc("tools/call", {"name": "get_homeegram", "arguments": {"integration_id": "home", "homeegram_id": 800}}).json()["result"]
        data = detail.get("structuredContent") or json.loads(detail["content"][0]["text"])
        self.assertEqual("Flur %20", data["homeegram"]["name"])

    def test_disabling_mcp_also_revokes_device_control_callback(self):
        for enabled, allowed in ((True, True), (False, True), (True, False)):
            self.client.put("/api/v1/mcp", headers=self.admin,
                json={"enabled": enabled, "allow_device_control": allowed})
            self.assertEqual(enabled and allowed, self.access.device_control.allowed())

    def test_native_creation_has_separate_permission_and_stays_disabled(self):
        arguments = {"integration_id": "home", "request_id": str(uuid.uuid4()), "homeegram": self.draft().model_dump()}
        refused = self.rpc("tools/call", {"name": "create_homeegram", "arguments": arguments}).json()["result"]
        self.assertTrue(refused["isError"])
        self.adapter.create_homeegram_disabled.assert_not_awaited()
        self.client.put("/api/v1/mcp", headers=self.admin, json={"enabled": True, "allow_homee_creation": True})
        accepted = self.rpc("tools/call", {"name": "create_homeegram", "arguments": arguments}).json()["result"]
        self.assertFalse(accepted.get("isError", False), accepted)
        hg, request_id = self.adapter.create_homeegram_disabled.await_args.args
        self.assertEqual(0, hg["active"])
        self.assertEqual(0, hg["play"])
        self.assertEqual(arguments["request_id"], request_id)
        self.assertEqual([], self.access.drafts())

    def test_admin_can_edit_and_deploy_previous_draft_via_server(self):
        key = str(uuid.uuid4())
        draft = self.access.create_draft("home", key, self.draft())
        catalog = self.client.get(f"/api/v1/mcp/drafts/{key}/catalog", headers=self.admin)
        self.assertEqual(200, catalog.status_code)
        self.assertEqual([12], [n["id"] for n in catalog.json()["nodes"]])
        edited = draft["homeegram"]
        edited["name"] = "Bearbeiteter Entwurf"
        edited["actions"]["attribute_actions"][0].update(value=0, branch=2)
        # Actual Swift encoding includes all empty collection arrays.
        edited["actions"]["webhook_actions"] = []
        result = self.client.post(f"/api/v1/mcp/drafts/{key}/deploy", headers=self.admin, json={"homeegram": edited})
        self.assertEqual(200, result.status_code, result.text)
        canonical = self.adapter.create_homeegram_disabled.await_args.args[0]
        self.assertEqual("Bearbeiteter Entwurf", canonical["name"])
        self.assertEqual(0, canonical["actions"]["attribute_actions"][0]["value"])
        self.assertEqual(2, canonical["actions"]["attribute_actions"][0]["branch"])
        self.assertEqual(800, self.access.drafts()[0]["homeegram_id"])

    def test_edited_content_is_validated_not_original_draft(self):
        key = str(uuid.uuid4())
        draft = self.access.create_draft("home", key, self.draft())
        draft["homeegram"]["actions"]["attribute_actions"][0]["node_id"] = 999
        result = self.client.post(f"/api/v1/mcp/drafts/{key}/deploy", headers=self.admin, json={"homeegram": draft["homeegram"]})
        self.assertEqual(409, result.status_code)
        self.adapter.create_homeegram_disabled.assert_not_awaited()

    def test_switching_has_separate_permission_and_uses_runtime_audit(self):
        arguments = {"node_id": 12, "attribute_id": 55, "operation": "on", "request_id": str(uuid.uuid4())}
        refused = self.rpc("tools/call", {"name": "control_device", "arguments": arguments}).json()["result"]
        self.assertTrue(refused["isError"])
        self.access.runtime.set_value.assert_not_awaited()
        self.client.put("/api/v1/mcp", headers=self.admin, json={"enabled": True, "allow_device_control": True})
        accepted = self.rpc("tools/call", {"name": "control_device", "arguments": arguments}).json()["result"]
        self.assertFalse(accepted.get("isError", False), accepted)
        data = accepted.get("structuredContent") or json.loads(accepted["content"][0]["text"])
        self.assertTrue(data["command_sent"])
        self.assertFalse(data["confirmed"])
        self.assertEqual("mcp", self.access.runtime.set_value.await_args.kwargs["source"])

    def test_read_tool_roundtrip(self):
        result = self.rpc("tools/call", {"name": "get_device_state", "arguments": {"node_id": 12}}).json()["result"]
        self.assertFalse(result.get("isError", False))
        self.assertNotIn("secret", str(result))
        self.assertIn("12.5", str(result))

    def test_create_tool_roundtrip_and_paths(self):
        result = self.rpc("tools/call", {"name": "create_homeegram_draft", "arguments": {"integration_id": "home", "request_id": str(uuid.uuid4()), "draft": self.draft().model_dump()}}).json()["result"]
        self.assertFalse(result.get("isError", False), result)
        hg = self.access.drafts()[0]["homeegram"]
        self.assertEqual(0, hg["active"])
        self.assertEqual(0, hg["play"])
        self.assertEqual(0, hg["conditions"]["attribute_conditions"][0]["branch"])
        self.assertEqual(1, hg["actions"]["attribute_actions"][0]["branch"])
        self.assertEqual([], self.adapter.resources["homeegrams"])

    def test_authentication_disabled_rotation_and_origin(self):
        self.assertEqual(401, self.client.post("/mcp/").status_code)
        self.assertEqual(401, self.client.post("/mcp/", headers=self.admin).status_code)
        self.assertEqual(403, self.client.post("/mcp/", headers={**self.headers, "Origin": "https://evil.example"}).status_code)
        self.client.post("/api/v1/mcp/rotate-token", headers=self.admin)
        self.assertEqual(401, self.rpc("tools/list").status_code)
        self.client.put("/api/v1/mcp", headers=self.admin, json={"enabled": False})
        self.assertEqual(403, self.rpc("tools/list").status_code)

    def test_mcp_key_cannot_administer(self):
        self.assertEqual(401, self.client.post("/api/v1/mcp/rotate-token", headers=self.headers).status_code)
        self.assertEqual(401, self.client.get("/api/v1/mcp/drafts", headers=self.headers).status_code)
        self.assertNotIn(self.token, str(self.db.setting("mcp_access")))
        self.assertIsNone(self.client.get("/api/v1/mcp", headers=self.admin).json()["token"])

    def test_idempotency_and_conflict(self):
        key = str(uuid.uuid4())
        first = self.access.create_draft("home", key, self.draft())
        self.assertEqual(first, self.access.create_draft("home", key, self.draft()))
        self.assertEqual(1, len(self.access.drafts()))
        with self.assertRaises(ValueError): self.access.create_draft("home", key, self.draft(name="Different"))
        self.client.delete("/api/v1/mcp/drafts/" + key, headers=self.admin)
        with self.assertRaises(ValueError): self.access.create_draft("home", key, self.draft())

    def test_permission_unknown_ids_and_read_only_attributes(self):
        self.client.put("/api/v1/mcp", headers=self.admin, json={"enabled": True, "allow_drafts": False})
        with self.assertRaises(ValueError): self.access.create_draft("home", str(uuid.uuid4()), self.draft())
        self.client.put("/api/v1/mcp", headers=self.admin, json={"enabled": True, "allow_drafts": True})
        for attr in (999, 56):
            with self.assertRaises(ValueError): self.access.create_draft("home", str(uuid.uuid4()), self.draft(actions=[{"node_id": 12, "attribute_id": attr, "value": 1}]))
        self.adapter.socket = None
        with self.assertRaises(ValueError): self.access.create_draft("home", str(uuid.uuid4()), self.draft())

    def test_no_unknown_elements_or_invalid_numbers(self):
        for override in ({"active": 1}, {"time_triggers": []}, {"actions": [{"node_id": 12, "attribute_id": 55, "value": float("nan")}]}):
            with self.assertRaises(ValidationError): self.draft(**override)
        with self.assertRaises(ValueError): self.access.create_draft("home", str(uuid.uuid4()), self.draft(actions=[{"node_id": 12, "attribute_id": 55, "value": 500}]))

    def test_request_limit_and_no_secret_fields(self):
        self.assertEqual(413, self.client.post("/mcp/", headers=self.headers, content=b"x" * 65537).status_code)
        self.assertNotIn("secret", str(safe_node(self.node)))

    def test_hg_webhook_redaction(self):
        self.adapter.resources["homeegrams"] = [{"id": 1, "name": "Test", "actions": {"webhook_actions": [{"url": "secret"}]}}]
        result = self.rpc("tools/call", {"name": "get_homeegram", "arguments": {"integration_id": "home", "homeegram_id": 1}}).json()["result"]
        self.assertNotIn("secret", str(result))
        self.assertIn("omitted_categories", str(result))


if __name__ == "__main__": unittest.main()
