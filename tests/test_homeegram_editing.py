import asyncio
from copy import deepcopy
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
import uuid

from pydantic import ValidationError

from server.database import Database
from server.homeegram_editing import HGElementChange, NativeHomeegramEditorService
from server.homeegram_revision import homeegram_revision


def original():
    return {"id": 877, "name": "Test%20HG", "active": 1, "play": 0,
            "triggers": {"switch_triggers": [{"id": 1, "homeegram_id": 877}]},
            "conditions": {"attribute_conditions": [{"id": 2, "homeegram_id": 877,
                "node_id": 12, "attribute_id": 55, "operand": 1, "operator": 1,
                "value": 0, "branch": 1, "check_moment": 3}]},
            "actions": {"attribute_actions": [{"id": 3, "homeegram_id": 877,
                "node_id": 12, "attribute_id": 55, "source_attribute_id": 0,
                "command": 1, "value": 1, "branch": 1, "delay": 0}],
                "webhook_actions": [{"id": 4, "url": "https://private.invalid/secret"}]}}


class NativeEditorTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = Database(self.folder.name)
        self.db.save_integration({"id": "home", "module_id": "homee", "name": "Test", "enabled": True, "configuration": {}})
        self.adapter = SimpleNamespace(socket=object(), resources={"homeegrams": [original()]},
            nodes={12: {"id": 12, "attributes": [
                {"id": 55, "type": 1, "editable": True, "minimum": 0, "maximum": 1},
                {"id": 56, "type": 3, "editable": False}] }}, homeegram_mutations={})
        self.runtime = SimpleNamespace(adapters={"home": self.adapter})
        self.service = NativeHomeegramEditorService(self.db, self.runtime)

    def tearDown(self):
        self.db._connection.close()
        self.folder.cleanup()

    def validate(self, change):
        return self.service.validate_changes(self.adapter, original(), [change])[0]

    def test_schema_rejects_nested_branches_unknown_fields_and_wrong_kind(self):
        for value in ({"operation": "add", "section": "conditions", "kind": "attribute_actions"},
                      {"operation": "update", "section": "conditions", "kind": "attribute_conditions"},
                      {"operation": "remove", "section": "actions", "kind": "webhook_actions", "element_id": 4, "parameters": {"url": "x"}},
                      {"operation": "add", "section": "triggers", "kind": "switch_triggers", "parameters": {"children": []}}):
            with self.assertRaises(ValidationError):
                HGElementChange.model_validate(value)

    def test_new_condition_requires_explicit_path_and_preserves_prerequisite(self):
        params = {"node_id": 12, "attribute_id": 55, "operator": 1, "value": 0}
        change = {"operation": "add", "section": "conditions", "kind": "attribute_conditions", "parameters": params}
        with self.assertRaises(ValueError):
            self.validate(change)
        for branch in (0, 1):
            result = self.validate({**change, "parameters": {**params, "branch": branch}})
            self.assertEqual(branch, result["parameters"]["branch"])
            self.assertEqual(1, result["parameters"]["check_moment"])
        for branch in (-1, 2, True):
            with self.assertRaises(ValueError):
                self.validate({**change, "parameters": {**params, "branch": branch}})

    def test_patch_does_not_default_existing_if_to_always_or_drop_check_moment(self):
        change = {"operation": "update", "section": "conditions", "kind": "attribute_conditions",
                  "element_id": 2, "parameters": {"value": 1}}
        result = self.validate(change)
        self.assertEqual({"value": 1}, result["parameters"])
        self.assertNotIn("branch", result["parameters"])
        self.assertNotIn("check_moment", result["parameters"])

    def test_remove_unsupported_category_is_allowed_but_add_is_not(self):
        result = self.validate({"operation": "remove", "section": "actions", "kind": "webhook_actions", "element_id": 4})
        self.assertNotIn("parameters", result)
        with self.assertRaises(ValueError):
            self.validate({"operation": "add", "section": "actions", "kind": "webhook_actions", "parameters": {"url": "https://invalid"}})

    def test_device_values_reject_foreign_noneditable_nonfinite_and_bool(self):
        base = {"operation": "add", "section": "actions", "kind": "attribute_actions",
                "parameters": {"node_id": 12, "attribute_id": 55, "command": 1, "value": 1, "branch": 0}}
        self.assertEqual(0, self.validate(base)["parameters"]["branch"])
        for override in ({"node_id": 99}, {"attribute_id": 56}, {"value": float("nan")},
                         {"value": float("inf")}, {"value": True}, {"node_id": True},
                         {"command": 2}, {"value": 3}, {"value": 0.5}, {"source_attribute_id": 56}, {"delay": -1}):
            with self.assertRaises(ValueError):
                self.validate({**base, "parameters": {**base["parameters"], **override}})

    def test_time_window_rules_until_month_and_day_are_preserved(self):
        base = {"operation": "add", "section": "conditions", "kind": "time_conditions", "parameters": {
            "dtstart": "20260916T163700Z", "rrule": "UNTIL=20260930T163700;BYDAY=TU,WE,TH,SU;BYHOUR=16;BYMINUTE=37;INTERVAL=1;FREQ=WEEKLY",
            "duration": "PT3600S", "branch": 1, "check_moment": 3}}
        self.assertEqual(base["parameters"], self.validate(base)["parameters"])
        monthly = {**base["parameters"], "rrule": "INTERVAL=2;FREQ=MONTHLY;BYMONTHDAY=1,15,31;BYHOUR=0;BYMINUTE=0", "duration": "PT86400S"}
        self.validate({**base, "parameters": monthly})
        for override in ({"rrule": "FREQ=DAILY&branch=0"}, {"rrule": "FREQ=DAILY;BYHOUR=99"},
                         {"rrule": "FREQ=MONTHLY;BYMONTHDAY=0"}, {"rrule": "FREQ=DAILY;FREQ=WEEKLY"},
                         {"dtstart": "20260231T120000Z"}, {"duration": "PT0S"}, {"duration": "PT1S&branch=0"}):
            with self.assertRaises(ValueError):
                self.validate({**base, "parameters": {**base["parameters"], **override}})

    def test_snapshot_revision_ignores_runtime_but_covers_activation_and_unknown_configuration(self):
        raw, revision = self.service.snapshot("home", 877, "Test HG")
        self.assertEqual(revision, homeegram_revision({**raw, "play": 1, "last_triggered": 9, "state": 2}))
        self.assertNotEqual(revision, homeegram_revision({**raw, "active": 0}))
        self.assertNotEqual(revision, homeegram_revision({**raw, "future": "field"}))
        raw["actions"] = {}
        self.assertIn("webhook_actions", self.adapter.resources["homeegrams"][0]["actions"])

    def test_edit_validates_revision_rechecks_authority_and_filters_private_result(self):
        async def run():
            request_id = str(uuid.uuid4())
            raw = original()
            self.adapter.edit_homeegram_confirmed = AsyncMock(return_value={"homeegram_id": 877,
                "name": "Test HG", "active": 0, "status": "edited_disabled", "backup_saved": True,
                "replayed": False, "updated": 1, "backup": raw, "request_id": request_id})
            changes = [{"operation": "update", "section": "conditions", "kind": "attribute_conditions", "element_id": 2, "parameters": {"value": 1}}]
            with self.assertRaises(ValueError):
                await self.service.edit("home", 877, "Test HG", "0" * 64, changes, request_id)
            self.adapter.edit_homeegram_confirmed.assert_not_awaited()
            result = await self.service.edit("home", 877, "Test HG", homeegram_revision(raw), changes, request_id, authorize=lambda: True)
            self.assertNotIn("backup", result)
            self.assertNotIn("secret", str(result))
            callback = self.adapter.edit_homeegram_confirmed.await_args.kwargs["authorize"]
            self.assertTrue(await callback())
            self.runtime.adapters["home"] = deepcopy(self.adapter)
            with self.assertRaises(ValueError):
                await callback()
        asyncio.run(run())

    def test_active_requires_exact_name_boolean_uuid_and_permission(self):
        async def run():
            self.adapter.set_homeegram_active_confirmed = AsyncMock(return_value={"homeegram_id": 877,
                "name": "Test HG", "active": 1, "status": "active", "backup_saved": True})
            for name, active, request in (("Wrong", True, str(uuid.uuid4())), ("Test HG", 1, str(uuid.uuid4())), ("Test HG", True, "bad")):
                with self.assertRaises(ValueError):
                    await self.service.set_active("home", 877, name, active, request)
            with self.assertRaises(ValueError):
                await self.service.set_active("home", 877, "Test HG", True, str(uuid.uuid4()), authorize=lambda: False)
            self.adapter.set_homeegram_active_confirmed.assert_not_awaited()
            result = await self.service.set_active("home", 877, "Test HG", True, str(uuid.uuid4()))
            self.assertEqual(1, result["active"])
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
