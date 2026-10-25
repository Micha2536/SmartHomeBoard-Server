"""MCP wire coverage for stored HG actions and other-HG event triggers.

All native operations are mocked. Configuring a future play/stop action must
never invoke a live play/stop/activation command during create/read/edit.
"""
from contextlib import asynccontextmanager
from copy import deepcopy
import asyncio
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from server.database import Database
from server.homeegram_revision import homeegram_revision
from server.mcp_access import DraftInput, MCPAccess


class HomeegramReferenceMCPTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = Database(self.folder.name)
        self.db.save_integration(dict(id="home", module_id="homee", name="Test homee", enabled=True, configuration={}))
        self.node = {"id": 12, "name": "Licht", "attributes": [
            {"id": 55, "type": 1, "current_value": 0, "minimum": 0, "maximum": 1, "editable": True}]}
        self.original = {"id": 800, "name": "Licht%20Flur", "active": 1, "added": 100,
                         "triggers": {}, "conditions": {}, "actions": {}}
        target = {"id": 810, "name": "Keller%20Licht", "active": 0,
                  "triggers": {}, "conditions": {}, "actions": {}, "private": "do-not-disclose"}
        self.adapter = SimpleNamespace(socket=object(), nodes={12: self.node}, resources={"homeegrams": [self.original, target]},
            homeegram_mutations={})
        self.adapter.create_homeegram_disabled = AsyncMock(return_value={"homeegram_id": 900, "active": 0, "play": 0, "status": "created_disabled"})
        self.adapter.edit_homeegram_confirmed = AsyncMock(return_value={"homeegram_id": 800, "name": "Licht Flur", "active": 0,
            "status": "edited_disabled", "backup_saved": True})
        self.adapter.set_homeegram_active_confirmed = AsyncMock()
        self.adapter.play_homeegram = AsyncMock()
        self.adapter.stop_homeegram = AsyncMock()
        self.access = MCPAccess(self.db, SimpleNamespace(adapters={"home": self.adapter}, visible_nodes=lambda: [self.node]), lambda: "admin")

        @asynccontextmanager
        async def lifespan(app):
            async with self.access.server.session_manager.run():
                yield

        app = FastAPI(lifespan=lifespan)
        app.include_router(self.access.router)
        app.mount("/mcp", self.access.http_app)
        self.client = TestClient(app).__enter__()
        self.admin = {"Authorization": "Bearer admin"}
        self.configure(allow_drafts=True)
        token = self.client.post("/api/v1/mcp/rotate-token", headers=self.admin).json()["token"]
        self.headers = {"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.db._connection.close()
        self.folder.cleanup()

    def configure(self, **grants):
        response = self.client.put("/api/v1/mcp", headers=self.admin, json={"enabled": True, **grants})
        self.assertEqual(200, response.status_code, response.text)

    def rpc(self, name, arguments):
        response = self.client.post("/mcp/", headers=self.headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
        self.assertEqual(200, response.status_code, response.text)
        return response.json()["result"]

    def payload(self, result):
        self.assertFalse(result.get("isError", False), result)
        return result.get("structuredContent") or json.loads(result["content"][0]["text"])

    def draft(self, **changes):
        return {"name": "HG Zusammenarbeit", "manual_play": False,
                "triggers": [{"type": "homeegram", "source_homeegram_id": 810}],
                "actions": [{"type": "homeegram", "target_homeegram_id": 810, "operation": "play", "branch": 1, "delay": 2.5}],
                **changes}

    def create(self, draft):
        return self.rpc("create_homeegram", {"integration_id": "home", "request_id": str(uuid.uuid4()), "homeegram": draft})

    def assert_no_immediate_execution(self):
        self.adapter.set_homeegram_active_confirmed.assert_not_awaited()
        self.adapter.play_homeegram.assert_not_awaited()
        self.adapter.stop_homeegram.assert_not_awaited()

    def test_create_four_stored_actions_native_codes_and_mixed_device_syntax(self):
        self.configure(allow_homee_creation=True)
        actions = [{"type": "homeegram", "target_homeegram_id": 810, "operation": operation,
                    "branch": index % 3, "delay": index + 0.25}
                   for index, operation in enumerate(("activate", "deactivate", "play", "stop"))]
        actions.append({"node_id": 12, "attribute_id": 55, "value": 1})
        self.payload(self.create(self.draft(actions=actions)))
        native = self.adapter.create_homeegram_disabled.await_args.args[0]
        self.assertEqual(0, native["active"])
        self.assertEqual(0, native["play"])
        self.assertEqual([1, 2, 3, 4], [row["homeegram_event"] for row in native["actions"]["homeegram_actions"]])
        self.assertEqual([0, 1, 2, 0], [row["branch"] for row in native["actions"]["homeegram_actions"]])
        self.assertEqual([0.25, 1.25, 2.25, 3.25], [row["delay"] for row in native["actions"]["homeegram_actions"]])
        self.assertEqual(1, len(native["actions"]["attribute_actions"]))
        for row in native["actions"]["homeegram_actions"]:
            self.assertEqual({"id", "target_homeegram_id", "homeegram_event", "branch", "delay"}, set(row))
        self.assert_no_immediate_execution()

    def test_other_hg_trigger_events_are_separate_from_own_play_button(self):
        self.configure(allow_drafts=True)
        triggers = [{"type": "homeegram", "source_homeegram_id": 810, "event": event}
                    for event in ("activated", "deactivated", "played", "stopped")]
        result = self.payload(self.rpc("create_homeegram_draft", {"integration_id": "home", "request_id": str(uuid.uuid4()),
            "draft": self.draft(triggers=triggers, manual_play=True)}))
        hg = result["homeegram"]
        self.assertEqual(1, len(hg["triggers"]["switch_triggers"]))
        self.assertEqual([1, 2, 3, 4], [row["homeegram_event"] for row in hg["triggers"]["homeegram_triggers"]])
        for row in hg["triggers"]["homeegram_triggers"]:
            self.assertEqual({"id", "source_homeegram_id", "homeegram_event"}, set(row))
        default = self.access.build_homeegram("home", DraftInput.model_validate(self.draft()))
        self.assertEqual([], default["triggers"]["switch_triggers"])
        self.assertEqual(3, default["triggers"]["homeegram_triggers"][0]["homeegram_event"])
        self.adapter.create_homeegram_disabled.assert_not_awaited()
        self.assert_no_immediate_execution()

    def test_existing_creation_grant_gates_references_without_expanding_permissions(self):
        result = self.create(self.draft())
        self.assertTrue(result["isError"])
        self.adapter.create_homeegram_disabled.assert_not_awaited()
        self.configure(allow_homee_creation=True)
        self.payload(self.create(self.draft()))
        status = self.access.status()
        self.assertFalse(status["allow_homee_activation"])
        self.assertFalse(status["allow_device_control"])
        self.assertFalse(status["allow_homee_editing"])
        self.assert_no_immediate_execution()

    def test_strict_reference_ids_and_no_attribute_fields_in_hg_reference(self):
        for bad_id in (0, -1, True, "810", 810.0, None):
            for section, key in (("actions", "target_homeegram_id"), ("triggers", "source_homeegram_id")):
                draft = self.draft()
                draft[section][0][key] = bad_id
                with self.assertRaises(ValidationError, msg=(section, bad_id)):
                    DraftInput.model_validate(draft)
        for section in ("actions", "triggers"):
            for extra in ({"node_id": 12}, {"attribute_id": 55}, {"value": 1}):
                draft = self.draft()
                draft[section][0].update(extra)
                with self.assertRaises(ValidationError, msg=(section, extra)):
                    DraftInput.model_validate(draft)
            draft = self.draft()
            del draft[section][0]["type"]
            with self.assertRaises(ValidationError):
                DraftInput.model_validate(draft)
        for changes in ({"actions": [{"type": "homeegram", "target_homeegram_id": 810, "operation": "restart"}]},
                        {"triggers": [{"type": "homeegram", "source_homeegram_id": 810, "event": "equal"}]}):
            with self.assertRaises(ValidationError):
                DraftInput.model_validate(self.draft(**changes))

    def test_cross_source_and_ambiguous_cache_references_are_rejected_before_write(self):
        self.configure(allow_homee_creation=True)
        self.db.save_integration(dict(id="other", module_id="homee", name="Other", enabled=True, configuration={}))
        self.access.runtime.adapters["other"] = SimpleNamespace(socket=object(), nodes={}, resources={"homeegrams": [{"id": 999}]})
        for section, key in (("actions", "target_homeegram_id"), ("triggers", "source_homeegram_id")):
            draft = self.draft()
            draft[section][0][key] = 999
            self.assertTrue(self.create(draft)["isError"])
        self.adapter.resources["homeegrams"].append(deepcopy(self.adapter.resources["homeegrams"][1]))
        self.assertTrue(self.create(self.draft())["isError"])
        self.adapter.create_homeegram_disabled.assert_not_awaited()

    def test_native_admin_deploy_preserves_branch_delay_event_and_checks_native_types(self):
        native = self.access.build_homeegram("home", DraftInput.model_validate(self.draft()))
        action = native["actions"]["homeegram_actions"][0]
        action.update(type=6, branch=2, delay=5.75, homeegram_event=4)
        native["triggers"]["homeegram_triggers"][0].update(type=5, homeegram_event=1)
        key = str(uuid.uuid4())
        self.access.create_draft("home", key, DraftInput.model_validate(self.draft()))
        result = self.client.post(f"/api/v1/mcp/drafts/{key}/deploy", headers=self.admin, json={"homeegram": native})
        self.assertEqual(200, result.status_code, result.text)
        canonical = self.adapter.create_homeegram_disabled.await_args.args[0]
        self.assertEqual((4, 2, 5.75), tuple(canonical["actions"]["homeegram_actions"][0][field] for field in ("homeegram_event", "branch", "delay")))
        self.assertEqual(1, canonical["triggers"]["homeegram_triggers"][0]["homeegram_event"])
        for section in ("triggers", "actions"):
            for field, bad in (("type", 99), ("type", True), ("homeegram_event", True), ("homeegram_event", "3"),
                               ("homeegram_event", 0), ("source_attribute_id", 55), ("url", "secret")):
                broken = deepcopy(native)
                broken[section]["homeegram_" + section][0][field] = bad
                with self.assertRaises((ValueError, ValidationError), msg=(section, field, bad)):
                    self.access.validate_edited_homeegram("home", broken)
        self.assert_no_immediate_execution()

    def test_read_includes_safe_target_source_names_and_missing_references(self):
        self.original["actions"] = {"homeegram_actions": [
            {"id": code + 20, "target_homeegram_id": 810, "homeegram_event": code, "branch": code % 3, "delay": code}
            for code in (1, 2, 3, 4)] + [{"id": 30, "target_homeegram_id": 999, "homeegram_event": 3, "branch": 1}]}
        self.original["triggers"] = {"homeegram_triggers": [{"id": 41, "source_homeegram_id": 810, "homeegram_event": 3}]}
        result = self.payload(self.rpc("get_homeegram", {"integration_id": "home", "homeegram_id": 800}))
        self.assertNotIn("homeegram_actions", result["omitted_categories"])
        self.assertNotIn("homeegram_triggers", result["omitted_categories"])
        rows = result["homeegram"]["actions"]["homeegram_actions"]
        self.assertEqual([1, 2, 3, 4], [row["homeegram_event"] for row in rows[:4]])
        self.assertTrue(all(row["target_homeegram"]["name"] == "Keller Licht" for row in rows[:4]))
        self.assertTrue(rows[4]["target_homeegram"]["missing"])
        trigger = result["homeegram"]["triggers"]["homeegram_triggers"][0]
        self.assertEqual("Keller Licht", trigger["source_homeegram"]["name"])
        self.assertNotIn("do-not-disclose", str(result))
        self.assert_no_immediate_execution()

    def test_edit_stores_native_reference_patches_behind_existing_edit_grant(self):
        self.original["actions"] = {"homeegram_actions": [{"id": 21, "target_homeegram_id": 810, "homeegram_event": 3, "branch": 2, "delay": 7}]}
        changes = [
            {"operation": "update", "section": "actions", "kind": "homeegram_actions", "element_id": 21,
             "parameters": {"homeegram_event": 4}},
            {"operation": "add", "section": "triggers", "kind": "homeegram_triggers",
             "parameters": {"source_homeegram_id": 810, "homeegram_event": 3}},
            {"operation": "add", "section": "actions", "kind": "homeegram_actions",
             "parameters": {"target_homeegram_id": 810, "homeegram_event": 1, "branch": 0, "delay": 5}},
        ]
        arguments = {"integration_id": "home", "homeegram_id": 800, "confirmation_name": "Licht Flur",
                     "expected_revision": homeegram_revision(self.original), "request_id": str(uuid.uuid4()), "changes": changes}
        self.assertTrue(self.rpc("edit_homeegram", arguments)["isError"])
        self.adapter.edit_homeegram_confirmed.assert_not_awaited()
        self.configure(allow_homee_editing=True)
        self.payload(self.rpc("edit_homeegram", arguments))
        wire = self.adapter.edit_homeegram_confirmed.await_args.args[2]
        self.assertEqual({"homeegram_event": 4}, wire[0]["parameters"])
        self.assertEqual({"source_homeegram_id": 810, "homeegram_event": 3}, wire[1]["parameters"])
        self.assertEqual(0, wire[2]["parameters"]["branch"])
        self.assertEqual(5, wire[2]["parameters"]["delay"])
        self.assertTrue(asyncio.run(self.adapter.edit_homeegram_confirmed.await_args.kwargs["authorize"]()))
        self.assert_no_immediate_execution()


if __name__ == "__main__":
    unittest.main()
