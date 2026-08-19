import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "modules" / "motionblinds" / "module.py"
SPEC = importlib.util.spec_from_file_location("test_motionblinds_module", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeContext:
    integration_name = "MotionBlinds"

    def __init__(self):
        self.published = []

    def stable_node_id(self, external_id):
        return 1_720_000_001

    @staticmethod
    def attribute_id(node_id, offset):
        return node_id * 100 + offset

    async def publish_node(self, node):
        self.published.append(node)


class MotionBlindsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.context = FakeContext()
        self.adapter = MODULE.MotionBlindsAdapter(
            {"bridge_ip": "192.168.1.80", "secret_key": "1234567890abcdef"}, self.context
        )

    def test_manifest_exposes_local_gateway_fields(self):
        manifest = MODULE.manifest()
        self.assertEqual("motionblinds", manifest["id"])
        self.assertFalse(manifest["supportsMultipleInstances"])
        self.assertEqual(
            {"bridge_ip", "secret_key", "response_port", "poll_seconds"},
            {field["key"] for field in manifest["fields"]},
        )

    def test_access_token_matches_aes_ecb_reference(self):
        if MODULE.Cipher is None:
            self.skipTest("cryptography ist in der Testumgebung nicht installiert")
        self.assertEqual("B9774B99120FC886DCF8D72D906E925D", MODULE._access_token("12345678", "1234567890abcdef"))

    async def test_device_maps_position_and_battery_voltage(self):
        mac = "AA:BB:CC:DD"
        self.adapter.summary_by_mac[mac] = {"mac": mac, "deviceType": MODULE.DEVICE_TYPE, "name": "Wohnzimmer"}
        self.adapter.details_by_mac[mac] = {"data": {"currentPosition": 67, "batteryLevel": 1240}}
        await self.adapter._publish(mac)
        attributes = {item["name"]: item for item in self.context.published[-1]["attributes"]}
        self.assertEqual(67, attributes["Position"]["current_value"])
        self.assertEqual(12.4, attributes["Batteriespannung"]["current_value"])

    async def test_write_ack_target_is_not_used_as_measured_position(self):
        mac = "AA:BB:CC:DD"
        self.adapter.summary_by_mac[mac] = {"mac": mac, "deviceType": MODULE.DEVICE_TYPE}
        self.adapter.details_by_mac[mac] = {"data": {"currentPosition": 20}}
        await self.adapter._apply_message({"msgType": "WriteDeviceAck", "mac": mac, "data": {"targetPosition": 90}})
        self.assertEqual(20, self.adapter.details_by_mac[mac]["data"]["currentPosition"])
        self.assertNotIn("targetPosition", self.adapter.details_by_mac[mac]["data"])

    async def test_live_reports_do_not_fill_request_inbox(self):
        self.adapter.inbox_event = type("Event", (), {"set": lambda self: None})()
        self.adapter.summary_by_mac["AA"] = {"mac": "AA", "deviceType": MODULE.DEVICE_TYPE}
        for position in range(150):
            self.adapter._received({"msgType": "Report", "mac": "AA", "data": {"currentPosition": position}})
        await MODULE.asyncio.sleep(0)
        self.assertEqual([], self.adapter.inbox)

    async def test_controls_map_to_gateway_protocol(self):
        node_id, mac = 1_720_000_001, "AA:BB:CC:DD"
        self.adapter.mac_by_node[node_id] = mac
        self.adapter.access_token = "TOKEN"
        sent = []

        async def send(packet):
            sent.append(packet)

        self.adapter._send = send
        self.adapter._follow_command = lambda *args: _done()
        base = self.context.attribute_id(node_id, 0)
        await self.adapter.set_value(node_id, base + 2, 0)
        await self.adapter.set_value(node_id, base + 2, 1)
        await self.adapter.set_value(node_id, base + 2, 2)
        await self.adapter.set_value(node_id, base + 1, 73)
        self.assertEqual([1, 0, 2], [packet["data"]["operation"] for packet in sent[:3]])
        self.assertEqual(73, sent[3]["data"]["targetPosition"])


async def _done():
    return None


if __name__ == "__main__":
    unittest.main()
