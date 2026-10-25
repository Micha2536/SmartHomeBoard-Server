"""Batch orchestration tests; every homee transport is an in-memory fake."""

from copy import deepcopy
import inspect
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import quote
from uuid import UUID, uuid4

from pydantic import ValidationError

from server.database import Database
from server.homeegram_batch_deletion import (
    HGDeletionTarget,
    HomeegramBatchDeletionService,
)


def original(identifier, name):
    return {
        "id": identifier,
        "name": quote(name, safe=""),
        "added": 1789650898 + identifier,
        "active": 0,
        "triggers": {"switch_triggers": [{"id": identifier + 1000}]},
        "conditions": {},
        "actions": {
            "webhook_actions": [{
                "id": identifier + 2000,
                "url": "https://private.invalid/private-webhook-token",
            }],
        },
    }


class HomeegramBatchDeletionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = Database(self.folder.name)
        self.db.save_integration({
            "id": "home",
            "module_id": "homee",
            "name": "Test homee",
            "enabled": True,
            "configuration": {},
        })
        self.live = [original(849, "Kellerlicht Hobbyraum"), original(877, "Test HG")]
        self.adapter = SimpleNamespace(
            socket=object(),
            resources={"homeegrams": deepcopy(self.live)},
            homeegram_deletions={},
        )
        self.runtime = SimpleNamespace(adapters={"home": self.adapter})
        self.calls = []
        self.sends = []
        self.confirmed = {}
        self.fail_once = set()
        self.bad_response = None
        self.adapter._refresh_homeegram_snapshot = AsyncMock(side_effect=self._refresh)
        self.adapter.delete_homeegram_confirmed = AsyncMock(side_effect=self._delete)
        self.service = HomeegramBatchDeletionService(self.db, self.runtime)

    def tearDown(self):
        self.db._connection.close()
        self.folder.cleanup()

    async def _refresh(self, socket):
        self.assertIs(socket, self.adapter.socket)
        self.adapter.resources["homeegrams"] = deepcopy(self.live)

    async def _delete(self, identifier, name, request_id, authorize=None):
        if authorize is not None:
            allowed = authorize()
            if inspect.isawaitable(allowed):
                allowed = await allowed
            if allowed is not True:
                raise ValueError("Freigabe wurde widerrufen")
        self.calls.append((identifier, name, request_id))
        UUID(request_id)
        if self.bad_response is not None:
            return deepcopy(self.bad_response)
        replayed = request_id in self.confirmed
        if not replayed:
            self.adapter.homeegram_deletions[request_id] = {"homeegram_id": identifier, "name": name}
            self.sends.append((identifier, request_id))
        result = {
            "homeegram_id": identifier,
            "name": name,
            "status": "deleted",
            "deleted": True,
            "backup_saved": True,
            "replayed": replayed,
            "backup": original(identifier, name),
            "secret": "private-result-secret",
        }
        self.confirmed[request_id] = deepcopy(result)
        self.live = [row for row in self.live if row["id"] != identifier]
        self.adapter.resources["homeegrams"] = deepcopy(self.live)
        if identifier in self.fail_once:
            self.fail_once.remove(identifier)
            # homee applied DELETE, but confirmation arrived after the timeout.
            # A retry may only confirm the reserved operation, never resend it.
            raise TimeoutError("Die Löschung ist noch nicht eindeutig bestätigt")
        return result

    @staticmethod
    def targets():
        return [
            HGDeletionTarget(homeegram_id=849, confirmation_name="Kellerlicht Hobbyraum"),
            HGDeletionTarget(homeegram_id=877, confirmation_name="Test HG"),
        ]

    async def delete(self, targets=None, request_id=None, authorize=None):
        return await self.service.delete(
            "home", self.targets() if targets is None else targets,
            str(uuid4()) if request_id is None else request_id,
            authorize=authorize,
        )

    def test_target_schema_requires_strict_positive_id_and_exact_visible_name(self):
        for identifier in (True, False, 0, -1, 849.0, "849"):
            with self.subTest(identifier=identifier), self.assertRaises(ValidationError):
                HGDeletionTarget(homeegram_id=identifier, confirmation_name="Kellerlicht")
        for name in ("", "   ", "x" * 501):
            with self.subTest(name=name[:20]), self.assertRaises(ValidationError):
                HGDeletionTarget(homeegram_id=849, confirmation_name=name)
        target = HGDeletionTarget(homeegram_id=849, confirmation_name="  Licht %20  ")
        self.assertEqual("  Licht %20  ", target.confirmation_name)
        with self.assertRaises(ValidationError):
            HGDeletionTarget(homeegram_id=849, confirmation_name="Licht", unexpected=True)

    async def test_invalid_uuid_counts_and_duplicate_ids_never_send_first_delete(self):
        invalid_batches = [[], [self.targets()[0]] * 2,
                           [HGDeletionTarget(homeegram_id=i, confirmation_name="Licht")
                            for i in range(1, 52)]]
        for targets in invalid_batches:
            with self.subTest(count=len(targets)), self.assertRaises(ValueError):
                await self.delete(targets=targets)
        with self.assertRaises(ValueError):
            await self.delete(request_id="not-a-uuid")
        self.adapter.delete_homeegram_confirmed.assert_not_awaited()

    async def test_missing_disabled_wrong_module_and_disconnected_source_are_rejected(self):
        for mode in ("missing", "disabled", "foreign", "disconnected", "missing_adapter"):
            with self.subTest(mode=mode):
                saved = self.db.integration("home")
                try:
                    if mode == "missing":
                        saved["id"] = "different-home"
                        # A non-existent integration is deliberately addressed.
                        with self.assertRaises(ValueError):
                            await self.service.delete("not-home", self.targets(), str(uuid4()))
                        continue
                    if mode == "disabled":
                        self.db.save_integration({**saved, "enabled": False})
                    elif mode == "foreign":
                        self.db.save_integration({**saved, "module_id": "other"})
                    elif mode == "disconnected":
                        self.adapter.socket = None
                    else:
                        self.runtime.adapters.clear()
                    with self.assertRaises((ValueError, ConnectionError)):
                        await self.delete()
                finally:
                    self.db.save_integration({**saved, "id": "home", "module_id": "homee", "enabled": True})
                    self.adapter.socket = object()
                    self.runtime.adapters["home"] = self.adapter
        self.adapter.delete_homeegram_confirmed.assert_not_awaited()

    async def test_missing_permission_is_rejected_without_delete(self):
        async def denied(adapter):
            self.assertIs(self.adapter, adapter)
            return False

        with self.assertRaises(ValueError):
            await self.delete(authorize=denied)
        self.adapter.delete_homeegram_confirmed.assert_not_awaited()

    async def test_all_fresh_names_are_checked_before_any_delete(self):
        # Cache still contains the old name. The fresh upstream snapshot changed.
        self.live[1]["name"] = quote("Umbenannt", safe="")
        with self.assertRaises(ValueError):
            await self.delete()
        self.adapter._refresh_homeegram_snapshot.assert_awaited()
        self.adapter.delete_homeegram_confirmed.assert_not_awaited()

    async def test_all_targets_must_exist_uniquely_before_first_delete(self):
        for live in ([self.live[0]], self.live + [deepcopy(self.live[1])]):
            with self.subTest(length=len(live)):
                self.live = deepcopy(live)
                with self.assertRaises(ValueError):
                    await self.delete()
                self.adapter.delete_homeegram_confirmed.assert_not_awaited()

    async def test_incomplete_original_in_later_target_prevents_entire_batch(self):
        for group in ("triggers", "conditions", "actions"):
            self.live = [original(849, "Kellerlicht Hobbyraum"), original(877, "Test HG")]
            del self.live[1][group]
            with self.subTest(group=group), self.assertRaises(ValueError):
                await self.delete()
            self.adapter.delete_homeegram_confirmed.assert_not_awaited()

    async def test_refresh_failure_never_sends_delete(self):
        self.adapter._refresh_homeegram_snapshot.side_effect = TimeoutError("Keine frische Liste")
        with self.assertRaises((TimeoutError, ValueError)):
            await self.delete()
        self.adapter.delete_homeegram_confirmed.assert_not_awaited()

    async def test_success_is_sequential_with_distinct_uuid_children_and_no_secrets(self):
        request_id = str(uuid4())
        result = await self.delete(request_id=request_id, authorize=lambda adapter: adapter is self.adapter)
        self.assertEqual("completed", result["status"])
        self.assertEqual(2, result["deleted_count"])
        self.assertEqual([849, 877], [row["homeegram_id"] for row in result["results"]])
        self.assertEqual(["deleted", "deleted"], [row["status"] for row in result["results"]])
        self.assertEqual([849, 877], [call[0] for call in self.calls])
        children = [call[2] for call in self.calls]
        self.assertEqual(2, len(set(children)))
        self.assertNotIn(request_id, children)
        self.assertTrue(all(UUID(child).version == 5 for child in children))
        encoded = json.dumps(result)
        for private in ("private-webhook-token", "private-result-secret", '"backup"'):
            self.assertNotIn(private, encoded)
        ledger = self.db.setting("mcp_homeegram_deletion_batches", {})
        self.assertIn(request_id, ledger)

    async def test_first_uncertain_result_stops_and_marks_later_targets_not_attempted(self):
        self.fail_once.add(849)
        result = await self.delete()
        self.assertIn(result["status"], {"partial", "stopped"})
        self.assertEqual(0, result["deleted_count"])
        self.assertEqual(["uncertain", "not_attempted"], [row["status"] for row in result["results"]])
        self.assertEqual([849], [call[0] for call in self.calls])

    async def test_partial_stop_preserves_success_and_does_not_continue_past_failure(self):
        self.live.append(original(888, "Drittes HG"))
        self.fail_once.add(877)
        targets = self.targets() + [HGDeletionTarget(homeegram_id=888, confirmation_name="Drittes HG")]
        result = await self.delete(targets)
        self.assertEqual("partial", result["status"])
        self.assertEqual(1, result["deleted_count"])
        self.assertEqual(["deleted", "uncertain", "not_attempted"], [row["status"] for row in result["results"]])
        self.assertEqual([849, 877], [call[0] for call in self.calls])

    async def test_retry_after_restart_reuses_same_child_uuid_and_tolerates_deleted_first_target(self):
        request_id = str(uuid4())
        self.fail_once.add(877)
        first = await self.delete(request_id=request_id)
        self.assertEqual(1, first["deleted_count"])
        first_children = {identifier: child for identifier, _, child in self.calls}
        self.db._connection.close()
        self.db = Database(self.folder.name)
        self.service = HomeegramBatchDeletionService(self.db, self.runtime)
        result = await self.delete(request_id=request_id)
        self.assertEqual("completed", result["status"])
        self.assertEqual(2, result["deleted_count"])
        for identifier, _, child in self.calls:
            self.assertEqual(first_children[identifier], child)
        self.assertGreaterEqual(sum(identifier == 877 for identifier, _, _ in self.calls), 2)
        self.assertEqual([849, 877], [identifier for identifier, _ in self.sends])

    async def test_completed_retry_cannot_create_new_child_request_ids(self):
        request_id = str(uuid4())
        await self.delete(request_id=request_id)
        first_children = {identifier: child for identifier, _, child in self.calls}
        result = await self.delete(request_id=request_id)
        self.assertEqual("completed", result["status"])
        self.assertEqual(2, result["deleted_count"])
        self.assertTrue(all(first_children[identifier] == child for identifier, _, child in self.calls))

    async def test_reserved_request_uuid_cannot_be_reused_for_changed_targets_or_source(self):
        request_id = str(uuid4())
        self.fail_once.add(849)
        await self.delete(request_id=request_id)
        count = len(self.calls)
        changed = [self.targets()[0], HGDeletionTarget(homeegram_id=877, confirmation_name="Different")]
        with self.assertRaises(ValueError):
            await self.delete(changed, request_id=request_id)
        with self.assertRaises(ValueError):
            await self.delete(list(reversed(self.targets())), request_id=request_id)
        self.db.save_integration({"id": "second", "module_id": "homee", "name": "Second", "enabled": True, "configuration": {}})
        self.runtime.adapters["second"] = self.adapter
        with self.assertRaises(ValueError):
            await self.service.delete("second", self.targets(), request_id)
        self.assertEqual(count, len(self.calls))

    async def test_literal_percent_sequences_and_surrounding_spaces_match_exactly(self):
        name = "  Licht %20 %2F  "
        self.live = [original(849, name)]
        result = await self.delete([HGDeletionTarget(homeegram_id=849, confirmation_name=name)])
        self.assertEqual("completed", result["status"])
        self.assertEqual(name, self.calls[0][1])
        self.assertEqual(name, result["results"][0]["name"])

    async def test_unconfirmed_or_wrong_identity_response_never_counts_as_deleted(self):
        for override in ({"deleted": False}, {"status": "unknown"}, {"homeegram_id": 877}, {"name": "Other"}):
            self.calls.clear()
            self.bad_response = {"deleted": True, "status": "deleted", "homeegram_id": 849,
                                 "name": "Kellerlicht Hobbyraum", **override}
            with self.subTest(override=override):
                result = await self.delete()
                self.assertEqual(0, result["deleted_count"])
                self.assertEqual("uncertain", result["results"][0]["status"])
                self.assertEqual("not_attempted", result["results"][1]["status"])
                self.assertEqual([849], [call[0] for call in self.calls])

    async def test_revocation_between_items_stops_before_next_delete(self):
        async def revoke_after_first(*args, **kwargs):
            result = await self._delete(*args, **kwargs)
            self.db.save_integration({**self.db.integration("home"), "enabled": False})
            return result

        self.adapter.delete_homeegram_confirmed.side_effect = revoke_after_first
        result = await self.delete(authorize=lambda adapter: adapter is self.adapter)
        self.assertEqual(1, result["deleted_count"])
        self.assertEqual([849], [call[0] for call in self.calls])
        self.assertNotEqual("deleted", result["results"][1]["status"])

    async def test_recreated_later_hg_with_same_id_and_name_is_not_silently_deleted(self):
        async def change_second_after_first(*args, **kwargs):
            result = await self._delete(*args, **kwargs)
            self.live[0]["added"] += 1
            self.adapter.resources["homeegrams"] = deepcopy(self.live)
            return result

        self.adapter.delete_homeegram_confirmed.side_effect = change_second_after_first
        result = await self.delete()
        self.assertEqual(1, result["deleted_count"])
        self.assertEqual([849], [call[0] for call in self.calls])
        self.assertEqual(["deleted", "uncertain"], [row["status"] for row in result["results"]])
        self.assertEqual([877], [row["id"] for row in self.live])

    async def test_failed_durable_batch_reservation_prevents_every_delete(self):
        with patch.object(self.db, "set_setting", side_effect=OSError("Disk full")):
            with self.assertRaises(OSError):
                await self.delete()
        self.adapter.delete_homeegram_confirmed.assert_not_awaited()
        self.assertEqual([], self.sends)

    async def test_replaced_source_during_refresh_never_sends_first_delete(self):
        async def replace_source(socket):
            await self._refresh(socket)
            self.runtime.adapters["home"] = SimpleNamespace(socket=object())

        self.adapter._refresh_homeegram_snapshot.side_effect = replace_source
        with self.assertRaises((ValueError, ConnectionError)):
            await self.delete()
        self.adapter.delete_homeegram_confirmed.assert_not_awaited()

    async def test_legacy_batch_without_new_pins_can_resume_unchanged_selection(self):
        request_id = str(uuid4())
        self.fail_once.add(849)
        await self.delete(request_id=request_id)
        journal = self.db.setting("mcp_homeegram_deletion_batches")
        for target in journal[request_id]["targets"]:
            target.pop("deletion_pin")
        self.db.set_setting("mcp_homeegram_deletion_batches", journal)
        result = await self.delete(request_id=request_id)
        self.assertEqual("completed", result["status"])
        self.assertEqual([849, 877], [identifier for identifier, _ in self.sends])

    async def test_legacy_batch_changed_selection_is_not_silently_re_pinned(self):
        request_id = str(uuid4())
        self.fail_once.add(849)
        await self.delete(request_id=request_id)
        journal = self.db.setting("mcp_homeegram_deletion_batches")
        for target in journal[request_id]["targets"]:
            target.pop("deletion_pin")
        self.db.set_setting("mcp_homeegram_deletion_batches", journal)
        self.live[0]["order"] = 0
        result = await self.delete(request_id=request_id)
        self.assertEqual("partial", result["status"])
        self.assertEqual(1, result["deleted_count"])
        self.assertEqual("selection_changed", result["results"][1]["error_code"])
        self.assertEqual([849], [identifier for identifier, _ in self.sends])
        persisted = self.db.setting("mcp_homeegram_deletion_batches")[request_id]
        self.assertNotIn("deletion_pin", persisted["targets"][1])

    async def test_private_transport_error_is_not_exposed_in_public_batch_result(self):
        self.adapter.delete_homeegram_confirmed.side_effect = RuntimeError(
            "https://private.invalid/private-webhook-token private-result-secret")
        result = await self.delete()
        self.assertEqual(0, result["deleted_count"])
        self.assertEqual("deletion_not_confirmed", result["results"][0]["error_code"])
        self.assertNotIn("private-webhook-token", json.dumps(result))
        self.assertNotIn("private-result-secret", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
