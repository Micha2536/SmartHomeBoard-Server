"""Local Sonos integration using UPnP through SoCo."""

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import time

import soco
from soco import SoCo


log = logging.getLogger("smarthomeboard.sonos")


def manifest():
    return {
        "id": "sonos", "name": "Sonos (lokal)", "version": "1.2.0", "icon": "hifispeaker.2.fill",
        "description": (
            "Findet Sonos-Lautsprecher direkt im lokalen Netz und stellt Wiedergabe, Lautstärke, "
            "Stummschaltung, Titelsteuerung und Gruppenlautstärke für Serverautomationen bereit."
        ),
        "supportsDiscovery": True, "supportsMultipleInstances": False,
        "fields": [
            {"key": "manual_hosts", "type": "text", "title": "Zusätzliche Lautsprecher-IP-Adressen",
             "placeholder": "192.168.178.40, 192.168.178.41",
             "help": "Optionaler Docker-/VLAN-Fallback, falls SSDP-Multicast den Server nicht erreicht."},
            {"key": "poll_seconds", "type": "duration", "title": "Statusaktualisierung", "default": 5,
             "minimum": 2, "maximum": 300, "unit": "s"},
            {"key": "discovery_seconds", "type": "duration", "title": "Erneute Gerätesuche", "default": 300,
             "minimum": 30, "maximum": 3600, "unit": "s"},
            {"key": "tts_language", "type": "select", "title": "Sprache der Ansagen", "default": "de",
             "options": [{"value": "de", "label": "Deutsch"}, {"value": "en", "label": "Englisch"},
                         {"value": "fr", "label": "Französisch"}, {"value": "it", "label": "Italienisch"},
                         {"value": "es", "label": "Spanisch"}]},
            {"key": "tts_base_url", "type": "url", "title": "Optionale Server-Adresse für Ansagen",
             "placeholder": "http://192.168.178.60:8787",
             "help": "Nur bei Docker/VLAN nötig. Der Lautsprecher muss diese Adresse erreichen können."},
        ],
        "actions": [
            {"id": "refresh", "title": "Sonos-Lautsprecher neu suchen", "icon": "arrow.clockwise"},
            {"id": "party_mode", "title": "Alle Lautsprecher gruppieren", "icon": "hifispeaker.2"},
            {"id": "restore_devices", "title": "Gelöschte Lautsprecher wieder zulassen", "icon": "arrow.uturn.backward"},
        ],
    }


def create(configuration, context):
    return SonosAdapter(configuration, context)


