import asyncio
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = Path(__file__).parents[1] / "modules" / "roborock" / "module.py"
SPEC = importlib.util.spec_from_file_location("test_roborock_module", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeContext:
    integration_name = "Meine Roborocks"

    def stable_node_id(self, external_id):
        return 1_750_000_001

    @staticmethod
    def attribute_id(node_id, offset):
        return node_id * 100 + offset


class FakeCommand:
    def __init__(self):
        self.commands = []

    async def send(self, command, params=None):
        self.commands.append(command if params is None else (command, params))


class RoborockModuleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.context = FakeContext()
        self.adapter = MODULE.RoborockAdapter(
            {"email": "test@example.com", "poll_seconds": 30},
            self.context,
        )

    def test_manifest_exposes_login_and_management(self):
        manifest = MODULE.manifest()
        self.assertEqual("roborock", manifest["id"])
        self.assertEqual(
            {"request_code", "refresh", "logout"},
            {action["id"] for action in manifest["actions"]},
        )
        self.assertIn("verification_code", {field["key"] for field in manifest["fields"]})

    def test_v1_status_is_mapped_to_readable_attributes(self):
        status = SimpleNamespace(
            state=SimpleNamespace(name="cleaning"),
            battery=78,
            clean_time=600,
            clean_area=12_500_000,
            error_code_name="none",
            fan_speed_name="balanced",
            water_mode_name="medium",
            fan_power=102,
        )
        device = SimpleNamespace(
            duid="vacuum-1",
            name="Küche",
            product=SimpleNamespace(model="roborock.vacuum.a70"),
            is_connected=True,
            v1_properties=SimpleNamespace(),
            b01_q10_properties=None,
        )

        node = self.adapter._node(device, status)
        attributes = {item["name"]: item for item in node["attributes"]}
        self.assertEqual(1, attributes["Reinigung"]["current_value"])
        self.assertEqual(78, attributes["Akkustand"]["current_value"])
        self.assertEqual("Reinigt", attributes["Status"]["data"])
        self.assertEqual(12.5, attributes["Gereinigte Fläche"]["current_value"])
        self.assertEqual(10, attributes["Reinigungszeit"]["current_value"])
        self.assertEqual("Ausgeglichen", attributes["Saugstufe"]["data"])

    async def test_v1_controls_start_pause_and_dock(self):
        command = FakeCommand()
        device = SimpleNamespace(
            v1_properties=SimpleNamespace(command=command),
            b01_q10_properties=None,
        )
        await self.adapter._set_cleaning(device, True)
        await self.adapter._set_cleaning(device, False)
        await self.adapter._return_to_dock(device)
        self.assertEqual(["app_start", "app_pause", "app_charge"], command.commands)

    async def test_room_and_suction_controls_use_discovered_options(self):
        command = FakeCommand()
        device = SimpleNamespace(
            duid="vacuum-1",
            v1_properties=SimpleNamespace(command=command),
            b01_q10_properties=None,
        )
        self.adapter.controls["vacuum-1"] = {
            "cleaning_types": [],
            "suction": [{"value": 103, "label": "Turbo", "command": 103, "key": "turbo"}],
            "water": [],
            "rooms": [{"value": 7, "label": "Küche", "command": 7, "key": "7"}],
            "routines": [],
        }

        await self.adapter._set_suction(device, 103)
        await self.adapter._clean_rooms(device, [7])

        self.assertEqual([
            ("set_custom_mode", [103]),
            ("app_segment_clean", [[7]]),
        ], command.commands)

    def test_choice_payload_contains_readable_options(self):
        data = MODULE._choice_data(2, [
            MODULE._choice(1, "Leise", 1),
            MODULE._choice(2, "Turbo", 2),
        ])
        self.assertIn('"label":"Turbo"', data)
        self.assertIn('"value":1', data)

    def test_poll_interval_is_bounded(self):
        self.adapter.configuration["poll_seconds"] = 1
        self.assertEqual(15, self.adapter._poll_seconds())
        self.adapter.configuration["poll_seconds"] = 99999
        self.assertEqual(3600, self.adapter._poll_seconds())


if __name__ == "__main__":
    unittest.main()
