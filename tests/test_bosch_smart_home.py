import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = Path(__file__).parents[1] / "modules" / "bosch_smart_home" / "module.py"
SPEC = importlib.util.spec_from_file_location("test_bosch_smart_home_module", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeContext:
    integration_id = "bosch-test"
    integration_name = "Bosch Zuhause"

    def __init__(self):
        self.secrets = {}
        self.published = []
        self.statuses = []

    def stable_node_id(self, external_id):
        return 1_760_000_001 if external_id.startswith("device:") else 1_760_000_002

    @staticmethod
    def attribute_id(node_id, offset):
        return node_id * 100 + offset

    def load_secret(self, name, default=""):
        return self.secrets.get(name, default)

    def save_secret(self, name, value):
        self.secrets[name] = value

    def clear_configuration_value(self, key):
        pass

    async def publish_node(self, node):
        self.published.append(node)

    async def set_status(self, status, error=None):
        self.statuses.append((status, error))

    def nodes(self):
        return []

    async def remove_node(self, node_id):
        pass


class FakeService:
    def __init__(self, service_id, state):
        self.id = service_id
        self.device_id = "device-1"
        self.state = state
        self.writes = []

    def put_state_element(self, key, value):
        self.writes.append((key, value))
        self.state[key] = value

    def short_poll(self):
        pass


class FakeSession:
    def __init__(self, device):
        self._device = device

    def room(self, room_id):
        return SimpleNamespace(name="Wohnzimmer")

    def device(self, device_id):
        return self._device


class BoschSmartHomeModuleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.context = FakeContext()
        self.adapter = MODULE.BoschSmartHomeAdapter({"host": "192.168.1.50"}, self.context)

    def test_manifest_exposes_pairing_without_cloud_credentials(self):
        manifest = MODULE.manifest()
        self.assertEqual("bosch-smart-home", manifest["id"])
        self.assertEqual({"host", "system_password"}, {field["key"] for field in manifest["fields"]})
        self.assertEqual(
            {"pair", "refresh", "reset_pairing"},
            {action["id"] for action in manifest["actions"]},
        )

    def test_host_normalization_rejects_empty_values(self):
        self.assertEqual("192.168.1.50", MODULE._host({"host": "https://192.168.1.50:8444/path"}))
        with self.assertRaises(ValueError):
            MODULE._host({"host": ""})

    def test_device_maps_known_and_unknown_scalar_states(self):
        switch = FakeService("PowerSwitch", {"@type": "powerSwitchState", "switchState": "ON"})
        climate = FakeService("TemperatureLevel", {"@type": "temperatureLevelState", "temperature": 21.45})
        generic = FakeService("CommunicationQuality", {"@type": "qualityState", "quality": "GOOD"})
        device = SimpleNamespace(
            id="device-1", name="Wohnzimmer", room_id="room-1", device_model="Smart Plug",
            status="AVAILABLE", device_services=[switch, climate, generic],
        )
        self.adapter.session = FakeSession(device)

        node = self.adapter._node(device)
        attributes = {item["name"]: item for item in node["attributes"]}
        self.assertEqual(1, attributes["Schalter"]["current_value"])
        self.assertTrue(attributes["Schalter"]["editable"])
        self.assertEqual(21.45, attributes["Temperatur"]["current_value"])
        self.assertEqual("Gut", attributes["Signalqualität"]["data"])
        self.assertIn("Wohnzimmer", node["note"])

    async def test_editable_switch_is_written_with_bosch_enum(self):
        switch = FakeService("PowerSwitch", {"@type": "powerSwitchState", "switchState": "OFF"})
        device = SimpleNamespace(
            id="device-1", name="Steckdose", room_id=None, device_model="Plug",
            status="AVAILABLE", device_services=[switch],
        )
        self.adapter.session = FakeSession(device)
        node = self.adapter._node(device)
        attribute = node["attributes"][0]

        await self.adapter.set_value(node["id"], attribute["id"], 1)

        self.assertEqual([("switchState", "ON")], switch.writes)
        self.assertEqual(1, self.context.published[-1]["attributes"][0]["current_value"])

    def test_text_states_use_data_for_readable_dashboards_and_epaper(self):
        descriptor = MODULE._descriptor("ShutterContact", "value", "OPEN")
        current, data = MODULE._display_value("OPEN", descriptor)
        self.assertIsInstance(current, int)
        self.assertEqual("text", descriptor[2])
        self.assertEqual("Offen", data)

    def test_shutter_fraction_is_displayed_and_written_as_percent(self):
        descriptor = MODULE._descriptor("ShutterControl", "level", 0.42)
        self.assertEqual((42.0, None), MODULE._display_value(0.42, descriptor))
        self.assertEqual(0.75, MODULE._control_value("fraction_percent", 75))

    async def test_unpaired_start_stays_available_for_pair_action(self):
        await self.adapter.start()
        self.assertEqual("Kopplung erforderlich", self.adapter.startup_status)
        self.assertEqual(("Kopplung erforderlich", None), self.context.statuses[-1])


if __name__ == "__main__":
    unittest.main()
