import asyncio
import copy
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from server.database import Database
from server.mcp_device_control import DeviceControl
from server.runtime import Runtime


class DeviceControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = Database(self.folder.name)
        self.db.save_integration({"id": "home", "module_id": "homee", "name": "Test",
                                  "enabled": True, "configuration": {}})
        self.node = {"id": 1200, "integration_id": "home", "integration_module": "homee", "name": "Licht",
            "attributes": [{"id": 5500, "type": 1, "current_value": 0, "editable": True},
                           {"id": 5600, "type": 3, "current_value": 12, "editable": True},
                           {"id": 5700, "type": 1, "current_value": 1, "editable": False}]}
        self.runtime = SimpleNamespace(visible_nodes=lambda: [self.node],
            adapters={"home": object()}, set_value=AsyncMock())
        self.allowed = True
        self.control = DeviceControl(self.db, self.runtime, lambda: self.allowed)

    def tearDown(self):
        self.db._connection.close()
        self.folder.cleanup()

    async def command(self, operation="on", key=None, node_id=1200, attribute_id=5500):
        return await self.control.control(node_id, attribute_id, operation, key or str(uuid.uuid4()))

    async def test_on_and_off_use_audited_runtime_path_without_claiming_confirmation(self):
        for operation, value in (("on", 1), ("off", 0)):
            result = await self.command(operation)
            self.assertTrue(result["command_sent"])
            self.assertFalse(result["confirmed"])
            self.assertEqual(value, result["requested_value"])
            args, kwargs = self.runtime.set_value.call_args
            self.assertEqual((1200, 5500, value), args)
            self.assertEqual("mcp", kwargs["source"])
            self.assertEqual("mcp", kwargs["client_id"])
            self.assertEqual(result["request_id"], kwargs["metadata"]["request_id"])

    async def test_separate_permission_and_ids_types_writability(self):
        self.allowed = False
        with self.assertRaisesRegex(ValueError, "nicht freigegeben"):
            await self.command()
        self.allowed = True
        for node, attribute in ((999, 5500), (1200, 999), (1200, 5600), (1200, 5700)):
            with self.assertRaises(ValueError):
                await self.command(node_id=node, attribute_id=attribute)
        self.runtime.set_value.assert_not_awaited()

    async def test_hidden_device_and_attribute_from_another_device_are_rejected(self):
        other = copy.deepcopy(self.node)
        other.update(id=1201, attributes=[{"id": 9999, "type": 1, "editable": True}])
        self.runtime.visible_nodes = lambda: [self.node, other]
        with self.assertRaises(ValueError):
            await self.command(attribute_id=9999)
        self.runtime.visible_nodes = lambda: []
        with self.assertRaises(ValueError):
            await self.command()
        self.runtime.set_value.assert_not_awaited()

    async def test_active_integration_required(self):
        self.runtime.adapters.clear()
        with self.assertRaisesRegex(ValueError, "aktiven"):
            await self.command()
        self.runtime.adapters["home"] = object()
        integration = self.db.integration("home")
        integration["enabled"] = False
        self.db.save_integration(integration)
        with self.assertRaisesRegex(ValueError, "aktiven"):
            await self.command()
        self.runtime.set_value.assert_not_awaited()

    async def test_permission_revoked_while_waiting_prevents_send(self):
        await self.control._lock.acquire()
        pending = asyncio.create_task(self.command())
        await asyncio.sleep(0)
        self.allowed = False
        self.control._lock.release()
        with self.assertRaisesRegex(ValueError, "nicht freigegeben"):
            await pending
        self.runtime.set_value.assert_not_awaited()

    async def test_toggle_only_uses_known_binary_current_cache(self):
        for previous, requested in ((0, 1), (1, 0)):
            self.node["attributes"][0]["current_value"] = previous
            result = await self.command("toggle")
            self.assertEqual(previous, result["previous_value"])
            self.assertEqual(requested, result["requested_value"])
            self.assertEqual("cached_current_value", result["toggle_basis"])
        self.runtime.set_value.reset_mock()
        for invalid in (None, "0", 2, -1, float("nan"), float("inf"), 10 ** 1000):
            self.node["attributes"][0]["current_value"] = invalid
            self.node["attributes"][0]["target_value"] = 0
            with self.assertRaisesRegex(ValueError, "Cachewert"):
                await self.command("toggle")
        self.runtime.set_value.assert_not_awaited()
        self.assertIsNone((await self.command("on"))["previous_value"])

    async def test_uuid_validation_and_no_other_operations(self):
        for operation, key, node_id in (("set", str(uuid.uuid4()), 1200),
                                        ("on", "not-an-id", 1200),
                                        ("on", str(uuid.uuid4()), True)):
            with self.assertRaises(ValueError):
                await self.command(operation, key, node_id)
        self.runtime.set_value.assert_not_awaited()

    async def test_retries_replay_original_result_after_cache_changes_and_restart(self):
        key = str(uuid.uuid4())
        result = await self.command("toggle", key)
        self.node["attributes"][0]["current_value"] = 1
        repeat = await self.command("toggle", key.upper())
        self.assertTrue(repeat["replayed"])
        self.assertEqual(result["requested_value"], repeat["requested_value"])
        self.control = DeviceControl(self.db, self.runtime, lambda: self.allowed)
        self.assertTrue((await self.command("toggle", key))["replayed"])
        self.runtime.set_value.assert_awaited_once()
        for operation, node, attribute in (("off", 1200, 5500), ("toggle", 1201, 5500), ("toggle", 1200, 5700)):
            with self.assertRaisesRegex(ValueError, "anderen Gerätebefehl"):
                await self.command(operation, key, node, attribute)

    async def test_concurrent_retries_send_once_and_wait_for_same_service_result(self):
        key = str(uuid.uuid4())
        entered, finish = asyncio.Event(), asyncio.Event()
        async def send(*args, **kwargs):
            entered.set()
            await finish.wait()
        self.runtime.set_value.side_effect = send
        first = asyncio.create_task(self.command("toggle", key))
        await entered.wait()
        second = asyncio.create_task(self.command("toggle", key))
        await asyncio.sleep(0)
        self.assertFalse(second.done())
        finish.set()
        a, b = await asyncio.gather(first, second)
        self.assertTrue(a["command_sent"])
        self.assertTrue(b["command_sent"])
        self.assertTrue(b["replayed"])
        self.runtime.set_value.assert_awaited_once()

    async def test_second_service_never_reissues_pending_reserved_command(self):
        key = str(uuid.uuid4())
        entered, finish = asyncio.Event(), asyncio.Event()
        async def send(*args, **kwargs):
            entered.set()
            await finish.wait()
        self.runtime.set_value.side_effect = send
        first = asyncio.create_task(self.command("toggle", key))
        await entered.wait()
        service = DeviceControl(self.db, self.runtime, lambda: True)
        repeat = await service.control(1200, 5500, "toggle", key)
        self.assertIsNone(repeat["command_sent"])
        self.assertEqual("pending", repeat["status"])
        self.assertTrue(repeat["replayed"])
        finish.set()
        await first
        self.runtime.set_value.assert_awaited_once()

    async def test_ambiguous_adapter_failure_is_durable_and_never_retried(self):
        key = str(uuid.uuid4())
        self.runtime.set_value.side_effect = TimeoutError("private adapter details")
        result = await self.command("toggle", key)
        self.assertEqual("uncertain", result["status"])
        self.assertIsNone(result["command_sent"])
        self.assertFalse(result["retry_allowed"])
        self.assertNotIn("private adapter details", str(result))
        self.control = DeviceControl(self.db, self.runtime, lambda: self.allowed)
        repeat = await self.command("toggle", key)
        self.assertTrue(repeat["replayed"])
        self.assertEqual("uncertain", repeat["status"])
        self.runtime.set_value.assert_awaited_once()

    async def test_cancelled_command_is_persisted_as_uncertain(self):
        key = str(uuid.uuid4())
        entered = asyncio.Event()
        async def send(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.runtime.set_value.side_effect = send
        task = asyncio.create_task(self.command("toggle", key))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        result = await self.command("toggle", key)
        self.assertEqual("uncertain", result["status"])
        self.assertTrue(result["replayed"])
        self.runtime.set_value.assert_awaited_once()

    async def test_journal_capacity_refuses_new_requests_without_evicting_replay_ids(self):
        self.control.MAX_RECORDS = 3
        key = str(uuid.uuid4())
        await self.command("on", key)
        with self.db._lock, self.db._connection:
            self.db._connection.executemany(
                "INSERT INTO mcp_device_commands(request_id,fingerprint,result) VALUES(?,?,?)",
                [(str(uuid.uuid4()), "f", "{}") for _ in range(self.control.MAX_RECORDS - 1)],
            )
        with self.assertRaisesRegex(ValueError, "Journal|journal"):
            await self.command()
        self.assertTrue((await self.command("on", key))["replayed"])
        self.runtime.set_value.assert_awaited_once()

    async def test_runtime_records_mcp_source_in_normal_command_audit(self):
        self.db.save_node("home", self.node)
        runtime = Runtime(self.db, SimpleNamespace())
        adapter = SimpleNamespace(set_value=AsyncMock())
        runtime.adapters["home"] = adapter
        self.control = DeviceControl(self.db, runtime, lambda: True)
        result = await self.command()
        rows = self.db.command_audit()
        self.assertEqual(1, len(rows))
        self.assertEqual("mcp", rows[0]["source"])
        self.assertEqual("sent", rows[0]["status"])
        self.assertEqual(result["request_id"], rows[0]["metadata"]["request_id"])
        adapter.set_value.assert_awaited_once_with(1200, 5500, 1)


if __name__ == "__main__":
    unittest.main()
