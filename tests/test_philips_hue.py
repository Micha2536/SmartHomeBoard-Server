import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path


class DummyClient:
    def __init__(self, **_kwargs): pass
    async def aclose(self): pass


if "httpx" not in sys.modules:
    sys.modules["httpx"] = types.SimpleNamespace()
sys.modules["httpx"].AsyncClient = DummyClient
sys.modules["httpx"].Timeout = lambda *_args, **_kwargs: None


MODULE_PATH = Path(__file__).parents[1] / "modules" / "philips_hue" / "module.py"
SPEC = importlib.util.spec_from_file_location("test_philips_hue_module", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeContext:
    def __init__(self):
        self.state = {}
        self.secrets = {}
        self.published = []
        self.removed = []

    def load_state(self, default=None): return self.state or default
    def save_state(self, value): self.state = value
    def load_secret(self, name, default=""): return self.secrets.get(name, default)
    def save_secret(self, name, value): self.secrets[name] = value
    def clear_configuration_value(self, _key): pass
    def stable_node_id(self, value): return 1_700_000_000 + sum(value.encode())
    def attribute_id(self, node_id, offset): return node_id * 100 + offset
    async def publish_node(self, node): self.published.append(node)
    async def remove_node(self, node_id): self.removed.append(node_id)
    async def set_status(self, *_args): pass


def resources():
    return [
        {"id": "device-1", "type": "device", "metadata": {"name": "Stehlampe"},
         "product_data": {"product_name": "Hue color lamp", "model_id": "LCT001"},
         "identify": {"action_values": ["identify"]}},
        {"id": "light-1", "type": "light", "owner": {"rid": "device-1", "rtype": "device"},
         "on": {"on": True}, "dimming": {"brightness": 45.5},
         "color_temperature": {"mirek": 250, "mirek_schema": {"mirek_minimum": 153, "mirek_maximum": 500}},
         "color": {"xy": {"x": 0.3, "y": 0.3}},
         "effects_v2": {"action": {"effect_values": ["no_effect", "candle", "prism"]},
                        "status": {"effect": "candle"}}},
        {"id": "device-2", "type": "device", "metadata": {"name": "Flur Sensor"},
         "product_data": {"product_name": "Hue motion sensor", "model_id": "SML001"}},
        {"id": "motion-1", "type": "motion", "owner": {"rid": "device-2", "rtype": "device"},
         "motion": {"motion": False}},
        {"id": "temperature-1", "type": "temperature", "owner": {"rid": "device-2", "rtype": "device"},
         "temperature": {"temperature": 21.4}},
        {"id": "power-1", "type": "device_power", "owner": {"rid": "device-2", "rtype": "device"},
         "power_state": {"battery_level": 87}},
    ]


class HueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.context = FakeContext()
        self.adapter = MODULE.HueAdapter({}, self.context)
        self.adapter.resources = {item["id"]: item for item in resources()}
        self.adapter._rebuild_index()

    async def asyncTearDown(self):
        await self.adapter.client.aclose()

    def test_manifest_exposes_pairing_and_sse_description(self):
        manifest = MODULE.manifest()
        self.assertEqual("philips_hue", manifest["id"])
        self.assertTrue(manifest["supportsDiscovery"])
        self.assertIn("SSE", manifest["description"])
        self.assertIn("pair", {item["id"] for item in manifest["actions"]})

    async def test_devices_are_aggregated_with_writable_light_controls(self):
        await self.adapter._publish_all()
        lamp = next(item for item in self.context.published if item["name"] == "Stehlampe")
        sensor = next(item for item in self.context.published if item["name"] == "Flur Sensor")
        self.assertEqual({1, 2, 23, 42, 45}, {item["type"] for item in lamp["attributes"]})
        self.assertEqual({5, 8, 25}, {item["type"] for item in sensor["attributes"]})
        self.assertTrue(all(item["editable"] for item in lamp["attributes"]))

    async def test_sse_update_merges_partial_resource_and_publishes_owner(self):
        await self.adapter._publish_all()
        self.context.published = []
        payload = [{"type": "update", "data": [
            {"id": "light-1", "type": "light", "dimming": {"brightness": 72.0}}
        ]}]
        await self.adapter._consume_sse_data(json.dumps(payload))
        lamp = self.context.published[-1]
        brightness = next(item for item in lamp["attributes"] if item["type"] == 2)
        self.assertEqual(72.0, brightness["current_value"])
        self.assertTrue(self.adapter.resources["light-1"]["on"]["on"])

    async def test_hue_button_last_event_is_exposed(self):
        self.adapter.resources.update({
            "device-3": {"id": "device-3", "type": "device", "metadata": {"name": "Dimmschalter"}},
            "button-1": {"id": "button-1", "type": "button", "owner": {"rid": "device-3"},
                         "button": {"last_event": "short_release"}},
        })
        self.adapter._rebuild_index()
        await self.adapter._publish_owner("device-3")
        attribute = self.context.published[-1]["attributes"][0]
        self.assertEqual(40, attribute["type"])
        self.assertEqual("Kurz losgelassen", attribute["data"])

    async def test_rooms_and_zones_are_clearly_prefixed(self):
        self.adapter.resources.update({
            "room-1": {"id": "room-1", "type": "room", "metadata": {"name": "Wohnzimmer"}},
            "room-light": {"id": "room-light", "type": "grouped_light", "owner": {"rid": "room-1"},
                           "on": {"on": True}},
            "zone-1": {"id": "zone-1", "type": "zone", "metadata": {"name": "Erdgeschoss"}},
            "zone-light": {"id": "zone-light", "type": "grouped_light", "owner": {"rid": "zone-1"},
                           "on": {"on": False}},
        })
        self.adapter._rebuild_index()
        await self.adapter._publish_owner("room-1")
        await self.adapter._publish_owner("zone-1")
        names = {item["name"] for item in self.context.published}
        self.assertIn("Raum: Wohnzimmer", names)
        self.assertIn("Zone: Erdgeschoss", names)

    async def test_light_commands_use_hue_v2_resource_body(self):
        await self.adapter._publish_owner("device-1")
        calls = []

        async def put(kind, resource_id, body): calls.append((kind, resource_id, body))
        self.adapter._put_resource = put
        lamp = self.context.published[-1]
        brightness = next(item for item in lamp["attributes"] if item["type"] == 2)
        await self.adapter.set_value(lamp["id"], brightness["id"], 61)
        self.assertEqual(("light", "light-1", {"dimming": {"brightness": 61.0}}), calls[-1])

    async def test_supported_effects_are_exposed_and_written_with_v2_payload(self):
        await self.adapter._publish_owner("device-1")
        calls = []

        async def put(kind, resource_id, body): calls.append((kind, resource_id, body))
        self.adapter._put_resource = put
        lamp = self.context.published[-1]
        effect = next(item for item in lamp["attributes"] if item["name"] == "Lichteffekt")
        data = json.loads(effect["data"])
        self.assertEqual("Kerze", data["label"])
        self.assertEqual(["Kein Effekt", "Kerze", "Prisma"], [item["label"] for item in data["options"]])
        await self.adapter.set_value(lamp["id"], effect["id"], 2)
        self.assertEqual(("light", "light-1", {"effects_v2": {"action": {"effect": "prism"}}}), calls[-1])

    async def test_legacy_effects_are_supported_without_v2_feature(self):
        light = self.adapter.resources["light-1"]
        light.pop("effects_v2")
        light["effects"] = {"status": "none", "effect_values": ["none", "colorloop"]}
        await self.adapter._publish_owner("device-1")
        calls = []

        async def put(kind, resource_id, body): calls.append((kind, resource_id, body))
        self.adapter._put_resource = put
        effect = next(item for item in self.context.published[-1]["attributes"] if item["name"] == "Lichteffekt")
        await self.adapter.set_value(self.context.published[-1]["id"], effect["id"], 1)
        self.assertEqual(("light", "light-1", {"effects": {"effect": "colorloop"}}), calls[-1])

    async def test_identify_is_only_exposed_when_device_reports_support(self):
        await self.adapter._publish_owner("device-1")
        calls = []

        async def put(kind, resource_id, body): calls.append((kind, resource_id, body))
        self.adapter._put_resource = put
        lamp = self.context.published[-1]
        identify = next(item for item in lamp["attributes"] if item["name"] == "Erkennungsmodus")
        self.assertEqual(0, identify["current_value"])
        await self.adapter.set_value(lamp["id"], identify["id"], 1)
        self.assertEqual(("device", "device-1", {"identify": {"action": "identify"}}), calls[-1])
        await self.adapter._publish_owner("device-2")
        sensor = next(item for item in self.context.published if item["name"] == "Flur Sensor")
        self.assertNotIn("Erkennungsmodus", {item["name"] for item in sensor["attributes"]})

    def test_rgb_xy_conversion_stays_in_valid_ranges(self):
        x, y = MODULE._rgb_to_xy(255, 80, 20)
        red, green, blue = MODULE._xy_to_rgb(x, y, 100)
        self.assertTrue(0 <= x <= 1 and 0 <= y <= 1)
        self.assertTrue(all(0 <= value <= 255 for value in (red, green, blue)))

    def test_attribute_offsets_are_allocated_per_device_without_global_limit(self):
        for index in range(150):
            self.assertEqual(1, self.adapter._attribute_offset(f"device-{index}", f"light-{index}:on"))
        self.assertEqual(1, self.adapter._attribute_offset("device-many", "resource:0"))
        for index in range(1, 120):
            self.adapter._attribute_offset("device-many", f"resource:{index}")
        self.assertEqual(120, self.adapter._attribute_offset("device-many", "resource:119"))


if __name__ == "__main__":
    unittest.main()
