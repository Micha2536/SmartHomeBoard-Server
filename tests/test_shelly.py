import base64
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path


sys.modules.setdefault("httpx", types.SimpleNamespace(AsyncClient=object, DigestAuth=object))
sys.modules.setdefault("websockets", types.SimpleNamespace(connect=None))

MODULE_PATH = Path(__file__).parents[1] / "modules" / "shelly" / "module.py"
SPEC = importlib.util.spec_from_file_location("test_shelly_module", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeContext:
    def __init__(self):
        self.published = []
        self.removed = []
        self.state = {}
        self.secrets = {}

    def load_state(self, default=None): return self.state or default
    def save_state(self, value): self.state = value
    def load_secret(self, name, default=""): return self.secrets.get(name, default)
    def save_secret(self, name, value): self.secrets[name] = value
    def stable_node_id(self, external): return 1_740_000_000 + sum(external.encode())
    async def publish_node(self, node): self.published.append(node)
    async def remove_node(self, node_id): self.removed.append(node_id)
    async def set_status(self, *_args): pass
    def clear_configuration_value(self, _key): pass


class ShellyTests(unittest.IsolatedAsyncioTestCase):
    def test_ble_relay_uses_bthome_service_data_and_shelly_scanner_api(self):
        script = MODULE.BLE_SCANNER_SCRIPT
        self.assertIn('result.service_data[BTHOME_SERVICE]', script)
        self.assertIn('BLE.Scanner.Start(', script)
        self.assertIn('BLE.Scanner.Subscribe(scan)', script)
        self.assertIn('BLE.Scanner.start(scanOptions)', script)
        self.assertIn('BLE.Scanner.subscribe(scan)', script)
        self.assertNotIn('BLE.Scanner.isRunning()', script)
        self.assertNotIn('result.advData', script)

    def test_manifest_exposes_discovery_and_blu_learning(self):
        manifest = MODULE.manifest()
        self.assertEqual("shelly", manifest["id"])
        self.assertTrue(manifest["supportsDiscovery"])
        actions = {item["id"]: item for item in manifest["actions"]}
        fields = {item["key"]: item for item in actions["start_blu_learning"]["fields"]}
        self.assertEqual("select", fields["template"]["type"])
        self.assertIn("motion", {item["value"] for item in fields["template"]["options"]})

    async def test_addon_components_stay_under_the_physical_shelly(self):
        context = FakeContext()
        adapter = MODULE.ShellyAdapter({}, context)
        gateway = {
            "host": "192.168.1.40",
            "info": {"id": "shellyplus1pm-aabbcc", "mac": "AA:BB:CC:11:22:33", "gen": 2, "model": "SNSW-001P16EU"},
            "config": {
                "sys": {"device": {"name": "Heizraum", "addon_type": "sensor"}},
                "switch:0": {"name": "Heizung"},
                "temperature:100": {"name": "Vorlauf"},
                "temperature:101": {"name": "Rücklauf"},
                "input:1": {"type": "analog", "name": "Drucksensor"},
            },
            "status": {
                "switch:0": {"output": True, "apower": 12.4, "aenergy": {"total": 2000}},
                "temperature:100": {"tC": 41.2},
                "temperature:101": {"tC": 35.8},
                "input:1": {"percent": 44.0},
            },
            "components": [],
        }

        await adapter._publish_gateway(gateway)

        self.assertEqual(1, len(context.published))
        node = context.published[0]
        self.assertEqual("Heizraum", node["name"])
        names = {item["name"] for item in node["attributes"]}
        self.assertIn("Heizung · Schalten", names)
        self.assertIn("Heizung · Leistung", names)
        self.assertIn("Heizung · Energie", names)
        self.assertIn("Plus Add-on · Vorlauf · Messwert", names)
        self.assertIn("Plus Add-on · Rücklauf · Messwert", names)
        self.assertIn("Plus Add-on · Drucksensor · Analogwert", names)

    async def test_dynamic_component_status_is_loaded_from_get_components(self):
        context = FakeContext()
        adapter = MODULE.ShellyAdapter({}, context)

        async def rpc(_host, method, _params=None):
            if method == "Shelly.GetStatus":
                return {"sys": {"uptime": 10}, "switch:0": {"output": False}}
            if method == "Shelly.GetConfig":
                return {"sys": {"device": {"name": "Keller", "addon_type": "sensor"}}}
            if method == "Shelly.ListMethods":
                return {"methods": []}
            raise AssertionError(method)

        async def components(_host):
            return [{
                "key": "temperature:100",
                "config": {"name": "Vorlauf"},
                "status": {"tC": 43.6},
            }]

        adapter._rpc = rpc
        adapter._get_components = components
        await adapter._load_gateway("192.168.1.40", {
            "id": "shellyplus1-aabbcc", "mac": "AA:BB:CC:11:22:33", "gen": 2, "model": "SNSW-001X16EU",
        })

        names = {item["name"] for item in context.published[-1]["attributes"]}
        self.assertIn("Plus Add-on · Vorlauf · Messwert", names)

    async def test_blu_discovery_is_merged_by_mac_across_gateways(self):
        adapter = MODULE.ShellyAdapter({}, FakeContext())
        adapter.learning = {"candidates": {}}
        event = {
            "component": "bthome",
            "event": "device_discovered",
            "device": {"addr": "3C:2E:F5:71:D5:2A", "local_name": "SBBT-002C", "rssi": -62},
        }
        await adapter._handle_event({"host": "192.168.1.41"}, event)
        event["device"]["rssi"] = -51
        await adapter._handle_event({"host": "192.168.1.42"}, event)

        self.assertEqual(["3c:2e:f5:71:d5:2a"], list(adapter.learning["candidates"]))
        candidate = adapter.learning["candidates"]["3c:2e:f5:71:d5:2a"]
        self.assertEqual({"192.168.1.41", "192.168.1.42"}, candidate["gateways"])
        self.assertEqual(-51, candidate["rssi"])

    async def test_duplicate_blu_packet_only_publishes_once(self):
        context = FakeContext()
        adapter = MODULE.ShellyAdapter({}, context)
        mac = "3c:2e:f5:71:d5:2a"
        adapter.learned_blu[mac] = {"mac": mac, "name": "Fenster", "template": "door_window"}
        first = {"blu_components": {"bthomedevice:200": {"kind": "device", "mac": mac}}}
        second = {"blu_components": {"bthomedevice:201": {"kind": "device", "mac": mac}}}
        status = {"packet_id": 17, "battery": 88, "rssi": -55, "last_update_ts": 1000}

        await adapter._handle_blu_status(first, "bthomedevice:200", status)
        await adapter._handle_blu_status(second, "bthomedevice:201", status)

        self.assertEqual(1, len(context.published))
        self.assertEqual("Fenster", context.published[0]["name"])

    async def test_registered_blu_device_status_becomes_learning_candidate(self):
        adapter = MODULE.ShellyAdapter({}, FakeContext())
        adapter.learning = {"candidates": {}}
        mac = "3c:2e:f5:71:d5:2a"
        gateway = {
            "host": "192.168.1.40",
            "blu_components": {
                "bthomedevice:200": {"kind": "device", "mac": mac, "name": "Fenster Büro"},
            },
        }

        await adapter._handle_blu_status(
            gateway,
            "bthomedevice:200",
            {"packet_id": 18, "battery": 91, "rssi": -47, "last_update_ts": 1000},
        )

        self.assertIn(mac, adapter.learning["candidates"])
        candidate = adapter.learning["candidates"][mac]
        self.assertEqual({"192.168.1.40"}, candidate["gateways"])
        self.assertEqual(-47, candidate["rssi"])
        self.assertEqual("Fenster Büro", candidate["device"]["local_name"])

    async def test_native_discovery_is_started_on_persistent_websocket(self):
        class Socket:
            def __init__(self): self.sent = []
            async def send(self, value): self.sent.append(value)

        adapter = MODULE.ShellyAdapter({}, FakeContext())
        socket = Socket()
        adapter.ws_connections["gateway-1"] = socket
        gateway = {
            "host": "192.168.1.40", "info": {"id": "shellypro4pm", "gen": 2},
            "config": {"sys": {"device": {"name": "Verteilung"}}}, "status": {},
        }

        result = await adapter._start_gateway_discovery("gateway-1", gateway)

        self.assertEqual("websocket", result["channel"])
        frame = json.loads(socket.sent[0])
        self.assertEqual("BTHome.StartDeviceDiscovery", frame["method"])
        self.assertEqual(30, frame["params"]["duration"])
        self.assertIn(frame["id"], adapter.discovery_request_ids)
        self.assertEqual("BTHome.StartDeviceDiscovery → WS", adapter.ws_diagnostics[-1]["method"])

    def test_door_window_bthome_advertisement_is_decoded(self):
        service = bytes([
            0xD2, 0xFC, 0x40,
            0x00, 0x17,
            0x01, 87,
            0x05, 0xD2, 0x04, 0x00,
            0x2D, 1,
            0x3F, 0x7B, 0x00,
        ])
        advertisement = bytes([len(service) + 1, 0x16]) + service

        decoded = MODULE._parse_bthome_advertisement(advertisement.hex())

        self.assertFalse(decoded["encrypted"])
        self.assertEqual(0x17, decoded["values"][0])
        self.assertEqual(87, decoded["values"][1])
        self.assertAlmostEqual(12.34, decoded["values"][5])
        self.assertEqual(1, decoded["values"][45])
        self.assertAlmostEqual(12.3, decoded["values"][63])

    async def test_script_relay_can_learn_and_update_door_window(self):
        context = FakeContext()
        adapter = MODULE.ShellyAdapter({}, context)
        adapter.learning = {"candidates": {}}
        gateway = {"host": "192.168.1.40"}
        service = bytes([0xD2, 0xFC, 0x40, 0x00, 1, 0x01, 90, 0x2D, 1])
        advertisement = (bytes([len(service) + 1, 0x16]) + service).hex()
        payload = {"addr": "3C:2E:F5:71:D5:2A", "rssi": -48, "local_name": "SBDW-002C", "adv_data": advertisement}

        await adapter._handle_raw_bthome(gateway, payload)
        mac = "3c:2e:f5:71:d5:2a"
        self.assertIn(mac, adapter.learning["candidates"])
        adapter.learned_blu[mac] = {"mac": mac, "name": "Fenster", "template": "door_window"}
        await adapter._handle_raw_bthome(gateway, payload)

        self.assertEqual(1, adapter.blu_values[mac]["obj:45:0"]["value"])
        self.assertEqual(90, adapter.blu_values[mac]["obj:1:0"]["value"])
        self.assertEqual("Fenster", context.published[-1]["name"])

    async def test_cloud_relay_infos_becomes_learning_candidate(self):
        adapter = MODULE.ShellyAdapter({}, FakeContext())
        adapter.learning = {"candidates": {}}
        service = bytes([0x40, 0x00, 1, 0x01, 90, 0x2D, 1])

        async def rpc(_host, method, _params=None):
            self.assertEqual("BLE.CloudRelay.ListInfos", method)
            return {
                "count": 1,
                "total": 1,
                "devices": {
                    "3C:2E:F5:71:D5:2A": {
                        "name": "SBDW-002C",
                        "rssi": -44,
                        "sdata": {"fcd2": base64.b64encode(service).decode()},
                    }
                },
            }

        adapter._rpc = rpc
        await adapter._read_cloud_relay_candidates({
            "host": "192.168.1.40", "info": {"id": "shellypro4pm-test"}, "config": {}
        })

        candidate = adapter.learning["candidates"]["3c:2e:f5:71:d5:2a"]
        self.assertEqual("SBDW-002C", candidate["device"]["local_name"])
        self.assertEqual(-44, candidate["rssi"])

    async def test_cloud_relay_infos_accepts_pro_nested_device_list(self):
        adapter = MODULE.ShellyAdapter({}, FakeContext())
        adapter.learning = {"candidates": {}}

        async def rpc(_host, _method, _params=None):
            return {
                "ts": 1787313052,
                "offset": 0,
                "count": 1,
                "total": 1,
                "devices": [{
                    "b0:c7:de:33:42:90": {
                        "name": None,
                        "model": 0,
                        "sdata": {"fcd2": "RACTAWQFTAQALQE/DAA="},
                        "mdata": {},
                        "last_seen": 1787312832,
                    }
                }],
            }

        adapter._rpc = rpc
        await adapter._read_cloud_relay_candidates({
            "host": "192.168.1.40", "info": {"id": "shellypro1pm-test"}, "config": {}
        })

        candidate = adapter.learning["candidates"]["b0:c7:de:33:42:90"]
        self.assertEqual({"192.168.1.40"}, candidate["gateways"])
        self.assertFalse(candidate["device"]["encrypted"])


if __name__ == "__main__":
    unittest.main()
