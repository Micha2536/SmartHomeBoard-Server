import importlib.util
import json
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "modules" / "zwave" / "module.py"
SPEC = importlib.util.spec_from_file_location("test_zwave_module", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeMetadata:
    def __init__(self, label, type_="number", unit="", writeable=False, states=None, minimum=None, maximum=None):
        self.label = label
        self.type = type_
        self.unit = unit
        self.writeable = writeable
        self.readable = True
        self.states = states or {}
        self.min = minimum
        self.max = maximum
        self.secret = False


class FakeValue:
    def __init__(self, value_id, command_class, property_, value, metadata, endpoint=0, property_key=None):
        self.value_id = value_id
        self.command_class = command_class
        self.property_ = property_
        self.property_key = property_key
        self.property_name = str(property_)
        self.property_key_name = None
        self.value = value
        self.metadata = metadata
        self.endpoint = endpoint


class FakeNode:
    node_id = 7
    name = "Wohnzimmer"
    label = "Multisensor"
    manufacturer = "Beispiel"
    ready = True
    status = 4
    is_controller_node = False
    manufacturer_id = 0x0086
    product_type = 0x0002
    product_id = 0x0064
    firmware_version = "1.2"
    device_class = None
    device_config = None

    def __init__(self, values):
        self.values = {value.value_id: value for value in values}


class FakeContext:
    def __init__(self):
        self.saved = []
        self.state = {}

    def load_state(self, default=None): return self.state or default
    def save_state(self, value): self.state = value
    def stable_node_id(self, _external): return 1_730_000_007
    @staticmethod
    def attribute_id(node_id, offset): return node_id * 100 + offset
    async def publish_node(self, node): self.saved.append(node)


class ZWaveTests(unittest.IsolatedAsyncioTestCase):
    def test_manifest_exposes_pairing_and_s2_pin_actions(self):
        manifest = MODULE.manifest()
        self.assertEqual("zwave", manifest["id"])
        actions = {item["id"]: item for item in manifest["actions"]}
        self.assertIn("start_inclusion", actions)
        self.assertIn("start_exclusion", actions)
        self.assertEqual("pin", actions["enter_pin"]["fields"][0]["key"])

    def test_current_and_target_values_are_merged_into_one_control(self):
        current = FakeValue("7-37-0-currentValue", 37, "currentValue", False, FakeMetadata("Schalter", type_="boolean"))
        target = FakeValue("7-37-0-targetValue", 37, "targetValue", True, FakeMetadata("Schalter", type_="boolean", writeable=True))
        pairs = MODULE._presentable_values(FakeNode([current, target]))
        self.assertEqual([(current, target)], pairs)

        # Z-Wave JS does not guarantee that currentValue is listed first.
        reverse_pairs = MODULE._presentable_values(FakeNode([target, current]))
        self.assertEqual([(current, target)], reverse_pairs)

        attribute = MODULE._attribute_from_value(1, 101, current, target)
        self.assertEqual(0, attribute["current_value"])
        self.assertEqual(1, attribute["target_value"])
        self.assertTrue(attribute["editable"])

    def test_read_only_feedback_has_no_target_value(self):
        current = FakeValue("temperature", 49, "Air temperature", 21.75, FakeMetadata("Lufttemperatur", unit="°C"))
        pairs = MODULE._presentable_values(FakeNode([current]))
        self.assertEqual([(current, None)], pairs)
        attribute = MODULE._attribute_from_value(1, 101, *pairs[0])
        self.assertEqual(21.75, attribute["current_value"])
        self.assertNotIn("target_value", attribute)
        self.assertFalse(attribute["editable"])

    async def test_common_values_map_to_homee_compatible_attributes(self):
        values = [
            FakeValue("temperature", 49, "Air temperature", 21.75, FakeMetadata("Lufttemperatur", unit="°C")),
            FakeValue("battery", 128, "level", 87, FakeMetadata("Batteriestand", unit="%")),
            FakeValue("mode", 64, "mode", 1, FakeMetadata("Betriebsmodus", writeable=True, states={0: "Aus", 1: "Heizen"})),
        ]
        context = FakeContext()
        adapter = MODULE.ZWaveAdapter({}, context)
        await adapter._publish_node(FakeNode(values))
        attributes = {item["name"]: item for item in context.saved[-1]["attributes"]}
        self.assertEqual(5, attributes["Lufttemperatur"]["type"])
        self.assertEqual(8, attributes["Batteriestand"]["type"])
        self.assertEqual("choice", attributes["Betriebsmodus"]["unit"])
        self.assertEqual("Heizen", json.loads(attributes["Betriebsmodus"]["data"])["label"])
        self.assertEqual(17, context.saved[-1]["protocol"])
        self.assertIn("Hersteller Beispiel (0x0086)", context.saved[-1]["note"])
        self.assertIn("Produkttyp 0x0002", context.saved[-1]["note"])
        self.assertIn("Produkt-ID 0x0064", context.saved[-1]["note"])
        self.assertIn("Firmware 1.2", context.saved[-1]["note"])

    def test_device_class_from_config_database_is_included(self):
        class Item:
            label = "Multilevel Sensor"

        class DeviceClass:
            specific = Item()

        node = FakeNode([])
        node.device_class = DeviceClass()
        self.assertIn("Geräteklasse Multilevel Sensor", MODULE._node_details(node))

    def test_config_database_is_fallback_for_device_creation(self):
        class DeviceConfig:
            manufacturer = "Config-Hersteller"
            label = "ZW100"
            description = "Mehrfachsensor"
            supports_zwave_plus = True

        node = FakeNode([])
        node.name = ""
        node.manufacturer = ""
        node.label = ""
        node.device_config = DeviceConfig()
        self.assertEqual("Config-Hersteller ZW100", MODULE._node_name(node))
        details = MODULE._node_details(node)
        self.assertIn("Mehrfachsensor", details)
        self.assertIn("Z-Wave Plus", details)

    def test_boolean_and_number_commands_are_converted(self):
        boolean = FakeValue("switch", 37, "targetValue", False, FakeMetadata("Schalter", type_="boolean", writeable=True))
        number = FakeValue("level", 38, "targetValue", 0, FakeMetadata("Dimmwert", writeable=True))
        self.assertIs(True, MODULE._command_value(boolean, 1))
        self.assertEqual(42, MODULE._command_value(number, 42.0))

    def test_binary_sensor_is_not_mistaken_for_a_switch(self):
        motion = MODULE._attribute_type(48, "Motion detected", "", True)
        contact = MODULE._attribute_type(48, "Window contact", "", False)
        self.assertEqual(25, motion)
        self.assertEqual(14, contact)

    def test_controller_state_resets_finished_inclusion(self):
        self.assertEqual("Anlernmodus aktiv", MODULE._inclusion_state_status(1))
        self.assertEqual("Ausschlussmodus aktiv", MODULE._inclusion_state_status(2))
        self.assertEqual("Verbunden", MODULE._inclusion_state_status(0))
        self.assertEqual("Verbunden", MODULE._inclusion_state_status(None))


if __name__ == "__main__":
    unittest.main()
