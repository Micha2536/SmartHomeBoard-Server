import importlib.util
import unittest
import json
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "modules" / "enocean" / "module.py"
SPEC = importlib.util.spec_from_file_location("test_enocean_module", MODULE_PATH)
enocean = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(enocean)


def esp3_frame(packet_type, data, optional=b""):
    header = len(data).to_bytes(2, "big") + bytes([len(optional), packet_type])
    payload = data + optional
    return b"\x55" + header + bytes([enocean.crc8(header)]) + payload + bytes([enocean.crc8(payload)])


class EnOceanTests(unittest.TestCase):
    def test_incremental_esp3_parser_and_radio_metadata(self):
        data = bytes.fromhex("A5 1C 08 0B 87 01 02 03 04 00")
        optional = bytes.fromhex("03 FF FF FF FF 4B 00")
        frame = esp3_frame(enocean.PACKET_RADIO_ERP1, data, optional)
        parser = enocean.ESP3StreamParser()
        self.assertEqual(parser.feed(b"noise" + frame[:8]), [])
        packets = parser.feed(frame[8:])
        self.assertEqual(len(packets), 1)
        telegram = enocean.parse_radio_erp1(packets[0][1], packets[0][2])
        self.assertEqual(telegram["sender_id"], "01020304")
        self.assertEqual(telegram["eep"], "A5-07-01")
        self.assertEqual(telegram["manufacturer"], 0x00B)
        self.assertEqual(telegram["rssi"], -75)

    def test_bad_crc_is_ignored_and_parser_resynchronizes(self):
        good = esp3_frame(1, bytes.fromhex("D5 09 AA BB CC DD 00"), bytes.fromhex("03 FF FF FF FF 55 00"))
        bad = bytearray(good)
        bad[-1] ^= 0xFF
        parser = enocean.ESP3StreamParser()
        packets = parser.feed(bytes(bad) + good)
        self.assertEqual(len(packets), 1)

    def test_common_eep_decoders(self):
        self.assertEqual(enocean.decode_eep("D5-00-01", bytes([0x09]))["open"], 0)
        self.assertEqual(enocean.decode_eep("D5-00-01", bytes([0x08]))["open"], 1)
        self.assertEqual(enocean.decode_eep("F6-10-00", bytes([0xD0]))["window_position"], 2)
        self.assertEqual(enocean.decode_eep("A5-14-09", bytes.fromhex("50000008"))["window_position"], 0)
        self.assertEqual(enocean.decode_eep("A5-14-09", bytes.fromhex("5000000E"))["window_position"], 1)
        self.assertEqual(enocean.decode_eep("A5-14-09", bytes.fromhex("5000000A"))["window_position"], 2)
        climate = enocean.decode_eep("A5-04-01", bytes([0, 125, 125, 8]))
        self.assertAlmostEqual(climate["humidity"], 50)
        self.assertAlmostEqual(climate["temperature"], 20)
        motion = enocean.decode_eep("A5-07-01", bytes([125, 0, 255, 8]))
        self.assertEqual(motion["motion"], 1)
        self.assertAlmostEqual(motion["supply_voltage"], 2.5)

    def test_override_parser_accepts_human_friendly_ids(self):
        result = enocean.parse_eep_overrides("01:9a:da:a0 = f6.10.00\n# Kommentar")
        self.assertEqual(result, {"019ADAA0": "F6-10-00"})

    def test_signal_is_reduced_to_four_quality_levels(self):
        self.assertEqual([enocean.link_quality(value) for value in (-55, -70, -85, -95)], [3, 2, 1, 0])

    def test_profile_catalog_has_unique_ids_and_eltako_examples(self):
        profiles = json.loads((MODULE_PATH.parent / "profiles.json").read_text(encoding="utf-8"))
        ids = [item["id"] for item in profiles]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("F6-10-00", ids)
        self.assertIn("D5-00-01", ids)
        self.assertIn("F6-05-02", ids)
        self.assertIn("F6-02-01-SINGLE", ids)
        self.assertTrue(any("Eltako" in item.get("examples", "") for item in profiles))

    def test_ft55_rocker_events_and_energy_bow_are_decoded_separately(self):
        single_i_pressed = enocean.decode_eep("F6-02-01", bytes([0x10]), "single")
        self.assertEqual(single_i_pressed["rocker_1"], 1)
        self.assertEqual(single_i_pressed["rocker_1_name"], "I gedrückt")
        self.assertEqual(single_i_pressed["energy_bow"], 1)
        self.assertNotIn("rocker_2", single_i_pressed)

        single_o_released = enocean.decode_eep("F6-02-01", bytes([0x20]), "single")
        self.assertEqual(single_o_released["rocker_1"], 4)
        self.assertEqual(single_o_released["rocker_1_name"], "O losgelassen")
        self.assertEqual(single_o_released["energy_bow"], 0)

        double_i_pressed = enocean.decode_eep("F6-02-01", bytes([0x50]), "double")
        self.assertEqual(double_i_pressed["rocker_2"], 1)
        self.assertEqual(double_i_pressed["rocker_2_name"], "I gedrückt")


