"""Bosch/BSH Home Connect cloud integration using OAuth device flow and SSE."""

import asyncio
import contextlib
import json
import logging
import time

import httpx


log = logging.getLogger("smarthomeboard.home_connect")

BASE_URLS = {
    "eu": "https://api.home-connect.com",
    "cn": "https://api.home-connect.cn",
}

KEY_NAMES = {
    "BSH.Common.Appliance.Connected": "Verbindung",
    "BSH.Common.Status.OperationState": "Betriebszustand",
    "BSH.Common.Status.DoorState": "Türzustand",
    "BSH.Common.Status.RemoteControlActive": "Fernsteuerung aktiv",
    "BSH.Common.Status.RemoteControlStartAllowed": "Fernstart erlaubt",
    "BSH.Common.Root.ActiveProgram": "Aktives Programm",
    "BSH.Common.Root.SelectedProgram": "Ausgewähltes Programm",
    "BSH.Common.Option.ProgramProgress": "Programmfortschritt",
    "BSH.Common.Option.RemainingProgramTime": "Restlaufzeit",
    "BSH.Common.Option.ElapsedProgramTime": "Verstrichene Zeit",
    "BSH.Common.Option.EstimatedTotalProgramTime": "Gesamtdauer",
    "BSH.Common.Event.ProgramFinished": "Programm beendet",
    "Dishcare.Dishwasher.Event.SaltNearlyEmpty": "Salz nachfüllen",
    "Dishcare.Dishwasher.Event.RinseAidNearlyEmpty": "Klarspüler nachfüllen",
}

ENUM_NAMES = {
    "Inactive": "Inaktiv", "Ready": "Bereit", "Run": "Läuft", "ActionRequired": "Aktion erforderlich",
    "Finished": "Beendet", "Error": "Fehler", "Aborting": "Wird abgebrochen",
    "Open": "Offen", "Closed": "Geschlossen", "Locked": "Verriegelt",
    "Present": "Aktiv", "Off": "Nicht aktiv", "DelayedStart": "Start verzögert",
    "Pause": "Pausiert",
}

APPLIANCE_ICONS = {
    "Dishwasher": "dishwasher.fill", "Washer": "washer.fill", "Dryer": "dryer.fill",
    "WasherDryer": "washer.fill", "Oven": "oven.fill", "Microwave": "microwave.fill",
    "CoffeeMaker": "cup.and.saucer.fill", "FridgeFreezer": "refrigerator.fill",
    "Refrigerator": "refrigerator.fill", "Freezer": "refrigerator.fill",
    "Hood": "fan", "CleaningRobot": "fan",
}


def manifest():
    return {
        "id": "home_connect",
        "name": "Bosch Home Connect",
        "version": "1.0.0",
        "icon": "house.and.flag",
        "description": (
            "Serverseitige Home-Connect-Cloud-Anbindung für Bosch/Siemens-Hausgeräte. "
            "Kopplung per Gerätecode und SingleKey-ID-Freigabe, persistente Tokens und Livewerte "
            "über einen SSE-Ereignisstrom. SingleKey-ID-Zugangsdaten werden nicht im Server gespeichert."
        ),
        "supportsDiscovery": False,
        "supportsMultipleInstances": True,
        "fields": [
            {"key": "client_id", "type": "text", "title": "Home Connect Client-ID", "required": True,
             "help": "Client-ID der im Home Connect Developer Portal angelegten Anwendung."},
            {"key": "client_secret", "type": "password", "title": "Client-Secret (optional)",
             "help": "Für den Device Flow optional, aber empfohlen. Wird getrennt als Server-Secret gespeichert."},
            {"key": "region", "type": "select", "title": "Region", "default": "eu", "options": [
                {"value": "eu", "label": "Europa / International"},
                {"value": "cn", "label": "China"},
            ]},
        ],
        "actions": [
            {"id": "start_pairing", "title": "Home Connect koppeln", "icon": "link.badge.plus"},
            {"id": "check_pairing", "title": "Kopplung jetzt prüfen", "icon": "checkmark.circle"},
            {"id": "refresh", "title": "Geräte jetzt aktualisieren", "icon": "arrow.clockwise"},
            {"id": "logout", "title": "Home Connect trennen", "icon": "rectangle.portrait.and.arrow.right", "role": "destructive"},
        ],
    }


