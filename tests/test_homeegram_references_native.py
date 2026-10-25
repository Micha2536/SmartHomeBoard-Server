"""Stored HG references use a fake peer and never execute referenced HGs."""
import asyncio
from copy import deepcopy
import json
import tempfile
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs
import uuid

from test_homee import Context, homee
from test_homeegram_editing import original
from server.database import Database
from server.homeegram_editing import NativeHomeegramEditorService
from server.homeegram_revision import homeegram_revision


def action(event=3, **extra):
    return {"target_homeegram_id": 7, "homeegram_event": event, "branch": 2, "delay": 3.5, **extra}


def trigger(event=3, **extra):
    return {"source_homeegram_id": 7, "homeegram_event": event, **extra}


def payload():
    return {"name": "HG references", "active": 0, "play": 0,
            "triggers": {"homeegram_triggers": [trigger()]}, "conditions": {},
            "actions": {"homeegram_actions": [action(event) for event in (1, 2, 3, 4)]}}


class ReferenceSocket:
    def __init__(self, adapter):
        self.adapter = adapter
        self.remote = [original(), {"id": 7, "name": "Target", "active": 1, "play": 0,
                                  "triggers": {}, "conditions": {}, "actions": {}}]
        self.messages = []
        self.backups = []
        self.remove_target_after_header = False
        self.lose_element_reply = False

    async def reply(self):
        await self.adapter._handle_message(json.dumps({"homeegrams": deepcopy(self.remote)}))

    async def send(self, command):
        self.messages.append(command)
        if command == "GET:homeegrams":
            await self.reply()
            return
        self.backups.append(deepcopy(self.adapter.context.state))
        path, _, query = command.partition("?")
        values = {key: self.scalar(items[0]) for key, items in parse_qs(query).items()}
        parts = path.split("/")
        verb = parts[0].split(":")[0]
        if path == "POST:homeegrams":
            self.remote.append({**values, "id": 878, "play": 0,
                                "triggers": {}, "conditions": {}, "actions": {}})
            if self.remove_target_after_header:
                self.remote = [row for row in self.remote if row["id"] != 7]
        else:
            hg = next(row for row in self.remote if row["id"] == int(parts[1]))
            if len(parts) == 2:
                hg.update(values)
            elif verb == "POST":
                section = parts[2]
                code = values.pop("type")
                kind = next(kind for kind, number in homee._HG_NATIVE_TYPES[section].items() if number == code)
                values.pop("logicType", None)
                values.update(id=100 + len(self.messages), homeegram_id=hg["id"])
                hg[section].setdefault(kind, []).append(values)
            else:
                section, identifier = parts[2], int(parts[3])
                for rows in hg[section].values():
                    row = next((item for item in rows if item["id"] == identifier), None)
                    if row is not None:
                        rows.remove(row) if verb == "DELETE" else row.update(values)
                        break
            if len(parts) > 2 and self.lose_element_reply:
                raise ConnectionError("Stored element, reply lost")
        await self.reply()

    @staticmethod
    def scalar(value):
        try:
            return float(value) if "." in value else int(value)
        except ValueError:
            return value


