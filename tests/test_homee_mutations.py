"""Native write tests use an in-memory homee only, never real devices."""
import asyncio
from copy import deepcopy
import json
import tempfile
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs
import uuid

from test_homee import homee, Context
from test_homeegram_editing import original
from server.homeegram_revision import homeegram_revision
from server.database import Database
from server.mcp_access import MCPAccess
from fastapi import FastAPI
from fastapi.testclient import TestClient


class MutatingSocket:
    def __init__(self, adapter):
        self.adapter = adapter
        self.remote = original()
        self.messages = []
        self.backups = []
        self.fail_after_write = False
        self.refuse_write = False
        self.only_preflight_get = False
        self.reorder = False

    async def reply(self):
        value = deepcopy(self.remote)
        if self.reorder:
            for section in ("triggers", "conditions", "actions"):
                for values in value[section].values():
                    values.reverse()
        await self.adapter._handle_message(json.dumps({"homeegrams": [value]}))

    async def send(self, command):
        self.messages.append(command)
        if command == "GET:homeegrams":
            if not self.only_preflight_get or not self.backups:
                await self.reply()
            return
        state = deepcopy(self.adapter.context.state)
        self.backups.append(state["homeegram_mutations"])
        if self.refuse_write:
            return
        path, _, query = command.partition("?")
        vals = {key: self.scalar(values[0]) for key, values in parse_qs(query, keep_blank_values=True).items()}
        if path == "PUT:homeegrams/877":
            self.remote["active"] = vals["active"]
        else:
            components = path.split("/")
            verb = components[0].split(":")[0]
            section = components[2]
            if verb == "POST":
                kind = next(k for k, code in homee._HG_NATIVE_TYPES[section].items() if code == vals["type"])
                vals.pop("type")
                vals.pop("logicType", None)
                vals.update(id=100 + len(self.backups), homeegram_id=877)
                self.remote[section].setdefault(kind, []).append(vals)
            else:
                identifier = int(components[3])
                for values in self.remote[section].values():
                    found = next((row for row in values if row["id"] == identifier), None)
                    if found:
                        if verb == "DELETE":
                            values.remove(found)
                        else:
                            found.update(vals)
                        break
        if self.fail_after_write:
            raise ConnectionError("Accepted native write, response lost")
        await self.reply()

    @staticmethod
    def scalar(value):
        try:
            return float(value) if "." in value else int(value)
        except ValueError:
            return value


