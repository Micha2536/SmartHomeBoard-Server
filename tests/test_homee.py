import asyncio
import importlib.util
import json
import unittest
import uuid
from unittest.mock import AsyncMock, patch
from pathlib import Path
from urllib.parse import parse_qs


MODULE_PATH = Path(__file__).parents[1] / "modules" / "homee" / "module.py"
SPEC = importlib.util.spec_from_file_location("homee_module", MODULE_PATH)
homee = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(homee)


class Context:
    def __init__(self):
        self.integration_id = "integration-test-1"
        self.published = []
        self.removed = []
        self.state = {}
        self.secrets = {}
        self.connections = {}

    def nodes(self):
        return []

    async def publish_node(self, node):
        self.published.append(node.copy())

    async def remove_node(self, node_id):
        self.removed.append(node_id)

    async def set_status(self, *_args):
        pass

    def report_connection(self, connection_id, **values):
        self.connections.setdefault(connection_id, {}).update(values)

    def load_state(self, default=None):
        return self.state or default

    def save_state(self, value):
        self.state = value

    def load_secret(self, name, default=""):
        return self.secrets.get(name, default)

    def save_secret(self, name, value):
        self.secrets[name] = value


class CreatingHomeeSocket:
    """An in-memory homee peer; never opens a socket or controls a device."""
    def __init__(self, adapter):
        self.adapter = adapter
        self.messages = []
        self.remote = [{"id": 7, "name": "Existing", "active": 1, "play": 0,
                        "triggers": {}, "conditions": {}, "actions": {}}]
        self.fail_after_element = None
        self.disconnect_after_create = False
        self.wrong_branch = False
        self.suppress_creation = False

    async def send(self, command):
        self.messages.append(command)
        if command == "GET:homeegrams":
            await self.adapter._handle_message(json.dumps({"homeegrams": self.remote}))
            return
        path, query = command.split("?", 1)
        values = {key: items[0] for key, items in parse_qs(query).items()}
        if path == "POST:homeegrams":
            if not self.suppress_creation:
                self.remote.append({"id": 8, "name": values["name"], "active": int(values["active"]), "play": 0,
                                    "triggers": {}, "conditions": {}, "actions": {}})
                await self.adapter._handle_message(json.dumps({"homeegram": self.remote[-1]}))
            if self.disconnect_after_create:
                self.adapter.socket = None
            return
        section = path.rsplit("/", 1)[-1]
        kind = "switch_triggers" if section == "triggers" and values["type"] == "3" else "attribute_" + section
        element = {key: float(value) if "." in value else int(value)
                   for key, value in values.items() if key not in {"type", "logicType"}}
        element.update(id=100 + sum(len(items) for fields in self.remote[-1].values() if isinstance(fields, dict) for items in fields.values()), homeegram_id=8)
        if self.wrong_branch and section == "actions":
            element["branch"] = 2
        self.remote[-1][section].setdefault(kind, []).append(element)
        if self.fail_after_element is not None:
            self.fail_after_element -= 1
            if self.fail_after_element == 0:
                raise ConnectionError("Response lost after accepted element")


def creation_payload(**overrides):
    return {"id": 0, "name": "Flur & Kerzen? #1", "active": 0, "play": 0,
            "triggers": {"switch_triggers": [{"id": -1}], "attribute_triggers": [
                {"id": -2, "node_id": 12, "attribute_id": 99, "operator": 3, "operand": 1, "value": 1}]},
            "conditions": {"attribute_conditions": [
                {"id": -3, "node_id": 12, "attribute_id": 100, "operator": 4, "operand": 1,
                 "value": 50, "branch": 0, "check_moment": 3}]},
            "actions": {"attribute_actions": [
                {"id": -4, "node_id": 13, "attribute_id": 101, "command": 1,
                 "value": 1, "branch": 1, "delay": 2.5, "source_attribute_id": 0}]}, **overrides}


class DeletingHomeeSocket:
    """A fake homee holding complete originals and snapshots of pre-delete backups."""
    def __init__(self, adapter):
        self.adapter = adapter
        self.messages = []
        self.remote = [{"id": 7, "name": "Flur%20%26%20Kerzen", "active": 1, "play": 0,
                        "image": "custom", "added": 123, "note": "Originalnotiz",
                        "triggers": {"switch_triggers": [{"id": 100, "homeegram_id": 7}]},
                        "conditions": {"attribute_conditions": [{"id": 101, "homeegram_id": 7, "branch": 1}]},
                        "actions": {"notification_actions": [{"id": 102, "message": "vollständiges Original", "user_ids": [5]}],
                                    "webhook_actions": [{"id": 103, "url": "https://example.invalid/private"}]}}]
        self.backups_at_delete = []
        self.keep_on_delete = False
        self.raise_after_delete = False
        self.raise_before_delete = False
        self.disconnect_after_delete = False
        self.after_snapshot = None
        self.delete_reply = None
        self.delete_reply_delay = 0
        self.respond_to_get_after_delete = True

    async def send(self, command):
        self.messages.append(command)
        if command == "GET:homeegrams":
            if self.backups_at_delete and not self.respond_to_get_after_delete:
                return
            await self.adapter._handle_message(json.dumps({"homeegrams": self.remote}))
            if self.after_snapshot:
                self.after_snapshot()
            return
        if not command.startswith("DELETE:homeegrams/"):
            raise AssertionError("Unerwarteter Befehl: " + command)
        self.backups_at_delete.append(json.loads(json.dumps(self.adapter.context.state["homeegram_deletions"])))
        if self.raise_before_delete:
            raise ConnectionError("Send failed; delivery uncertain")
        if not self.keep_on_delete:
            identifier = int(command.rsplit("/", 1)[-1])
            self.remote = [row for row in self.remote if row["id"] != identifier]
        if self.delete_reply:
            payload = ({"homeegrams": self.remote} if self.delete_reply == "full"
                       else {"homeegram": {"id": int(command.rsplit("/", 1)[-1]), "deleted": 1}})
            reply = json.dumps(payload)

            async def deliver_reply():
                if self.delete_reply_delay:
                    await asyncio.sleep(self.delete_reply_delay)
                await self.adapter._handle_message(reply)

            if self.delete_reply_delay:
                asyncio.create_task(deliver_reply())
            else:
                await deliver_reply()
        if self.disconnect_after_delete:
            self.adapter.socket = None
        if self.raise_after_delete:
            raise ConnectionError("Delete accepted but reply lost")