def create(configuration, context):
    return HomeConnectAdapter(configuration, context)


class HomeConnectAdapter:
    def __init__(self, configuration, context):
        self.configuration = configuration
        self.context = context
        self.client = httpx.AsyncClient(timeout=20, trust_env=False)
        self.event_task = None
        self.pairing_task = None
        self.pairing_lock = None
        self.stopping = False
        self.appliances = {}
        persisted = context.load_state({}) or {}
        self.values = dict(persisted.get("values", {})) if isinstance(persisted, dict) else {}
        self.offsets = dict(persisted.get("offsets", {})) if isinstance(persisted, dict) else {}
        self.startup_status = "Kopplung erforderlich"
        self.startup_error = None

    @property
    def base_url(self):
        return BASE_URLS.get(str(self.configuration.get("region", "eu")), BASE_URLS["eu"])

    async def start(self):
        self.stopping = False
        supplied_secret = str(self.configuration.get("client_secret", "")).strip()
        if supplied_secret:
            self.context.save_secret("client_secret", supplied_secret)
            self.context.clear_configuration_value("client_secret")
        if not self._client_id():
            self.startup_error = "Home Connect Client-ID fehlt"
            return
        tokens = self.context.load_secret("tokens", {}) or {}
        if not tokens.get("refresh_token") and not tokens.get("access_token"):
            self.startup_status = "Kopplung erforderlich"
            self.startup_error = "Bitte Home Connect koppeln."
            return
        try:
            await self._refresh_all()
            self._start_event_task()
            self.startup_status = f"Verbunden · {len(self.appliances)} Home-Connect-Geräte"
        except Exception as error:
            self.startup_status = "Nicht erreichbar"
            self.startup_error = str(error)

    async def stop(self):
        self.stopping = True
        for task in (self.event_task, self.pairing_task):
            if task:
                task.cancel()
        for task in (self.event_task, self.pairing_task):
            if task:
                await asyncio.gather(task, return_exceptions=True)
        self.context.remove_connection("home-connect-sse")
        await self.client.aclose()

    async def health_check(self):
        await self._refresh_all()

    async def action(self, action_id, _payload):
        if action_id == "start_pairing":
            pending = await self._start_pairing()
            uri = pending.get("verification_uri_complete") or pending.get("verification_uri", "")
            return {
                "message": f"Code {pending['user_code']} eingeben und dort mit SingleKey ID anmelden. Der Server wartet auf die Bestätigung.",
                "verification_url": uri,
                "user_code": pending["user_code"],
            }
        if action_id == "check_pairing":
            try:
                if await self._poll_pairing_once():
                    return {"message": f"Home Connect wurde verbunden. {len(self.appliances)} Geräte geladen."}
            except PairingPending:
                pass
            pending = self.context.load_secret("pending_pairing", {}) or {}
            if not pending:
                raise ValueError("Keine Home-Connect-Kopplung aktiv")
            uri = pending.get("verification_uri_complete") or pending.get("verification_uri", "")
            return {"message": f"Noch nicht bestätigt. Code {pending.get('user_code', '')} unter {uri} eingeben und mit SingleKey ID freigeben."}
        if action_id == "refresh":
            await self._refresh_all()
            self._start_event_task()
            return {"message": f"{len(self.appliances)} Home-Connect-Geräte wurden aktualisiert."}
        if action_id == "logout":
            await self._logout()
            return {"message": "Home Connect wurde getrennt und alle zugehörigen Geräte wurden entfernt."}
        raise ValueError("Unbekannte Home-Connect-Aktion")

    async def set_value(self, _node_id, _attribute_id, _value):
        raise ValueError("Home Connect ist in der ersten Serverversion nur zur Anzeige eingebunden")

    async def _start_pairing(self):
        data = {"client_id": self._client_id(), "scope": "IdentifyAppliance Monitor"}
        response = await self.client.post(f"{self.base_url}/security/oauth/device_authorization", data=data)
        if response.is_error:
            raise ValueError(_api_error(response, "Home-Connect-Kopplung konnte nicht gestartet werden"))
        pending = response.json()
        if not pending.get("device_code") or not pending.get("user_code"):
            raise ValueError("Home Connect hat keinen Kopplungscode geliefert")
        pending["created_at"] = time.time()
        self.context.save_secret("pending_pairing", pending)
        uri = pending.get("verification_uri_complete") or pending.get("verification_uri", "")
        await self.context.set_status("Kopplung läuft", f"Code {pending['user_code']} · {uri}")
        if self.pairing_task:
            self.pairing_task.cancel()
        self.pairing_task = asyncio.create_task(self._pairing_loop())
        return pending

    async def _pairing_loop(self):
        pending = self.context.load_secret("pending_pairing", {}) or {}
        interval = max(5, int(pending.get("interval", 5)))
        expires_at = float(pending.get("created_at", 0)) + int(pending.get("expires_in", 300))
        while not self.stopping and time.time() < expires_at:
            await asyncio.sleep(interval)
            try:
                if await self._poll_pairing_once():
                    return
            except PairingPending:
                continue
            except Exception as error:
                await self.context.set_status("Kopplung fehlgeschlagen", str(error))
                return
        await self.context.set_status("Kopplung abgelaufen", "Bitte einen neuen Home-Connect-Code anfordern")

    async def _poll_pairing_once(self):
        if self.pairing_lock is None:
            self.pairing_lock = asyncio.Lock()
        async with self.pairing_lock:
            return await self._poll_pairing_locked()

    async def _poll_pairing_locked(self):
        pending = self.context.load_secret("pending_pairing", {}) or {}
        if not pending.get("device_code"):
            tokens = self.context.load_secret("tokens", {}) or {}
            if tokens.get("access_token") or tokens.get("refresh_token"):
                await self._refresh_all()
                self._start_event_task()
                return True
            return False
        data = {
            "grant_type": "device_code", "device_code": pending["device_code"],
            "client_id": self._client_id(),
        }
        secret = self._client_secret()
        if secret:
            data["client_secret"] = secret
        response = await self.client.post(f"{self.base_url}/security/oauth/token", data=data)
        if response.status_code == 400:
            payload = response.json()
            if payload.get("error") in {"authorization_pending", "slow_down"}:
                raise PairingPending()
        response.raise_for_status()
        tokens = response.json()
        self._save_tokens(tokens)
        self.context.save_secret("pending_pairing", {})
        await self._refresh_all()
        self._start_event_task()
        await self.context.set_status(f"Verbunden · {len(self.appliances)} Home-Connect-Geräte")
        return True

    async def _access_token(self):
        tokens = self.context.load_secret("tokens", {}) or {}
        if tokens.get("access_token") and float(tokens.get("expires_at", 0)) > time.time() + 90:
            return str(tokens["access_token"])
        refresh_token = str(tokens.get("refresh_token", ""))
        if not refresh_token:
            raise ValueError("Home Connect ist nicht gekoppelt")
        data = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": self._client_id()}
        secret = self._client_secret()
        if secret:
            data["client_secret"] = secret
        response = await self.client.post(f"{self.base_url}/security/oauth/token", data=data)
        response.raise_for_status()
        fresh = response.json()
        if not fresh.get("refresh_token"):
            fresh["refresh_token"] = refresh_token
        self._save_tokens(fresh)
        return str(fresh["access_token"])

    def _save_tokens(self, tokens):
        stored = dict(tokens)
        stored["expires_at"] = time.time() + int(tokens.get("expires_in", 86400))
        self.context.save_secret("tokens", stored)

    async def _request(self, method, path, **kwargs):
        token = await self._access_token()
        response = await self.client.request(method, f"{self.base_url}{path}", headers={"Authorization": f"Bearer {token}"}, **kwargs)
        response.raise_for_status()
        return response.json() if response.content else {}

    async def _refresh_all(self):
        payload = await self._request("GET", "/api/homeappliances")
        appliances = ((payload.get("data") or {}).get("homeappliances") or []) if isinstance(payload, dict) else []
        self.appliances = {str(item.get("haId")): item for item in appliances if item.get("haId")}
        for appliance_id in list(self.appliances):
            await self._refresh_appliance(appliance_id)
        self._save_state()
        return len(self.appliances)

    async def _refresh_appliance(self, appliance_id):
        values = self.values.setdefault(appliance_id, {})
        values["BSH.Common.Appliance.Connected"] = {"value": bool(self.appliances[appliance_id].get("connected")), "unit": "", "timestamp": time.time()}
        for resource in ("status", "settings"):
            try:
                payload = await self._request("GET", f"/api/homeappliances/{appliance_id}/{resource}")
            except httpx.HTTPStatusError as error:
                if error.response.status_code in {400, 403, 404}:
                    continue
                raise
            for item in ((payload.get("data") or {}).get(resource) or []):
                self._store_item(appliance_id, item)
        for resource, key in (("active", "BSH.Common.Root.ActiveProgram"), ("selected", "BSH.Common.Root.SelectedProgram")):
            try:
                payload = await self._request("GET", f"/api/homeappliances/{appliance_id}/programs/{resource}")
            except httpx.HTTPStatusError as error:
                if error.response.status_code in {400, 403, 404}:
                    continue
                raise
            program = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(program, dict) and program.get("key"):
                self._store_item(appliance_id, {"key": key, "value": program["key"]})
                for option in program.get("options", []):
                    self._store_item(appliance_id, option)
        await self._publish(appliance_id)

    def _start_event_task(self):
        if not self.event_task or self.event_task.done():
            self.event_task = asyncio.create_task(self._event_loop())

    async def _event_loop(self):
        while not self.stopping:
            try:
                token = await self._access_token()
                self.context.report_connection(
                    "home-connect-sse", protocol="SSE", direction="Ausgehend",
                    endpoint=f"{self.base_url}/api/homeappliances/events", peer="Home Connect Cloud",
                    state="Verbindung wird aufgebaut", error="",
                )
                timeout = httpx.Timeout(20, read=None)
                headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
                async with self.client.stream("GET", f"{self.base_url}/api/homeappliances/events", headers=headers, timeout=timeout) as response:
                    response.raise_for_status()
                    self.context.report_connection("home-connect-sse", state="Verbunden", error="")
                    event = {"event": "", "id": "", "data": ""}
                    async for line in response.aiter_lines():
                        if not line:
                            await self._handle_sse(event)
                            event = {"event": "", "id": "", "data": ""}
                        elif line.startswith("event:"):
                            event["event"] = line[6:].strip()
                        elif line.startswith("id:"):
                            event["id"] = line[3:].strip()
                        elif line.startswith("data:"):
                            event["data"] += line[5:].strip()
                raise ConnectionError("Home Connect hat den Ereignisstrom beendet")
            except asyncio.CancelledError:
                return
            except Exception as error:
                self.context.report_connection("home-connect-sse", state="Getrennt", error=str(error))
                await self.context.set_status("Verbindung unterbrochen", str(error))
                await asyncio.sleep(15)

    async def _handle_sse(self, event):
        kind = str(event.get("event", "")).upper()
        appliance_id = str(event.get("id", ""))
        if kind == "KEEP-ALIVE":
            self.context.report_connection("home-connect-sse", message_received=True)
            return
        if kind in {"PAIRED", "DEPAIRED"}:
            await self._refresh_all()
            return
        if appliance_id not in self.appliances or not event.get("data"):
            return
        try:
            payload = json.loads(event["data"])
        except json.JSONDecodeError:
            return
        for item in payload.get("items", []):
            self._store_item(appliance_id, item)
        self._save_state()
        await self._publish(appliance_id)
        self.context.report_connection("home-connect-sse", message_received=True)
        await self.context.set_status(f"Verbunden · {len(self.appliances)} Home-Connect-Geräte")

    def _store_item(self, appliance_id, item):
        key = str(item.get("key", ""))
        if not key:
            return
        self.values.setdefault(appliance_id, {})[key] = {
            "value": item.get("value"), "unit": str(item.get("unit", "")),
            "timestamp": float(item.get("timestamp", time.time())),
        }

    async def _publish(self, appliance_id):
        appliance = self.appliances.get(appliance_id)
        if not appliance:
            return
        node_id = self.context.stable_node_id(f"home-connect:{appliance_id}")
        attributes = []
        for key, item in sorted(self.values.get(appliance_id, {}).items()):
            offset = self._offset(key)
            value, text = _attribute_value(key, item.get("value"))
            unit = str(item.get("unit", ""))
            attribute = {
                "id": self.context.attribute_id(node_id, offset), "node_id": node_id,
                "type": _attribute_type(key, value, text), "name": _key_name(key), "unit": unit,
                "current_value": value, "editable": False,
                "last_changed": float(item.get("timestamp", time.time())),
            }
            if text is not None:
                attribute["unit"] = "text"
                attribute["data"] = text
            attributes.append(attribute)
        appliance_type = str(appliance.get("type", ""))
        await self.context.publish_node({
            "id": node_id, "name": str(appliance.get("name") or appliance.get("brand") or appliance_type or "Home Connect"),
            "note": f"Server · Home Connect · {appliance.get('brand', '')} {appliance.get('vib', '')} · {appliance_id}",
            "state": 1 if appliance.get("connected") else 2, "profile": 0, "protocol": 20,
            "image": APPLIANCE_ICONS.get(appliance_type, "house.and.flag.fill"),
            "state_changed": time.time(), "attributes": attributes,
        })

    def _offset(self, key):
        if key not in self.offsets:
            used = {int(value) for value in self.offsets.values()}
            candidate = 1
            while candidate in used:
                candidate += 1
            self.offsets[key] = candidate
        return int(self.offsets[key])

    def _save_state(self):
        self.context.save_state({"values": self.values, "offsets": self.offsets})

    async def _logout(self):
        for task in (self.event_task, self.pairing_task):
            if task:
                task.cancel()
        self.event_task = self.pairing_task = None
        self.context.save_secret("tokens", {})
        self.context.save_secret("pending_pairing", {})
        self.context.remove_connection("home-connect-sse")
        for node in list(self.context.nodes()):
            await self.context.remove_node(node["id"])
        self.appliances.clear()
        self.values.clear()
        self._save_state()
        await self.context.set_status("Kopplung erforderlich", "Home Connect wurde getrennt")

    def _client_id(self):
        return str(self.configuration.get("client_id", "")).strip()

    def _client_secret(self):
        return str(self.context.load_secret("client_secret", "") or "")