class SonosAdapter:
    def __init__(self, configuration, context):
        self.configuration, self.context = configuration, context
        self.players, self.uid_by_node = {}, {}
        self.radio_favorites = {}
        self.task = None
        self.stopping = False
        self.command_lock = asyncio.Lock()
        state = context.load_state({}) or {}
        self.known = dict(state.get("known", {})) if isinstance(state.get("known", {}), dict) else {}
        self.snapshots = dict(state.get("snapshots", {})) if isinstance(state.get("snapshots", {}), dict) else {}
        self.ignored = {str(value) for value in state.get("ignored", [])} if isinstance(state, dict) else set()
        self.last_discovery = 0.0
        self.startup_status, self.startup_error = "Sonos-Suche läuft", None
        soco.config.REQUEST_TIMEOUT = 6.0

    async def start(self):
        self.stopping = False
        count = await self._discover()
        if count:
            await self._poll()
            self.startup_status = f"Verbunden · {count} Sonos-Lautsprecher"
        else:
            self.startup_status = "Bereit · keine Sonos-Lautsprecher gefunden"
            self.startup_error = "SSDP hat kein Sonos-Gerät gefunden. Bei Docker gegebenenfalls IP-Adressen eintragen."
        self.task = asyncio.create_task(self._loop())

    async def stop(self):
        self.stopping = True
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        self.players.clear()
        self.uid_by_node.clear()
        self.radio_favorites.clear()

    async def health_check(self):
        if not self.players:
            await self._discover()
        if not self.players:
            raise ConnectionError("Keine Sonos-Lautsprecher im lokalen Netz gefunden")
        await self._poll()

    async def action(self, action_id, payload):
        if action_id == "refresh":
            count = await self._discover()
            await self._poll()
            return {"message": f"{count} Sonos-Lautsprecher wurden gefunden und aktualisiert."}
        if action_id == "party_mode":
            if not self.players:
                raise ValueError("Keine Sonos-Lautsprecher verbunden")
            await asyncio.to_thread(next(iter(self.players.values())).partymode)
            await asyncio.sleep(0.5)
            await self._poll()
            return {"message": "Alle verfügbaren Sonos-Lautsprecher wurden gruppiert."}
        if action_id == "restore_devices":
            self.ignored.clear()
            self._save_state()
            count = await self._discover()
            await self._poll()
            return {"message": f"Gelöschte Sonos-Lautsprecher sind wieder zugelassen. {count} Geräte gefunden."}
        if action_id == "speak":
            return await self._speak(payload or {})
        raise ValueError("Unbekannte Sonos-Aktion")

    async def _speak(self, payload):
        node_id = int(payload.get("nodeID", 0))
        player = self.players.get(node_id)
        if not player:
            raise KeyError("Sonos-Lautsprecher ist nicht verbunden")
        text = str(payload.get("text") or "").strip()
        if not text:
            raise ValueError("Der Text für die Sonos-Sprachausgabe ist leer")
        if len(text) > 500:
            raise ValueError("Die Sonos-Sprachausgabe darf höchstens 500 Zeichen enthalten")
        volume = payload.get("volume")
        volume = None if volume in (None, "") else _clamp(round(float(volume)), 0, 100)
        restore = payload.get("restorePlayback", True) is not False
        language = str(self.configuration.get("tts_language") or "de")
        token = secrets.token_urlsafe(32)
        directory = Path(os.getenv("SHB_DATA_DIR", "/data")) / "sonos-tts"
        file_path = directory / f"{token}.mp3"
        directory.mkdir(parents=True, exist_ok=True)
        async with self.command_lock:
            try:
                duration = await asyncio.to_thread(self._create_speech_file, text, language, file_path)
                coordinator = player.group.coordinator if player.group else player
                snapshot = None
                if restore:
                    with contextlib.suppress(Exception):
                        from soco.snapshot import Snapshot
                        snapshot = Snapshot(coordinator)
                        await asyncio.to_thread(snapshot.snapshot)
                if volume is not None:
                    await asyncio.to_thread(setattr, coordinator, "volume", volume)
                url = f"{self._tts_base_url(player)}/api/v1/sonos/tts/{token}.mp3"
                await asyncio.to_thread(coordinator.play_uri, url, "SmartHomeBoard Sprachausgabe")
                await asyncio.sleep(max(1.0, duration + 0.8))
                if snapshot is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(snapshot.restore, False)
                else:
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(coordinator.stop)
                await self._poll_nodes(self._group_node_ids(player))
            finally:
                with contextlib.suppress(OSError):
                    file_path.unlink()
        return {"message": f"Sprachausgabe auf {getattr(player, 'player_name', 'Sonos')} gestartet."}

    @staticmethod
    def _create_speech_file(text, language, file_path):
        from gtts import gTTS
        from mutagen.mp3 import MP3
        gTTS(text=text, lang=language).save(str(file_path))
        return float(MP3(str(file_path)).info.length)

    def _tts_base_url(self, player):
        configured = str(self.configuration.get("tts_base_url") or "").strip().rstrip("/")
        if configured:
            if not configured.startswith(("http://", "https://")):
                raise ValueError("Die Server-Adresse für Ansagen muss mit http:// oder https:// beginnen")
            return configured
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((str(player.ip_address), 1400))
            host = sock.getsockname()[0]
        finally:
            sock.close()
        try:
            from server.config import load_server_config
            port = int(load_server_config().get("port", 8787))
        except Exception:
            port = int(os.getenv("SHB_PORT", "8787"))
        return f"http://{host}:{port}"

    async def delete_device(self, node_id):
        node_id = int(node_id)
        uid = self.uid_by_node.get(node_id) or next(
            (key for key in self.known if self.context.stable_node_id(key) == node_id), None
        )
        if not uid:
            raise KeyError("Sonos-Lautsprecher wurde nicht gefunden")
        self.ignored.add(uid)
        self.known.pop(uid, None)
        self.snapshots.pop(uid, None)
        self.players.pop(node_id, None)
        self.uid_by_node.pop(node_id, None)
        self._save_state()

    async def set_value(self, node_id, attribute_id, value):
        node_id = int(node_id)
        player = self.players.get(node_id)
        if not player:
            raise KeyError("Sonos-Lautsprecher ist nicht verbunden")
        offset = int(attribute_id) - self.context.attribute_id(node_id, 0)
        async with self.command_lock:
            await asyncio.to_thread(self._set_value_sync, player, offset, float(value))
            await asyncio.sleep(0.25)
            await self._poll_nodes(self._group_node_ids(player))

    @staticmethod
    def _set_value_sync(player, offset, value):
        group = player.group
        coordinator = group.coordinator if group else player
        if offset == 1:
            coordinator.play() if value >= 0.5 else coordinator.pause()
        elif offset == 2:
            player.volume = _clamp(round(value), 0, 100)
        elif offset == 3:
            player.mute = value >= 0.5
        elif offset == 4:
            if group:
                group.volume = _clamp(round(value), 0, 100)
            else:
                player.volume = _clamp(round(value), 0, 100)
        elif offset == 5:
            if group:
                group.mute = value >= 0.5
            else:
                player.mute = value >= 0.5
        elif offset == 6 and value >= 0.5:
            coordinator.stop()
        elif offset == 7 and value >= 0.5:
            coordinator.next()
        elif offset == 8 and value >= 0.5:
            coordinator.previous()
        elif offset == 14:
            player.bass = _clamp(round(value), -10, 10)
        elif offset == 15:
            player.treble = _clamp(round(value), -10, 10)
        elif offset == 16:
            player.loudness = value >= 0.5
        elif offset == 18:
            favorites = getattr(player, "_shb_radio_favorites", [])
            index = int(round(value))
            if index < 0 or index >= len(favorites):
                raise ValueError("Der ausgewählte Sonos-Radiosender ist nicht mehr verfügbar")
            favorite = favorites[index]
            kwargs = {"uri": favorite["uri"], "title": favorite["title"]}
            if favorite.get("meta"):
                kwargs = {"uri": favorite["uri"], "meta": favorite["meta"]}
            elif str(favorite["uri"]).startswith(("http://", "https://")):
                kwargs["force_radio"] = True
            coordinator.play_uri(**kwargs)
        else:
            raise ValueError("Dieses Sonos-Attribut ist nicht schreibbar")

    async def _loop(self):
        while not self.stopping:
            try:
                await asyncio.sleep(_seconds(self.configuration.get("poll_seconds"), 5, 2, 300))
                if time.monotonic() - self.last_discovery >= _seconds(
                    self.configuration.get("discovery_seconds"), 300, 30, 3600
                ):
                    await self._discover()
                await self._poll()
                await self.context.set_status(f"Verbunden · {len(self.players)} Sonos-Lautsprecher")
            except asyncio.CancelledError:
                return
            except Exception as error:
                log.warning("Sonos-Aktualisierung fehlgeschlagen: %s", error)
                await self.context.set_status("Sonos teilweise nicht erreichbar", _friendly_error(error))

    async def _discover(self):
        discovered = await asyncio.to_thread(self._discover_sync)
        found_uids = set()
        for player in discovered:
            try:
                identity = await asyncio.to_thread(self._identity_sync, player)
            except Exception as error:
                log.debug("Sonos-Identität konnte nicht gelesen werden: %s", error)
                continue
            uid = identity["uid"]
            if uid in self.ignored:
                continue
            found_uids.add(uid)
            node_id = self.context.stable_node_id(uid)
            self.players[node_id], self.uid_by_node[node_id] = player, uid
            self.known[uid] = identity
        # A missed multicast packet must not make a speaker disappear. Known IPs
        # are kept as direct fallbacks and will be marked offline by polling.
        for uid, identity in list(self.known.items()):
            if uid in self.ignored or uid in found_uids:
                continue
            host = str(identity.get("host", ""))
            if host:
                node_id = self.context.stable_node_id(uid)
                self.players.setdefault(node_id, SoCo(host))
                self.uid_by_node[node_id] = uid
        self.last_discovery = time.monotonic()
        self._save_state()
        return len(self.players)

    def _discover_sync(self):
        players = set(soco.discover(timeout=4, include_invisible=False) or set())
        for host in _manual_hosts(self.configuration.get("manual_hosts", "")):
            players.add(SoCo(host))
        return players

    @staticmethod
    def _identity_sync(player):
        info = player.get_speaker_info(refresh=True, timeout=6) or {}
        uid = str(getattr(player, "uid", "") or info.get("uid") or info.get("serial_number") or player.ip_address)
        return {
            "uid": uid, "host": str(player.ip_address),
            "name": str(getattr(player, "player_name", "") or info.get("zone_name") or f"Sonos {player.ip_address}"),
            "model": str(info.get("model_name") or info.get("model_number") or "Sonos"),
        }

    async def _poll(self):
        await self._poll_nodes(list(self.players))

    async def _poll_nodes(self, node_ids):
        tasks = [self._poll_player(node_id, self.players[node_id]) for node_id in node_ids if node_id in self.players]
        if tasks:
            await asyncio.gather(*tasks)
        self._save_state()

    async def _poll_player(self, node_id, player):
        uid = self.uid_by_node[node_id]
        try:
            snapshot = await asyncio.to_thread(self._read_player_sync, player)
            snapshot["connected"] = True
            self.snapshots[uid] = snapshot
        except Exception as error:
            snapshot = dict(self.snapshots.get(uid, {}))
            snapshot.update({"connected": False, "error": _friendly_error(error)})
            self.snapshots[uid] = snapshot
        await self._publish(node_id, uid, snapshot)

    @staticmethod
    def _read_player_sync(player):
        group = player.group
        coordinator = group.coordinator if group else player
        transport = coordinator.get_current_transport_info() or {}
        track = coordinator.get_current_track_info() or {}
        favorites = _radio_favorites(coordinator)
        player._shb_radio_favorites = favorites
        state = str(transport.get("current_transport_state", "STOPPED"))
        album_art = str(track.get("album_art") or track.get("album_art_uri") or "")
        if album_art.startswith("/"):
            album_art = f"http://{coordinator.ip_address}:1400{album_art}"
        current_uri = str(track.get("uri") or "")
        selected_station = next(
            (index for index, favorite in enumerate(favorites) if favorite.get("uri") == current_uri), -1
        )
        return {
            "playing": state in {"PLAYING", "TRANSITIONING"}, "transport_state": state,
            "volume": int(player.volume), "mute": bool(player.mute),
            "group_volume": int(group.volume if group else player.volume),
            "group_mute": bool(group.mute if group else player.mute),
            "group": str(group.label if group else getattr(player, "player_name", "Sonos")),
            "group_uid": str(group.uid if group else getattr(player, "uid", "")),
            "title": str(track.get("title", "")), "artist": str(track.get("artist", "")),
            "album": str(track.get("album", "")), "position": str(track.get("position", "")),
            "album_art": album_art, "radio_favorites": favorites, "selected_station": selected_station,
            "duration": str(track.get("duration", "")), "bass": int(player.bass),
            "treble": int(player.treble), "loudness": bool(player.loudness),
        }

    async def _publish(self, node_id, uid, snapshot):
        now, identity = time.time(), self.known.get(uid, {})
        base = self.context.attribute_id(node_id, 0)

        def number(offset, type_id, name, value, unit="", editable=False, minimum=0, maximum=1):
            attribute = {"id": base + offset, "node_id": node_id, "type": type_id, "instance": 1,
                         "name": name, "unit": unit, "current_value": value, "last_changed": now}
            if editable:
                attribute.update({"target_value": value, "editable": True, "minimum": minimum,
                                  "maximum": maximum, "step_value": 1})
            return attribute

        def text(offset, name, value):
            return {"id": base + offset, "node_id": node_id, "type": 213, "instance": 1, "name": name,
                    "unit": "text", "current_value": 0, "data": str(value or "—"), "last_changed": now}

        def choice(offset, name, options, selected):
            return {"id": base + offset, "node_id": node_id, "type": 45, "instance": 1, "name": name,
                    "unit": "text", "current_value": selected, "target_value": selected,
                    "editable": True, "minimum": -1, "maximum": max(-1, len(options) - 1), "step_value": 1,
                    "data": json.dumps({"options": [{"value": index, "label": item["title"]}
                                                       for index, item in enumerate(options)]}, ensure_ascii=False),
                    "last_changed": now}

        transport_name = {"PLAYING": "Wiedergabe", "TRANSITIONING": "Wird geladen",
                          "PAUSED_PLAYBACK": "Pausiert", "STOPPED": "Gestoppt",
                          "NO_MEDIA_PRESENT": "Keine Wiedergabe"}.get(
                              str(snapshot.get("transport_state", "")),
                              str(snapshot.get("transport_state", "Unbekannt")))
        attributes = [
            number(1, 1, "Wiedergabe", 1 if snapshot.get("playing") else 0, editable=True),
            number(2, 93, "Lautstärke", snapshot.get("volume", 0), "%", True, 0, 100),
            number(3, 1, "Stumm", 1 if snapshot.get("mute") else 0, editable=True),
            number(4, 93, "Gruppenlautstärke", snapshot.get("group_volume", 0), "%", True, 0, 100),
            number(5, 1, "Gruppe stumm", 1 if snapshot.get("group_mute") else 0, editable=True),
            number(6, 1, "Wiedergabe stoppen", 0, editable=True),
            number(7, 1, "Nächster Titel", 0, editable=True),
            number(8, 1, "Vorheriger Titel", 0, editable=True),
            text(9, "Titel", snapshot.get("title")), text(10, "Interpret", snapshot.get("artist")),
            text(11, "Album", snapshot.get("album")), text(12, "Sonos-Gruppe", snapshot.get("group")),
            text(13, "Wiedergabestatus", transport_name),
            number(14, 214, "Bass", snapshot.get("bass", 0), "", True, -10, 10),
            number(15, 214, "Höhen", snapshot.get("treble", 0), "", True, -10, 10),
            number(16, 1, "Loudness", 1 if snapshot.get("loudness") else 0, editable=True),
            number(17, 1, "Erreichbar", 1 if snapshot.get("connected") else 0),
            choice(18, "Radiosender auswählen", snapshot.get("radio_favorites", []),
                   snapshot.get("selected_station", -1)),
            text(19, "Cover-URL", snapshot.get("album_art")),
        ]
        await self.context.publish_node({
            "id": node_id, "integration_source": "server",
            "name": identity.get("name") or f"Sonos {identity.get('host', '')}",
            "note": f"Server · Sonos · {identity.get('model', 'Lautsprecher')} · {identity.get('host', '')}",
            "state": 1 if snapshot.get("connected") else 2, "profile": 2010, "protocol": 21,
            "image": "hifispeaker.fill", "state_changed": now, "attributes": attributes,
        })

    def _group_node_ids(self, player):
        try:
            member_uids = {str(member.uid) for member in player.group.members}
        except Exception:
            return [node_id for node_id, candidate in self.players.items() if candidate is player]
        return [node_id for node_id, uid in self.uid_by_node.items() if uid in member_uids]

    def _save_state(self):
        self.context.save_state({"known": self.known, "snapshots": self.snapshots, "ignored": sorted(self.ignored)})


