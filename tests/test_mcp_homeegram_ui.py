"""MCP Apps resource, permissions, shared-draft revisions and safe deployment."""
import asyncio
import hashlib
import json
import tempfile
import unittest
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from server.database import Database
from server.mcp_access import MCPAccess, DraftInput
from server.mcp_homeegram_ui import RESOURCE_MIME, RESOURCE_URI, WorkspaceDraft, register_homeegram_ui


class HomeegramWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = Database(self.folder.name)
        self.db.save_integration(dict(id="home", module_id="homee", name="Mein homee", enabled=True,
                                      configuration={"host": "192.0.2.1", "password": "never-share-this"}))
        self.node = {"id": 12, "name": "Küche%20Licht", "attributes": [
            {"id": 55, "type": 1, "current_value": 0, "minimum": 0, "maximum": 1, "editable": True},
            {"id": 56, "type": 3, "current_value": 5.5, "unit": "W", "editable": False},
            {"id": 57, "type": 25, "current_value": None, "editable": False, "data": "private"},
            {"id": 58, "type": 243, "current_value": 7, "editable": False}]}
        self.adapter = SimpleNamespace(nodes={12: self.node}, resources={"homeegrams": []}, socket=object(),
            create_homeegram_disabled=AsyncMock(return_value={"homeegram_id": 900, "active": 0, "play": 0,
                                                              "status": "created_disabled"}))
        self.runtime = SimpleNamespace(adapters={"home": self.adapter}, visible_nodes=lambda: [])
        self.access = MCPAccess(self.db, self.runtime, lambda: "admin")
        self.service = getattr(self.access, "homeegram_workspace", None) or register_homeegram_ui(self.access)
        self.db.set_setting("mcp_access", {"enabled": True, "allow_drafts": True, "allow_homee_creation": False,
                                            "token_hash": hashlib.sha256(b"test-key").hexdigest()})

    def tearDown(self):
        self.db._connection.close()
        self.folder.cleanup()

    def create(self, **overrides):
        return self.service.create(**{"integration_id": "home", "request_id": str(uuid.uuid4()), **overrides})

    def filled(self, **overrides):
        return WorkspaceDraft.model_validate({"name": "Bewegung Küche", "manual_play": True,
            "triggers": [{"node_id": 12, "attribute_id": 55, "value": 1}],
            "conditions": [{"node_id": 12, "attribute_id": 55, "value": 0, "branch": 0},
                           {"node_id": 12, "attribute_id": 55, "value": 1, "branch": 1}],
            "actions": [{"node_id": 12, "attribute_id": 55, "value": 1, "branch": 0, "operation": "toggle"},
                        {"node_id": 12, "attribute_id": 55, "value": 1, "branch": 1, "delay": 30},
                        {"node_id": 12, "attribute_id": 55, "value": 0, "branch": 2}], **overrides})

    def write(self, created, model=None, **overrides):
        workspace = created["workspace"]
        return self.service.update(**{"draft_id": workspace["draft_id"], "expected_revision": workspace["revision"],
                                     "request_id": str(uuid.uuid4()), "draft": model or self.filled(), **overrides})

    def grant_creation(self):
        settings = self.db.setting("mcp_access")
        settings["allow_homee_creation"] = True
        self.db.set_setting("mcp_access", settings)

    def test_empty_creation_stays_local_in_native_shared_draft_store(self):
        result = self.create()
        workspace = result["workspace"]
        self.assertEqual("draft", workspace["mode"])
        self.assertEqual(1, workspace["revision"])
        self.assertTrue(workspace["editable"])
        self.assertEqual([], workspace["draft"]["actions"])
        row = self.db.setting("mcp_drafts")[0]
        self.assertEqual(workspace["draft_id"], row["id"])
        self.assertEqual(0, row["homeegram"]["active"])
        self.assertEqual(0, row["homeegram"]["play"])
        self.adapter.create_homeegram_disabled.assert_not_awaited()

    def test_creation_is_idempotent_but_rejects_changed_name(self):
        request_id = str(uuid.uuid4())
        first = self.create(request_id=request_id)
        again = self.create(request_id=request_id)
        self.assertEqual(first["workspace"], again["workspace"])
        self.assertEqual(1, len(self.access.drafts()))
        with self.assertRaisesRegex(ValueError, "anderen Entwurf"):
            self.create(request_id=request_id, name="Anderer Name")

    def test_removed_draft_uuid_cannot_recreate_deleted_item(self):
        request_id = str(uuid.uuid4())
        self.db.set_setting("mcp_removed_drafts", [request_id])
        with self.assertRaisesRegex(ValueError, "entfernt"):
            self.create(request_id=request_id)

    def test_native_ids_and_all_three_paths_are_preserved(self):
        result = self.write(self.create())
        workspace = result["workspace"]
        self.assertEqual(2, workspace["revision"])
        self.assertEqual([0, 1], [row["branch"] for row in workspace["homeegram"]["conditions"]["attribute_conditions"]])
        actions = workspace["homeegram"]["actions"]["attribute_actions"]
        self.assertEqual([0, 1, 2], [row["branch"] for row in actions])
        self.assertEqual(3, actions[0]["command"])
        self.assertEqual(30, actions[1]["delay"])
        self.assertTrue(workspace["flow"]["new_paths"])
        self.assertFalse(workspace["flow"]["nested_conditions"])
        self.adapter.create_homeegram_disabled.assert_not_awaited()

    def test_old_ui_revision_cannot_overwrite_ki_change(self):
        first = self.create()
        changed = self.write(first)
        with self.assertRaisesRegex(ValueError, "Versionskonflikt"):
            self.write(first, self.filled(name="Veralteter Browser"))
        reloaded = self.service.get(draft_id=first["workspace"]["draft_id"])
        self.assertEqual(changed["workspace"], reloaded["workspace"])

    def test_update_retry_is_idempotent_with_authoritative_current_state(self):
        first = self.create()
        request_id = str(uuid.uuid4())
        changed = self.write(first, request_id=request_id)
        again = self.write(first, request_id=request_id)
        self.assertEqual(changed["workspace"], again["workspace"])
        with self.assertRaisesRegex(ValueError, "anderen Änderung"):
            self.write(first, self.filled(name="Anderer Inhalt"), request_id=request_id)

    def test_other_drafts_survive_updates(self):
        first, second = self.create(), self.create()
        self.write(first)
        self.assertEqual(2, len(self.access.drafts()))
        self.assertEqual(1, self.service.get(draft_id=second["workspace"]["draft_id"])["workspace"]["revision"])

    def test_invalid_source_foreign_attribute_or_readonly_action_rejected(self):
        first = self.create()
        for action in ({"node_id": 999, "attribute_id": 55, "value": 1},
                       {"node_id": 12, "attribute_id": 999, "value": 1},
                       {"node_id": 12, "attribute_id": 56, "value": 1},
                       {"node_id": 12, "attribute_id": 55, "value": 2}):
            with self.assertRaises(ValueError):
                self.write(first, self.filled(actions=[action]))
        self.assertEqual(1, self.service.get(draft_id=first["workspace"]["draft_id"])["workspace"]["revision"])

    def test_schema_rejects_nested_conditions_bad_branches_and_nonfinite_values(self):
        for conditions in ([{"node_id": 12, "attribute_id": 55, "value": 0, "branch": 2}],
                           [{"node_id": 12, "attribute_id": 55, "value": 0, "conditions": []}]):
            with self.assertRaises(ValidationError):
                self.filled(conditions=conditions)
        with self.assertRaises(ValidationError):
            self.filled(actions=[{"node_id": True, "attribute_id": 55, "value": 1}])
        with self.assertRaises(ValidationError):
            self.filled(actions=[{"node_id": 12, "attribute_id": 55, "value": float("nan")}])

    def test_local_writes_and_creation_have_separate_permissions(self):
        created = self.create()
        with self.assertRaisesRegex(ValueError, "HG-Anlage"):
            asyncio.run(self.service.deploy(created["workspace"]["draft_id"], 1, str(uuid.uuid4())))
        config = self.db.setting("mcp_access")
        config.update(allow_drafts=False, allow_homee_creation=True)
        self.db.set_setting("mcp_access", config)
        with self.assertRaisesRegex(ValueError, "Entwürfe"):
            self.write(created)
        with self.assertRaisesRegex(ValueError, "Entwürfe"):
            self.create()
        self.assertFalse(self.service.get(draft_id=created["workspace"]["draft_id"])["workspace"]["editable"])
        self.adapter.create_homeegram_disabled.assert_not_awaited()

    def test_explicit_deploy_always_disabled_and_retry_does_not_duplicate(self):
        changed = self.write(self.create())
        self.grant_creation()
        draft_id = changed["workspace"]["draft_id"]
        request_id = str(uuid.uuid4())
        deployed = asyncio.run(self.service.deploy(draft_id, 2, request_id))
        self.assertEqual(900, deployed["deployment"]["homeegram_id"])
        self.assertFalse(deployed["workspace"]["editable"])
        canonical, adapter_request = self.adapter.create_homeegram_disabled.await_args.args
        self.assertEqual(0, canonical["active"])
        self.assertEqual(0, canonical["play"])
        self.assertEqual(request_id, adapter_request)
        replay = asyncio.run(self.service.deploy(draft_id, 2, request_id))
        self.assertTrue(replay["deployment"]["replayed"])
        self.assertEqual(1, self.adapter.create_homeegram_disabled.await_count)
        with self.assertRaisesRegex(ValueError, "gesendet"):
            self.write(deployed)
        with self.assertRaisesRegex(ValueError, "bereits gesendet"):
            asyncio.run(self.service.deploy(draft_id, 3, str(uuid.uuid4())))

    def test_empty_draft_not_deployed_and_not_locked_by_validation_error(self):
        first = self.create()
        self.grant_creation()
        with self.assertRaises(ValueError):
            asyncio.run(self.service.deploy(first["workspace"]["draft_id"], 1, str(uuid.uuid4())))
        self.assertTrue(self.service.get(draft_id=first["workspace"]["draft_id"])["workspace"]["editable"])
        self.adapter.create_homeegram_disabled.assert_not_awaited()

    def test_uncertain_deploy_locks_edits_and_requires_same_request(self):
        changed = self.write(self.create())
        self.grant_creation()
        draft_id = changed["workspace"]["draft_id"]
        request_id = str(uuid.uuid4())
        self.adapter.create_homeegram_disabled.side_effect = TimeoutError("secret-network-error")
        with self.assertRaisesRegex(ValueError, "Anlage nicht bestätigt") as error:
            asyncio.run(self.service.deploy(draft_id, 2, request_id))
        self.assertNotIn("secret-network-error", str(error.exception))
        self.assertFalse(self.service.get(draft_id=draft_id)["workspace"]["editable"])
        with self.assertRaisesRegex(ValueError, "bereits gesendet"):
            asyncio.run(self.service.deploy(draft_id, 2, str(uuid.uuid4())))
        self.adapter.create_homeegram_disabled.side_effect = None
        deployed = asyncio.run(self.service.deploy(draft_id, 2, request_id))
        self.assertEqual(900, deployed["deployment"]["homeegram_id"])
        self.assertEqual([request_id, request_id], [call.args[1] for call in self.adapter.create_homeegram_disabled.await_args_list])

    def test_originals_readonly_unknown_categories_are_safe_visible_placeholders(self):
        self.adapter.resources["homeegrams"] = [{"id": 877, "name": "Test%20HG", "active": 1,
            "triggers": {"switch_triggers": [{"id": 100}]},
            "conditions": {"attribute_conditions": [{"id": 101, "branch": 1, "node_id": 12, "attribute_id": 55, "value": 1}]},
            "actions": {"webhook_actions": [{"id": 102, "branch": 0, "url": "https://private.example/secret"}],
                        "notification_actions": [{"id": 103, "branch": 2, "text": "private-message", "user_id": 123}]}}]
        result = self.service.get("home", homeegram_id=877)
        workspace = result["workspace"]
        self.assertEqual("Test HG", workspace["name"])
        self.assertFalse(workspace["editable"])
        self.assertIsNone(workspace["draft"])
        self.assertTrue(workspace["flow"]["new_paths"])
        self.assertEqual({"id": 102, "branch": 0, "unsupported": True}, workspace["homeegram"]["actions"]["webhook_actions"][0])
        self.assertIn("Spezialelement", str(workspace["warnings"]))
        for secret in ("private.example", "private-message", "never-share-this", "user_id"):
            self.assertNotIn(secret, json.dumps(result))

    def test_legacy_branch_zero_is_not_mislabelled_as_new_conditional_flow(self):
        self.adapter.resources["homeegrams"] = [{"id": 5, "name": "Legacy", "active": 1,
            "conditions": {"attribute_conditions": [{"id": 1, "branch": 0}]},
            "actions": {"attribute_actions": [{"id": 2, "branch": 0}]}}]
        result = self.service.get("home", homeegram_id=5)
        self.assertFalse(result["workspace"]["flow"]["new_paths"])

    def test_existing_drafts_are_shared_and_unsupported_items_cannot_be_dropped(self):
        created = self.access.create_draft("home", str(uuid.uuid4()), DraftInput.model_validate(self.filled().model_dump()))
        result = self.service.get(draft_id=created["id"])
        self.assertTrue(result["workspace"]["editable"])
        self.assertEqual(3, len(result["workspace"]["draft"]["actions"]))
        rows = self.access.drafts()
        rows[0]["homeegram"]["actions"]["webhook_actions"] = [{"id": -99, "url": "secret"}]
        self.db.set_setting("mcp_drafts", rows)
        result = self.service.get(draft_id=created["id"])
        self.assertFalse(result["workspace"]["editable"])
        self.assertNotIn("secret", json.dumps(result))
        with self.assertRaisesRegex(ValueError, "Nicht unterstützte Elemente"):
            self.service.update(created["id"], 1, str(uuid.uuid4()), self.filled())

    def test_catalog_keeps_missing_current_values_and_german_state_labels(self):
        result = self.service.get("home")
        attributes = {row["id"]: row for row in result["catalog"]["nodes"][0]["attributes"]}
        self.assertEqual("Bewegung erkannt", attributes[57]["value_labels"]["1"])
        self.assertNotIn("data", attributes[57])
        self.assertEqual("Bewölkt", attributes[58]["value_labels"]["7"])
        self.assertEqual("Einschalten", attributes[55]["action_labels"]["1"])

    def test_empty_entrypoint_works_with_no_or_multiple_homee_sources(self):
        self.db.save_integration(dict(id="second", module_id="homee", name="Zweiter homee", enabled=True, configuration={}))
        result = self.service.get()
        self.assertEqual("browser", result["workspace"]["mode"])
        self.assertEqual("", result["workspace"]["integration_id"])
        self.assertEqual(2, len(result["sources"]))
        with self.assertRaisesRegex(ValueError, "mehrere Quellen"):
            self.service.create("", str(uuid.uuid4()))

    def test_wrong_source_draft_hg_and_disabled_integration_rejected(self):
        first = self.create()
        with self.assertRaisesRegex(ValueError, "Quelle"):
            self.service.get("other", draft_id=first["workspace"]["draft_id"])
        with self.assertRaisesRegex(ValueError, "Entweder"):
            self.service.get("home", homeegram_id=5, draft_id=first["workspace"]["draft_id"])
        source = self.db.integration("home")
        source["enabled"] = False
        self.db.save_integration(source)
        with self.assertRaisesRegex(ValueError, "nicht aktiviert"):
            self.write(first)

    def test_wire_resource_metadata_render_data_split_and_authentication(self):
        @asynccontextmanager
        async def lifespan(app):
            async with self.access.server.session_manager.run():
                yield
        app = FastAPI(lifespan=lifespan)
        app.mount("/mcp", self.access.http_app)
        with TestClient(app) as client:
            headers = {"Authorization": "Bearer test-key", "Accept": "application/json, text/event-stream"}
            def rpc(method, params=None):
                response = client.post("/mcp/", headers=headers,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
                self.assertEqual(200, response.status_code, response.text)
                return response.json()["result"]
            tools = {tool["name"]: tool for tool in rpc("tools/list")["tools"]}
            self.assertEqual(RESOURCE_URI, tools["show_homeegram"]["_meta"]["ui"]["resourceUri"])
            self.assertIn("app", tools["update_homeegram_workspace"]["_meta"]["ui"]["visibility"])
            self.assertNotIn("resourceUri", tools["update_homeegram_workspace"]["_meta"]["ui"])
            self.assertTrue(tools["show_homeegram"]["annotations"]["readOnlyHint"])
            self.assertFalse(tools["update_homeegram_workspace"]["annotations"]["readOnlyHint"])
            resource = next(row for row in rpc("resources/list")["resources"] if row["uri"] == RESOURCE_URI)
            self.assertEqual(RESOURCE_MIME, resource["mimeType"])
            self.assertEqual([], resource["_meta"]["ui"]["csp"]["connectDomains"])
            rendered = rpc("resources/read", {"uri": RESOURCE_URI})["contents"][0]
            self.assertEqual(RESOURCE_MIME, rendered["mimeType"])
            self.assertIn("<!doctype html", rendered["text"].lower())
            self.assertNotIn("never-share-this", rendered["text"])
            opened = rpc("tools/call", {"name": "show_homeegram", "arguments": {}})
            self.assertEqual("browser", opened["structuredContent"]["workspace"]["mode"])
            created = rpc("tools/call", {"name": "create_homeegram_workspace", "arguments": {
                "integration_id": "home", "request_id": str(uuid.uuid4())}})
            self.assertEqual(1, created["structuredContent"]["workspace"]["revision"])
            rejected = client.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": RESOURCE_URI}})
            self.assertEqual(401, rejected.status_code)
            origin = client.post("/mcp/", headers={**headers, "Origin": "https://evil.example"}, json={})
            self.assertEqual(403, origin.status_code)


if __name__ == "__main__":
    unittest.main()