class NativeMutationTransportTests(unittest.TestCase):
    def setup_adapter(self):
        context = Context()
        adapter = homee.HomeeAdapter({}, context)
        adapter.homeegram_wait_seconds = 0.03
        adapter.socket = MutatingSocket(adapter)
        return adapter, context, adapter.socket

    @staticmethod
    def changes():
        return [{"operation": "update", "section": "conditions", "kind": "attribute_conditions",
                 "element_id": 2, "parameters": {"value": 1}},
                {"operation": "add", "section": "actions", "kind": "attribute_actions",
                 "parameters": {"node_id": 12, "attribute_id": 55, "source_attribute_id": 0,
                                "command": 1, "value": 0, "branch": 2, "delay": 5}}]

    def test_active_backs_up_before_send_confirms_direct_reply_and_never_plays(self):
        async def run():
            adapter, context, socket = self.setup_adapter()
            socket.only_preflight_get = True
            result = await adapter.set_homeegram_active_confirmed(877, "Test HG", False, str(uuid.uuid4()))
            self.assertEqual(0, result["active"])
            self.assertEqual(["GET:homeegrams", "PUT:homeegrams/877?active=0"], socket.messages)
            entry = next(iter(socket.backups[0].values()))
            self.assertEqual(original(), entry["backup"])
            self.assertEqual(1, entry["commands_reserved"])
            self.assertNotIn("secret", str(result))
            self.assertFalse(any("play=" in command for command in socket.messages))
        asyncio.run(run())

    def test_edit_preserves_unknown_categories_and_if_branch_stays_disabled(self):
        async def run():
            adapter, context, socket = self.setup_adapter()
            socket.only_preflight_get = True
            socket.reorder = True
            result = await adapter.edit_homeegram_confirmed(877, "Test HG", self.changes(), str(uuid.uuid4()),
                expected_revision=homeegram_revision(original()))
            self.assertEqual("edited_disabled", result["status"])
            self.assertEqual(0, socket.remote["active"])
            condition = socket.remote["conditions"]["attribute_conditions"][0]
            self.assertEqual((1, 3, 1), (condition["branch"], condition["check_moment"], condition["value"]))
            self.assertEqual(original()["actions"]["webhook_actions"], socket.remote["actions"]["webhook_actions"])
            self.assertTrue(socket.messages[1].endswith("?active=0"))
            self.assertIn("branch=1", socket.messages[2])
            self.assertIn("check_moment=3", socket.messages[2])
            self.assertIn("branch=2", socket.messages[3])
            self.assertEqual(1, socket.messages.count("GET:homeegrams"))
            self.assertFalse(any("play=" in c or "active=1" in c for c in socket.messages))
        asyncio.run(run())

    def test_uncertain_request_survives_restart_and_never_resends(self):
        async def run():
            adapter, context, socket = self.setup_adapter()
            key = str(uuid.uuid4())
            socket.fail_after_write = True
            with self.assertRaises(ConnectionError):
                await adapter.set_homeegram_active_confirmed(877, "Test HG", False, key)
            self.assertEqual("uncertain_or_partial", context.state["homeegram_mutations"][key]["status"])
            restarted = homee.HomeeAdapter({}, context)
            restarted.socket = MutatingSocket(restarted)
            restarted.socket.remote = deepcopy(socket.remote)
            restarted.homeegram_wait_seconds = 0.03
            replay = await restarted.set_homeegram_active_confirmed(877, "Test HG", False, key)
            self.assertTrue(replay["replayed"])
            self.assertEqual(["GET:homeegrams"], restarted.socket.messages)
            with self.assertRaises(ValueError):
                await restarted.set_homeegram_active_confirmed(877, "Test HG", True, key)
            self.assertEqual(["GET:homeegrams"], restarted.socket.messages)
        asyncio.run(run())

    def test_partial_edit_retry_only_checks_and_refuses_second_send(self):
        async def run():
            adapter, context, socket = self.setup_adapter()
            key = str(uuid.uuid4())
            socket.fail_after_write = True
            revision = homeegram_revision(original())
            with self.assertRaises(ConnectionError):
                await adapter.edit_homeegram_confirmed(877, "Test HG", self.changes(), key, expected_revision=revision)
            socket.fail_after_write = False
            count = len(socket.backups)
            with self.assertRaises(ValueError):
                await adapter.edit_homeegram_confirmed(877, "Test HG", self.changes(), key, expected_revision=revision)
            self.assertEqual(count, len(socket.backups))
            self.assertEqual(0, socket.remote["active"])
            self.assertEqual(0, socket.remote["conditions"]["attribute_conditions"][0]["value"])
        asyncio.run(run())

    def test_stale_revision_name_permission_or_full_journal_never_write(self):
        async def run():
            for mode in ("revision", "name", "permission", "full"):
                adapter, context, socket = self.setup_adapter()
                if mode == "full":
                    adapter.homeegram_mutations = {str(i): {} for i in range(1000)}
                with self.assertRaises(ValueError):
                    await adapter.edit_homeegram_confirmed(877, "Wrong" if mode == "name" else "Test HG", self.changes(), str(uuid.uuid4()),
                        expected_revision="0" * 64 if mode == "revision" else homeegram_revision(original()),
                        authorize=(lambda: False) if mode == "permission" else None)
                self.assertFalse(socket.backups, mode)
        asyncio.run(run())

    def test_backup_persistence_failure_prevents_every_write(self):
        async def run():
            adapter, context, socket = self.setup_adapter()
            save = context.save_state
            def failure(value):
                if value.get("homeegram_mutations"):
                    raise OSError("Disk full")
                save(value)
            context.save_state = failure
            with self.assertRaises(OSError):
                await adapter.set_homeegram_active_confirmed(877, "Test HG", False, str(uuid.uuid4()))
            self.assertFalse(socket.backups)
        asyncio.run(run())

    def test_failed_disable_confirmation_prevents_element_writes(self):
        async def run():
            adapter, _, socket = self.setup_adapter()
            socket.refuse_write = True
            with self.assertRaises(TimeoutError):
                await adapter.edit_homeegram_confirmed(877, "Test HG", self.changes(), str(uuid.uuid4()))
            writes = [c for c in socket.messages if not c.startswith("GET:")]
            self.assertEqual(["PUT:homeegrams/877?active=0"], writes)
        asyncio.run(run())

    def test_new_condition_requires_explicit_branch_at_transport(self):
        async def run():
            adapter, _, socket = self.setup_adapter()
            changes = [{"operation": "add", "section": "conditions", "kind": "attribute_conditions",
                        "parameters": {"node_id": 12, "attribute_id": 55, "value": 1}}]
            with self.assertRaises(ValueError):
                await adapter.edit_homeegram_confirmed(877, "Test HG", changes, str(uuid.uuid4()))
            self.assertFalse(socket.backups)
        asyncio.run(run())