class HomeeModuleTests(unittest.TestCase):
    def test_manifest_exposes_required_connection_fields(self):
        fields = {item["key"] for item in homee.manifest()["fields"]}
        self.assertEqual({"host", "port", "username", "password"}, fields)

    def test_each_integration_gets_a_stable_separate_client_identity(self):
        first = homee._client_id("integration-a")
        self.assertEqual(first, homee._client_id("integration-a"))
        self.assertNotEqual(first, homee._client_id("integration-b"))
        self.assertTrue(first.startswith("shb-server-"))

    def test_health_check_uses_existing_socket(self):
        class Socket:
            def __init__(self):
                self.messages = []

            async def send(self, message):
                self.messages.append(message)

        adapter = homee.HomeeAdapter({}, Context())
        adapter.socket = Socket()
        asyncio.run(adapter.health_check())
        self.assertEqual(adapter.socket.messages, ["GET:nodes"])

    def test_parallel_connect_triggers_open_only_one_login(self):
        async def scenario():
            adapter = homee.HomeeAdapter({}, Context())
            attempts = []

            async def connect_once():
                attempts.append("login")
                await asyncio.sleep(0.01)
                adapter.socket = object()

            adapter._connect_once = connect_once
            results = await asyncio.gather(adapter._connect(), adapter._connect(), adapter._connect())
            self.assertEqual(attempts, ["login"])
            self.assertEqual(results, [True, False, False])

        asyncio.run(scenario())

    def test_new_login_is_allowed_only_after_socket_was_dropped(self):
        async def scenario():
            adapter = homee.HomeeAdapter({}, Context())
            attempts = []

            async def connect_once():
                attempts.append("login")
                adapter.socket = Socket()

            class Socket:
                async def close(self):
                    pass

            adapter._connect_once = connect_once
            self.assertTrue(await adapter._connect())
            self.assertFalse(await adapter._connect())
            await adapter._drop_socket()
            self.assertTrue(await adapter._connect())
            self.assertEqual(attempts, ["login", "login"])

        asyncio.run(scenario())

    def test_get_all_is_rate_limited_after_initial_snapshot(self):
        async def scenario():
            class Socket:
                def __init__(self):
                    self.messages = []

                async def send(self, message):
                    self.messages.append(message)

            adapter = homee.HomeeAdapter({}, Context())
            adapter.socket = Socket()
            self.assertTrue(await adapter._request_all(force=True))
            self.assertFalse(await adapter._request_all())
            self.assertEqual(adapter.socket.messages, ["GET:all"])

        asyncio.run(scenario())

    def test_socket_accepts_large_get_all_response(self):
        async def scenario():
            class Socket:
                def __init__(self):
                    self.messages = []

                async def send(self, message):
                    self.messages.append(message)

            socket = Socket()
            adapter = homee.HomeeAdapter({}, Context())
            with patch.object(homee.websockets, "connect", AsyncMock(return_value=socket)) as connect:
                await adapter._open_socket("192.0.2.1", 7681, "token")
            self.assertEqual(connect.await_args.kwargs["max_size"], 32 * 1024 * 1024)
            self.assertEqual(socket.messages, ["GET:all", "GET:groups", "GET:relationships"])

        asyncio.run(scenario())

    def test_group_memberships_are_requested_once_even_during_get_all_protection(self):
        async def scenario():
            class Socket:
                def __init__(self):
                    self.messages = []

                async def send(self, message):
                    self.messages.append(message)

            first, second = Socket(), Socket()
            adapter = homee.HomeeAdapter({}, Context())
            with patch.object(homee.websockets, "connect", AsyncMock(side_effect=[first, second])):
                await adapter._open_socket("192.0.2.1", 7681, "token")
                await adapter._open_socket("192.0.2.1", 7681, "token")
            self.assertEqual(first.messages, ["GET:all", "GET:groups", "GET:relationships"])
            self.assertEqual(second.messages, ["GET:nodes", "GET:groups", "GET:relationships"])
            await adapter._handle_message('{"relationship":{"id":1,"group_id":3,"node_id":12}}')
            await adapter._handle_message('{"group":{"id":3,"name":"Wohnzimmer"}}')
            await adapter.health_check()
            self.assertEqual(second.messages,
                             ["GET:nodes", "GET:groups", "GET:relationships", "GET:nodes"])
            # Live events and health checks must not start group polling.
            self.assertEqual(1, second.messages.count("GET:groups"))
            self.assertEqual(1, second.messages.count("GET:relationships"))

        asyncio.run(scenario())

    def test_explicit_get_all_refreshes_groups_but_rate_limit_and_recovery_do_not_poll(self):
        async def scenario():
            class Socket:
                def __init__(self):
                    self.messages = []

                async def send(self, message):
                    self.messages.append(message)

            adapter = homee.HomeeAdapter({}, Context())
            adapter.socket = Socket()
            result = await adapter.action("send_websocket", {"command": "GET:all"})
            self.assertEqual({"sent": "GET:all"}, result)
            self.assertEqual(adapter.socket.messages, ["GET:all", "GET:groups", "GET:relationships"])
            with self.assertRaises(ValueError):
                await adapter.action("send_websocket", {"command": "GET:all"})
            self.assertEqual(adapter.socket.messages, ["GET:all", "GET:groups", "GET:relationships"])
            adapter.last_all_request_at = 0
            await adapter._merge_attribute({"id": 99, "node_id": 12, "current_value": 1})
            self.assertEqual(adapter.socket.messages,
                             ["GET:all", "GET:groups", "GET:relationships", "GET:all"])

        asyncio.run(scenario())

    def test_message_processing_error_does_not_trigger_reconnect(self):
        async def scenario():
            class Socket:
                def __init__(self):
                    self.messages = ["bad", "good"]

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    if not self.messages:
                        raise StopAsyncIteration
                    return self.messages.pop(0)

            adapter = homee.HomeeAdapter({}, Context())
            adapter.socket = Socket()
            handled = []
            reconnects = []

            async def handle(message):
                if message == "bad":
                    raise ValueError("defekte Nutzlast")
                handled.append(message)
                adapter.stopping = True

            async def reconnect():
                reconnects.append(True)

            adapter._handle_message = handle
            adapter._connect = reconnect
            await adapter._receive_forever()
            self.assertEqual(handled, ["good"])
            self.assertEqual(reconnects, [])

        asyncio.run(scenario())

    def test_manual_command_and_filtered_protocol_log(self):
        async def scenario():
            class Socket:
                def __init__(self):
                    self.messages = []

                async def send(self, message):
                    self.messages.append(message)

            adapter = homee.HomeeAdapter({}, Context())
            adapter.socket = Socket()
            await adapter.action("send_websocket", {"command": "GET:nodes"})
            await adapter._handle_message('{"attribute":{"id":1,"node_id":2,"current_value":3}}')
            result = await adapter.action("protocol_log", {"category": "attribute", "limit": 10})
            self.assertEqual(adapter.socket.messages[0], "GET:nodes")
            self.assertEqual(len(result["messages"]), 1)
            self.assertEqual(result["messages"][0]["category"], "attribute")

        asyncio.run(scenario())

    def test_history_uses_existing_socket_and_returns_matching_response(self):
        async def scenario():
            class Socket:
                def __init__(self):
                    self.messages = []

                async def send(self, message):
                    self.messages.append(message)

            adapter = homee.HomeeAdapter({}, Context())
            adapter.nodes = {
                12: {"id": 12, "attributes": [{"id": 99, "node_id": 12, "current_value": 21.5}]}
            }
            adapter.socket = Socket()
            request = asyncio.create_task(adapter.attribute_history(12, 99, 1000, 2000))
            await asyncio.sleep(0)
            self.assertEqual(
                adapter.socket.messages,
                ["GET:nodes/12/attributes/99/history?from=1000&till=2000"],
            )
            await adapter._handle_message(
                '{"attribute_history":{"node_id":12,"attribute_id":99,"from":1000,"till":2000,'
                '"results":[{"series":[{"values":[[1000,20.5],[2000,21.5]]}]}]}}'
            )
            history = await request
            self.assertEqual(history["node_id"], 12)
            self.assertEqual(history["results"][0]["series"][0]["values"][-1], [2000, 21.5])

        asyncio.run(scenario())

    def test_protocol_categories_normalize_plural_and_payload_wrappers(self):
        self.assertEqual("node", homee._protocol_category({"nodes": []}))
        self.assertEqual("attribute", homee._protocol_category({"attributes": []}))
        self.assertEqual("homeegram", homee._protocol_category({"payload": {"homeegrams": []}}))
        self.assertEqual("warning", homee._protocol_category({"payload": {"warning": {"message": "Test"}}}))
        self.assertEqual("all", homee._protocol_category({"all": {"nodes": []}}))

    def test_prefixed_snapshot_is_parsed_and_persisted(self):
        context = Context()
        adapter = homee.HomeeAdapter({}, context)
        asyncio.run(adapter._handle_message('GET:nodes {"nodes":[{"id":12,"name":"Sensor","attributes":[{"id":99,"current_value":21.5}]}]}'))
        self.assertEqual(12, context.published[0]["id"])
        self.assertEqual(12, context.published[0]["attributes"][0]["node_id"])

    def test_attribute_update_merges_into_full_node(self):
        context = Context()
        adapter = homee.HomeeAdapter({}, context)
        adapter.nodes = {12: {"id": 12, "name": "Sensor", "attributes": [{"id": 99, "node_id": 12, "current_value": 20}]}}
        asyncio.run(adapter._handle_message('{"attribute":{"id":99,"node_id":12,"current_value":22}}'))
        self.assertEqual(22, context.published[-1]["attributes"][0]["current_value"])
        self.assertEqual("Sensor", context.published[-1]["name"])

    def test_get_all_resources_are_persisted_and_live_records_are_merged(self):
        context = Context()
        adapter = homee.HomeeAdapter({}, context)
        asyncio.run(adapter._handle_message('{"all":{"nodes":[{"id":12,"name":"Sensor","attributes":[{"id":99,"current_value":21.5}]}],"homeegrams":[{"id":7,"name":"Morgen"}],"groups":[{"id":3,"name":"Wohnzimmer"}]}}'))
        self.assertEqual(12, context.published[0]["id"])
        self.assertEqual(99, context.published[0]["attributes"][0]["id"])
        self.assertEqual(12, context.published[0]["attributes"][0]["node_id"])
        self.assertEqual("Morgen", context.state["resources"]["homeegrams"][0]["name"])
        self.assertEqual("Wohnzimmer", context.state["resources"]["groups"][0]["name"])
        asyncio.run(adapter._handle_message('{"homeegram":{"id":7,"name":"Guten Morgen","enabled":true}}'))
        stored = context.state["resources"]["homeegrams"]
        self.assertEqual(1, len(stored))
        self.assertEqual("Guten Morgen", stored[0]["name"])
        self.assertTrue(stored[0]["enabled"])

    def test_group_and_relationship_snapshots_replace_exactly_and_live_updates_merge(self):
        async def scenario():
            context = Context()
            adapter = homee.HomeeAdapter({}, context)
            groups = [
                {"id": 3, "name": "Wohnzimmer%20Licht", "image": "light", "order": 2},
                {"id": 4, "name": "Keller", "attributes": [{"type": 1, "value": 1}]},
            ]
            relationships = [
                {"id": 68, "group_id": 3, "node_id": 12, "homeegram_id": 0, "order": 0},
                {"id": 69, "group_id": 3, "node_id": 13, "homeegram_id": 0, "order": 1},
            ]
            await adapter._handle_message(json.dumps({"groups": groups}))
            await adapter._handle_message(json.dumps({"relationships": relationships}))
            self.assertEqual(groups, context.state["resources"]["groups"])
            self.assertEqual(relationships, context.state["resources"]["relationships"])
            await adapter._handle_message('{"group":{"id":3,"name":"Neuer%20Name"}}')
            await adapter._handle_message('{"relationship":{"id":68,"order":4}}')
            stored_groups = context.state["resources"]["groups"]
            stored_relationships = context.state["resources"]["relationships"]
            self.assertEqual([3, 4], [row["id"] for row in stored_groups])
            self.assertEqual({**groups[0], "name": "Neuer%20Name"}, stored_groups[0])
            self.assertEqual([68, 69], [row["id"] for row in stored_relationships])
            self.assertEqual({**relationships[0], "order": 4}, stored_relationships[0])
            # A complete list after deletion must remove stale memberships.
            await adapter._handle_message(json.dumps({"relationships": [relationships[1]]}))
            self.assertEqual([relationships[1]], context.state["resources"]["relationships"])
            await adapter._handle_message('{"groups":[],"relationships":[]}')
            self.assertEqual([], context.state["resources"]["groups"])
            self.assertEqual([], context.state["resources"]["relationships"])
            restored = homee.HomeeAdapter({}, context)
            self.assertEqual([], restored.resources["groups"])
            self.assertEqual([], restored.resources["relationships"])

        asyncio.run(scenario())

    def test_disabled_homeegram_creation_preserves_types_paths_and_encoding(self):
        async def scenario():
            adapter = homee.HomeeAdapter({}, Context())
            adapter.socket = CreatingHomeeSocket(adapter)
            result = await adapter.create_homeegram_disabled(creation_payload(), "request-1")
            self.assertEqual(8, result["homeegram_id"])
            self.assertTrue(result["created"])
            self.assertEqual("created_disabled", result["status"])
            actual = result["homeegram"]
            self.assertEqual(0, actual["active"])
            self.assertEqual(0, actual["play"])
            self.assertEqual("Flur & Kerzen? #1", actual["name"])
            self.assertEqual(0, actual["conditions"]["attribute_conditions"][0]["branch"])
            self.assertEqual(3, actual["conditions"]["attribute_conditions"][0]["check_moment"])
            self.assertEqual(1, actual["actions"]["attribute_actions"][0]["branch"])
            self.assertEqual(2.5, actual["actions"]["attribute_actions"][0]["delay"])
            commands = adapter.socket.messages
            self.assertEqual("GET:homeegrams", commands[0])
            self.assertIn("name=Flur%20%26%20Kerzen%3F%20%231", commands[1])
            self.assertTrue(any("logicType=0" in command for command in commands))
            self.assertTrue(any("logicType=1" in command for command in commands))
            self.assertEqual(1, adapter.socket.remote[0]["active"])
            self.assertTrue(all(command.startswith(("GET:", "POST:")) for command in commands))
            self.assertFalse(any("play=" in command or "active=1" in command for command in commands))
        asyncio.run(scenario())

    def test_creation_retry_and_restart_return_same_confirmed_disabled_homeegram(self):
        async def scenario():
            context = Context()
            adapter = homee.HomeeAdapter({}, context)
            socket = CreatingHomeeSocket(adapter)
            adapter.socket = socket
            first = await adapter.create_homeegram_disabled(creation_payload(), "repeat")
            second = await adapter.create_homeegram_disabled(creation_payload(), "repeat")
            self.assertFalse(second["created"])
            self.assertEqual(first["homeegram_id"], second["homeegram_id"])
            replacement = homee.HomeeAdapter({}, context)
            replacement.socket = socket
            socket.adapter = replacement
            third = await replacement.create_homeegram_disabled(creation_payload(), "repeat")
            self.assertFalse(third["created"])
            self.assertEqual(1, sum(message.startswith("POST:homeegrams?") for message in socket.messages))
            with self.assertRaises(ValueError):
                await replacement.create_homeegram_disabled(creation_payload(name="Changed"), "repeat")
        asyncio.run(scenario())

    def test_parallel_retries_create_exactly_one_homeegram(self):
        async def scenario():
            adapter = homee.HomeeAdapter({}, Context())
            adapter.socket = CreatingHomeeSocket(adapter)
            results = await asyncio.gather(*(adapter.create_homeegram_disabled(creation_payload(), "parallel") for _ in range(3)))
            self.assertEqual(1, sum(result["created"] for result in results))
            self.assertEqual(1, sum(message.startswith("POST:homeegrams?") for message in adapter.socket.messages))
        asyncio.run(scenario())

    def test_partial_creation_is_retained_and_retry_never_reposts_elements(self):
        async def scenario():
            context = Context()
            adapter = homee.HomeeAdapter({}, context)
            socket = CreatingHomeeSocket(adapter)
            socket.fail_after_element = 2
            adapter.socket = socket
            with self.assertRaises(ConnectionError):
                await adapter.create_homeegram_disabled(creation_payload(), "partial")
            self.assertEqual(8, context.state["homeegram_creations"]["partial"]["homeegram_id"])
            self.assertEqual(0, socket.remote[-1]["active"])
            before = len([command for command in socket.messages if command.startswith("POST:")])
            replacement = homee.HomeeAdapter({}, context)
            replacement.socket = socket
            socket.adapter = replacement
            with self.assertRaisesRegex(ValueError, "keine zweite Kopie"):
                await replacement.create_homeegram_disabled(creation_payload(), "partial")
            self.assertEqual(before, len([command for command in socket.messages if command.startswith("POST:")]))
        asyncio.run(scenario())

    def test_lost_final_acknowledgement_can_be_confirmed_without_reposting(self):
        async def scenario():
            adapter = homee.HomeeAdapter({}, Context())
            socket = CreatingHomeeSocket(adapter)
            socket.fail_after_element = 4
            # A pre-existing HG with the same name must never be selected.
            socket.remote[0]["name"] = creation_payload()["name"]
            adapter.socket = socket
            with self.assertRaises(ConnectionError):
                await adapter.create_homeegram_disabled(creation_payload(), "lost-ack")
            count = len([command for command in socket.messages if command.startswith("POST:")])
            result = await adapter.create_homeegram_disabled(creation_payload(), "lost-ack")
            self.assertEqual(8, result["homeegram_id"])
            self.assertFalse(result["created"])
            self.assertEqual(count, len([command for command in socket.messages if command.startswith("POST:")]))
        asyncio.run(scenario())

    def test_disconnect_after_create_never_writes_into_replacement_connection(self):
        async def scenario():
            adapter = homee.HomeeAdapter({}, Context())
            socket = CreatingHomeeSocket(adapter)
            socket.disconnect_after_create = True
            adapter.socket = socket
            with self.assertRaises(ConnectionError):
                await adapter.create_homeegram_disabled(creation_payload(), "disconnected")
            self.assertEqual(2, len(socket.messages))
            self.assertEqual(0, socket.remote[-1]["active"])
        asyncio.run(scenario())

    def test_unconfirmed_create_is_recorded_and_not_blindly_repeated(self):
        async def scenario():
            adapter = homee.HomeeAdapter({}, Context())
            adapter.homeegram_wait_seconds = 0.01
            socket = CreatingHomeeSocket(adapter)
            socket.suppress_creation = True
            adapter.socket = socket
            with self.assertRaises(TimeoutError):
                await adapter.create_homeegram_disabled(creation_payload(), "uncertain")
            with self.assertRaises(ValueError):
                await adapter.create_homeegram_disabled(creation_payload(), "uncertain")
            self.assertEqual(1, sum(message.startswith("POST:homeegrams?") for message in socket.messages))
        asyncio.run(scenario())

    def test_confirmation_rejects_wrong_paths_and_keeps_partial_homeegram_disabled(self):
        async def scenario():
            adapter = homee.HomeeAdapter({}, Context())
            adapter.homeegram_wait_seconds = 0.01
            socket = CreatingHomeeSocket(adapter)
            socket.wrong_branch = True
            adapter.socket = socket
            with self.assertRaises(TimeoutError):
                await adapter.create_homeegram_disabled(creation_payload(), "wrong-path")
            self.assertEqual(0, socket.remote[-1]["active"])
            self.assertFalse(any("active=1" in command for command in socket.messages))
        asyncio.run(scenario())

    def test_invalid_or_unsupported_create_payload_never_sends_homee_commands(self):
        async def scenario():
            adapter = homee.HomeeAdapter({}, Context())
            adapter.socket = CreatingHomeeSocket(adapter)
            for payload in (creation_payload(active=1), creation_payload(play=1), creation_payload(id=7),
                            creation_payload(unknown=True), creation_payload(triggers={"webhook_triggers": []}),
                            creation_payload(actions={}),
                            creation_payload(actions={"attribute_actions": [{"node_id": 12, "attribute_id": 99, "value": float("nan")}]})):
                with self.assertRaises(ValueError):
                    await adapter.create_homeegram_disabled(payload, "invalid")
            self.assertEqual([], adapter.socket.messages)
        asyncio.run(scenario())

    def test_toggle_creation_omits_target_value_and_never_reactivates_on_retry(self):
        async def scenario():
            adapter = homee.HomeeAdapter({}, Context())
            socket = CreatingHomeeSocket(adapter)
            adapter.socket = socket
            payload = creation_payload(actions={"attribute_actions": [
                {"node_id": 13, "attribute_id": 101, "command": 3, "value": 1, "branch": 0}]})
            await adapter.create_homeegram_disabled(payload, "toggle")
            action = next(command for command in socket.messages if command.startswith("POST:homeegrams/8/actions?"))
            self.assertIn("command=3", action)
            self.assertNotIn("value=", action)
            socket.remote[-1]["active"] = 1
            count = len([command for command in socket.messages if command.startswith("POST:")])
            with self.assertRaises(ValueError):
                await adapter.create_homeegram_disabled(payload, "toggle")
            self.assertEqual(1, socket.remote[-1]["active"])
            self.assertEqual(count, len([command for command in socket.messages if command.startswith("POST:")]))
        asyncio.run(scenario())

    def test_delete_backs_up_full_original_before_send_and_confirms_fresh_absence(self):
        async def scenario():
            context = Context()
            adapter = homee.HomeeAdapter({}, context)
            socket = DeletingHomeeSocket(adapter)
            adapter.socket = socket
            original = json.loads(json.dumps(socket.remote[0]))
            key = str(uuid.uuid4())
            authorizations = []

            def authorize():
                authorizations.append(True)
                return True

            result = await adapter.delete_homeegram_confirmed(7, "Flur & Kerzen", key, authorize=authorize)
            self.assertEqual(["GET:homeegrams", "DELETE:homeegrams/7", "GET:homeegrams"], socket.messages)
            self.assertEqual(2, len(authorizations))
            backup_at_send = socket.backups_at_delete[0][key]
            self.assertEqual(original, backup_at_send["backup"])
            self.assertEqual("delete_reserved", backup_at_send["status"])
            self.assertEqual("Flur & Kerzen", backup_at_send["name"])
            self.assertEqual(original, context.state["homeegram_deletions"][key]["backup"])
            self.assertEqual("deleted", context.state["homeegram_deletions"][key]["status"])
            self.assertEqual({"homeegram_id": 7, "name": "Flur & Kerzen", "deleted": True,
                              "status": "deleted", "backup_saved": True, "request_id": key, "replayed": False}, result)
            self.assertNotIn("backup", result)
            self.assertNotIn("private", str(result))
        asyncio.run(scenario())

    def test_delete_direct_full_list_confirms_without_additional_get(self):
        async def scenario():
            context = Context()
            adapter = homee.HomeeAdapter({}, context)
            socket = DeletingHomeeSocket(adapter)
            socket.delete_reply = "full"
            socket.respond_to_get_after_delete = False
            socket.remote.append({"id": 8, "name": "Anderes HG", "triggers": {}, "conditions": {}, "actions": {}})
            adapter.socket = socket
            adapter.homeegram_wait_seconds = 0.05
            key = str(uuid.uuid4())
            result = await adapter.delete_homeegram_confirmed(7, "Flur & Kerzen", key)
            self.assertTrue(result["deleted"])
            self.assertEqual(["GET:homeegrams", "DELETE:homeegrams/7"], socket.messages)
            self.assertEqual([8], [row["id"] for row in adapter.resources["homeegrams"]])
            self.assertEqual("deleted", context.state["homeegram_deletions"][key]["status"])
            self.assertIn("backup", context.state["homeegram_deletions"][key])
            replay = await adapter.delete_homeegram_confirmed(7, "Flur & Kerzen", key)
            self.assertTrue(replay["replayed"])
            self.assertEqual(1, socket.messages.count("DELETE:homeegrams/7"))
        asyncio.run(scenario())

    def test_delete_delayed_full_reply_confirms_while_fallback_get_is_unanswered(self):
        async def scenario():
            adapter = homee.HomeeAdapter({}, Context())
            socket = DeletingHomeeSocket(adapter)
            socket.delete_reply = "full"
            socket.delete_reply_delay = 0.01
            socket.respond_to_get_after_delete = False
            adapter.socket = socket
            adapter.homeegram_wait_seconds = 0.2
            key = str(uuid.uuid4())
            result = await adapter.delete_homeegram_confirmed(7, "Flur & Kerzen", key)
            self.assertTrue(result["deleted"])
            self.assertEqual(["GET:homeegrams", "DELETE:homeegrams/7", "GET:homeegrams"], socket.messages)
            self.assertEqual(2, adapter.homeegram_snapshot_revision)
            self.assertEqual("deleted", adapter.homeegram_deletions[key]["status"])
        asyncio.run(scenario())

    def test_delete_direct_reply_still_containing_target_does_not_confirm(self):
        async def scenario():
            adapter = homee.HomeeAdapter({}, Context())
            socket = DeletingHomeeSocket(adapter)
            socket.delete_reply = "full"
            socket.keep_on_delete = True
            socket.respond_to_get_after_delete = False
            adapter.socket = socket
            adapter.homeegram_wait_seconds = 0.01
            key = str(uuid.uuid4())
            with self.assertRaises(TimeoutError):
                await adapter.delete_homeegram_confirmed(7, "Flur & Kerzen", key)
            self.assertEqual([7], [row["id"] for row in adapter.resources["homeegrams"]])
            self.assertEqual("uncertain", adapter.homeegram_deletions[key]["status"])
            self.assertEqual(1, socket.messages.count("DELETE:homeegrams/7"))
        asyncio.run(scenario())

    def test_delete_missing_reply_remains_uncertain_and_retry_only_reads(self):
        async def scenario():
            adapter = homee.HomeeAdapter({}, Context())
            socket = DeletingHomeeSocket(adapter)
            socket.respond_to_get_after_delete = False
            adapter.socket = socket
            adapter.homeegram_wait_seconds = 0.01
            key = str(uuid.uuid4())
            with self.assertRaises(TimeoutError):
                await adapter.delete_homeegram_confirmed(7, "Flur & Kerzen", key)
            self.assertEqual("uncertain", adapter.homeegram_deletions[key]["status"])
            self.assertIn("backup", adapter.homeegram_deletions[key])
            self.assertEqual(1, adapter.homeegram_snapshot_revision)
            socket.respond_to_get_after_delete = True
            result = await adapter.delete_homeegram_confirmed(7, "Flur & Kerzen", key)
            self.assertTrue(result["deleted"])
            self.assertTrue(result["replayed"])
            self.assertEqual(1, socket.messages.count("DELETE:homeegrams/7"))
        asyncio.run(scenario())

    def test_delete_singular_reply_is_not_a_complete_list_confirmation(self):
        async def scenario():
            adapter = homee.HomeeAdapter({}, Context())
            socket = DeletingHomeeSocket(adapter)
            socket.delete_reply = "singular"
            socket.respond_to_get_after_delete = False
            adapter.socket = socket
            adapter.homeegram_wait_seconds = 0.01
            key = str(uuid.uuid4())
            with self.assertRaises(TimeoutError):
                await adapter.delete_homeegram_confirmed(7, "Flur & Kerzen", key)
            self.assertEqual(1, adapter.homeegram_snapshot_revision)
            self.assertEqual("uncertain", adapter.homeegram_deletions[key]["status"])
            self.assertIn("backup", adapter.homeegram_deletions[key])
            self.assertEqual(1, socket.messages.count("DELETE:homeegrams/7"))
        asyncio.run(scenario())

    def test_delete_direct_confirmation_does_not_hide_a_disconnected_session(self):
        async def scenario():
            adapter = homee.HomeeAdapter({}, Context())
            socket = DeletingHomeeSocket(adapter)
            socket.delete_reply = "full"
            socket.disconnect_after_delete = True
            adapter.socket = socket
            key = str(uuid.uuid4())
            with self.assertRaises(ConnectionError):
                await adapter.delete_homeegram_confirmed(7, "Flur & Kerzen", key)
            self.assertEqual([], adapter.resources["homeegrams"])
            self.assertEqual("uncertain", adapter.homeegram_deletions[key]["status"])
            self.assertIn("backup", adapter.homeegram_deletions[key])
            self.assertEqual(["GET:homeegrams", "DELETE:homeegrams/7"], socket.messages)
        asyncio.run(scenario())

    def test_delete_rejects_invalid_ids_keys_name_mismatch_and_missing_original(self):
        async def scenario():
            adapter = homee.HomeeAdapter({}, Context())
            socket = DeletingHomeeSocket(adapter)
            adapter.socket = socket
            for identifier, name, key in ((0, "Flur & Kerzen", str(uuid.uuid4())),
                                           (-1, "Flur & Kerzen", str(uuid.uuid4())),
                                           (True, "Flur & Kerzen", str(uuid.uuid4())),
                                           ("7", "Flur & Kerzen", str(uuid.uuid4())),
                                           (7, "", str(uuid.uuid4())),
                                           (7, "Flur & Kerzen", "invalid")):
                with self.assertRaises(ValueError):
                    await adapter.delete_homeegram_confirmed(identifier, name, key)
            self.assertEqual([], socket.messages)
            for identifier, name in ((7, "Flur & Kerzen "), (7, "anderes HG"), (99, "Flur & Kerzen")):
                with self.assertRaises(ValueError):
                    await adapter.delete_homeegram_confirmed(identifier, name, str(uuid.uuid4()))
            self.assertTrue(all(command == "GET:homeegrams" for command in socket.messages))
            self.assertEqual({}, adapter.homeegram_deletions)
        asyncio.run(scenario())

    def test_delete_uses_fresh_name_instead_of_stale_cache_and_requires_full_backup(self):
        async def scenario():
            adapter = homee.HomeeAdapter({}, Context())
            socket = DeletingHomeeSocket(adapter)
            adapter.socket = socket
            adapter.resources["homeegrams"] = json.loads(json.dumps(socket.remote))
            socket.remote[0]["name"] = "Umbenannt"
            with self.assertRaises(ValueError):
                await adapter.delete_homeegram_confirmed(7, "Flur & Kerzen", str(uuid.uuid4()))
            socket.remote[0].pop("actions")
            with self.assertRaisesRegex(ValueError, "vollständiges"):
                await adapter.delete_homeegram_confirmed(7, "Umbenannt", str(uuid.uuid4()))
            self.assertTrue(all(command == "GET:homeegrams" for command in socket.messages))
        asyncio.run(scenario())

    def test_delete_compares_once_decoded_name_and_preserves_literal_percent_sequences(self):
        async def scenario():
            adapter = homee.HomeeAdapter({}, Context())
            socket = DeletingHomeeSocket(adapter)
            socket.remote[0]["name"] = "Literal%2520Name"
            adapter.socket = socket
            for wrong_name in ("Literal Name", "Literal%2520Name"):
                with self.assertRaisesRegex(ValueError, "Name"):
                    await adapter.delete_homeegram_confirmed(7, wrong_name, str(uuid.uuid4()))
            self.assertNotIn("DELETE:homeegrams/7", socket.messages)
            result = await adapter.delete_homeegram_confirmed(7, "Literal%20Name", str(uuid.uuid4()))
            self.assertEqual("Literal%20Name", result["name"])
            self.assertTrue(result["deleted"])
        asyncio.run(scenario())

    def test_delete_repeat_survives_restart_and_never_deletes_reused_id(self):
        async def scenario():
            context = Context()
            adapter = homee.HomeeAdapter({}, context)
            socket = DeletingHomeeSocket(adapter)
            adapter.socket = socket
            key = str(uuid.uuid4())
            await adapter.delete_homeegram_confirmed(7, "Flur & Kerzen", key)
            socket.remote = [{"id": 7, "name": "Neues HG", "active": 1,
                              "triggers": {}, "conditions": {}, "actions": {}}]
            replacement = homee.HomeeAdapter({}, context)
            replacement.socket = socket
            socket.adapter = replacement
            replay = await replacement.delete_homeegram_confirmed(7, "Flur & Kerzen", key)
            self.assertTrue(replay["replayed"])
            self.assertEqual("Neues HG", socket.remote[0]["name"])
            self.assertEqual(1, socket.messages.count("DELETE:homeegrams/7"))
            with self.assertRaises(ValueError):
                await replacement.delete_homeegram_confirmed(7, "Neues HG", key)
        asyncio.run(scenario())

    def test_parallel_same_delete_request_sends_only_once(self):
        async def scenario():
            adapter = homee.HomeeAdapter({}, Context())
            socket = DeletingHomeeSocket(adapter)
            adapter.socket = socket
            key = str(uuid.uuid4())
            results = await asyncio.gather(*(adapter.delete_homeegram_confirmed(7, "Flur & Kerzen", key) for _ in range(3)))
            self.assertEqual(1, sum(not result["replayed"] for result in results))
            self.assertEqual(1, socket.messages.count("DELETE:homeegrams/7"))
        asyncio.run(scenario())

    def test_uncertain_delete_is_only_confirmed_by_reading_after_restart(self):
        async def scenario():
            context = Context()
            adapter = homee.HomeeAdapter({}, context)
            socket = DeletingHomeeSocket(adapter)
            socket.raise_after_delete = True
            adapter.socket = socket
            key = str(uuid.uuid4())
            with self.assertRaises(ConnectionError):
                await adapter.delete_homeegram_confirmed(7, "Flur & Kerzen", key)
            self.assertEqual("uncertain", context.state["homeegram_deletions"][key]["status"])
            replacement = homee.HomeeAdapter({}, context)
            replacement.socket = socket
            socket.adapter = replacement
            replay = await replacement.delete_homeegram_confirmed(7, "Flur & Kerzen", key)
            self.assertTrue(replay["deleted"])
            self.assertTrue(replay["replayed"])
            self.assertEqual(1, socket.messages.count("DELETE:homeegrams/7"))
        asyncio.run(scenario())

    def test_uncertain_delete_with_original_still_present_never_retries_delete(self):
        async def scenario():
            adapter = homee.HomeeAdapter({}, Context())
            socket = DeletingHomeeSocket(adapter)
            socket.raise_before_delete = True
            adapter.socket = socket
            key = str(uuid.uuid4())
            with self.assertRaises(ConnectionError):
                await adapter.delete_homeegram_confirmed(7, "Flur & Kerzen", key)
            socket.raise_before_delete = False
            with self.assertRaisesRegex(ValueError, "nicht erneut gelöscht"):
                await adapter.delete_homeegram_confirmed(7, "Flur & Kerzen", key)
            self.assertEqual(1, socket.messages.count("DELETE:homeegrams/7"))
            self.assertEqual(1, len(socket.remote))
        asyncio.run(scenario())

    def test_delete_timeout_preserves_backup_and_does_not_repeat_delete(self):
        async def scenario():
            adapter = homee.HomeeAdapter({}, Context())
            adapter.homeegram_wait_seconds = 0.01
            socket = DeletingHomeeSocket(adapter)
            socket.keep_on_delete = True
            adapter.socket = socket
            key = str(uuid.uuid4())
            with self.assertRaises(TimeoutError):
                await adapter.delete_homeegram_confirmed(7, "Flur & Kerzen", key)
            self.assertIn("backup", adapter.homeegram_deletions[key])
            with self.assertRaises(ValueError):
                await adapter.delete_homeegram_confirmed(7, "Flur & Kerzen", key)
            self.assertEqual(1, socket.messages.count("DELETE:homeegrams/7"))
        asyncio.run(scenario())

    def test_delete_checks_permission_after_lock_and_again_before_send(self):
        async def scenario():
            adapter = homee.HomeeAdapter({}, Context())
            socket = DeletingHomeeSocket(adapter)
            adapter.socket = socket
            with self.assertRaisesRegex(ValueError, "nicht freigegeben"):
                await adapter.delete_homeegram_confirmed(7, "Flur & Kerzen", str(uuid.uuid4()), authorize=lambda: False)
            self.assertEqual([], socket.messages)
            allowed = [True]
            socket.after_snapshot = lambda: allowed.__setitem__(0, False)
            with self.assertRaisesRegex(ValueError, "nicht freigegeben"):
                await adapter.delete_homeegram_confirmed(7, "Flur & Kerzen", str(uuid.uuid4()), authorize=lambda: allowed[0])
            self.assertEqual(["GET:homeegrams"], socket.messages)
        asyncio.run(scenario())

    def test_delete_checks_revoked_permission_after_waiting_for_lock(self):
        async def scenario():
            adapter = homee.HomeeAdapter({}, Context())
            socket = DeletingHomeeSocket(adapter)
            adapter.socket = socket
            adapter.homeegram_delete_lock = asyncio.Lock()
            await adapter.homeegram_delete_lock.acquire()
            allowed = [True]

            async def authorize():
                await asyncio.sleep(0)
                return allowed[0]

            pending = asyncio.create_task(adapter.delete_homeegram_confirmed(7, "Flur & Kerzen", str(uuid.uuid4()), authorize=authorize))
            await asyncio.sleep(0)
            allowed[0] = False
            adapter.homeegram_delete_lock.release()
            with self.assertRaisesRegex(ValueError, "nicht freigegeben"):
                await pending
            self.assertEqual([], socket.messages)
        asyncio.run(scenario())

    def test_delete_requires_persisted_backup_and_keeps_journal_bounded(self):
        async def scenario():
            context = Context()
            adapter = homee.HomeeAdapter({}, context)
            socket = DeletingHomeeSocket(adapter)
            adapter.socket = socket
            save = context.save_state

            def fail_backup(value):
                if value.get("homeegram_deletions"):
                    raise OSError("Backup volume unavailable")
                save(value)

            context.save_state = fail_backup
            with self.assertRaises(OSError):
                await adapter.delete_homeegram_confirmed(7, "Flur & Kerzen", str(uuid.uuid4()))
            self.assertEqual(["GET:homeegrams"], socket.messages)
            context.save_state = save
            adapter.homeegram_deletions = {str(index): {} for index in range(1000)}
            with self.assertRaisesRegex(ValueError, "1000"):
                await adapter.delete_homeegram_confirmed(7, "Flur & Kerzen", str(uuid.uuid4()))
            self.assertEqual(1000, len(adapter.homeegram_deletions))
            self.assertEqual(["GET:homeegrams"], socket.messages)
        asyncio.run(scenario())

    def test_delete_disconnect_after_send_preserves_backup_and_does_not_reconnect(self):
        async def scenario():
            context = Context()
            adapter = homee.HomeeAdapter({}, context)
            socket = DeletingHomeeSocket(adapter)
            socket.disconnect_after_delete = True
            adapter.socket = socket
            key = str(uuid.uuid4())
            with self.assertRaises(ConnectionError):
                await adapter.delete_homeegram_confirmed(7, "Flur & Kerzen", key)
            self.assertEqual(["GET:homeegrams", "DELETE:homeegrams/7"], socket.messages)
            self.assertEqual("uncertain", context.state["homeegram_deletions"][key]["status"])
            self.assertIn("backup", context.state["homeegram_deletions"][key])
            self.assertIsNone(adapter.socket)
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
