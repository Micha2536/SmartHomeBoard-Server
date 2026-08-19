import asyncio
import hashlib
import time

import httpx


BASE_URL = "https://app.velux-active.com"
CLIENT_ID = "5931426da127d981e76bdd3f"
CLIENT_SECRET = "6ae2d89d15e767ae5c56b456b452d319"
USER_AGENT = "Velux/1.6.1 (iPhone, ioc13, Scale/3.0)"


def manifest():
    return {
        "id": "velux", "name": "VELUX ACTIVE", "version": "1.0.0", "icon": "window.shade.open",
        "description": (
            "VELUX ACTIVE Rollläden über die Velux-Cloud. Position und Auf/Ab/Stopp stehen "
            "dauerhaft für Dashboard, E-Paper und Serverautomationen bereit."
        ),
        "supportsDiscovery": False, "supportsMultipleInstances": True,
        "fields": [
            {"key": "email", "type": "text", "title": "VELUX E-Mail", "required": True,
             "placeholder": "name@beispiel.de"},
            {"key": "password", "type": "password", "title": "VELUX Passwort", "required": True},
            {"key": "poll_seconds", "type": "duration", "title": "Abfrageintervall", "default": 30,
             "minimum": 5, "maximum": 3600, "unit": "s"},
        ],
        "actions": [{"id": "refresh", "title": "Geräte neu einlesen", "icon": "arrow.clockwise"}],
    }


def create(configuration, context):
    return VeluxAdapter(configuration, context)


