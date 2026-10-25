import importlib.util
import sys
import types
import unittest
from pathlib import Path


class FakeConfig:
    REQUEST_TIMEOUT = 20


fake_soco = types.ModuleType("soco")
fake_soco.config = FakeConfig()
fake_soco.discover = lambda **_kwargs: set()
fake_soco.SoCo = lambda host: None
sys.modules.setdefault("soco", fake_soco)

MODULE_PATH = Path(__file__).parents[1] / "modules" / "sonos" / "module.py"
SPEC = importlib.util.spec_from_file_location("test_sonos_module", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeContext:
    integration_name = "Sonos"

    def __init__(self):
        self.state, self.published = {}, []

    def load_state(self, default=None): return self.state or default
    def save_state(self, value): self.state = value
    def stable_node_id(self, external): return 1_720_000_000 + sum(external.encode()) % 1_000_000
    @staticmethod
    def attribute_id(node_id, offset): return node_id * 100 + offset
    async def publish_node(self, node): self.published.append(node)
    async def set_status(self, *_args): pass


class FakeGroup:
    def __init__(self, player):
        self.coordinator, self.members = player, {player}
        self.volume, self.mute, self.label, self.uid = 27, False, "Wohnzimmer", "GROUP-1"


class FakeMusicLibrary:
    def get_favorite_radio_stations(self, **_kwargs):
        return {"favorites": [{"title": "Radio Eins", "uri": "x-sonosapi-stream:radio-eins"}]}

    def get_sonos_favorites(self, **_kwargs):
        return {"favorites": []}


class FakePlayer:
    def __init__(self):
        self.uid, self.ip_address, self.player_name = "RINCON_1", "192.168.1.20", "Wohnzimmer"
        self.volume, self.mute, self.bass, self.treble, self.loudness = 23, False, 1, 2, True
        self.group, self.commands = FakeGroup(self), []
        self.music_library = FakeMusicLibrary()

    def get_speaker_info(self, **_kwargs): return {"model_name": "Sonos One", "zone_name": self.player_name}
    def get_current_transport_info(self): return {"current_transport_state": "PLAYING"}
    def get_current_track_info(self):
        return {"title": "Titel", "artist": "Interpret", "album": "Album",
                "album_art": "/getaa?u=cover", "uri": "x-sonosapi-stream:radio-eins"}
    def play(self): self.commands.append("play")
    def pause(self): self.commands.append("pause")
    def stop(self): self.commands.append("stop")
    def next(self): self.commands.append("next")
    def previous(self): self.commands.append("previous")
    def play_uri(self, **kwargs): self.commands.append(("play_uri", kwargs))
    def partymode(self): self.commands.append("party")


class SonosTests(unittest.IsolatedAsyncioTestCase):
    def test_manifest_exposes_local_discovery(self):
        manifest = MODULE.manifest()
        self.assertEqual("sonos", manifest["id"])
        self.assertTrue(manifest["supportsDiscovery"])
        self.assertEqual(
            {"manual_hosts", "poll_seconds", "discovery_seconds", "tts_language", "tts_base_url"},
            {field["key"] for field in manifest["fields"]},
        )

    def test_manual_hosts_are_validated(self):
        self.assertEqual(["192.168.1.2", "10.0.0.4"], MODULE._manual_hosts("192.168.1.2; 10.0.0.4"))
        with self.assertRaisesRegex(ValueError, "Ungültige Sonos-IP-Adresse"):
            MODULE._manual_hosts("sonos.local")

    async def test_player_attributes_are_published_for_automations(self):
        context, player = FakeContext(), FakePlayer()
        adapter = MODULE.SonosAdapter({}, context)
        identity = adapter._identity_sync(player)
        node_id = context.stable_node_id(identity["uid"])
        adapter.known[identity["uid"]] = identity
        adapter.players[node_id], adapter.uid_by_node[node_id] = player, identity["uid"]
        await adapter._poll_player(node_id, player)

        attributes = {item["name"]: item for item in context.published[-1]["attributes"]}
        self.assertEqual("Titel", attributes["Titel"]["data"])
        self.assertEqual(23, attributes["Lautstärke"]["current_value"])
        self.assertTrue(attributes["Gruppenlautstärke"]["editable"])
        self.assertTrue(attributes["Nächster Titel"]["editable"])
        self.assertEqual("http://192.168.1.20:1400/getaa?u=cover", attributes["Cover-URL"]["data"])
        self.assertEqual("Radio Eins", MODULE.json.loads(attributes["Radiosender auswählen"]["data"])["options"][0]["label"])

    async def test_set_value_controls_player_and_group(self):
        context, player = FakeContext(), FakePlayer()
        adapter = MODULE.SonosAdapter({}, context)
        node_id = context.stable_node_id(player.uid)
        adapter.known[player.uid] = {"uid": player.uid, "name": player.player_name, "host": player.ip_address}
        adapter.players[node_id], adapter.uid_by_node[node_id] = player, player.uid
        adapter._poll_nodes = lambda _node_ids: _done()
        base = context.attribute_id(node_id, 0)

        await adapter.set_value(node_id, base + 1, 0)
        await adapter.set_value(node_id, base + 2, 44)
        await adapter.set_value(node_id, base + 4, 38)
        await adapter.set_value(node_id, base + 7, 1)

        self.assertEqual(["pause", "next"], player.commands)
        self.assertEqual(44, player.volume)
        self.assertEqual(38, player.group.volume)

    def test_radio_favorite_can_be_selected(self):
        player = FakePlayer()
        favorites = MODULE._radio_favorites(player)
        player._shb_radio_favorites = favorites
        MODULE.SonosAdapter._set_value_sync(player, 18, 0)
        self.assertEqual("x-sonosapi-stream:radio-eins", player.commands[-1][1]["uri"])


async def _done():
    return None


if __name__ == "__main__":
    unittest.main()