class PairingPending(Exception):
    pass


def _api_error(response, prefix):
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    code = str(payload.get("error", "")).strip() if isinstance(payload, dict) else ""
    description = str(payload.get("error_description", "")).strip() if isinstance(payload, dict) else ""
    detail = " · ".join(value for value in (code, description) if value)
    if code == "unauthorized_client":
        detail += (" · Im Home Connect Developer Portal prüfen: richtige Client-ID, echte API-Umgebung "
                   "und OAuth-Flow ‚Device Flow‘.")
    elif code == "invalid_scope":
        detail += " · Die Anwendung benötigt mindestens IdentifyAppliance und Monitor."
    return f"{prefix}: {detail or f'HTTP {response.status_code}'}"


def _key_name(key):
    if key in KEY_NAMES:
        return KEY_NAMES[key]
    tail = str(key).rsplit(".", 1)[-1]
    words = []
    current = ""
    for char in tail:
        if char.isupper() and current:
            words.append(current)
            current = char
        else:
            current += char
    if current:
        words.append(current)
    return " ".join(words) or str(key)


def _attribute_value(key, value):
    if isinstance(value, bool):
        return (1 if value else 0), None
    if isinstance(value, (int, float)):
        return value, None
    text = str(value or "")
    tail = text.rsplit(".", 1)[-1]
    translated = ENUM_NAMES.get(tail, _key_name(tail))
    codes = {
        "Open": 1, "Closed": 0, "Locked": 0, "Present": 1, "Off": 0,
        "Run": 1, "Ready": 0, "Inactive": 0, "Pause": 2, "Finished": 3,
        "Error": 4, "ActionRequired": 5,
    }
    return codes.get(tail, 0), translated


def _attribute_type(key, value, text=None):
    if "DoorState" in key:
        return 14
    if "ProgramProgress" in key:
        return 15
    if "Temperature" in key:
        return 5
    if "Time" in key or "Duration" in key:
        return 222
    if text is not None:
        return 213
    if isinstance(value, (int, float)):
        return 1 if value in (0, 1) else 3
    return 213