class NativeHGReferenceTests(unittest.TestCase):
    def adapter(self):
        adapter = homee.HomeeAdapter({}, Context())
        adapter.homeegram_wait_seconds = 0.02
        adapter.socket = ReferenceSocket(adapter)
        adapter.resources["homeegrams"] = deepcopy(adapter.socket.remote)
        return adapter, adapter.socket

    def assert_no_target_execution(self, socket):
        self.assertFalse(any(message.startswith("PUT:homeegrams/7") for message in socket.messages))
        self.assertFalse(any("play=" in message or "active=1" in message for message in socket.messages))

    def test_create_all_four_actions_hg_only_and_played_trigger_native_types_and_idempotence(self):
        async def run():
            adapter, socket = self.adapter()
            first = await adapter.create_homeegram_disabled(payload(), "references-create")
            self.assertEqual("created_disabled", first["status"])
            self.assertEqual(0, first["homeegram"]["active"])
            self.assertEqual([1, 2, 3, 4], [row["homeegram_event"] for row in first["homeegram"]["actions"]["homeegram_actions"]])
            self.assertEqual(3, first["homeegram"]["triggers"]["homeegram_triggers"][0]["homeegram_event"])
            self.assertTrue(any("/triggers?homeegram_event=3&source_homeegram_id=7&type=5" in command for command in socket.messages))
            self.assertEqual(4, sum("target_homeegram_id=7&type=6" in command for command in socket.messages))
            self.assertTrue(all(row["branch"] == 2 and row["delay"] == 3.5 for row in first["homeegram"]["actions"]["homeegram_actions"]))
            write_count = sum(not command.startswith("GET:") for command in socket.messages)
            second = await adapter.create_homeegram_disabled(payload(), "references-create")
            self.assertFalse(second["created"])
            self.assertEqual(write_count, sum(not command.startswith("GET:") for command in socket.messages))
            self.assert_no_target_execution(socket)
        asyncio.run(run())

    def test_all_four_trigger_events_are_stored_without_executing_them(self):
        async def run():
            adapter, socket = self.adapter()
            draft = payload()
            draft["triggers"]["homeegram_triggers"] = [trigger(event) for event in (1, 2, 3, 4)]
            result = await adapter.create_homeegram_disabled(draft, "trigger-events")
            self.assertEqual([1, 2, 3, 4], [row["homeegram_event"] for row in result["homeegram"]["triggers"]["homeegram_triggers"]])
            self.assert_no_target_execution(socket)
        asyncio.run(run())

    def test_unknown_duplicate_and_other_homee_ids_rejected_before_post(self):
        async def run():
            for source, target, duplicate in ((404, 7, False), (7, 404, False), (7, 7, True)):
                adapter, socket = self.adapter()
                draft = payload()
                draft["triggers"]["homeegram_triggers"][0]["source_homeegram_id"] = source
                draft["actions"]["homeegram_actions"][0]["target_homeegram_id"] = target
                if duplicate:
                    socket.remote.append(deepcopy(socket.remote[1]))
                with self.assertRaises(ValueError):
                    await adapter.create_homeegram_disabled(draft, str(uuid.uuid4()))
                self.assertEqual(["GET:homeegrams"], socket.messages)
                self.assertFalse(adapter.homeegram_creations)
        asyncio.run(run())

    def test_reference_disappearing_after_header_prevents_reference_element_post(self):
        async def run():
            adapter, socket = self.adapter()
            socket.remove_target_after_header = True
            with self.assertRaises(ValueError):
                await adapter.create_homeegram_disabled(payload(), "missing-reference")
            self.assertEqual(1, sum(command.startswith("POST:") for command in socket.messages))
            self.assertEqual("uncertain_or_partial", adapter.homeegram_creations["missing-reference"]["status"])
            self.assert_no_target_execution(socket)
        asyncio.run(run())

    def test_parser_rejects_boolean_bad_event_delay_and_unknown_fields_without_writes(self):
        for override in ({"target_homeegram_id": True}, {"target_homeegram_id": 0},
                         {"homeegram_event": True}, {"homeegram_event": 0}, {"homeegram_event": 5},
                         {"homeegram_event": 3.0}, {"branch": True}, {"branch": 3},
                         {"delay": True}, {"delay": -1}, {"delay": float("inf")},
                         {"delay": float("nan")}, {"delay": 604801}, {"command": 3}):
            draft = payload()
            draft["actions"]["homeegram_actions"] = [action(**override)]
            with self.subTest(override=override), self.assertRaises(ValueError):
                homee._disabled_homeegram_payload(draft)
        for override in ({"source_homeegram_id": True}, {"homeegram_event": False}, {"homeegram_event": 5}):
            draft = payload()
            draft["triggers"]["homeegram_triggers"] = [trigger(**override)]
            with self.subTest(override=override), self.assertRaises(ValueError):
                homee._disabled_homeegram_payload(draft)

    def test_edit_add_update_remove_references_preserve_legacy_and_self_reference(self):
        async def run():
            adapter, socket = self.adapter()
            changes = [{"operation": "add", "section": "triggers", "kind": "homeegram_triggers", "parameters": trigger()},
                       {"operation": "add", "section": "actions", "kind": "homeegram_actions", "parameters": action(4)}]
            result = await adapter.edit_homeegram_confirmed(877, "Test HG", changes, str(uuid.uuid4()), expected_revision=homeegram_revision(socket.remote[0]))
            self.assertEqual("edited_disabled", result["status"])
            current = socket.remote[0]
            self.assertEqual(original()["actions"]["webhook_actions"], current["actions"]["webhook_actions"])
            stored = current["actions"]["homeegram_actions"][0]
            updated = [{"operation": "update", "section": "actions", "kind": "homeegram_actions", "element_id": stored["id"],
                        "parameters": {"target_homeegram_id": 877, "homeegram_event": 3}}]
            await adapter.edit_homeegram_confirmed(877, "Test HG", updated, str(uuid.uuid4()), expected_revision=homeegram_revision(current))
            edited = current["actions"]["homeegram_actions"][0]
            self.assertEqual((877, 3, 2, 3.5), tuple(edited[key] for key in ("target_homeegram_id", "homeegram_event", "branch", "delay")))
            for section, kind in (("actions", "homeegram_actions"), ("triggers", "homeegram_triggers")):
                change = {"operation": "remove", "section": section, "kind": kind, "element_id": current[section][kind][0]["id"]}
                await adapter.edit_homeegram_confirmed(877, "Test HG", [change], str(uuid.uuid4()), expected_revision=homeegram_revision(current))
                self.assertEqual([], current[section][kind])
            self.assert_no_target_execution(socket)
        asyncio.run(run())

    def test_edit_fresh_reference_check_and_authorize_recheck_prevents_disable(self):
        async def run():
            changes = [{"operation": "add", "section": "actions", "kind": "homeegram_actions", "parameters": action()}]
            for mode in ("fresh-missing", "authorization-changed"):
                adapter, socket = self.adapter()
                calls = 0
                async def authorize():
                    nonlocal calls
                    calls += 1
                    if mode == "authorization-changed" and calls >= 2:
                        adapter.resources["homeegrams"] = [adapter.resources["homeegrams"][0]]
                    return True
                if mode == "fresh-missing":
                    socket.remote = socket.remote[:1]
                with self.assertRaises(ValueError):
                    await adapter.edit_homeegram_confirmed(877, "Test HG", changes, str(uuid.uuid4()), authorize=authorize)
                self.assertFalse(socket.backups)
                self.assertEqual(1, socket.remote[0]["active"])
        asyncio.run(run())

    def test_edit_uncertain_reference_post_is_never_repeated_on_retry(self):
        async def run():
            adapter, socket = self.adapter()
            socket.remote[0]["active"] = 0
            socket.lose_element_reply = True
            changes = [{"operation": "add", "section": "actions", "kind": "homeegram_actions", "parameters": action()}]
            key = str(uuid.uuid4())
            revision = homeegram_revision(socket.remote[0])
            with self.assertRaises(ConnectionError):
                await adapter.edit_homeegram_confirmed(877, "Test HG", changes, key, expected_revision=revision)
            socket.lose_element_reply = False
            result = await adapter.edit_homeegram_confirmed(877, "Test HG", changes, key, expected_revision=revision)
            self.assertTrue(result["replayed"])
            self.assertEqual(1, sum(command.startswith("POST:") for command in socket.messages))
            self.assert_no_target_execution(socket)
        asyncio.run(run())

    def test_legacy_reference_event_update_does_not_invent_missing_branch_or_delay(self):
        async def run():
            adapter, socket = self.adapter()
            legacy = {"id": 42, "homeegram_id": 877, "target_homeegram_id": 7, "homeegram_event": 3}
            socket.remote[0]["actions"]["homeegram_actions"] = [legacy]
            raw = deepcopy(socket.remote[0])
            change = {"operation": "update", "section": "actions", "kind": "homeegram_actions", "element_id": 42,
                      "parameters": {"homeegram_event": 4}}
            with tempfile.TemporaryDirectory() as folder:
                db = Database(folder)
                try:
                    service = NativeHomeegramEditorService(db, SimpleNamespace(adapters={}))
                    wire = service.validate_changes(adapter, raw, [change])
                    self.assertEqual({"homeegram_event": 4}, wire[0]["parameters"])
                    result = await adapter.edit_homeegram_confirmed(877, "Test HG", wire, str(uuid.uuid4()),
                        expected_revision=homeegram_revision(raw))
                    self.assertEqual("edited_disabled", result["status"])
                    self.assertEqual({**legacy, "homeegram_event": 4}, socket.remote[0]["actions"]["homeegram_actions"][0])
                    element_put = next(command for command in socket.messages if command.startswith("PUT:homeegrams/877/actions/42?"))
                    self.assertNotIn("branch=", element_put)
                    self.assertNotIn("delay=", element_put)
                    self.assert_no_target_execution(socket)
                finally:
                    db._connection.close()
        asyncio.run(run())

    def test_new_reference_action_requires_branch_but_legacy_explicit_invalid_branch_is_rejected(self):
        adapter, _ = self.adapter()
        raw = original()
        raw["actions"]["homeegram_actions"] = [{"id": 42, "homeegram_id": 877,
                                                "target_homeegram_id": 7, "homeegram_event": 3}]
        with tempfile.TemporaryDirectory() as folder:
            db = Database(folder)
            try:
                service = NativeHomeegramEditorService(db, SimpleNamespace(adapters={}))
                addition = {"operation": "add", "section": "actions", "kind": "homeegram_actions",
                            "parameters": {"target_homeegram_id": 7, "homeegram_event": 3}}
                for validator in (lambda value: service.validate_changes(adapter, raw, [value]),
                                  lambda value: homee._prepare_homeegram_edit(raw, homee._homeegram_edit_changes([value]))):
                    with self.assertRaises(ValueError):
                        validator(addition)
                    for invalid in (None, True, 3, -1):
                        update = {"operation": "update", "section": "actions", "kind": "homeegram_actions", "element_id": 42,
                                  "parameters": {"homeegram_event": 4, "branch": invalid}}
                        with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                            validator(update)
                raw["actions"]["homeegram_actions"][0]["delay"] = -1
                update = {"operation": "update", "section": "actions", "kind": "homeegram_actions", "element_id": 42,
                          "parameters": {"homeegram_event": 4}}
                with self.assertRaises(ValueError):
                    service.validate_changes(adapter, raw, [update])
                with self.assertRaises(ValueError):
                    homee._prepare_homeegram_edit(raw, [update])
            finally:
                db._connection.close()

    def test_service_same_source_strict_fields_and_partial_updates(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(folder)
            try:
                adapter, socket = self.adapter()
                service = NativeHomeegramEditorService(db, SimpleNamespace(adapters={}))
                for event in (1, 2, 3, 4):
                    for section, kind, parameters in (("actions", "homeegram_actions", action(event)),
                                                      ("triggers", "homeegram_triggers", trigger(event))):
                        change = {"operation": "add", "section": section, "kind": kind, "parameters": parameters}
                        wire = service.validate_changes(adapter, original(), [change])
                        self.assertEqual(event, wire[0]["parameters"]["homeegram_event"])
                for override in ({"target_homeegram_id": True}, {"target_homeegram_id": 404},
                                 {"homeegram_event": True}, {"homeegram_event": 5}, {"branch": True},
                                 {"delay": float("inf")}, {"delay": -1}, {"delay": True}):
                    change = {"operation": "add", "section": "actions", "kind": "homeegram_actions", "parameters": action(**override)}
                    with self.subTest(override=override), self.assertRaises(ValueError):
                        service.validate_changes(adapter, original(), [change])
                raw = original()
                raw["actions"]["homeegram_actions"] = [{"id": 42, "homeegram_id": 877, **action()}]
                update = {"operation": "update", "section": "actions", "kind": "homeegram_actions", "element_id": 42,
                          "parameters": {"homeegram_event": 4}}
                self.assertEqual({"homeegram_event": 4}, service.validate_changes(adapter, raw, [update])[0]["parameters"])
                adapter.resources["homeegrams"].append(deepcopy(socket.remote[1]))
                with self.assertRaises(ValueError):
                    service.validate_changes(adapter, raw, [update])
            finally:
                db._connection.close()


if __name__ == "__main__":
    unittest.main()
