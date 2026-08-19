import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "modules" / "velux" / "module.py"
SPEC = importlib.util.spec_from_file_location("test_velux_module", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeContext:
    integration_name = "VELUX"

    def __init__(self):
        self.secrets = {}
        self.published = []

    def load_secret(self, name, default=""):
        return self.secrets.get(name, default)

    def save_secret(self, name, value):
        self.secrets[name] = value

    def stable_node_id(self, external_id):
        return 1_710_000_001

    @staticmethod
    def attribute_id(node_id, offset):
        return node_id * 100 + offset

    async def publish_node(self, node):
        self.published.append(node)


class VeluxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.context = FakeContext()
        self.adapter = MODULE.VeluxAdapter({"email": "test@example.com", "password": "secret"}, self.context)

    async def asyncTearDown(self):
        await self.adapter.client.aclose()

    def test_manifest_exposes_cloud_credentials_and_refresh(self):
        manifest = MODULE.manifest()
        self.assertEqual("velux", manifest["id"])
        self.assertEqual({"email", "password", "poll_seconds"}, {field["key"] for field in manifest["fields"]})
        self.assertEqual("refresh", manifest["actions"][0]["id"])

    async def test_changed_credentials_discard_old_tokens(self):
        context = FakeContext()
        context.secrets = {
            "credential_fingerprint": "old-account", "access_token": "OLD",
            "refresh_token": "REFRESH", "expires_at": 9_999_999_999,
        }
        adapter = MODULE.VeluxAdapter({"email": "new@example.com", "password": "new-secret"}, context)
        self.assertEqual("", adapter.access_token)
        self.assertEqual("", adapter.refresh_token)
        self.assertEqual(0, adapter.expires_at)
        self.assertNotEqual("old-account", context.secrets["credential_fingerprint"])
        await adapter.client.aclose()

    def test_response_variants_are_parsed(self):
        home = {"id": "home-1"}
        self.assertEqual(home, MODULE._first_home({"body": {"homes": [home]}}))
        module = {"module_id": "blind-1", "states": {"current_position": "72"}}
        self.assertEqual(module, MODULE._modules_by_id({"body": {"home": {"modules": [module]}}})["blind-1"])
        self.assertEqual(72, MODULE._position(module))

    async def test_published_position_is_reversed_like_ios_adapter(self):
        node_id = self.context.stable_node_id("blind-1")
        self.adapter.module_by_node[node_id] = "blind-1"
        self.adapter.names[node_id] = "Dachfenster"
        self.adapter.positions[node_id] = 28
        await self.adapter._publish(node_id)
        attributes = {item["name"]: item for item in self.context.published[-1]["attributes"]}
        self.assertEqual(28, attributes["Position"]["current_value"])
        self.assertEqual(135, attributes["Richtung"]["type"])

    async def test_direction_maps_to_open_close_and_stop_position(self):
        node_id = self.context.stable_node_id("blind-1")
        self.adapter.module_by_node[node_id] = "blind-1"
        self.adapter.names[node_id] = "Dachfenster"
        self.adapter.positions[node_id] = 42
        calls = []

        async def set_state(module_id, velux_position):
            calls.append((module_id, velux_position))

        self.adapter._set_state = set_state
        self.adapter._delayed_poll = lambda: _done()
        base = self.context.attribute_id(node_id, 0)
        await self.adapter.set_value(node_id, base + 2, 0)
        await self.adapter.set_value(node_id, base + 2, 1)
        self.adapter.positions[node_id] = 42
        await self.adapter.set_value(node_id, base + 2, 2)
        self.assertEqual([("blind-1", 100), ("blind-1", 0), ("blind-1", 58)], calls)


async def _done():
    return None


if __name__ == "__main__":
    unittest.main()