class VeluxAdapter:
    def __init__(self, configuration, context):
        self.configuration, self.context = configuration, context
        self.client = httpx.AsyncClient(timeout=30, trust_env=False, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
        self.task = None
        self.access_token = str(context.load_secret("access_token", ""))
        self.refresh_token = str(context.load_secret("refresh_token", ""))
        self.expires_at = float(context.load_secret("expires_at", 0) or 0)
        fingerprint = hashlib.sha256(
            (str(configuration.get("email", "")).strip().lower() + "\0" + str(configuration.get("password", ""))).encode()
        ).hexdigest()
        previous_fingerprint = str(context.load_secret("credential_fingerprint", ""))
        if previous_fingerprint and previous_fingerprint != fingerprint:
            self.access_token = ""
            self.refresh_token = ""
            self.expires_at = 0
            context.save_secret("access_token", "")
            context.save_secret("refresh_token", "")
            context.save_secret("expires_at", 0)
        context.save_secret("credential_fingerprint", fingerprint)
        self.home_id = ""
        self.bridge_id = ""
        self.bridge_by_module = {}
        self.module_by_node = {}
        self.positions = {}
        self.names = {}

    async def start(self):
        await self._discover()
        self.task = asyncio.create_task(self._loop())
        await self.context.set_status("Verbunden")

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        await self.client.aclose()

    async def action(self, action_id, payload):
        if action_id != "refresh":
            raise ValueError("Unbekannte VELUX-Aktion")
        count = await self._discover()
        return {"status": "refreshed", "devices": count}

    async def set_value(self, node_id, attribute_id, value):
        node_id = int(node_id)
        module_id = self.module_by_node.get(node_id)
        if not module_id:
            raise ValueError("Das VELUX-Gerät ist nicht mehr bekannt")
        offset = int(attribute_id) - self.context.attribute_id(node_id, 0)
        if offset == 2:
            operation = int(round(float(value)))
            target = 0 if operation == 0 else (100 if operation == 1 else self.positions.get(node_id, 0))
        elif offset == 1:
            target = min(100, max(0, round(float(value))))
        else:
            raise ValueError("Dieses VELUX-Attribut ist nicht schreibbar")
        await self._set_state(module_id, 100 - target)
        self.positions[node_id] = target
        await self._publish(node_id)
        asyncio.create_task(self._delayed_poll())

    async def _delayed_poll(self):
        await asyncio.sleep(2)
        try:
            await self._poll()
        except Exception:
            pass

    async def _loop(self):
        while True:
            try:
                await asyncio.sleep(max(5, min(3600, int(float(self.configuration.get("poll_seconds", 30))))))
                await self._poll()
                await self.context.set_status("Verbunden")
            except asyncio.CancelledError:
                return
            except Exception as error:
                await self.context.set_status("Nicht erreichbar", _friendly_error(error))

    async def _discover(self):
        self._require_configuration()
        await self._authenticate()
        homes_data = await self._request("GET", "/api/homesdata", bearer=True)
        home = _first_home(homes_data)
        if not home:
            raise ValueError("Kein VELUX-Zuhause gefunden")
        self._capture_home(home)
        status = await self._home_status()
        status_by_id = _modules_by_id(status)
        found = set()
        for module in home.get("modules", []) if isinstance(home.get("modules"), list) else []:
            if str(module.get("type", "")).upper() != "NXO":
                continue
            module_id = _string(module.get("id"))
            if not module_id:
                continue
            node_id = self.context.stable_node_id(module_id)
            found.add(node_id)
            self.module_by_node[node_id] = module_id
            self.names[node_id] = _string(module.get("name")) or f"VELUX {module_id[-5:]}"
            bridge = _string(module.get("bridge"))
            if bridge:
                self.bridge_by_module[module_id] = bridge
            position = _position(status_by_id.get(module_id, module))
            self.positions[node_id] = 100 - (position if position is not None else 0)
            await self._publish(node_id)
        for node in list(self.context.nodes()):
            if int(node["id"]) not in found:
                await self.context.remove_node(int(node["id"]))
        return len(found)

    async def _poll(self):
        await self._authenticate()
        status = await self._home_status()
        for module_id, module in _modules_by_id(status).items():
            node_id = next((node for node, external in self.module_by_node.items() if external == module_id), None)
            position = _position(module)
            if node_id is None or position is None:
                continue
            self.positions[node_id] = 100 - position
            await self._publish(node_id)

    async def _publish(self, node_id):
        now = time.time()
        position = self.positions.get(node_id, 0)
        module_id = self.module_by_node[node_id]
        base = self.context.attribute_id(node_id, 0)
        await self.context.publish_node({
            "id": node_id, "integration_source": "server", "name": self.names.get(node_id, "VELUX"),
            "note": f"Server · VELUX · {module_id}", "state": 1, "profile": 2004, "protocol": 20,
            "image": "nodeicon_shutter", "state_changed": now,
            "attributes": [
                {"id": base + 1, "node_id": node_id, "type": 15, "instance": 1, "name": "Position", "unit": "%",
                 "current_value": position, "target_value": position, "editable": True, "minimum": 0,
                 "maximum": 100, "step_value": 1, "last_changed": now},
                {"id": base + 2, "node_id": node_id, "type": 135, "instance": 1, "name": "Richtung", "unit": "",
                 "current_value": 2, "target_value": 2, "editable": True, "minimum": 0,
                 "maximum": 2, "step_value": 1, "last_changed": now},
            ],
        })

    async def _authenticate(self):
        if self.access_token and self.expires_at > time.time() + 60:
            return
        if self.refresh_token:
            try:
                await self._token({"grant_type": "refresh_token", "refresh_token": self.refresh_token})
                return
            except Exception:
                self.refresh_token = ""
        self._require_configuration()
        await self._token({"grant_type": "password", "username": str(self.configuration["email"]).strip(),
                           "password": str(self.configuration["password"]), "user_prefix": "velux"})

    async def _token(self, form):
        values = dict(form, client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
        response = await self._request("POST", "/oauth2/token", form=values)
        token = _string(response.get("access_token"))
        if not token:
            raise ValueError("VELUX-Anmeldung fehlgeschlagen: " + _api_error(response))
        self.access_token = token
        self.refresh_token = _string(response.get("refresh_token")) or self.refresh_token
        self.expires_at = time.time() + max(60, _number(response.get("expires_in"), 600) - 60)
        self.context.save_secret("access_token", self.access_token)
        self.context.save_secret("refresh_token", self.refresh_token)
        self.context.save_secret("expires_at", self.expires_at)
        home = response.get("body", {}).get("home") if isinstance(response.get("body"), dict) else None
        if isinstance(home, dict):
            self._capture_home(home)

    async def _home_status(self):
        if not self.home_id:
            raise ValueError("Kein VELUX-Zuhause gefunden")
        return await self._request("POST", "/api/homestatus", form={"access_token": self.access_token, "home_id": self.home_id})

    async def _set_state(self, module_id, velux_position):
        await self._authenticate()
        bridge = self.bridge_by_module.get(module_id) or self.bridge_id
        if not bridge:
            raise ValueError("Keine VELUX-Bridge für das Gerät gefunden")
        body = {"home": {"id": self.home_id, "modules": [{"bridge": bridge, "id": module_id,
                 "target_position": int(round(velux_position)), "nonce": 0, "sign_key_id": CLIENT_SECRET}]},
                "app_version": "1.6.1"}
        response = await self._request("POST", "/syncapi/v1/setstate", bearer=True,
                                       headers={"home_id": self.home_id}, json=body)
        if response.get("error") is not None:
            raise ValueError("VELUX-Cloud: " + _api_error(response))

    async def _request(self, method, path, bearer=False, headers=None, form=None, json=None):
        request_headers = dict(headers or {})
        if bearer:
            request_headers["Authorization"] = f"Bearer {self.access_token}"
        response = await self.client.request(method, BASE_URL + path, headers=request_headers, data=form, json=json)
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code < 200 or response.status_code >= 300:
            raise ValueError("VELUX-Cloud: " + _api_error(payload))
        if not isinstance(payload, dict):
            raise ValueError("Die VELUX-Cloud hat eine ungültige Antwort geliefert")
        return payload

    def _capture_home(self, home):
        self.home_id = _string(home.get("id")) or self.home_id
        self.bridge_id = _string(home.get("bridge_id") or home.get("gateway_id") or home.get("bridgeId") or home.get("gatewayId")) or self.bridge_id
        for module in home.get("modules", []) if isinstance(home.get("modules"), list) else []:
            module_id, bridge = _string(module.get("id")), _string(module.get("bridge"))
            if module_id and bridge:
                self.bridge_by_module[module_id] = bridge

    def _require_configuration(self):
        if not str(self.configuration.get("email", "")).strip() or not str(self.configuration.get("password", "")):
            raise ValueError("VELUX E-Mail oder Passwort fehlt")


def _first_home(response):
    body = response.get("body") if isinstance(response.get("body"), dict) else {}
    homes = body.get("homes") if isinstance(body.get("homes"), list) else response.get("homes")
    return homes[0] if isinstance(homes, list) and homes else None


def _modules_by_id(response):
    body = response.get("body") if isinstance(response.get("body"), dict) else {}
    home = body.get("home") if isinstance(body.get("home"), dict) else {}
    for modules in (response.get("modules"), body.get("modules"), home.get("modules")):
        if isinstance(modules, list) and modules:
            result = {}
            for module in modules:
                module_id = _string(module.get("id") or module.get("module_id") or module.get("device_id"))
                if module_id:
                    result[module_id] = module
            return result
    return {}


def _position(module):
    states = module.get("states") if isinstance(module.get("states"), dict) else {}
    value = module.get("current_position", module.get("position", states.get("current_position", states.get("position"))))
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _string(value):
    return str(value) if value is not None else None


def _number(value, default=0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _api_error(payload):
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return _string(error.get("message")) or "Unbekannter Fehler"
    return _string(payload.get("message") or payload.get("error_description") or error) or "Unbekannter Fehler"


def _friendly_error(error):
    return str(error) or error.__class__.__name__
