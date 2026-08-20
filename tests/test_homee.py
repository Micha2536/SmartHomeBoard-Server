import asyncio
import importlib.util
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path


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

    def nodes(self):
        return []

    async def publish_node(self, node):
        self.published.append(node.copy())

    async def remove_node(self, node_id):
        self.removed.append(node_id)

    async def set_status(self, *_args):
        pass

    def load_state(self, default=None):
        return self.state or default

    def save_state(self, value):
        self.state = value

    def load_secret(self, name, default=""):
        return self.secrets.get(name, default)

    def save_secret(self, name, value):
        self.secrets[name] = value


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
            self.assertEqual(socket.messages, ["GET:all"])

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


if __name__ == "__main__":
    unittest.main()
