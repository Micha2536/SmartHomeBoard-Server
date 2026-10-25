import importlib.util
import sys
import types
import unittest
from pathlib import Path


class FakeAsyncClient:
    def __init__(self, **_kwargs): pass
    async def aclose(self): pass


sys.modules.setdefault("httpx", types.SimpleNamespace(
    AsyncClient=FakeAsyncClient,
    HTTPStatusError=type("HTTPStatusError", (Exception,), {}),
    Timeout=lambda *_args, **_kwargs: None,
))


MODULE_PATH = Path(__file__).parents[1] / "modules" / "home_connect" / "module.py"
SPEC = importlib.util.spec_from_file_location("test_home_connect_module", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeContext:
    integration_id = "home-connect-1"
    integration_name = "Home Connect"

    def __init__(self):
        self.state = {}
        self.secrets = {}
        self.published = []
        self.removed = []
        self.connections = {}

    def load_state(self, default=None): return self.state or default
    def save_state(self, value): self.state = value
    def load_secret(self, name, default=""): return self.secrets.get(name, default)
    def save_secret(self, name, value): self.secrets[name] = value
    def clear_configuration_value(self, _key): pass
    def stable_node_id(self, external): return 1_750_000_000 + sum(external.encode()) % 1_000_000
    @staticmethod
    def attribute_id(node_id, offset): return node_id * 100 + offset
    async def publish_node(self, node): self.published.append(node)
    async def remove_node(self, node_id): self.removed.append(node_id)
    async def set_status(self, *_args): pass
    def nodes(self): return list(self.published)
    def report_connection(self, connection_id, **values): self.connections.setdefault(connection_id, {}).update(values)
    def remove_connection(self, connection_id): self.connections.pop(connection_id, None)


class HomeConnectTests(unittest.IsolatedAsyncioTestCase):
    def test_manifest_uses_device_flow_credentials_and_monitoring_actions(self):
        manifest = MODULE.manifest()
        self.assertEqual("home_connect", manifest["id"])
        self.assertEqual({"client_id", "client_secret", "region"}, {field["key"] for field in manifest["fields"]})
        actions = {action["id"] for action in manifest["actions"]}
        self.assertEqual({"start_pairing", "check_pairing", "refresh", "logout"}, actions)

    def test_home_connect_values_are_translated_for_shb(self):
        self.assertEqual((1, "Offen"), MODULE._attribute_value("BSH.Common.Status.DoorState", "BSH.Common.EnumType.DoorState.Open"))
        self.assertEqual((0, "Geschlossen"), MODULE._attribute_value("BSH.Common.Status.DoorState", "BSH.Common.EnumType.DoorState.Closed"))
        self.assertEqual((1, None), MODULE._attribute_value("BSH.Common.Appliance.Connected", True))
        self.assertEqual("Programmfortschritt", MODULE._key_name("BSH.Common.Option.ProgramProgress"))

    def test_home_connect_error_explains_device_flow_configuration(self):
        response = types.SimpleNamespace(
            json=lambda: {"error": "unauthorized_client", "error_description": "Client is not allowed"},
            status_code=400,
        )
        message = MODULE._api_error(response, "Kopplung fehlgeschlagen")
        self.assertIn("unauthorized_client", message)
        self.assertIn("Device Flow", message)

    async def test_appliance_is_published_as_single_server_node(self):
        context = FakeContext()
        adapter = MODULE.HomeConnectAdapter({"client_id": "x", "region": "eu"}, context)
        appliance_id = "BOSCH-DISHWASHER-1"
        adapter.appliances[appliance_id] = {
            "haId": appliance_id, "name": "Geschirrspüler", "brand": "Bosch",
            "type": "Dishwasher", "vib": "SMV", "connected": True,
        }
        adapter._store_item(appliance_id, {"key": "BSH.Common.Status.DoorState", "value": "BSH.Common.EnumType.DoorState.Open"})
        adapter._store_item(appliance_id, {"key": "BSH.Common.Option.ProgramProgress", "value": 42, "unit": "%"})

        await adapter._publish(appliance_id)
        await adapter.client.aclose()

        self.assertEqual(1, len(context.published))
        node = context.published[0]
        self.assertEqual("Geschirrspüler", node["name"])
        self.assertEqual("dishwasher.fill", node["image"])
        attributes = {item["name"]: item for item in node["attributes"]}
        self.assertEqual("Offen", attributes["Türzustand"]["data"])
        self.assertEqual(42, attributes["Programmfortschritt"]["current_value"])

    async def test_sse_status_updates_existing_appliance(self):
        context = FakeContext()
        adapter = MODULE.HomeConnectAdapter({"client_id": "x"}, context)
        appliance_id = "BOSCH-OVEN-1"
        adapter.appliances[appliance_id] = {"haId": appliance_id, "name": "Backofen", "type": "Oven", "connected": True}
        event = {
            "event": "STATUS", "id": appliance_id,
            "data": '{"items":[{"key":"BSH.Common.Status.OperationState","value":"BSH.Common.EnumType.OperationState.Run"}]}'
        }

        await adapter._handle_sse(event)
        await adapter.client.aclose()

        self.assertEqual("Läuft", context.published[-1]["attributes"][0]["data"])
        self.assertIn("home-connect-sse", context.connections)


if __name__ == "__main__":
    unittest.main()
