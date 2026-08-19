import importlib.util
import json
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
    def __init__(self, device, devices=None):
        self._device = device
        self.devices = devices or [device]
        self.scenarios = []

    def room(self, room_id):
        return SimpleNamespace(name="Wohnzimmer")

    def device(self, device_id):
        return next((device for device in self.devices if str(device.id) == str(device_id)), self._device)


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

    def test_room_climate_control_and_thermostat_are_one_logical_device(self):
        climate = SimpleNamespace(
            id="roomClimateControl_room-1", name="Room Climate Control", room_id="room-1",
            device_model="RoomClimateControl", status="AVAILABLE", device_services=[
                FakeService("RoomClimateControl", {
                    "setpointTemperature": 21.0,
                    "boostMode": True,
                    "summerMode": False,
                    "operationMode": "AUTOMATIC",
                })
            ],
        )
        thermostat = SimpleNamespace(
            id="thermostat-1", name="Raumthermostat", room_id="room-1",
            device_model="Room Thermostat", status="AVAILABLE", device_services=[
                FakeService("TemperatureLevel", {"temperature": 20.4}),
                FakeService("HumidityLevel", {"humidity": 48.0}),
            ],
        )
        self.adapter.session = FakeSession(climate, [climate, thermostat])

        groups = self.adapter._logical_device_groups(self.adapter.session.devices)
        self.assertEqual(1, len(groups))
        external_id, devices = groups[0]
        self.assertEqual("device:thermostat-1", external_id)
        self.assertEqual({"roomClimateControl_room-1", "thermostat-1"}, {item.id for item in devices})

        node = self.adapter._node(devices, external_id)
        attributes = {item["name"]: item for item in node["attributes"]}
        self.assertEqual("Wohnzimmer Heizung", node["name"])
        self.assertEqual(20.4, attributes["Temperatur"]["current_value"])
        self.assertEqual(48.0, attributes["Luftfeuchtigkeit"]["current_value"])
        self.assertEqual(21.0, attributes["Solltemperatur"]["current_value"])
        self.assertEqual(1, attributes["Boost"]["current_value"])
        self.assertEqual(0, attributes["Sommermodus"]["current_value"])
        self.assertEqual("choice", attributes["Betriebsmodus"]["unit"])
        self.assertEqual("Automatisch", json.loads(attributes["Betriebsmodus"]["data"])["label"])
        self.assertTrue(attributes["Solltemperatur"]["editable"])
        self.assertTrue(attributes["Betriebsmodus"]["editable"])
        self.assertTrue(attributes["Boost"]["editable"])
        self.assertTrue(attributes["Sommermodus"]["editable"])

    def test_temperature_sensor_without_thermostat_hint_is_not_merged(self):
        climate = SimpleNamespace(
            id="roomClimateControl_room-1", name="Klima", room_id="room-1",
            device_model="RoomClimateControl", status="AVAILABLE",
            device_services=[FakeService("RoomClimateControl", {"setpointTemperature": 20.0})],
        )
        air_sensor = SimpleNamespace(
            id="sensor-1", name="Universalsensor", room_id="room-1", device_model="Sensor",
            status="AVAILABLE", device_services=[FakeService("TemperatureLevel", {"temperature": 19.0})],
        )

        groups = self.adapter._logical_device_groups([climate, air_sensor])
        self.assertEqual(2, len(groups))

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

    def test_operation_mode_is_a_writable_translated_choice(self):
        descriptor = MODULE._descriptor("RoomClimateControl", "operationMode", "AUTOMATIC")
        current, data = MODULE._display_value("AUTOMATIC", descriptor)
        self.assertEqual(0, current)
        self.assertEqual("Automatisch", json.loads(data)["label"])
        self.assertEqual("AUTOMATIC", MODULE._control_value("operation_mode", 0))
        self.assertEqual("MANUAL", MODULE._control_value("operation_mode", 1))

    async def test_unpaired_start_stays_available_for_pair_action(self):
        await self.adapter.start()
        self.assertEqual("Kopplung erforderlich", self.adapter.startup_status)
        self.assertEqual(("Kopplung erforderlich", None), self.context.statuses[-1])


if __name__ == "__main__":
    unittest.main()
