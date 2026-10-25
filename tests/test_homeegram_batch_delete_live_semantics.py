"""Real MCP + adapter regression tests with an in-memory homee WebSocket.

Deleting an HG can renumber the remaining homeegram order values and remove
references to that deleted HG. Those homee-owned changes must not abort an
otherwise explicitly confirmed batch, whereas an unrelated edit must stop it.
No network connection or real device mutation is performed by these tests.
"""

from contextlib import asynccontextmanager
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from urllib.parse import quote
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.database import Database
from server.mcp_access import MCPAccess


MODULE_PATH = Path(__file__).parents[1] / "modules" / "homee" / "module.py"
SPEC = importlib.util.spec_from_file_location("homee_batch_live_semantics_module", MODULE_PATH)
homee = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(homee)


class DatabaseContext:
    integration_id = "home"

    def __init__(self, database):
        self.database = database

    def nodes(self):
        return []

    def load_state(self, default=None):
        return self.database.setting("module_state:home", default)

    def save_state(self, value):
        self.database.set_setting("module_state:home", value)


def original(identifier, name, order):
    return {
        "id": identifier,
        "name": quote(name, safe=""),
        "added": 1789650898 + identifier,
        "active": 0,
        "play": 0,
        "order": order,
        "triggers": {"switch_triggers": [{"id": identifier + 1000,
                                            "homeegram_id": identifier}]},
        "conditions": {},
        "actions": {"attribute_actions": [{"id": identifier + 2000,
                                             "homeegram_id": identifier,
                                             "node_id": 67, "attribute_id": 262,
                                             "command": 1, "value": 1, "branch": 0,
                                             "delay": 0}]},
    }


class CascadingDeleteReplySocket:
    """A complete HG snapshot is returned directly for every DELETE and GET."""

    def __init__(self, adapter, originals):
        self.adapter = adapter
        self.remote = deepcopy(originals)
        self.messages = []
        self.backups_at_send = {}
        self.mutation = "none"
        self.delete_count = 0
        self.metadata_only_on_delete = False
        self.drop_delete_replies_once = set()

    async def send(self, command):
        self.messages.append(command)
        if command == "GET:homeegrams":
            await self.adapter._handle_message(json.dumps({"homeegrams": self.remote}))
            return
        if not command.startswith("DELETE:homeegrams/"):
            raise AssertionError("Unexpected command: " + command)
        identifier = int(command.rsplit("/", 1)[-1])
        original = next(row for row in self.remote if row["id"] == identifier)
        journal = self.adapter.context.load_state()["homeegram_deletions"]
        reserved = next(row for row in journal.values() if row["homeegram_id"] == identifier)
        if reserved["status"] != "delete_reserved" or reserved.get("backup") != original:
            raise AssertionError("The current full HG must be durably backed up before DELETE")
        self.backups_at_send[identifier] = deepcopy(reserved["backup"])
        self.remote = [row for row in self.remote if row["id"] != identifier]
        self.delete_count += 1
        if self.mutation == "reorder":
            for order, row in enumerate(self.remote):
                row["order"] = order
        if self.mutation == "cleanup_reference":
            for row in self.remote:
                for section, collection in (("triggers", "homeegram_triggers"),
                                            ("conditions", "homeegram_conditions"),
                                            ("actions", "homeegram_actions")):
                    if collection not in row[section]:
                        continue
                    row[section][collection] = [
                        item for item in row[section].get(collection, [])
                        if item.get("target_homeegram_id") != identifier
                    ]
        if self.mutation == "unrelated_edit" and self.delete_count == 1:
            self.remote[0]["actions"]["attribute_actions"][0]["value"] = 0
        if self.mutation == "unrelated_reference_removal" and self.delete_count == 1:
            self.remote[0]["actions"]["homeegram_actions"] = []
        if self.mutation == "identity_reuse" and self.delete_count == 1:
            self.remote[0]["added"] += 1
        if self.mutation == "rename" and self.delete_count == 1:
            self.remote[0]["name"] = quote("Geänderter HG-Name", safe="")
        if identifier in self.drop_delete_replies_once:
            # homee applied the command, but the client could not determine
            # whether send completed and received no corresponding reply.
            self.drop_delete_replies_once.remove(identifier)
            raise TimeoutError("Simulated lost DELETE confirmation")
        reply = self.remote
        if self.metadata_only_on_delete:
            reply = [{key: row[key] for key in ("id", "name", "order", "added", "active")}
                     for row in self.remote]
        await self.adapter._handle_message(json.dumps({"homeegrams": reply}))


class HomeegramBatchDeletionLiveSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = Database(self.folder.name)
        self.db.save_integration({"id": "home", "module_id": "homee", "name": "Test homee",
                                  "enabled": True, "configuration": {}})
        self.originals = [original(identifier, name, order)
                          for order, (identifier, name) in enumerate([
                              (849, "Kellerlicht Hobbyraum"),
                              (850, "Kellerlicht Lager"),
                              (851, "Kellerlicht Werkstatt"),
                          ])]
        self.adapter = homee.HomeeAdapter({}, DatabaseContext(self.db))
        self.adapter.homeegram_wait_seconds = 0.05
        self.adapter.resources["homeegrams"] = deepcopy(self.originals)
        self.socket = CascadingDeleteReplySocket(self.adapter, self.originals)
        self.adapter.socket = self.socket
        runtime = SimpleNamespace(adapters={"home": self.adapter}, visible_nodes=lambda: [])
        self.access = MCPAccess(self.db, runtime, lambda: "admin-test")

        @asynccontextmanager
        async def lifespan(app):
            async with self.access.server.session_manager.run():
                yield

        app = FastAPI(lifespan=lifespan)
        app.include_router(self.access.router)
        app.mount("/mcp", self.access.http_app)
        self.client = TestClient(app).__enter__()
        admin = {"Authorization": "Bearer admin-test"}
        response = self.client.put("/api/v1/mcp", headers=admin,
                                   json={"enabled": True, "allow_homee_deletion": True})
        self.assertEqual(200, response.status_code, response.text)
        token = self.client.post("/api/v1/mcp/rotate-token", headers=admin).json()["token"]
        self.headers = {"Authorization": "Bearer " + token,
                        "Accept": "application/json, text/event-stream"}
        self.arguments = {"integration_id": "home", "request_id": str(uuid4()), "homeegrams": [
            {"homeegram_id": row["id"], "confirmation_name": name}
            for row, name in zip(self.originals, ["Kellerlicht Hobbyraum", "Kellerlicht Lager",
                                                   "Kellerlicht Werkstatt"])]}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.db._connection.close()
        self.folder.cleanup()

    def delete_mcp(self):
        response = self.client.post("/mcp/", headers=self.headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "delete_homeegrams", "arguments": self.arguments}})
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()["result"]
        self.assertFalse(result.get("isError", False), result)
        return result.get("structuredContent") or json.loads(result["content"][0]["text"])

    def assert_completed(self, result):
        self.assertEqual("completed", result["status"], result)
        self.assertEqual(3, result["deleted_count"])
        self.assertEqual(["deleted"] * 3, [row["status"] for row in result["results"]])
        self.assertEqual(["DELETE:homeegrams/849", "DELETE:homeegrams/850",
                          "DELETE:homeegrams/851"],
                         [message for message in self.socket.messages if message.startswith("DELETE:")])
        self.assertEqual([], self.socket.remote)
        self.assertEqual({849, 850, 851}, set(self.socket.backups_at_send))

    def test_full_mcp_batch_uses_real_adapter_and_retry_never_resends(self):
        self.assert_completed(self.delete_mcp())
        messages = list(self.socket.messages)
        result = self.delete_mcp()
        self.assertEqual("completed", result["status"])
        self.assertTrue(result["replayed"])
        self.assertEqual(messages, self.socket.messages)

    def test_homee_reordering_after_first_delete_must_not_abort_batch(self):
        self.socket.mutation = "reorder"
        self.assert_completed(self.delete_mcp())

    def test_metadata_only_delete_reply_is_refreshed_before_next_hg_validation(self):
        # DELETE can legitimately acknowledge absence via a complete list of
        # metadata-only HG records. The subsequent GET returns full originals.
        self.socket.metadata_only_on_delete = True
        self.assert_completed(self.delete_mcp())
        first = self.socket.messages.index("DELETE:homeegrams/849")
        second = self.socket.messages.index("DELETE:homeegrams/850")
        self.assertIn("GET:homeegrams", self.socket.messages[first + 1:second])

    def test_homee_reference_cleanup_after_first_delete_must_not_abort_batch(self):
        self.socket.remote[1]["actions"]["homeegram_actions"] = [{
            "id": 9991, "homeegram_id": 850, "target_homeegram_id": 849,
            "homeegram_event": 3, "branch": 1, "delay": 0,
        }]
        self.socket.mutation = "cleanup_reference"
        self.assert_completed(self.delete_mcp())

    def test_unrelated_action_edit_still_stops_before_second_delete(self):
        self.socket.mutation = "unrelated_edit"
        result = self.delete_mcp()
        self.assertEqual("partial", result["status"])
        self.assertEqual(1, result["deleted_count"])
        self.assertEqual(["deleted", "uncertain", "not_attempted"],
                         [row["status"] for row in result["results"]])
        self.assertEqual(["DELETE:homeegrams/849"],
                         [message for message in self.socket.messages if message.startswith("DELETE:")])
        self.assertEqual([850, 851], [row["id"] for row in self.socket.remote])

    def test_removing_reference_to_not_yet_deleted_hg_still_stops(self):
        # Only a reference to a positively confirmed earlier deletion may be
        # cleaned up automatically. HG 851 is still live and not yet attempted.
        self.socket.remote[1]["actions"]["homeegram_actions"] = [{
            "id": 9991, "homeegram_id": 850, "target_homeegram_id": 851,
            "homeegram_event": 3, "branch": 1, "delay": 0,
        }]
        self.socket.mutation = "unrelated_reference_removal"
        result = self.delete_mcp()
        self.assertEqual("partial", result["status"])
        self.assertEqual(1, result["deleted_count"])
        self.assertEqual(["DELETE:homeegrams/849"],
                         [message for message in self.socket.messages if message.startswith("DELETE:")])

    def test_reused_id_and_name_with_new_creation_time_still_stops(self):
        self.socket.mutation = "identity_reuse"
        result = self.delete_mcp()
        self.assertEqual("partial", result["status"])
        self.assertEqual(1, result["deleted_count"])
        self.assertEqual(["DELETE:homeegrams/849"],
                         [message for message in self.socket.messages if message.startswith("DELETE:")])

    def test_foreign_rename_still_stops_before_second_delete(self):
        self.socket.mutation = "rename"
        result = self.delete_mcp()
        self.assertEqual("partial", result["status"])
        self.assertEqual(1, result["deleted_count"])
        self.assertEqual(["DELETE:homeegrams/849"],
                         [message for message in self.socket.messages if message.startswith("DELETE:")])

    def test_fifteen_selected_hgs_delete_with_metadata_replies_and_reordering_only(self):
        names = ["Ausgewähltes HG " + str(index + 1) for index in range(15)]
        selected = [original(1000 + index, name, index)
                    for index, name in enumerate(names)]
        outside = original(507, "Trockner Push", 15)
        # Keep unrelated HG content meaningful, including fields that must not
        # be normalized away merely because order is managed by homee itself.
        outside["active"] = 1
        outside["actions"]["notification_actions"] = [{
            "id": 7788, "homeegram_id": 507, "user_id": 58,
            "message": "Trockner fertig", "branch": 0, "delay": 0,
        }]
        self.socket.remote = deepcopy(selected + [outside])
        self.adapter.resources["homeegrams"] = deepcopy(self.socket.remote)
        self.arguments["homeegrams"] = [
            {"homeegram_id": row["id"], "confirmation_name": name}
            for row, name in zip(selected, names)]
        self.socket.mutation = "reorder"
        self.socket.metadata_only_on_delete = True

        result = self.delete_mcp()
        self.assertEqual("completed", result["status"], result)
        self.assertEqual(15, result["deleted_count"])
        self.assertEqual(15, result["total_count"])
        self.assertEqual(["deleted"] * 15, [row["status"] for row in result["results"]])
        self.assertEqual(["DELETE:homeegrams/" + str(row["id"]) for row in selected],
                         [message for message in self.socket.messages if message.startswith("DELETE:")])
        self.assertNotIn("DELETE:homeegrams/507", self.socket.messages)
        self.assertEqual({row["id"] for row in selected}, set(self.socket.backups_at_send))
        self.assertEqual([507], [row["id"] for row in self.socket.remote])
        remaining = self.socket.remote[0]
        self.assertEqual({key: value for key, value in outside.items() if key != "order"},
                         {key: value for key, value in remaining.items() if key != "order"})
        self.assertEqual(0, remaining["order"])

        messages = list(self.socket.messages)
        replay = self.delete_mcp()
        self.assertEqual("completed", replay["status"])
        self.assertEqual(15, replay["deleted_count"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(messages, self.socket.messages)

    def test_lost_second_confirmation_retry_only_confirms_then_finishes_remainder(self):
        self.socket.mutation = "reorder"
        self.socket.metadata_only_on_delete = True
        self.socket.drop_delete_replies_once.add(850)
        request_id = self.arguments["request_id"]

        first = self.delete_mcp()
        self.assertEqual("partial", first["status"])
        self.assertEqual(1, first["deleted_count"])
        self.assertEqual(["deleted", "uncertain", "not_attempted"],
                         [row["status"] for row in first["results"]])
        self.assertEqual(["DELETE:homeegrams/849", "DELETE:homeegrams/850"],
                         [message for message in self.socket.messages if message.startswith("DELETE:")])
        self.assertEqual([851], [row["id"] for row in self.socket.remote])
        first_children = [row["request_id"] for row in first["results"]]

        replay = self.delete_mcp()
        self.assertEqual(request_id, self.arguments["request_id"])
        self.assert_completed(replay)
        self.assertTrue(replay["replayed"])
        self.assertEqual(first_children, [row["request_id"] for row in replay["results"]])
        self.assertTrue(replay["results"][1]["replayed"])
        self.assertEqual(1, self.socket.messages.count("DELETE:homeegrams/850"))


if __name__ == "__main__":
    unittest.main()