def _manual_hosts(value):
    hosts = []
    for item in str(value or "").replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            hosts.append(str(ipaddress.ip_address(item)))
        except ValueError as error:
            raise ValueError(f"Ungültige Sonos-IP-Adresse: {item}") from error
    return hosts


def _seconds(value, default, minimum, maximum):
    try:
        return max(minimum, min(maximum, int(float(value))))
    except (TypeError, ValueError):
        return default


def _clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def _friendly_error(error):
    return str(error).strip() or error.__class__.__name__


def _radio_favorites(player):
    """Return radio favorites in a stable, version-tolerant representation."""
    cached_at = float(getattr(player, "_shb_radio_favorites_at", 0) or 0)
    cached = getattr(player, "_shb_radio_favorites", None)
    if cached is not None and time.monotonic() - cached_at < 300:
        return cached
    items = []
    with contextlib.suppress(Exception):
        items.extend(_favorite_items(player.music_library.get_favorite_radio_stations(max_items=200)))
    # Newer Sonos installations may keep stations only in Sonos Favorites.
    with contextlib.suppress(Exception):
        for item in _favorite_items(player.music_library.get_sonos_favorites(max_items=200)):
            uri = item["uri"].lower()
            if any(marker in uri for marker in ("radio", "stream", "mp3radio", "hls")):
                items.append(item)
    unique = {}
    for item in items:
        if item.get("uri"):
            unique.setdefault(item["uri"], item)
    result = sorted(unique.values(), key=lambda item: item["title"].casefold())
    player._shb_radio_favorites = result
    player._shb_radio_favorites_at = time.monotonic()
    return result


def _favorite_items(result):
    if result is None:
        return []
    if isinstance(result, dict):
        raw_items = result.get("favorites") or result.get("items") or []
    else:
        raw_items = getattr(result, "items", result if isinstance(result, (list, tuple)) else [])
    normalized = []
    for item in raw_items:
        if isinstance(item, dict):
            title, uri, meta = item.get("title"), item.get("uri"), item.get("meta", "")
        else:
            title = getattr(item, "title", "")
            resources = getattr(item, "resources", None) or []
            uri = getattr(resources[0], "uri", "") if resources else getattr(item, "uri", "")
            meta = ""
            with contextlib.suppress(Exception):
                from soco.data_structures import to_didl_string
                meta = to_didl_string(item)
        if title and uri:
            normalized.append({"title": str(title), "uri": str(uri), "meta": str(meta or "")})
    return normalized