class MCPNativeMutationTests(unittest.TestCase):
    def test_mcp_real_service_and_adapter_permission_paths_revision_and_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Database(folder)
            try:
                database.save_integration({"id": "home", "module_id": "homee", "name": "homee", "enabled": True, "configuration": {}})
                adapter = homee.HomeeAdapter({}, Context())
                adapter.homeegram_wait_seconds = 0.03
                adapter.resources["homeegrams"] = [original()]
                adapter.nodes = {12: {"id": 12, "attributes": [{"id": 55, "type": 1, "editable": True, "minimum": 0, "maximum": 1}]}}
                socket = MutatingSocket(adapter)
                adapter.socket = socket
                runtime = SimpleNamespace(adapters={"home": adapter}, visible_nodes=lambda: [])
                access = MCPAccess(database, runtime, lambda: "test-admin")

                @asynccontextmanager
                async def lifespan(app):
                    async with access.server.session_manager.run():
                        yield

                app = FastAPI(lifespan=lifespan)
                app.include_router(access.router)
                app.mount("/mcp", access.http_app)
                admin = {"Authorization": "Bearer test-admin"}
                with TestClient(app) as client:
                    client.put("/api/v1/mcp", headers=admin, json={"enabled": True})
                    token = client.post("/api/v1/mcp/rotate-token", headers=admin).json()["token"]
                    headers = {"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"}

                    def call(name, arguments):
                        response = client.post("/mcp/", headers=headers, json={"jsonrpc": "2.0", "id": 1,
                            "method": "tools/call", "params": {"name": name, "arguments": arguments}})
                        self.assertEqual(200, response.status_code, response.text)
                        return response.json()["result"]

                    def data(result):
                        self.assertFalse(result.get("isError", False), result)
                        return result.get("structuredContent") or json.loads(result["content"][0]["text"])

                    activate = {"integration_id": "home", "homeegram_id": 877,
                                "confirmation_name": "Test HG", "active": False, "request_id": str(uuid.uuid4())}
                    self.assertTrue(call("set_homeegram_active", activate)["isError"])
                    self.assertFalse(socket.messages)
                    client.put("/api/v1/mcp", headers=admin, json={"enabled": True, "allow_homee_activation": True})
                    self.assertEqual(0, data(call("set_homeegram_active", activate))["active"])
                    read = data(call("get_homeegram", {"integration_id": "home", "homeegram_id": 877}))
                    self.assertEqual(homeegram_revision(socket.remote), read["revision"])
                    edit = {"integration_id": "home", "homeegram_id": 877, "confirmation_name": "Test HG",
                            "expected_revision": read["revision"], "changes": NativeMutationTransportTests.changes(),
                            "request_id": str(uuid.uuid4())}
                    self.assertTrue(call("edit_homeegram", edit)["isError"])
                    client.put("/api/v1/mcp", headers=admin,
                        json={"enabled": True, "allow_homee_activation": True, "allow_homee_editing": True})
                    accepted = data(call("edit_homeegram", edit))
                    self.assertEqual("edited_disabled", accepted["status"])
                    self.assertEqual(0, accepted["active"])
                    self.assertNotIn("secret", str(accepted))
                    self.assertTrue(data(call("edit_homeegram", edit))["replayed"])
                    self.assertEqual(1, sum(c.startswith("POST:") for c in socket.messages))
                    self.assertFalse(any("play=" in c or "active=1" in c for c in socket.messages))
            finally:
                database._connection.close()


if __name__ == "__main__":
    unittest.main()
