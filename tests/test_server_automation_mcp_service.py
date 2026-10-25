"""Stateful regression coverage without a network, timers or real devices."""

import asyncio
import json
from copy import deepcopy
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from pydantic import ValidationError

from server.automations import AutomationEngine
from server.database import Database
from server.mcp_server_automations import ServerAutomationInput, ServerAutomationMCP


class ServerAutomationServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = Database(self.folder.name)
        self.db.save_integration({"id": "home", "module_id": "homee", "name": "homee", "enabled": True,
                                  "configuration": {"password": "never-exposed-password"}})
        self.node = {"id": 12, "integration_id": "home", "integration_module": "homee", "integration_source": "server",
                     "name": "Flurlicht", "attributes": [
                         {"id": 55, "name": "An/Aus", "type": 1, "current_value": 0, "editable": True, "minimum": 0, "maximum": 1},
                         {"id": 56, "name": "Leistung", "type": 3, "current_value": 12, "editable": False}]}
        self.db.save_node("home", self.node)
        self.db.set_setting("device_groups", [{"id": "lights", "name": "Lichter", "node_ids": [12]}])
        self.db.set_setting("push_devices", [{"id": "phone", "device_name": "iPhone", "device_token": "never-exposed-apns-token"}])
        self.runtime = SimpleNamespace(database=self.db, visible_nodes=self.db.nodes, adapters={"home": object()},
                                       set_value=AsyncMock(), broadcast=AsyncMock())
        self.engine = AutomationEngine(self.runtime)
        self.runtime.automation_engine = self.engine
        self.engine.push_service = SimpleNamespace(status=lambda: {"configured": True, "devices": [{"id": "phone", "name": "iPhone"}]},
                                                  send=AsyncMock())
        self.grants = {"allow_server_automation_creation": True, "allow_server_automation_activation": True}
        self.service = ServerAutomationMCP(self.db, self.runtime, lambda name: self.grants.get(name, False))

    def tearDown(self):
        self.db._connection.close()
        self.folder.cleanup()

    def source(self, **values):
        return {"name": "Flurlicht automatisch", "actions": [
            {"kind": "setAttribute", "nodeID": 12, "attributeID": 55, "value": 1}], **values}

    async def create(self, source=None, request=None):
        return await self.service.create(ServerAutomationInput.model_validate(source or self.source()), request or str(uuid4()))

    async def active(self, result, value, request=None):
        current = self.service.get(result["automation_id"])
        return await self.service.set_active(result["automation_id"], current["automation"]["name"], value,
                                             current["revision"], request or str(uuid4()))

    def assert_not_executed(self):
        self.runtime.set_value.assert_not_awaited()
        self.runtime.broadcast.assert_not_awaited()
        self.engine.push_service.send.assert_not_awaited()
        self.assertIsNone(self.engine.timer_task)

    async def test_create_is_real_server_rule_disabled_with_stable_element_ids(self):
        result = await self.create()
        stored = self.db.setting("automations")[0]
        self.assertEqual(result["automation"], stored)
        self.assertIn(stored["id"], self.db.setting("automation_server_owned_ids"))
        self.assertFalse(stored["isEnabled"])
        self.assertEqual([], stored["triggers"])
        self.assertTrue(stored["actions"][0]["id"])
        self.assert_not_executed()

    async def test_create_retry_after_activation_does_not_disable_rule(self):
        request = str(uuid4())
        created = await self.create(request=request)
        await self.active(created, True)
        retry = await self.create(request=request)
        self.assertTrue(retry["replayed"])
        self.assertTrue(retry["current"]["active"])
        self.assertTrue(self.service.get(created["automation_id"])["automation"]["isEnabled"])
        self.assertEqual(1, len(self.db.setting("automations")))
        self.assert_not_executed()

    async def test_activation_replay_after_later_disable_does_not_reenable(self):
        created = await self.create()
        current = self.service.get(created["automation_id"])
        args = (created["automation_id"], current["automation"]["name"], True, current["revision"], str(uuid4()))
        await self.service.set_active(*args)
        await self.active(created, False)
        retry = await self.service.set_active(*args)
        self.assertTrue(retry["replayed"])
        self.assertFalse(retry["current"]["active"])
        self.assertFalse(self.service.get(created["automation_id"])["automation"]["isEnabled"])
        self.assert_not_executed()

    async def test_creation_retry_survives_service_and_engine_restart(self):
        request = str(uuid4())
        created = await self.create(request=request)
        self.runtime.automation_engine = AutomationEngine(self.runtime)
        restarted = ServerAutomationMCP(self.db, self.runtime, self.service.allowed)
        result = await restarted.create(ServerAutomationInput.model_validate(self.source()), request)
        self.assertEqual(created["automation_id"], result["automation_id"])
        self.assertTrue(result["replayed"])
        self.assertEqual(1, len(self.db.setting("automations")))

    async def test_creation_retry_never_resurrects_deleted_rule(self):
        request = str(uuid4())
        created = await self.create(request=request)
        self.engine.delete_server(created["automation_id"])
        result = await self.create(request=request)
        self.assertTrue(result["replayed"])
        self.assertFalse(result["current"]["present"])
        self.assertEqual([], self.db.setting("automations"))

    async def test_request_ids_bind_operation_and_payload(self):
        request = str(uuid4())
        created = await self.create(request=request)
        with self.assertRaisesRegex(ValueError, "anderen Automationsauftrag"):
            await self.create(self.source(name="Andere Regel"), request)
        current = self.service.get(created["automation_id"])
        with self.assertRaisesRegex(ValueError, "anderen Automationsauftrag"):
            await self.service.set_active(created["automation_id"], current["automation"]["name"], True,
                                          current["revision"], request)
        self.assertFalse(self.service.get(created["automation_id"])["automation"]["isEnabled"])

    async def test_pending_create_recovers_after_persist_without_reapplying(self):
        request = str(uuid4())
        created = await self.create(request=request)
        key = self.service._journal_key(request)
        record = self.db.setting(key)
        record.update(status="pending")
        record.pop("result")
        self.db.set_setting(key, record)
        await self.active(created, True)
        self.engine.upsert_server = Mock(wraps=self.engine.upsert_server)
        replay = await self.create(request=request)
        self.assertTrue(replay["replayed"])
        self.assertTrue(replay["current"]["active"])
        self.engine.upsert_server.assert_not_called()

    async def test_unconfirmed_pending_create_is_not_reissued(self):
        request = str(uuid4())
        created = await self.create(request=request)
        key = self.service._journal_key(request)
        record = self.db.setting(key)
        record.update(status="pending")
        record.pop("result")
        self.db.set_setting(key, record)
        self.engine.delete_server(created["automation_id"])
        with self.assertRaisesRegex(ValueError, "unbestätigt"):
            await self.create(request=request)
        self.assertEqual([], self.db.setting("automations"))

    async def test_pending_activation_recovers_observed_exact_result_only(self):
        created = await self.create()
        request = str(uuid4())
        before = self.service.get(created["automation_id"])
        args = (created["automation_id"], before["automation"]["name"], True, before["revision"], request)
        await self.service.set_active(*args)
        key = self.service._journal_key(request)
        record = self.db.setting(key)
        record.update(status="pending")
        record.pop("result")
        self.db.set_setting(key, record)
        self.engine.control = AsyncMock(wraps=self.engine.control)
        replay = await self.service.set_active(*args)
        self.assertTrue(replay["replayed"])
        self.engine.control.assert_not_awaited()
        await self.active(created, False)
        self.db.set_setting(key, record)
        with self.assertRaisesRegex(ValueError, "unbestätigt"):
            await self.service.set_active(*args)
        self.assertFalse(self.service.get(created["automation_id"])["automation"]["isEnabled"])

    async def test_creation_and_activation_permissions_rechecked_after_lock_wait(self):
        await self.service._lock.acquire()
        pending = asyncio.create_task(self.create())
        await asyncio.sleep(0)
        self.grants["allow_server_automation_creation"] = False
        self.service._lock.release()
        with self.assertRaisesRegex(ValueError, "nicht freigegeben"):
            await pending
        self.assertEqual([], self.db.setting("automations", []))
        self.grants["allow_server_automation_creation"] = True
        created = await self.create()
        await self.service._lock.acquire()
        pending = asyncio.create_task(self.active(created, True))
        await asyncio.sleep(0)
        self.grants["allow_server_automation_activation"] = False
        self.service._lock.release()
        with self.assertRaisesRegex(ValueError, "nicht freigegeben"):
            await pending
        self.assertFalse(self.service.get(created["automation_id"])["automation"]["isEnabled"])

    async def test_activation_revalidates_disabled_or_missing_references(self):
        created = await self.create()
        integration = self.db.integration("home")
        self.db.save_integration({**integration, "enabled": False})
        with self.assertRaisesRegex(ValueError, "aktiven"):
            await self.active(created, True)
        self.assertFalse(self.service.get(created["automation_id"])["automation"]["isEnabled"])
        # Deactivation remains possible even if the original device disappeared.
        result = await self.active(created, False)
        self.assertFalse(result["active"])

    async def test_group_members_must_be_active_visible_and_offer_attribute(self):
        group = {"kind": "groupAttribute", "groupID": "lights", "attributeType": 1, "value": 1}
        await self.create(self.source(conditions=[group]))
        for attributes in ([], [{"id": 55, "type": 999, "editable": True}]):
            self.db.save_node("home", {**self.node, "attributes": attributes})
            with self.assertRaises(ValueError):
                await self.create(self.source(conditions=[group]))
        self.db.save_node("home", self.node)
        self.db.set_setting("device_groups", [{"id": "lights", "name": "Lichter", "node_ids": [12, 999]}])
        with self.assertRaisesRegex(ValueError, "versteckte"):
            await self.create(self.source(conditions=[group]))

    async def test_old_app_rules_are_sanitized_and_not_mutable_by_activation(self):
        rule_id = str(uuid4())
        self.engine.replace_from_app([{**self.source(), "id": rule_id, "isEnabled": False,
                                       "private": {"token": "never-exposed-rule-token"},
                                       "alwaysActions": [{"kind": "showPopup", "secret": "never-exposed-popup"}]}])
        result = self.service.get(rule_id)
        self.assertEqual("app", result["origin"])
        self.assertNotIn("never-exposed", str(result))
        self.assertTrue(result["warnings"])
        with self.assertRaisesRegex(ValueError, "App"):
            await self.service.set_active(rule_id, result["automation"]["name"], True, result["revision"], str(uuid4()))

    async def test_catalog_never_exposes_credentials_or_push_tokens(self):
        result = self.service.capabilities()
        self.assertIn("iPhone", str(result))
        self.assertIn("Lichter", str(result))
        self.assertNotIn("never-exposed", str(result))
        self.assertEqual(6, len(result["action_kinds"]))
        self.assertIn("elseElseActions", result["native_schema"]["properties"])

    def test_strict_models_reject_nonfinite_unknown_ignored_and_invalid_fields(self):
        cases = [self.source(isEnabled=True), self.source(id="fixed"), self.source(name=" "),
                 self.source(cooldownSeconds=float("inf")), self.source(cooldownSeconds=True),
                 self.source(actions=[]), self.source(actions=[{"kind": "clientOnly"}]),
                 self.source(actions=[{"kind": "toggleAttribute", "nodeID": 12, "attributeID": 55, "value": 1}]),
                 self.source(actions=[{"kind": "setAttribute", "nodeID": "12", "attributeID": 55, "value": 1}]),
                 self.source(actions=[{"kind": "setAttribute", "nodeID": True, "attributeID": 55, "value": 1}]),
                 self.source(actions=[{"kind": "setAttribute", "nodeID": 12, "attributeID": 55, "value": float("nan")}]),
                 self.source(actions=[{"kind": "serverPushNotification", "nodeID": 12, "attributeID": 55}]),
                 self.source(triggers=[{"kind": "timeOnce", "scheduledAt": "2027-01-01T12:00:00"}]),
                 self.source(triggers=[{"kind": "timeDaily", "minuteOfDay": 1440}]),
                 self.source(triggers=[{"kind": "attributeChanged", "nodeID": 12, "attributeID": 55, "comparison": "equal"}]),
                 self.source(actions=self.source()["actions"] * 101)]
        for source in cases:
            with self.subTest(source=source):
                with self.assertRaises(ValidationError):
                    ServerAutomationInput.model_validate(source)

    async def test_server_upsert_preserves_unrelated_pending_actions(self):
        created = await self.create()
        target = Mock()
        unrelated = Mock()
        self.engine.action_tasks = {created["automation_id"] + ":wait": target, "unrelated:wait": unrelated}
        self.engine.action_timing = {created["automation_id"] + ":wait": {}, "unrelated:wait": {}}
        self.engine.upsert_server(deepcopy(created["automation"]))
        target.cancel.assert_called_once()
        unrelated.cancel.assert_not_called()
        self.assertEqual({"unrelated:wait": unrelated}, self.engine.action_tasks)

    async def test_disabling_already_disabled_rule_still_cancels_its_pending_wait(self):
        created = await self.create()
        target, unrelated = Mock(), Mock()
        self.engine.action_tasks = {created["automation_id"] + ":wait": target, "unrelated:wait": unrelated}
        self.engine.action_timing = {created["automation_id"] + ":wait": {}, "unrelated:wait": {}}
        result = await self.active(created, False)
        self.assertEqual("unchanged", result["status"])
        target.cancel.assert_called_once()
        unrelated.cancel.assert_not_called()
        self.assertEqual({"unrelated:wait": unrelated}, self.engine.action_tasks)
        self.assert_not_executed()

    async def test_creation_rule_ownership_sync_and_journal_commit_atomically(self):
        request = str(uuid4())
        original = self.db.set_setting

        def fail_completion(key, value):
            if key == self.service._journal_key(request) and value.get("status") == "complete":
                raise RuntimeError("simulated local persistence failure")
            original(key, value)

        with patch.object(self.db, "set_setting", side_effect=fail_completion):
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                await self.create(request=request)
        self.assertEqual([], self.db.setting("automations", []))
        self.assertEqual([], self.db.setting("automation_server_owned_ids", []))
        self.assertIsNone(self.db.setting("automations_synced_at"))
        self.assertIsNone(self.db.setting(self.service._journal_key(request)))
        self.assertEqual([], self.engine.rules)
        self.assertEqual(set(), self.engine.server_owned_ids)
        # No partial mutation escaped the transaction; this exact request can
        # now be safely committed once, rather than replaying a partial write.
        created = await self.create(request=request)
        self.assertEqual(1, len(self.engine.rules))
        self.assertEqual("complete", self.db.setting(self.service._journal_key(request))["status"])
        self.assertFalse(created["active"])

    async def test_activation_rollback_reloads_inmemory_enabled_state(self):
        created = await self.create()
        request = str(uuid4())
        original = self.db.set_setting

        def fail_completion(key, value):
            if key == self.service._journal_key(request) and value.get("status") == "complete":
                raise RuntimeError("simulated activation failure")
            original(key, value)

        with patch.object(self.db, "set_setting", side_effect=fail_completion):
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                await self.active(created, True, request)
        self.assertFalse(self.db.setting("automations")[0]["isEnabled"])
        self.assertFalse(self.engine.rules[0]["isEnabled"])
        self.assertIsNone(self.db.setting(self.service._journal_key(request)))
        self.assert_not_executed()

    async def test_engine_control_and_preflight_share_one_write_transaction(self):
        created = await self.create()
        original_find = self.service._find
        original_control = self.engine.control
        checks = []

        def find_inside_transaction(automation_id):
            self.assertTrue(self.db._connection.in_transaction)
            checks.append("preflight")
            return original_find(automation_id)

        async def control_inside_transaction(*args, **kwargs):
            self.assertTrue(self.db._connection.in_transaction)
            checks.append("control")
            return await original_control(*args, **kwargs)

        before = self.service.get(created["automation_id"])
        with patch.object(self.service, "_find", side_effect=find_inside_transaction), patch.object(self.engine, "control", side_effect=control_inside_transaction):
            await self.service.set_active(created["automation_id"], before["automation"]["name"], True,
                                          before["revision"], str(uuid4()))
        self.assertIn("preflight", checks)
        self.assertIn("control", checks)
        self.assertFalse(self.db._connection.in_transaction)

    async def test_journal_bound_refuses_new_requests_without_evicting_replay_ids(self):
        self.service.MAX_RECORDS = 1
        request = str(uuid4())
        first = await self.create(request=request)
        with self.assertRaisesRegex(ValueError, "Journal|journal"):
            await self.create(self.source(name="Neue Regel"))
        replay = await self.create(request=request)
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["automation_id"], replay["automation_id"])
        self.assertEqual(1, len(self.engine.rules))

    async def test_64bit_server_ids_are_not_rounded_through_float(self):
        large_id, large_attribute = 9007199254740993, 9007199254740995
        self.db.save_node("home", {**self.node, "id": large_id, "attributes": [
            {"id": large_attribute, "type": 1, "editable": True, "minimum": 0, "maximum": 1}]})
        result = await self.create(self.source(actions=[
            {"kind": "setAttribute", "nodeID": large_id, "attributeID": large_attribute, "value": 1}]))
        self.assertEqual(large_id, result["automation"]["actions"][0]["nodeID"])
        self.assertEqual(large_attribute, result["automation"]["actions"][0]["attributeID"])

    def test_positive_hold_rejected_for_changed_and_time_triggers(self):
        for trigger in ({"kind": "attributeChanged", "nodeID": 12, "attributeID": 55},
                        {"kind": "attributeChangedBy", "nodeID": 12, "attributeID": 55, "value": 1},
                        {"kind": "timeDaily", "minuteOfDay": 12},
                        {"kind": "timeOnce", "scheduledAt": "2027-01-01T12:00:00Z"}):
            ServerAutomationInput.model_validate(self.source(triggers=[trigger]))
            with self.assertRaisesRegex(ValidationError, "holdSeconds"):
                ServerAutomationInput.model_validate(self.source(triggers=[{**trigger, "holdSeconds": 2}]))

    async def test_once_time_is_canonical_internet_timestamp_for_app_roundtrip(self):
        result = await self.create(self.source(triggers=[
            {"kind": "timeOnce", "scheduledAt": "2027-01-01 12:00:00+01:00"}]))
        self.assertEqual("2027-01-01T12:00:00+01:00", result["automation"]["triggers"][0]["scheduledAt"])
        current = self.service.get(result["automation_id"])
        self.assertEqual("2027-01-01T12:00:00+01:00", current["automation"]["triggers"][0]["scheduledAt"])
        await self.active(result, True)
        with self.assertRaises(ValidationError):
            ServerAutomationInput.model_validate(self.source(triggers=[
                {"kind": "timeOnce", "scheduledAt": "2027-01-01T12:00:00+01:00:30"}]))

    def test_held_attribute_threshold_does_not_trigger_on_first_edge(self):
        self.db.save_node("home", {**self.node, "attributes": [
            {"id": 55, "type": 1, "current_value": 1, "editable": True}]})
        trigger = {"kind": "attribute", "id": "held", "nodeID": 12, "attributeID": 55,
                   "comparison": "equal", "value": 1, "holdSeconds": 3}
        event = {"node_id": 12, "attribute_id": 55, "previous": 0, "value": 1}
        with patch("server.automations.time.monotonic", return_value=100):
            self.assertFalse(self.engine._event_trigger(trigger, event))
        with patch("server.automations.time.monotonic", return_value=102):
            self.assertFalse(self.engine._condition(trigger))
        with patch("server.automations.time.monotonic", return_value=103):
            self.assertTrue(self.engine._condition(trigger))
            self.assertFalse(self.engine._event_trigger(trigger, event))
            self.assertTrue(self.engine._condition(trigger))

    def test_held_group_threshold_emits_only_from_timer_not_member_events(self):
        self.db.save_node("home", {**self.node, "attributes": [
            {"id": 55, "type": 1, "current_value": 1, "editable": True}]})
        trigger = {"kind": "groupAttribute", "id": "held-group", "groupID": "lights", "attributeType": 1,
                   "comparison": "equal", "value": 1, "aggregation": "all", "holdSeconds": 3}
        event = {"node_id": 12, "attribute_id": 55, "attribute_type": 1, "previous": 0, "value": 1}
        with patch("server.automations.time.monotonic", return_value=100):
            self.assertFalse(self.engine._event_trigger(trigger, event))
        with patch("server.automations.time.monotonic", return_value=103):
            self.assertTrue(self.engine._condition(trigger))
            self.assertFalse(self.engine._event_trigger(trigger, event))
            self.assertTrue(self.engine._condition(trigger))

    def test_settings_transaction_excludes_a_separate_process_writer(self):
        # A second interpreter uses another SQLite connection, exactly like the
        # API/setup process split on the server. It cannot commit in the middle
        # of the optimistic preflight + native engine + journal transaction.
        script = """import json,sqlite3,sys
c=sqlite3.connect(sys.argv[1], timeout=5)
print('ready', flush=True)
c.execute('INSERT INTO settings(key,value) VALUES(?,?)', ('other_process', json.dumps(True)))
c.commit()
print('committed', flush=True)
"""
        process = None
        try:
            with self.db.settings_transaction():
                self.db.set_setting("first", 1)
                process = subprocess.Popen([sys.executable, "-c", script, self.folder.name + "/smarthomeboard.sqlite3"],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                self.assertEqual("ready", process.stdout.readline().strip())
                self.assertIsNone(process.poll())
                self.assertIsNone(self.db.setting("other_process"))
                self.db.set_setting("second", 2)
                self.assertTrue(self.db._connection.in_transaction)
            output, error = process.communicate(timeout=10)
            self.assertEqual(0, process.returncode, error)
            self.assertEqual("committed", output.strip())
            self.assertTrue(self.db.setting("other_process"))
            self.assertEqual(1, self.db.setting("first"))
            self.assertEqual(2, self.db.setting("second"))
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()


if __name__ == "__main__":
    unittest.main()
