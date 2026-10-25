"""Shared workspace and safe read projection for native HG references."""

import asyncio
from copy import deepcopy
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

from pydantic import ValidationError

from server.database import Database
from server.homeegram_references import (ACTION_EVENTS, TRIGGER_EVENTS, HomeegramAction, HomeegramTrigger,
                                         expand_homeegram_reference)
from server.mcp_access import MCPAccess
from server.mcp_homeegram_ui import WorkspaceDraft, _draft_from_native


class HomeegramReferenceWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = Database(self.folder.name)
        self.db.save_integration(dict(id="home", module_id="homee", name="Test homee", enabled=True, configuration={}))
        self.target = {"id": 810, "name": "Keller%20Licht", "active": 1,
                       "triggers": {}, "conditions": {}, "actions": {}, "url": "private-hg-url"}
        self.adapter = SimpleNamespace(nodes={}, resources={"homeegrams": [self.target]}, socket=object(),
            create_homeegram_disabled=AsyncMock(return_value={"homeegram_id": 900, "active": 0, "play": 0,
                                                              "status": "created_disabled"}))
        self.access = MCPAccess(self.db, SimpleNamespace(adapters={"home": self.adapter}), lambda: "admin")
        self.db.set_setting("mcp_access", {"enabled": True, "allow_drafts": True, "allow_homee_creation": True})
        self.service = self.access.homeegram_workspace

    def tearDown(self):
        self.db._connection.close()
        self.folder.cleanup()

    def reference_draft(self):
        return WorkspaceDraft(name="HG Verknüpfung", manual_play=False,
            triggers=[{"type": "homeegram", "source_homeegram_id": 810, "event": event} for event in TRIGGER_EVENTS],
            actions=[{"type": "homeegram", "target_homeegram_id": 810, "operation": operation,
                      "branch": index % 3, "delay": index + 0.5} for index, operation in enumerate(ACTION_EVENTS)])

    def create_filled(self):
        created = self.service.create("home", str(uuid4()), "HG Verknüpfung")["workspace"]
        return self.service.update(created["draft_id"], created["revision"], str(uuid4()), self.reference_draft())

    def test_workspace_supports_all_four_reference_actions_and_trigger_events(self):
        workspace = self.create_filled()["workspace"]
        self.assertTrue(workspace["editable"])
        self.assertFalse(workspace["draft"]["manual_play"])
        self.assertEqual(list(ACTION_EVENTS), [row["operation"] for row in workspace["draft"]["actions"]])
        self.assertEqual(list(TRIGGER_EVENTS), [row["event"] for row in workspace["draft"]["triggers"]])
        native = workspace["homeegram"]
        self.assertEqual([], native["triggers"]["switch_triggers"])
        for section in ("triggers", "actions"):
            rows = native[section]["homeegram_" + section]
            self.assertEqual([1, 2, 3, 4], [row["homeegram_event"] for row in rows])
            reference_key = "target_homeegram" if section == "actions" else "source_homeegram"
            self.assertTrue(all(row[reference_key]["name"] == "Keller Licht" for row in rows))
        self.assertNotIn("private-hg-url", json.dumps(workspace))
        self.adapter.create_homeegram_disabled.assert_not_awaited()

    def test_recover_legacy_native_shared_draft_is_lossless_for_supported_refs(self):
        native = self.access.build_homeegram("home", self.reference_draft())
        recovered = _draft_from_native(native)
        self.assertEqual(self.reference_draft().model_dump(), recovered.model_dump())
        self.assertEqual(native, self.access.build_homeegram("home", recovered))
        broken = deepcopy(native)
        broken["actions"]["homeegram_actions"][0]["future_parameter"] = "do-not-drop"
        self.assertIsNone(_draft_from_native(broken))

    def test_deploy_hg_only_actions_keeps_parent_disabled_and_never_executes_target(self):
        workspace = self.create_filled()["workspace"]
        result = asyncio.run(self.service.deploy(workspace["draft_id"], workspace["revision"], str(uuid4())))
        self.assertEqual(900, result["workspace"]["deployment"]["homeegram_id"])
        payload = self.adapter.create_homeegram_disabled.await_args.args[0]
        self.assertEqual((0, 0), (payload["active"], payload["play"]))
        self.assertEqual(4, len(payload["actions"]["homeegram_actions"]))
        self.assertEqual([], payload["actions"]["attribute_actions"])

    def test_invalid_reference_does_not_update_durable_workspace(self):
        workspace = self.create_filled()["workspace"]
        draft = self.reference_draft().model_dump()
        draft["actions"][0]["target_homeegram_id"] = 999
        with self.assertRaises(ValueError):
            self.service.update(workspace["draft_id"], workspace["revision"], str(uuid4()), WorkspaceDraft.model_validate(draft))
        after = self.service.get(draft_id=workspace["draft_id"])["workspace"]
        self.assertEqual(workspace["revision"], after["revision"])
        self.assertEqual(workspace["draft"], after["draft"])

    def test_reference_models_reject_mixed_device_fields_invalid_types_and_events(self):
        good = {"type": "homeegram", "target_homeegram_id": 810, "operation": "play"}
        for overrides in ({"target_homeegram_id": True}, {"target_homeegram_id": "810"}, {"target_homeegram_id": 0},
                          {"operation": "delete"}, {"branch": True}, {"delay": True}, {"delay": float("inf")},
                          {"delay": -1}, {"delay": 604801}, {"node_id": 12}, {"value": 1}):
            with self.subTest(overrides=overrides), self.assertRaises(ValidationError):
                HomeegramAction.model_validate({**good, **overrides})
        self.assertEqual("played", HomeegramTrigger(type="homeegram", source_homeegram_id=810).event)
        with self.assertRaises(ValidationError):
            HomeegramTrigger(type="homeegram", source_homeegram_id=810, event="play")

    def test_read_projection_keeps_missing_and_unknown_references_explicit(self):
        for section, id_field, reference in (("actions", "target_homeegram_id", "target_homeegram"),
                                              ("triggers", "source_homeegram_id", "source_homeegram")):
            row = expand_homeegram_reference(self.adapter, section, {"id": 1, id_field: 810,
                "homeegram_event": 3, "url": "never-share", "message": "private"})
            self.assertEqual("Keller Licht", row[reference]["name"])
            self.assertFalse(row[reference]["missing"])
            self.assertIn("Abspielen" if section == "actions" else "Wird ausgelöst", row["summary"])
            self.assertNotIn("never-share", json.dumps(row))
            unknown = expand_homeegram_reference(self.adapter, section, {id_field: 999, "homeegram_event": 88})
            self.assertTrue(unknown[reference]["missing"])
            self.assertIn("Unbekanntes", unknown["summary"])
            self.assertTrue(unknown["warnings"])


if __name__ == "__main__":
    unittest.main()