class FakeContext:
    integration_name = "EnOcean"

    def __init__(self, state):
        self.state = state
        self.published = []
        self.removed = []

    def load_state(self, default=None): return self.state or default
    def save_state(self, value): self.state = value
    def stable_node_id(self, external_id): return 1_700_000_123
    @staticmethod
    def attribute_id(node_id, offset): return node_id * 100 + offset
    async def publish_node(self, node): self.published.append(node)
    async def remove_node(self, node_id): self.removed.append(node_id)
    async def set_status(self, status, error=None): pass


class EnOceanDeviceManagementTests(unittest.IsolatedAsyncioTestCase):
    async def test_ft55_double_rocker_publishes_two_instances_and_energy_harvesting(self):
        context = FakeContext({"devices": {}})
        adapter = enocean.EnOceanAdapter({}, context)
        device = {
            "eep": "F6-02-01",
            "profile_id": "F6-02-01",
            "variant": "double",
            "name": "FT55 Flur",
            "raw": "10",
            "values": enocean.decode_eep("F6-02-01", bytes([0x10]), "double"),
            "last_seen": 1,
            "rssi": -55,
        }
        await adapter._publish("019ADAA0", device)
        attributes = context.published[-1]["attributes"]
        rocker_1 = next(item for item in attributes if item["name"] == "Wippe 1")
        rocker_2 = next(item for item in attributes if item["name"] == "Wippe 2")
        harvesting = next(item for item in attributes if item["name"] == "Energy Harvesting")
        self.assertEqual((rocker_1["type"], rocker_1["instance"]), (40, 1))
        self.assertEqual((rocker_2["type"], rocker_2["instance"]), (40, 2))
        self.assertEqual(harvesting["instance"], 3)
        self.assertEqual(harvesting["current_value"], 1)

    async def test_single_rocker_profile_keeps_real_eep_and_one_rocker_instance(self):
        context = FakeContext({"devices": {}})
        adapter = enocean.EnOceanAdapter({}, context)
        await adapter.action("start_learning", {"eep": "F6-02-01-SINGLE", "name": "FT55 Einfach", "seconds": 60})
        optional = bytes.fromhex("03FFFFFFFF3C00")
        await adapter._handle_radio(bytes.fromhex("F6 10 AA BB CC DD 30"), optional)
        device = adapter.devices["AABBCCDD"]
        self.assertEqual(device["eep"], "F6-02-01")
        self.assertEqual(device["profile_id"], "F6-02-01-SINGLE")
        self.assertEqual(device["variant"], "single")
        rocker_names = [item["name"] for item in context.published[-1]["attributes"] if item["type"] == 40]
        self.assertEqual(rocker_names, ["Wippe 1"])

    async def test_device_can_be_renamed_reprofiled_and_deleted(self):
        state = {"devices": {"019ADAA0": {"eep": "F6-02-01", "name": "Alt", "raw": "D0", "values": {}, "last_seen": 1, "rssi": -60}}}
        context = FakeContext(state)
        adapter = enocean.EnOceanAdapter({}, context)
        result = await adapter.action("update_device", {"sender_id": "01:9A:DA:A0", "name": "Fenster Büro", "eep": "F6-10-00"})
        self.assertEqual(result["devices"][0]["sender_id"], "019ADAA0")
        self.assertEqual(adapter.devices["019ADAA0"]["values"]["window_position"], 2)
        self.assertEqual(context.published[-1]["name"], "Fenster Büro")
        result = await adapter.action("delete_device", {"sender_id": "019ADAA0"})
        self.assertEqual(result["devices"], [])
        self.assertFalse(adapter.devices)
        self.assertEqual(context.removed, [1_700_000_123])

    async def test_learning_requires_profile_accepts_one_new_matching_sender_and_locks_ids(self):
        state = {"devices": {"01020304": {"eep": "F6-02-01", "name": "Bekannt", "raw": "D0", "values": {}, "last_seen": 1}}}
        context = FakeContext(state)
        adapter = enocean.EnOceanAdapter({}, context)

        with self.assertRaisesRegex(ValueError, "EEP-Profil"):
            await adapter.action("start_learning", {})
        management = await adapter.action("start_learning", {"eep": "F6-10-00", "name": "Fenster Küche", "seconds": 60})
        self.assertTrue(management["learning"])

        optional = bytes.fromhex("03FFFFFFFF3C00")
        # Ein bereits gespeicherter Sender aktualisiert sich, bleibt aber für
        # diesen und alle späteren Lernläufe gesperrt.
        await adapter._handle_radio(bytes.fromhex("F6 D0 01 02 03 04 00"), optional)
        self.assertGreater(adapter.learning_until, 0)

        # Eine falsche Telegrammfamilie wird nicht dem gewählten F6-Profil zugeordnet.
        await adapter._handle_radio(bytes.fromhex("D5 08 11 22 33 44 00"), optional)
        self.assertNotIn("11223344", adapter.devices)
        self.assertGreater(adapter.learning_until, 0)

        await adapter._handle_radio(bytes.fromhex("F6 D0 AA BB CC DD 00"), optional)
        self.assertIn("AABBCCDD", adapter.devices)
        self.assertEqual(adapter.devices["AABBCCDD"]["eep"], "F6-10-00")
        self.assertEqual(adapter.devices["AABBCCDD"]["name"], "Fenster Küche")
        self.assertEqual(adapter.learning_until, 0)

        # Nach dem automatischen Ende darf kein weiteres Telegramm als Gerät erscheinen.
        await adapter._handle_radio(bytes.fromhex("F6 D0 55 66 77 88 00"), optional)
        self.assertNotIn("55667788", adapter.devices)

    async def test_a5_14_09_teach_in_immediately_publishes_window_handle_state(self):
        context = FakeContext({"devices": {}})
        adapter = enocean.EnOceanAdapter({}, context)
        await adapter.action("start_learning", {"eep": "A5-14-09", "name": "Fenster Büro", "seconds": 60})

        optional = bytes.fromhex("03FFFFFFFF3C00")
        await adapter._handle_radio(bytes.fromhex("A5 50 48 0D 80 AA BB CC DD 00"), optional)

        self.assertEqual(adapter.devices["AABBCCDD"]["values"]["window_position"], 0)
        node = context.published[-1]
        self.assertEqual(node["profile"], 2001)
        position = next(attribute for attribute in node["attributes"] if attribute["type"] == 10)
        self.assertEqual(position["current_value"], 0)
        self.assertEqual(position["data"], "Geschlossen")
        self.assertFalse(any(attribute["name"] == "Rohwert" for attribute in node["attributes"]))


if __name__ == "__main__":
    unittest.main()
