from __future__ import annotations

import logging
import os
import json
import re
import secrets
import asyncio
import datetime as dt
import hashlib
import hmac
import time
import uuid
from contextlib import asynccontextmanager
from hmac import compare_digest
from html import escape
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.requests import HTTPConnection

from .automations import AutomationEngine
from .config import SETUP_PORT, load_server_config, save_server_config
from .database import Database
from .display_discovery import start_display_discovery
from .homee_enums import ATTRIBUTE_TYPES, NODE_PROFILES
from .modules import ModuleRegistry
from .runtime import Runtime
from .setup_portal import (
    automations_page as portal_automations,
    dashboard as portal_dashboard,
    displays_page as portal_displays,
    integrations_page as portal_integrations,
)

logging.basicConfig(level=os.getenv("SHB_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
VERSION = "0.12.0"
SETUP_SESSION_COOKIE = "shb_setup_session"
ENV_API_TOKEN = os.getenv("SHB_API_TOKEN", "").strip()
database = Database(os.getenv("SHB_DATA_DIR", "/data"))
registry = ModuleRegistry(os.getenv("SHB_MODULE_DIR", "/app/modules"))
runtime = Runtime(database, registry)
automation_engine = AutomationEngine(runtime, os.getenv("SHB_TIMEZONE", "Europe/Berlin"))
runtime.automation_engine = automation_engine


class IntegrationPayload(BaseModel):
    id: str = ""
    module_id: str
    name: str
    enabled: bool = True
    configuration: dict = Field(default_factory=dict)
    status: Optional[str] = None
    error: Optional[str] = None
    device_count: Optional[int] = 0


class AttributeCommand(BaseModel):
    value: float


class AutomationPayload(BaseModel):
    automations: list[dict] = Field(default_factory=list)


class ModbusProfilePayload(BaseModel):
    profile: dict


class ModuleActionPayload(BaseModel):
    payload: dict = Field(default_factory=dict)


class DisplayRegistrationPayload(BaseModel):
    device_id: str = Field(min_length=8, max_length=80, pattern=r"^[a-zA-Z0-9._:-]+$")
    name: str = Field(default="M5Paper", min_length=1, max_length=80)
    model: str = Field(default="M5Paper", min_length=1, max_length=80)
    firmware_version: str = Field(default="1.0.0", min_length=1, max_length=40)
    device_token: str = Field(default="", max_length=160)


class DisplayHeartbeatPayload(BaseModel):
    firmware_version: str = Field(default="", max_length=40)


class DisplayPairingPayload(BaseModel):
    pairing_code: str = Field(min_length=6, max_length=12)
    name: str = Field(default="", max_length=80)


class DisplayConfigurationPayload(BaseModel):
    configuration: dict = Field(default_factory=dict)


def effective_api_token():
    return ENV_API_TOKEN or str(database.setting("api_token", "")).strip()


def setup_session_value():
    """Return a verifier for the current API key without putting that key in the cookie."""
    token = effective_api_token()
    if not token:
        return ""
    return hmac.new(token.encode("utf-8"), b"SmartHomeBoard setup session v1", hashlib.sha256).hexdigest()


def setup_session_authenticated(request: Request):
    token = effective_api_token()
    if not token:
        return True
    cookie = request.cookies.get(SETUP_SESSION_COOKIE, "")
    return bool(cookie) and compare_digest(cookie, setup_session_value())


def setup_credentials_valid(request: Request, submitted_token=""):
    token = effective_api_token()
    return not token or setup_session_authenticated(request) or (
        bool(submitted_token) and compare_digest(submitted_token, token)
    )


def setup_response(content, status_code=200, authenticate=False):
    response = HTMLResponse(content, status_code=status_code)
    if authenticate and effective_api_token():
        # Ohne max_age/expires bleibt dies bewusst eine Browser-Sitzung.
        response.set_cookie(
            SETUP_SESSION_COOKIE,
            setup_session_value(),
            httponly=True,
            samesite="strict",
            secure=os.getenv("SHB_SETUP_SECURE_COOKIE") == "1",
            path="/setup",
        )
    return response


def authorize(connection: HTTPConnection, authorization: Optional[str] = Header(default=None)):
    # WebSockets authentifizieren sich direkt im Endpunkt über den Query-Token.
    # Eine Request-Abhängigkeit würde den WebSocket-Handshake vor dem Upgrade abbrechen.
    if connection.scope["type"] == "websocket":
        return
    if connection.url.path == "/" or connection.url.path.startswith("/setup"):
        return
    if connection.url.path == "/api/v1/displays/register" or connection.url.path.startswith("/api/v1/displays/device/"):
        return
    token = effective_api_token()
    if token and (not authorization or not compare_digest(authorization, f"Bearer {token}")):
        raise HTTPException(status_code=401, detail="API-Schlüssel ist ungültig")


@asynccontextmanager
async def lifespan(_app):
    discovery_transport = None
    server_id = str(database.setting("server_id", "")).strip()
    if not server_id:
        server_id = str(uuid.uuid4())
        database.set_setting("server_id", server_id)
    if os.getenv("SHB_DISABLE_DISPLAY_DISCOVERY") != "1":
        try:
            discovery_transport = await start_display_discovery(
                load_server_config()["port"], server_id, VERSION
            )
        except OSError as error:
            logging.getLogger("smarthomeboard.displays").warning(
                "M5Paper-Serversuche konnte nicht gestartet werden: %s", error
            )
    await runtime.start()
    await automation_engine.start()
    yield
    await automation_engine.stop()
    await runtime.shutdown()
    if discovery_transport:
        discovery_transport.close()


@asynccontextmanager
async def setup_lifespan(_app):
    registry.load()
    yield


app = FastAPI(title="SmartHomeBoard Server", version=VERSION, lifespan=lifespan, dependencies=[Depends(authorize)])
setup_app = FastAPI(title="SmartHomeBoard Einrichtung", version=VERSION, lifespan=setup_lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@setup_app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/setup")


@setup_app.get("/setup", response_class=HTMLResponse, include_in_schema=False)
async def setup_page(request: Request):
    return setup_html(authenticated=setup_session_authenticated(request))


@setup_app.get("/setup/integrations", response_class=HTMLResponse, include_in_schema=False)
async def setup_integrations_page(request: Request):
    selected_id = request.query_params.get("edit", "")
    protocol_filter = request.query_params.get("protocol_filter", "").strip().lower()
    homee_protocol = {}
    selected = database.integration(selected_id) if selected_id else None
    if selected and selected.get("module_id") == "homee" and selected.get("enabled"):
        try:
            homee_protocol = await load_homee_protocol(selected_id, protocol_filter)
        except (ValueError, httpx.HTTPError):
            homee_protocol = {}
    return portal_integrations(
        VERSION, registry.manifests(), database.integrations(),
        selected_module=request.query_params.get("module", ""),
        selected_id=selected_id,
        authenticated=setup_session_authenticated(request),
        token_required=bool(effective_api_token()),
        homee_protocol=homee_protocol,
        protocol_filter=protocol_filter,
    )


@setup_app.post("/setup/integrations/save", response_class=HTMLResponse, include_in_schema=False)
async def save_setup_integration(request: Request):
    values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    integration_id = _form_value(values, "integration_id")
    module_id = _form_value(values, "module_id")
    authenticated = setup_credentials_valid(request, _form_value(values, "current_token"))
    if effective_api_token() and not authenticated:
        return setup_response(portal_integrations(VERSION, registry.manifests(), database.integrations(), selected_module=module_id, selected_id=integration_id, error="Der API-Schlüssel ist nicht korrekt.", token_required=True), status_code=403)
    manifest = next((item for item in registry.manifests() if item["id"] == module_id), None)
    current = database.integration(integration_id) if integration_id else None
    if not manifest or (integration_id and not current):
        return setup_response(portal_integrations(VERSION, registry.manifests(), database.integrations(), error="Integration oder Servermodul wurde nicht gefunden.", authenticated=authenticated, token_required=bool(effective_api_token())), status_code=404, authenticate=authenticated)
    try:
        configuration = _integration_configuration(values, manifest, (current or {}).get("configuration", {}))
        payload = {"module_id": module_id, "name": _form_value(values, "name") or manifest["name"], "enabled": _form_value(values, "enabled") == "1", "configuration": configuration, "device_count": (current or {}).get("device_count", 0)}
        path = f"api/v1/integrations/{integration_id}" if integration_id else "api/v1/integrations"
        saved = await call_local_api(path, payload, method="PUT" if integration_id else "POST")
    except (ValueError, httpx.HTTPError) as action_error:
        return setup_response(portal_integrations(VERSION, registry.manifests(), database.integrations(), selected_module=module_id, selected_id=integration_id, error=str(action_error), authenticated=authenticated, token_required=bool(effective_api_token())), status_code=400, authenticate=authenticated)
    return setup_response(portal_integrations(VERSION, registry.manifests(), database.integrations(), selected_id=saved.get("id", integration_id), message="Die Integration wurde persistent gespeichert. Der Verbindungsaufbau läuft im Hintergrund.", authenticated=True, token_required=bool(effective_api_token())), authenticate=True)


@setup_app.post("/setup/integrations/test", response_class=HTMLResponse, include_in_schema=False)
async def test_setup_integration(request: Request):
    values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    integration_id = _form_value(values, "integration_id")
    authenticated = setup_credentials_valid(request, _form_value(values, "current_token"))
    if effective_api_token() and not authenticated:
        return setup_response(portal_integrations(VERSION, registry.manifests(), database.integrations(), selected_id=integration_id, error="Der API-Schlüssel ist nicht korrekt.", token_required=True), status_code=403)
    try:
        await call_local_api(f"api/v1/integrations/{integration_id}/test", {})
        message, error, status_code = "Die Verbindung wurde erfolgreich getestet.", "", 200
    except (ValueError, httpx.HTTPError) as action_error:
        message, error, status_code = "", str(action_error), 502
    return setup_response(portal_integrations(VERSION, registry.manifests(), database.integrations(), selected_id=integration_id, message=message, error=error, authenticated=True, token_required=bool(effective_api_token())), status_code=status_code, authenticate=True)


@setup_app.post("/setup/integrations/action", response_class=HTMLResponse, include_in_schema=False)
async def perform_setup_integration_action(request: Request):
    values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    integration_id = _form_value(values, "integration_id")
    action_id = _form_value(values, "action_id")
    authenticated = setup_credentials_valid(request, _form_value(values, "current_token"))
    integration = database.integration(integration_id)
    manifest = next(
        (item for item in registry.manifests() if integration and item["id"] == integration.get("module_id")),
        None,
    )
    allowed = {str(item.get("id", "")) for item in (manifest or {}).get("actions", [])}
    if not integration or not manifest or action_id not in allowed:
        return setup_response(
            portal_integrations(
                VERSION, registry.manifests(), database.integrations(), selected_id=integration_id,
                error="Integration oder Modulaktion wurde nicht gefunden.",
                authenticated=authenticated, token_required=bool(effective_api_token()),
            ),
            status_code=404, authenticate=authenticated,
        )
    if effective_api_token() and not authenticated:
        return setup_response(
            portal_integrations(
                VERSION, registry.manifests(), database.integrations(), selected_id=integration_id,
                error="Der API-Schlüssel ist nicht korrekt.", token_required=True,
            ),
            status_code=403,
        )
    message, error, status_code = "", "", 200
    try:
        response = await call_local_api(f"api/v1/integrations/{integration_id}/actions/{action_id}", {"payload": {}})
        result = response.get("result", {}) if isinstance(response, dict) else {}
        message = str(result.get("message") or "Die Modulaktion wurde ausgeführt.")
    except (ValueError, httpx.HTTPError) as action_error:
        error, status_code = str(action_error), 502
    return setup_response(
        portal_integrations(
            VERSION, registry.manifests(), database.integrations(), selected_id=integration_id,
            message=message, error=error, authenticated=True, token_required=bool(effective_api_token()),
        ),
        status_code=status_code, authenticate=True,
    )


@setup_app.post("/setup/integrations/homee/send", response_class=HTMLResponse, include_in_schema=False)
async def send_setup_homee_websocket(request: Request):
    values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    integration_id = _form_value(values, "integration_id")
    command = _form_value(values, "command")
    authenticated = setup_credentials_valid(request, _form_value(values, "current_token"))
    integration = database.integration(integration_id)
    if not integration or integration.get("module_id") != "homee":
        return setup_response(portal_integrations(VERSION, registry.manifests(), database.integrations(), error="Die homee-Integration wurde nicht gefunden."), status_code=404)
    if effective_api_token() and not authenticated:
        return setup_response(portal_integrations(VERSION, registry.manifests(), database.integrations(), selected_id=integration_id, error="Der API-Schlüssel ist nicht korrekt.", token_required=True), status_code=403)
    message, error, status_code = "", "", 200
    try:
        await call_local_api(
            f"api/v1/integrations/{integration_id}/actions/send_websocket",
            {"payload": {"command": command}},
        )
        message = f"WebSocket-Nachricht gesendet: {command}"
    except (ValueError, httpx.HTTPError) as action_error:
        error, status_code = str(action_error), 502
    try:
        protocol = await load_homee_protocol(integration_id, "")
    except (ValueError, httpx.HTTPError):
        protocol = {}
    return setup_response(
        portal_integrations(
            VERSION, registry.manifests(), database.integrations(), selected_id=integration_id,
            message=message, error=error, authenticated=True,
            token_required=bool(effective_api_token()), homee_protocol=protocol,
        ),
        status_code=status_code, authenticate=True,
    )


@setup_app.get("/setup/integrations/homee/protocol", response_class=JSONResponse, include_in_schema=False)
async def setup_homee_protocol_feed(request: Request):
    integration_id = request.query_params.get("integration_id", "").strip()
    category = request.query_params.get("category", "").strip().lower()
    try:
        limit = max(1, min(100, int(request.query_params.get("limit", "100"))))
    except ValueError:
        limit = 100
    integration = database.integration(integration_id)
    if not integration or integration.get("module_id") != "homee":
        return JSONResponse({"messages": [], "error": "Die homee-Integration wurde nicht gefunden."}, status_code=404)
    try:
        protocol = await load_homee_protocol(integration_id, category, limit)
    except (ValueError, httpx.HTTPError) as action_error:
        return JSONResponse({"messages": [], "error": str(action_error)}, status_code=502)
    return JSONResponse({
        "messages": protocol.get("messages", []) if isinstance(protocol, dict) else [],
        "categories": protocol.get("categories", []) if isinstance(protocol, dict) else [],
        "category": category,
    })


@setup_app.post("/setup/integrations/delete", response_class=HTMLResponse, include_in_schema=False)
async def delete_setup_integration(request: Request):
    values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    integration_id = _form_value(values, "integration_id")
    authenticated = setup_credentials_valid(request, _form_value(values, "current_token"))
    if effective_api_token() and not authenticated:
        return setup_response(portal_integrations(VERSION, registry.manifests(), database.integrations(), selected_id=integration_id, error="Der API-Schlüssel ist nicht korrekt.", token_required=True), status_code=403)
    try:
        await call_local_api(f"api/v1/integrations/{integration_id}", method="DELETE")
    except (ValueError, httpx.HTTPError) as action_error:
        return setup_response(portal_integrations(VERSION, registry.manifests(), database.integrations(), selected_id=integration_id, error=str(action_error), authenticated=True, token_required=bool(effective_api_token())), status_code=502, authenticate=True)
    return setup_response(portal_integrations(VERSION, registry.manifests(), database.integrations(), message="Die Integration und ihre gespeicherten Geräte wurden gelöscht.", authenticated=True, token_required=bool(effective_api_token())), authenticate=True)


@setup_app.get("/setup/displays", response_class=HTMLResponse, include_in_schema=False)
async def setup_displays_page(request: Request):
    return portal_displays(VERSION, database.displays(), database.nodes(), selected_id=request.query_params.get("display", ""), authenticated=setup_session_authenticated(request), token_required=bool(effective_api_token()))


@setup_app.post("/setup/displays/save", response_class=HTMLResponse, include_in_schema=False)
async def save_setup_display(request: Request):
    values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    display_id = _form_value(values, "display_id")
    authenticated = setup_credentials_valid(request, _form_value(values, "current_token"))
    if effective_api_token() and not authenticated:
        return setup_response(portal_displays(VERSION, database.displays(), database.nodes(), selected_id=display_id, error="Der API-Schlüssel ist nicht korrekt.", token_required=True), status_code=403)
    display = database.display(display_id)
    if not display:
        return setup_response(portal_displays(VERSION, database.displays(), database.nodes(), error="Das Display wurde nicht gefunden.", authenticated=authenticated, token_required=bool(effective_api_token())), status_code=404, authenticate=authenticated)
    try:
        sleep_minutes = max(1, min(1440, int(_form_value(values, "sleep_minutes") or 5)))
        widgets = _display_widgets(values)
    except ValueError as action_error:
        return setup_response(portal_displays(VERSION, database.displays(), database.nodes(), selected_id=display_id, error=str(action_error), authenticated=authenticated, token_required=bool(effective_api_token())), status_code=400, authenticate=authenticated)
    configuration = dict(display.get("configuration", {}))
    configuration.update({"title": _form_value(values, "title") or "SmartHomeBoard", "layout": "grid" if _form_value(values, "layout") == "grid" else "list", "sleep_minutes": sleep_minutes, "widgets": widgets})
    database.save_display_configuration(display_id, configuration)
    database.rename_display(display_id, _form_value(values, "name") or display["name"])
    return setup_response(portal_displays(VERSION, database.displays(), database.nodes(), selected_id=display_id, message="Die E-Paper-Konfiguration wurde persistent gespeichert.", authenticated=True, token_required=bool(effective_api_token())), authenticate=True)


@setup_app.post("/setup/displays/delete", response_class=HTMLResponse, include_in_schema=False)
async def delete_setup_display(request: Request):
    values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    display_id = _form_value(values, "display_id")
    authenticated = setup_credentials_valid(request, _form_value(values, "current_token"))
    if effective_api_token() and not authenticated:
        return setup_response(portal_displays(VERSION, database.displays(), database.nodes(), selected_id=display_id, error="Der API-Schlüssel ist nicht korrekt.", token_required=True), status_code=403)
    if not database.delete_display(display_id):
        return setup_response(portal_displays(VERSION, database.displays(), database.nodes(), error="Das Display wurde nicht gefunden.", authenticated=authenticated, token_required=bool(effective_api_token())), status_code=404, authenticate=authenticated)
    return setup_response(portal_displays(VERSION, database.displays(), database.nodes(), message="Das Display wurde entfernt und kann erneut gekoppelt werden.", authenticated=True, token_required=bool(effective_api_token())), authenticate=True)


@setup_app.get("/setup/automations", response_class=HTMLResponse, include_in_schema=False)
async def setup_automations_page(request: Request):
    return portal_automations(VERSION, automation_engine.status(), authenticated=setup_session_authenticated(request), token_required=bool(effective_api_token()))


@setup_app.post("/setup", response_class=HTMLResponse, include_in_schema=False)
async def update_setup(request: Request):
    values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    current = values.get("current_token", [""])[0].strip()
    new_token = values.get("new_token", [""])[0].strip()
    confirmation = values.get("confirm_token", [""])[0].strip()
    requested_port = values.get("server_port", [str(load_server_config()["port"])])[0].strip()
    configured = bool(effective_api_token())
    authenticated = setup_credentials_valid(request, current)
    if ENV_API_TOKEN:
        return setup_response(setup_html(error="Der API-Schlüssel wird noch durch SHB_API_TOKEN in Docker Compose vorgegeben. Entferne dort die Variable und starte den Container neu.", authenticated=authenticated), status_code=409, authenticate=authenticated)
    if configured and not authenticated:
        return setup_response(setup_html(error="Der bisherige API-Schlüssel ist nicht korrekt."), status_code=403)
    if not configured and len(new_token) < 16:
        return setup_response(setup_html(error="Der neue API-Schlüssel muss mindestens 16 Zeichen lang sein.", authenticated=authenticated), status_code=400, authenticate=authenticated)
    if configured and new_token and len(new_token) < 16:
        return setup_response(setup_html(error="Der neue API-Schlüssel muss mindestens 16 Zeichen lang sein.", authenticated=authenticated), status_code=400, authenticate=authenticated)
    if new_token != confirmation:
        return setup_response(setup_html(error="Die beiden neuen API-Schlüssel stimmen nicht überein.", authenticated=authenticated), status_code=400, authenticate=authenticated)
    try:
        port = int(requested_port)
        if not 1024 <= port <= 65535 or port == SETUP_PORT:
            raise ValueError
    except ValueError:
        return setup_response(setup_html(error=f"Der Kommunikationsport muss zwischen 1024 und 65535 liegen und darf nicht {SETUP_PORT} sein.", authenticated=authenticated), status_code=400, authenticate=authenticated)
    old_port = load_server_config()["port"]
    if new_token:
        database.set_setting("api_token", new_token)
    save_server_config({"port": port})
    port_changed = port != old_port
    message = "Die Servereinstellungen wurden gespeichert."
    if port_changed:
        message += f" Der Container startet jetzt neu und ist danach auf Port {port} erreichbar."
        if os.getenv("SHB_DISABLE_SELF_RESTART") != "1":
            asyncio.get_running_loop().call_later(2.0, lambda: os._exit(0))
    return setup_response(
        setup_html(message=message, revealed_token=new_token, restart_port=port if port_changed else None, authenticated=True),
        authenticate=True,
    )


@setup_app.get("/setup/modbus", response_class=HTMLResponse, include_in_schema=False)
async def modbus_templates_page(request: Request):
    return modbus_templates_html(
        selected_id=request.query_params.get("profile", ""),
        authenticated=setup_session_authenticated(request),
    )


@setup_app.post("/setup/modbus", response_class=HTMLResponse, include_in_schema=False)
async def save_modbus_template(request: Request):
    values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    current_token = values.get("current_token", [""])[0].strip()
    source = values.get("profile_json", [""])[0]
    token = effective_api_token()
    authenticated = setup_credentials_valid(request, current_token)
    if token and not authenticated:
        return setup_response(modbus_templates_html(source=source, error="Der API-Schlüssel ist nicht korrekt."), status_code=403)
    try:
        profile = validate_modbus_profile(json.loads(source))
        if profile["id"] in built_in_modbus_profile_ids():
            raise ValueError("Die ID gehört zu einem mitgelieferten Profil. Bitte für die eigene Kopie eine neue ID vergeben.")
        write_custom_modbus_profile(profile)
    except (json.JSONDecodeError, TypeError, ValueError, OSError) as error:
        return setup_response(modbus_templates_html(source=source, error=str(error), authenticated=authenticated), status_code=400, authenticate=authenticated)
    message = f"Das eigene Profil „{profile['manufacturer']} · {profile['model']}“ wurde gespeichert. Der Container startet neu."
    if os.getenv("SHB_DISABLE_SELF_RESTART") != "1":
        asyncio.get_running_loop().call_later(2.0, lambda: os._exit(0))
    return setup_response(modbus_templates_html(selected_id=profile["id"], message=message, authenticated=True), authenticate=True)


@setup_app.post("/setup/automations/test", response_class=HTMLResponse, include_in_schema=False)
async def test_automation_from_setup(request: Request):
    values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    current_token = values.get("current_token", [""])[0].strip()
    rule_id = values.get("rule_id", [""])[0].strip()
    token = effective_api_token()
    authenticated = setup_credentials_valid(request, current_token)
    if token and not authenticated:
        return setup_response(portal_automations(VERSION, automation_engine.status(), error="Der API-Schlüssel für den Automationstest ist nicht korrekt.", token_required=True), status_code=403)
    result = await automation_engine.test(rule_id)
    if not result["ok"]:
        return setup_response(portal_automations(VERSION, automation_engine.status(), error=result["message"], authenticated=authenticated, token_required=bool(effective_api_token())), status_code=409, authenticate=authenticated)
    return setup_response(portal_automations(VERSION, automation_engine.status(), message=result["message"], authenticated=True, token_required=bool(effective_api_token())), authenticate=True)


@setup_app.post("/setup/displays/pair", response_class=HTMLResponse, include_in_schema=False)
async def pair_display_from_setup(request: Request):
    values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    display_id = values.get("display_id", [""])[0].strip()
    pairing_code = values.get("pairing_code", [""])[0].strip()
    name = values.get("name", [""])[0].strip()
    current_token = values.get("current_token", [""])[0].strip()
    authenticated = setup_credentials_valid(request, current_token)
    if effective_api_token() and not authenticated:
        return setup_response(
            portal_displays(VERSION, database.displays(), database.nodes(), selected_id=display_id, error="Der API-Schlüssel für die Displaykopplung ist nicht korrekt.", token_required=True),
            status_code=403,
        )
    display = database.display(display_id)
    if not display:
        return setup_response(
            portal_displays(VERSION, database.displays(), database.nodes(), error="Das M5Paper wurde nicht gefunden.", authenticated=authenticated, token_required=bool(effective_api_token())),
            status_code=404,
            authenticate=authenticated,
        )
    if display["status"] != "pending":
        return setup_response(
            portal_displays(VERSION, database.displays(), database.nodes(), selected_id=display_id, error="Das M5Paper ist bereits gekoppelt.", authenticated=authenticated, token_required=bool(effective_api_token())),
            status_code=409,
            authenticate=authenticated,
        )
    if not compare_digest(display["pairing_code"], pairing_code):
        return setup_response(
            portal_displays(VERSION, database.displays(), database.nodes(), selected_id=display_id, error="Der Kopplungscode ist nicht korrekt.", authenticated=authenticated, token_required=bool(effective_api_token())),
            status_code=403,
            authenticate=authenticated,
        )
    saved = database.pair_display(display_id, name or display["name"])
    return setup_response(
        portal_displays(VERSION, database.displays(), database.nodes(), selected_id=display_id, message=f"M5Paper „{saved['name']}“ wurde gekoppelt.", authenticated=True, token_required=bool(effective_api_token())),
        authenticate=True,
    )


@setup_app.get("/setup/enocean", response_class=HTMLResponse, include_in_schema=False)
async def enocean_setup_page(request: Request):
    return enocean_setup_html(
        selected_integration=request.query_params.get("integration", ""),
        authenticated=setup_session_authenticated(request),
    )


@setup_app.get("/setup/enocean/status", response_class=JSONResponse, include_in_schema=False)
async def enocean_setup_status(request: Request):
    integration_id = request.query_params.get("integration_id", "").strip()
    integration = database.integration(integration_id)
    if not integration or integration.get("module_id") != "enocean":
        return JSONResponse({"error": "Die EnOcean-Integration wurde nicht gefunden."}, status_code=404)
    state = database.setting(f"module_state:{integration_id}", {}) or {}
    devices = state.get("devices", {}) if isinstance(state, dict) else {}
    learning_until = float(state.get("learning_until", 0) or 0) if isinstance(state, dict) else 0
    learning_seconds = max(0, int(learning_until - time.time()))
    selected_profile = str(state.get("learning_profile_id") or state.get("learning_eep", "") or "")
    revision_source = json.dumps(devices, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return JSONResponse({
        "learning": learning_seconds > 0,
        "learning_seconds": learning_seconds,
        "selected_profile": selected_profile,
        "device_count": len(devices),
        "device_revision": hashlib.sha256(revision_source.encode("utf-8")).hexdigest(),
        "devices_html": enocean_devices_html(devices, integration_id),
    })


@setup_app.post("/setup/enocean/action", response_class=HTMLResponse, include_in_schema=False)
async def enocean_setup_action(request: Request):
    values = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    integration_id = values.get("integration_id", [""])[0].strip()
    action_id = values.get("action_id", [""])[0].strip()
    current_token = values.get("current_token", [""])[0].strip()
    integration = database.integration(integration_id)
    if not integration or integration.get("module_id") != "enocean":
        return HTMLResponse(enocean_setup_html(error="Die EnOcean-Integration wurde nicht gefunden."), status_code=404)
    authenticated = setup_credentials_valid(request, current_token)
    if effective_api_token() and not authenticated:
        return setup_response(enocean_setup_html(selected_integration=integration_id, error="Der API-Schlüssel ist nicht korrekt."), status_code=403)
    allowed = {"start_learning", "stop_learning", "update_device", "delete_device"}
    if action_id not in allowed:
        return HTMLResponse(enocean_setup_html(selected_integration=integration_id, error="Unbekannte EnOcean-Aktion."), status_code=400)
    payload = {
        "sender_id": values.get("sender_id", [""])[0].strip(),
        "name": values.get("name", [""])[0].strip(),
        "eep": values.get("eep", [""])[0].strip(),
    }
    try:
        await call_local_api(f"api/v1/integrations/{integration_id}/actions/{action_id}", {"payload": payload})
    except (httpx.HTTPError, ValueError) as error:
        return setup_response(enocean_setup_html(selected_integration=integration_id, error=str(error), authenticated=authenticated), status_code=502, authenticate=authenticated)
    messages = {
        "start_learning": "Der EnOcean-Anlernmodus läuft jetzt 60 Sekunden.",
        "stop_learning": "Der EnOcean-Anlernmodus wurde beendet.",
        "update_device": "Name und Geräteprofil wurden gespeichert.",
        "delete_device": "Das EnOcean-Gerät wurde dauerhaft gelöscht.",
    }
    return setup_response(enocean_setup_html(selected_integration=integration_id, message=messages[action_id], authenticated=True), authenticate=True)


def _form_value(values, key, default=""):
    return values.get(key, [default])[0].strip()


def _integration_configuration(values, manifest, previous):
    result = dict(previous)
    for field in manifest.get("fields", []):
        key, kind = field["key"], field.get("type", "text")
        raw = _form_value(values, f"config__{key}")
        if kind == "password" and not raw and key in previous:
            continue
        if field.get("required") and not raw:
            raise ValueError(f"{field.get('title', key)} darf nicht leer sein")
        if kind in ("port", "integer", "duration"):
            value = int(float(raw or field.get("default", 0)))
        elif kind == "number":
            value = float(raw or field.get("default", 0))
        else:
            value = raw
        if "minimum" in field and isinstance(value, (int, float)) and value < field["minimum"]:
            raise ValueError(f"{field.get('title', key)} muss mindestens {field['minimum']} sein")
        if "maximum" in field and isinstance(value, (int, float)) and value > field["maximum"]:
            raise ValueError(f"{field.get('title', key)} darf höchstens {field['maximum']} sein")
        result[key] = value
    return result


def _display_widgets(values):
    sources = values.get("widget_source", [])
    labels = values.get("widget_label", [])
    decimals = values.get("widget_decimals", [])
    widget_ids = values.get("widget_id", [])
    widgets = []
    for index, source in enumerate(sources[:8]):
        source = source.strip()
        if not source:
            continue
        try:
            node_id, attribute_id = (int(part) for part in source.split(":", 1))
            precision = max(0, min(3, int(decimals[index] if index < len(decimals) else 1)))
        except (TypeError, ValueError):
            raise ValueError("Eine Wertzuordnung des Displays ist ungültig")
        widgets.append({
            "id": (widget_ids[index].strip() if index < len(widget_ids) else "") or str(uuid.uuid4()),
            "label": labels[index].strip() if index < len(labels) else "",
            "node_id": node_id,
            "attribute_id": attribute_id,
            "decimals": precision,
        })
    return widgets


async def call_local_api(path, payload=None, method="POST"):
    headers = {"Authorization": f"Bearer {effective_api_token()}"} if effective_api_token() else {}
    url = f"http://127.0.0.1:{load_server_config()['port']}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        response = await client.request(method, url, headers=headers, json=payload)
    if response.is_error:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        detail = detail or f"HTTP {response.status_code} ohne Fehlertext"
        raise ValueError(f"Serveraktion fehlgeschlagen: {detail}")
    return response.json()


async def load_homee_protocol(integration_id, category="", limit=50):
    response = await call_local_api(
        f"api/v1/integrations/{integration_id}/actions/protocol_log",
        {"payload": {"category": category, "limit": limit}},
    )
    result = response.get("result", {}) if isinstance(response, dict) else {}
    return result if isinstance(result, dict) else {}


def enocean_profiles():
    path = Path(os.getenv("SHB_MODULE_DIR", "/app/modules")) / "enocean" / "profiles.json"
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
        return records if isinstance(records, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def enocean_devices_html(devices, integration_id):
    device_cards = []
    for sender_id, device in sorted(devices.items(), key=lambda item: str(item[1].get("name", item[0])).casefold()):
        last_seen = float(device.get("last_seen", 0) or 0)
        last_text = dt.datetime.fromtimestamp(last_seen).strftime("%d.%m.%Y %H:%M:%S") if last_seen else "Noch kein Datentelegramm"
        rssi = device.get("rssi")
        device_cards.append(f'''
<article class="device"><form method="post" action="/setup/enocean/action" class="token-form">
<input type="hidden" name="current_token"><input type="hidden" name="integration_id" value="{escape(integration_id)}"><input type="hidden" name="action_id" value="update_device"><input type="hidden" name="sender_id" value="{escape(sender_id)}">
<div class="device-head"><div><b>{escape(str(device.get('name') or sender_id))}</b><small>Sender {escape(sender_id)} · {escape(str(rssi if rssi is not None else '?'))} dBm</small></div><code>{escape(str(device.get('eep', 'Unbekannt')))}</code></div>
<div class="device-grid"><label>Anzeigename<input name="name" value="{escape(str(device.get('name', '')))}" maxlength="120"></label><label>Geräteprofil<input name="eep" list="eepProfiles" value="{escape(str(device.get('profile_id') or device.get('eep', '')))}" required></label></div>
<small>Zuletzt empfangen: {escape(last_text)} · Rohdaten: {escape(str(device.get('raw', '—')))}</small><div class="buttons"><button type="submit">Änderungen speichern</button></div></form>
<form method="post" action="/setup/enocean/action" class="token-form delete-form"><input type="hidden" name="current_token"><input type="hidden" name="integration_id" value="{escape(integration_id)}"><input type="hidden" name="action_id" value="delete_device"><input type="hidden" name="sender_id" value="{escape(sender_id)}"><button class="danger" type="submit">Gerät löschen</button></form></article>''')
    return "".join(device_cards) or '<p class="muted">Noch keine Geräte angelernt. Anlernmodus starten und anschließend den Sensor oder Taster betätigen.</p>'


def enocean_setup_html(selected_integration="", message="", error="", authenticated=False):
    integrations = [item for item in database.integrations() if item.get("module_id") == "enocean"]
    selected = next((item for item in integrations if item["id"] == selected_integration), integrations[0] if integrations else None)
    profiles = enocean_profiles()
    state = database.setting(f"module_state:{selected['id']}", {}) if selected else {}
    devices = (state or {}).get("devices", {})
    learning_until = float((state or {}).get("learning_until", 0) or 0)
    learning_seconds = max(0, int(learning_until - time.time()))
    notice = f'<div class="success"><b>Erledigt</b><br>{escape(message)}</div>' if message else ""
    warning = f'<div class="error"><b>Fehler</b><br>{escape(error)}</div>' if error else ""
    if effective_api_token() and not authenticated:
        token_input = '<label>API-Schlüssel<input id="apiToken" type="password" autocomplete="current-password" placeholder="Einmalig für diese Browsersitzung"></label>'
    elif effective_api_token():
        token_input = '<input id="apiToken" type="hidden" value=""><p class="success">Browsersitzung ist freigeschaltet.</p>'
    else:
        token_input = '<input id="apiToken" type="hidden" value="">'
    integration_options = "".join(
        f'<option value="{escape(item["id"])}" {"selected" if selected and item["id"] == selected["id"] else ""}>{escape(item["name"])}</option>'
        for item in integrations
    )
    profile_options = "".join(f'<option value="{escape(item["id"])}">{escape(item["name"])}</option>' for item in profiles)
    selected_learning_eep = str((state or {}).get("learning_profile_id") or (state or {}).get("learning_eep", "") or "")
    learning_profile_options = '<option value="">Profil auswählen …</option>' + "".join(
        f'<option value="{escape(item["id"])}" data-search="{escape(" ".join(str(item.get(key, "")) for key in ("id", "eep", "name", "category", "examples")).casefold())}" {"selected" if item["id"] == selected_learning_eep else ""}>{escape(item["id"])} · {escape(item["name"])} · {escape(item.get("examples", ""))}</option>'
        for item in profiles
    )
    selected_id = selected["id"] if selected else ""
    devices_html = enocean_devices_html(devices, selected_id)
    profile_rows = "".join(
        f'''<tr data-search="{escape(' '.join(str(item.get(key, '')) for key in ('id','name','category','examples')).casefold())}"><td><code>{escape(item['id'])}</code></td><td><b>{escape(item.get('name',''))}</b><small>{escape(item.get('examples',''))}</small></td><td>{escape(item.get('category',''))}</td><td>{'Empfang + Senden' if item.get('direction') == 'both' else ('Senden' if item.get('direction') == 'tx' else 'Empfang')}</td><td><span class="badge {escape(item.get('support','catalog'))}">{'Dekodiert' if item.get('support') == 'decoded' else ('Rohdaten' if item.get('support') == 'raw' else 'Katalog')}</span></td></tr>'''
        for item in profiles
    )
    integration_selector = (
        f'''<label style="margin-top:12px">EnOcean-Integration<select onchange="location.href='/setup/enocean?integration='+encodeURIComponent(this.value)">{integration_options}</select></label>'''
        if integrations else '<p class="error">Noch keine EnOcean-Integration in der App angelegt.</p>'
    )
    return f'''<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>EnOcean · SmartHomeBoard</title><style>
:root{{--bg:#edf2f7;--card:#fff;--text:#172033;--muted:#657086;--accent:#1677ff;--ok:#16834b;--bad:#b42318;--line:#d8e0ea}}@media(prefers-color-scheme:dark){{:root{{--bg:#0c1421;--card:#151f2e;--text:#eef4ff;--muted:#9cabc2;--line:#2b3a50}}}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:16px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}main{{max-width:1120px;margin:auto;padding:24px 18px 60px}}header,.device-head,.buttons{{display:flex;align-items:center;justify-content:space-between;gap:12px}}h1{{font-size:26px;margin:0}}h2{{font-size:19px;margin:0 0 14px}}.card,.device{{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:18px;margin-bottom:16px;box-shadow:0 8px 25px #0000000a}}.devices{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}}.device{{margin:0;display:grid;gap:12px}}.device form{{display:grid;gap:12px}}.device-grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}label{{display:grid;gap:7px;font-weight:650}}input,select{{width:100%;border:1px solid var(--line);border-radius:11px;padding:11px;font:inherit;color:var(--text);background:var(--bg)}}button{{border:0;border-radius:11px;padding:12px 16px;background:var(--accent);color:white;font-weight:750;cursor:pointer}}button.secondary{{background:#64748b}}button.danger{{background:#b42318;width:100%}}small,.muted{{display:block;color:var(--muted);margin-top:4px}}code{{font-family:ui-monospace,SFMono-Regular,monospace}}.actions{{display:flex;gap:10px;flex-wrap:wrap}}.success,.error{{padding:13px;border-radius:12px;margin-bottom:14px}}.success{{background:#16a34a20;color:var(--ok)}}.error{{background:#ef444420;color:var(--bad)}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:11px 8px;border-bottom:1px solid var(--line);vertical-align:top}}.badge{{display:inline-block;padding:4px 8px;border-radius:999px;background:#64748b22}}.badge.decoded{{color:var(--ok);background:#16a34a20}}.badge.raw{{color:#b36b00;background:#f59e0b20}}a{{color:var(--accent);font-weight:700;text-decoration:none}}@media(max-width:760px){{.devices,.device-grid{{grid-template-columns:1fr}}table{{font-size:13px}}th:nth-child(3),td:nth-child(3),th:nth-child(4),td:nth-child(4){{display:none}}}}
</style></head><body><main><header><div><h1>EnOcean-Geräte</h1><div class="muted">Anlernen, benennen, Profil zuordnen und entfernen</div></div><a href="/setup">← Servereinstellungen</a></header>{notice}{warning}
<section class="card"><h2>Integration</h2>{token_input}{integration_selector}</section>
<section id="enoceanLive" data-integration-id="{escape(selected_id)}" data-device-revision="{hashlib.sha256(json.dumps(devices, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()}"><div class="card"><h2>Neues Gerät anlernen</h2><p class="muted">Zuerst das Geräteprofil wählen, dann den Lernmodus starten und genau das gewünschte Gerät betätigen. Der erste neue Sender mit passender Telegrammfamilie wird gespeichert; danach endet der Lernmodus automatisch. Bereits gespeicherte Sender-IDs sind gesperrt.</p><form method="post" action="/setup/enocean/action" class="token-form"><input type="hidden" name="current_token"><input type="hidden" name="integration_id" value="{escape(selected_id)}"><input type="hidden" name="action_id" value="start_learning"><label>Profil suchen<input id="learningProfileSearch" type="search" placeholder="z. B. FT55, Einfachwippe, Doppelwippe …" autocomplete="off"></label><label>EnOcean-Profil<select id="learningProfileSelect" name="eep" required>{learning_profile_options}</select><small>FT55 Einfachwippe legt „Wippe 1“ an; FT55 Doppelwippe legt „Wippe 1“ und „Wippe 2“ als getrennte Instanzen an.</small></label><label>Anzeigename (optional)<input name="name" maxlength="120" placeholder="z. B. Taster Wohnzimmer"></label><button type="submit" {'disabled' if not selected else ''}>Anlernen starten</button></form><div class="actions" style="margin-top:12px"><form method="post" action="/setup/enocean/action" class="token-form"><input type="hidden" name="current_token"><input type="hidden" name="integration_id" value="{escape(selected_id)}"><input type="hidden" name="action_id" value="stop_learning"><button class="secondary" type="submit" {'disabled' if not selected else ''}>Anlernen beenden</button></form></div><p><b id="enoceanLearningStatus">{f'Anlernmodus aktiv · {escape(selected_learning_eep)} · noch etwa {learning_seconds} Sekunden' if learning_seconds else 'Anlernmodus nicht aktiv'}</b></p></div>
<section><h2>Angelernte Geräte · <span id="enoceanDeviceCount">{len(devices)}</span></h2><datalist id="eepProfiles">{profile_options}</datalist><div id="enoceanDevices" class="devices">{devices_html}</div></section></section>
<section class="card" style="margin-top:20px"><h2>Verfügbare Geräteprofile · {len(profiles)}</h2><p class="muted">„Dekodiert“ liefert bereits passende Dashboard-Attribute. „Rohdaten“ legt das Gerät an und zeigt Telegramme zur weiteren Profilentwicklung. „Katalog“ ist vorbereitet, benötigt aber insbesondere bei Aktoren noch die sichere Sende- und Anlernlogik.</p><label>Profil oder Eltako-Gerät suchen<input id="profileSearch" type="search" placeholder="z. B. Fenstergriff, FT55, Rauchmelder, FSB14 …"></label><div style="overflow:auto"><table><thead><tr><th>EEP</th><th>Profil und Beispiele</th><th>Kategorie</th><th>Richtung</th><th>Stand</th></tr></thead><tbody id="profileRows">{profile_rows}</tbody></table></div></section>
</main><script>const token=document.getElementById('apiToken');function bindTokenForms(root=document){{root.querySelectorAll('.token-form:not([data-bound])').forEach(form=>{{form.dataset.bound='1';form.addEventListener('submit',event=>{{form.querySelector('[name=current_token]').value=token?.value||'';if(form.classList.contains('delete-form')&&!confirm('Dieses EnOcean-Gerät wirklich dauerhaft löschen?'))event.preventDefault();}});}});}}bindTokenForms();const search=document.getElementById('profileSearch');search?.addEventListener('input',()=>{{const q=search.value.trim().toLocaleLowerCase();document.querySelectorAll('#profileRows tr').forEach(row=>row.hidden=q&&!row.dataset.search.includes(q));}});const learningSearch=document.getElementById('learningProfileSearch');const learningSelect=document.getElementById('learningProfileSelect');const learningOptions=learningSelect?[...learningSelect.options].map(option=>({{value:option.value,text:option.textContent,search:option.dataset.search||'',selected:option.selected}})):[];function filterLearningProfiles(){{if(!learningSelect)return;const q=learningSearch.value.trim().toLocaleLowerCase();const selected=learningSelect.value;learningSelect.replaceChildren();learningOptions.filter(option=>!option.value||!q||option.search.includes(q)||option.value===selected).forEach(item=>{{const option=document.createElement('option');option.value=item.value;option.textContent=item.text;option.dataset.search=item.search;option.selected=item.value===(selected||learningOptions.find(entry=>entry.selected)?.value||'');learningSelect.append(option);}});}}learningSearch?.addEventListener('input',filterLearningProfiles);const live=document.getElementById('enoceanLive');let enoceanPollTimer=null;async function refreshEnOceanStatus(){{if(!live?.dataset.integrationId||document.hidden)return scheduleEnOceanPoll(2000);try{{const query=new URLSearchParams({{integration_id:live.dataset.integrationId}});const response=await fetch('/setup/enocean/status?'+query,{{cache:'no-store'}});if(!response.ok)throw new Error('HTTP '+response.status);const data=await response.json();const status=document.getElementById('enoceanLearningStatus');status.textContent=data.learning?'Anlernmodus aktiv · '+data.selected_profile+' · noch etwa '+data.learning_seconds+' Sekunden':'Anlernmodus nicht aktiv';document.getElementById('enoceanDeviceCount').textContent=String(data.device_count);if(data.device_revision!==live.dataset.deviceRevision){{const devices=document.getElementById('enoceanDevices');devices.innerHTML=data.devices_html;live.dataset.deviceRevision=data.device_revision;bindTokenForms(devices);}}scheduleEnOceanPoll(data.learning?1000:4000);}}catch(_){{scheduleEnOceanPoll(4000);}}}}function scheduleEnOceanPoll(delay){{clearTimeout(enoceanPollTimer);enoceanPollTimer=setTimeout(refreshEnOceanStatus,delay);}}document.addEventListener('visibilitychange',()=>{{if(!document.hidden)refreshEnOceanStatus();}});scheduleEnOceanPoll({1000 if selected else 4000});</script></body></html>'''


def modbus_profile_records():
    directories = [
        (Path(os.getenv("SHB_MODULE_DIR", "/app/modules")) / "modbus" / "profiles", False),
        (Path(os.getenv("SHB_DATA_DIR", "/data")) / "modbus-profiles", True),
    ]
    records = {}
    for directory, custom in directories:
        directory.mkdir(parents=True, exist_ok=True)
        for path in sorted(directory.glob("*.json")):
            try:
                profile = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(profile, dict) and profile.get("id"):
                    records[profile["id"]] = {"profile": profile, "custom": custom}
            except (OSError, json.JSONDecodeError):
                continue
    return sorted(records.values(), key=lambda item: (item["profile"].get("manufacturer", ""), item["profile"].get("model", "")))


def built_in_modbus_profile_ids():
    directory = Path(os.getenv("SHB_MODULE_DIR", "/app/modules")) / "modbus" / "profiles"
    result = set()
    for path in directory.glob("*.json"):
        try:
            result.add(json.loads(path.read_text(encoding="utf-8"))["id"])
        except (OSError, KeyError, json.JSONDecodeError):
            continue
    return result


def custom_modbus_profile_path(profile_id):
    return Path(os.getenv("SHB_DATA_DIR", "/data")) / "modbus-profiles" / f"{profile_id}.json"


def write_custom_modbus_profile(profile):
    path = custom_modbus_profile_path(profile["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def validate_modbus_profile(profile):
    if not isinstance(profile, dict):
        raise ValueError("Das Template muss ein JSON-Objekt sein.")
    profile_id = str(profile.get("id", "")).strip()
    if not re.fullmatch(r"[a-zA-Z0-9._-]{3,80}", profile_id):
        raise ValueError("Die Profil-ID muss 3–80 Zeichen lang sein und darf nur Buchstaben, Zahlen, Punkt, Unterstrich und Bindestrich enthalten.")
    manufacturer = str(profile.get("manufacturer", "")).strip()
    model = str(profile.get("model", "")).strip()
    if not manufacturer or not model:
        raise ValueError("Hersteller und Modell müssen angegeben werden.")
    registers = profile.get("registers")
    if not isinstance(registers, list):
        raise ValueError("registers muss eine JSON-Liste sein.")
    allowed_types = {"int16", "uint16", "int32", "uint32", "float32", "int64", "uint64", "float64"}
    for index, mapping in enumerate(registers, start=1):
        if not isinstance(mapping, dict):
            raise ValueError(f"Register {index} ist kein JSON-Objekt.")
        try:
            address = int(mapping["address"])
            attribute_type = int(mapping["attribute_type"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"Register {index}: address und attribute_type müssen Ganzzahlen sein.")
        if not 0 <= address <= 65535 or attribute_type < 0:
            raise ValueError(f"Register {index}: Adresse oder Attributtyp liegt außerhalb des gültigen Bereichs.")
        if mapping.get("register_type", "holding") not in {"holding", "input"}:
            raise ValueError(f"Register {index}: register_type muss holding oder input sein.")
        if mapping.get("data_type", "uint16") not in allowed_types:
            raise ValueError(f"Register {index}: unbekannter data_type.")
        if mapping.get("word_order", "bigEndian") not in {"bigEndian", "swappedWords"}:
            raise ValueError(f"Register {index}: word_order muss bigEndian oder swappedWords sein.")
        if mapping.get("writable", False) and mapping.get("register_type", "holding") != "holding":
            raise ValueError(f"Register {index}: Ein schreibbares Register muss vom Typ holding sein.")
        try:
            if float(mapping.get("scale", 1)) == 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError(f"Register {index}: scale muss eine Zahl ungleich 0 sein.")
        if not str(mapping.get("name", "")).strip():
            raise ValueError(f"Register {index}: name fehlt.")
    profile["id"], profile["manufacturer"], profile["model"] = profile_id, manufacturer, model
    return profile


def modbus_templates_html(selected_id="", source=None, message="", error="", authenticated=False):
    records = modbus_profile_records()
    selected = next((item for item in records if item["profile"]["id"] == selected_id), None)
    if source is None:
        profile = selected["profile"] if selected else {
            "id": "mein-hersteller.mein-geraet.v1",
            "manufacturer": "Mein Hersteller",
            "model": "Mein Modbus-Gerät",
            "default_unit_id": 1,
            "minimum_poll_seconds": 5,
            "node_profile": 0,
            "icon": "server.rack",
            "configuration_hint": "Modbus TCP am Gerät aktivieren.",
            "registers": [
                {"id": "power", "address": 100, "register_type": "holding", "data_type": "int32", "word_order": "bigEndian", "scale": 1, "offset": 0, "attribute_type": 3, "name": "Leistung", "unit": "W", "writable": False}
            ]
        }
        source = json.dumps(profile, ensure_ascii=False, indent=2)
    profile_options = '<option value="">Neues Template</option>' + "".join(
        f'''<option value="{escape(item['profile']['id'])}" {'selected' if item['profile']['id'] == selected_id else ''}>{escape(item['profile'].get('manufacturer', ''))} · {escape(item['profile'].get('model', ''))} · {len(item['profile'].get('registers', []))} Register</option>'''
        for item in records
    )
    node_profiles_json = json.dumps([
        {"id": int(profile_id), "name": item["label"].replace("Node Profile ", "")}
        for profile_id, item in NODE_PROFILES.items()
    ], ensure_ascii=False)
    attribute_types_json = json.dumps([
        {"id": int(attribute_id), "name": item["label"].replace("Attribute Type ", "")}
        for attribute_id, item in ATTRIBUTE_TYPES.items()
    ], ensure_ascii=False)
    token_field = '<label>API-Schlüssel<input name="current_token" type="password" autocomplete="current-password" required placeholder="Einmalig für diese Browsersitzung"></label>' if effective_api_token() and not authenticated else ""
    notice = f'<div class="success"><b>Gespeichert</b><br>{escape(message)}</div>' if message else ""
    warning = f'<div class="error"><b>Nicht gespeichert</b><br>{escape(error)}</div>' if error else ""
    return f'''<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Modbus-Templates · SmartHomeBoard</title><style>
:root{{--bg:#edf2f7;--card:#fff;--text:#172033;--muted:#657086;--accent:#1677ff;--ok:#16834b;--bad:#b42318;--line:#d8e0ea}}
@media(prefers-color-scheme:dark){{:root{{--bg:#0c1421;--card:#151f2e;--text:#eef4ff;--muted:#9cabc2;--line:#2b3a50}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:16px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}main{{max-width:1100px;margin:auto;padding:24px 18px 60px}}a{{color:inherit}}header{{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:22px}}h1{{font-size:25px;margin:0}}h2{{font-size:18px;margin:0 0 14px}}.back{{text-decoration:none;color:var(--accent);font-weight:700}}.card{{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:18px;margin-bottom:18px;box-shadow:0 8px 25px #0000000a}}.muted{{color:var(--muted)}}form,.mapping-grid{{display:grid;gap:14px}}label{{display:grid;gap:7px;font-weight:650}}input,select,textarea{{width:100%;border:1px solid var(--line);border-radius:11px;padding:12px;font:14px ui-monospace,SFMono-Regular,monospace;color:var(--text);background:var(--bg)}}textarea{{min-height:520px;resize:vertical;line-height:1.45}}button{{border:0;border-radius:11px;padding:13px 17px;background:var(--accent);color:white;font-weight:750;font-size:15px;cursor:pointer}}.success,.error{{border-radius:12px;padding:13px;margin-bottom:14px}}.success{{background:#16a34a20;color:var(--ok)}}.error{{background:#ef444420;color:var(--bad)}}.mapping{{padding:12px;border:1px solid var(--line);border-radius:12px}}code{{font-family:ui-monospace,SFMono-Regular,monospace;overflow-wrap:anywhere}}@media(max-width:700px){{header{{align-items:flex-start;flex-direction:column}}textarea{{min-height:430px}}}}
</style></head><body><main><header><div><h1>Modbus-Templates</h1><div class="muted">Vorhandene Profile vergleichen und eigene Geräteprofile anlegen</div></div><a class="back" href="/setup">← Servereinstellungen</a></header>
<section class="card"><h2>Vorhandene Templates</h2><p class="muted">Profil auswählen und als Grundlage für ein eigenes Template verwenden. Für eine eigene Variante anschließend mindestens die <code>id</code> ändern.</p><label>Verfügbares Profil<input id="templateProfileSearch" type="search" placeholder="Template suchen …"><select id="templateProfileSelect" onchange="location.href=this.value ? '/setup/modbus?profile='+encodeURIComponent(this.value) : '/setup/modbus'">{profile_options}</select></label></section>
<section class="card"><h2>Eigenes Template</h2>{notice}{warning}<p class="muted">Erlaubte Datentypen: int16, uint16, int32, uint32, float32, int64, uint64 und float64. Registertypen: holding oder input. Eigene Profile werden persistent unter <code>/data/modbus-profiles</code> gespeichert.</p>
<form method="post" action="/setup/modbus">{token_field}<div class="mapping-grid"><label>Nodeprofil<input id="nodeProfileSearch" type="search" placeholder="Nodeprofil oder ID suchen …"><select id="nodeProfile"></select></label><div id="attributeMappings"></div></div><label>Profil als JSON<textarea id="profileJson" name="profile_json" required spellcheck="false">{escape(source)}</textarea></label><button type="submit">Eigenes Template prüfen und speichern</button></form></section>
</main><script>
const nodeProfiles={node_profiles_json};
const attributeTypes={attribute_types_json};
const editor=document.getElementById('profileJson');
const nodeSelect=document.getElementById('nodeProfile');
const nodeSearch=document.getElementById('nodeProfileSearch');
const mappings=document.getElementById('attributeMappings');
const option=(item,current)=>`<option value="${{item.id}}" ${{Number(current)===item.id?'selected':''}}>${{item.name}} · ID ${{item.id}}</option>`;
function fillSelect(select,items,current,query=''){{
  const normalized=query.trim().toLocaleLowerCase();
  let visible=normalized ? items.filter(item=>item.name.toLocaleLowerCase().includes(normalized)||String(item.id).includes(normalized)) : [...items];
  const selected=items.find(item=>item.id===Number(current))||{{id:Number(current),name:'Unbekannt'}};
  if(!visible.some(item=>item.id===selected.id)) visible.unshift(selected);
  select.innerHTML=visible.length ? visible.map(item=>option(item,current)).join('') : '<option disabled>Keine Treffer</option>';
}}
function readProfile(){{try{{return JSON.parse(editor.value)}}catch(error){{return null}}}}
function writeProfile(profile){{editor.value=JSON.stringify(profile,null,2);renderMappings()}}
function renderMappings(){{
  const profile=readProfile(); if(!profile){{nodeSelect.innerHTML='<option>JSON zuerst korrigieren</option>';mappings.innerHTML='';return}}
  fillSelect(nodeSelect,nodeProfiles,profile.node_profile??0,nodeSearch.value);
  mappings.innerHTML='';
  (profile.registers||[]).forEach((register,index)=>{{
    const box=document.createElement('label'); box.className='mapping';
    box.textContent=`${{register.name||register.id||'Register'}} · Register ${{register.address??'?'}}`;
    const search=document.createElement('input'); search.type='search'; search.placeholder='Attributtyp oder ID suchen …';
    const select=document.createElement('select'); fillSelect(select,attributeTypes,register.attribute_type??0);
    search.addEventListener('input',()=>fillSelect(select,attributeTypes,register.attribute_type??0,search.value));
    select.addEventListener('change',()=>{{const current=readProfile();current.registers[index].attribute_type=Number(select.value);writeProfile(current)}});
    box.appendChild(search); box.appendChild(select); mappings.appendChild(box);
  }});
}}
nodeSelect.addEventListener('change',()=>{{const profile=readProfile();if(profile){{profile.node_profile=Number(nodeSelect.value);writeProfile(profile)}}}});
nodeSearch.addEventListener('input',()=>{{const profile=readProfile();if(profile)fillSelect(nodeSelect,nodeProfiles,profile.node_profile??0,nodeSearch.value)}});
const templateSelect=document.getElementById('templateProfileSelect');
const templateSearch=document.getElementById('templateProfileSearch');
const templateOptions=[...templateSelect.options].map(item=>({{value:item.value,text:item.text,selected:item.selected}}));
templateSearch.addEventListener('input',()=>{{
  const query=templateSearch.value.trim().toLocaleLowerCase();
  const current=templateSelect.value;
  const visible=templateOptions.filter(item=>!item.value||!query||item.text.toLocaleLowerCase().includes(query));
  templateSelect.innerHTML=visible.map(item=>`<option value="${{item.value}}">${{item.text}}</option>`).join('');
  if(visible.some(item=>item.value===current)) templateSelect.value=current;
}});
editor.addEventListener('input',renderMappings); renderMappings();
</script></body></html>'''


def setup_html(message="", error="", revealed_token="", restart_port=None, authenticated=False):
    configured = bool(effective_api_token())
    current_port = load_server_config()["port"]
    suggested = "" if configured else secrets.token_urlsafe(32)
    integrations = database.integrations()
    nodes = database.nodes()
    displays = database.displays()
    automation_status = automation_engine.status()
    return portal_dashboard(
        VERSION, len(registry.modules), integrations, len(nodes), displays,
        automation_status["count"], current_port, SETUP_PORT, configured,
        authenticated, bool(ENV_API_TOKEN), suggested, message, error,
        revealed_token, restart_port,
    )
    automation_cards = "".join(
        f'''<div class="module"><span><b>{escape(item['name'])}</b><small>{'Aktiv' if item['enabled'] else 'Deaktiviert'} · {item['trigger_count']} Auslöser · {item['action_count']} Aktionen</small></span><small>{'Zuletzt ' + dt.datetime.fromtimestamp(item['last_triggered_at']).strftime('%d.%m. %H:%M') if item.get('last_triggered_at') else 'Noch nicht ausgelöst'}</small></div>'''
        for item in automation_status["automations"]
    ) or '<p class="muted">Noch keine Automationen von der App übertragen.</p>'
    automation_options = "".join(
        f'''<option value="{escape(item['id'])}">{escape(item['name'])}</option>'''
        for item in automation_status["automations"]
    )
    automation_test_token = '<label>API-Schlüssel<input name="current_token" type="password" autocomplete="current-password" required></label>' if effective_api_token() and not authenticated else ""
    automation_events = "".join(
        f'''<div class="event {escape(item['level'])}"><time>{dt.datetime.fromtimestamp(item['timestamp']).strftime('%d.%m. %H:%M:%S')}</time><span><b>{escape(item['rule_name'])}</b><small>{escape(item['message'])}</small></span></div>'''
        for item in automation_status.get("recent_events", [])[:12]
    ) or '<p class="muted">Noch keine Ausführung protokolliert.</p>'
    display_token_field = '<label>API-Schlüssel<input name="current_token" type="password" autocomplete="current-password" required></label>' if effective_api_token() and not authenticated else ""
    display_card_items = []
    for item in displays:
        if item["status"] == "pending":
            display_action = f'''<form class="pair-form" method="post" action="/setup/displays/pair"><input type="hidden" name="display_id" value="{escape(item['id'])}">{display_token_field}<label>Name<input name="name" value="{escape(item['name'])}" maxlength="80" required></label><label>Kopplungscode<input name="pairing_code" inputmode="numeric" pattern="[0-9]{{6}}" minlength="6" maxlength="6" placeholder="Code vom M5Paper" required></label><button type="submit">M5Paper koppeln</button></form>'''
        else:
            display_action = f'''<span class="paired">Gekoppelt · Konfiguration {item['configuration_version']}</span>'''
        display_card_items.append(
            f'''<div class="display-card"><div><b>{escape(item['name'])}</b><small>{escape(item['model'])} · {escape(item['ip_address'] or 'IP unbekannt')} · Firmware {escape(item['firmware_version'])}</small><small>ID: {escape(item['id'])} · zuletzt {escape(item['last_seen'])}</small></div>{display_action}</div>'''
        )
    display_cards = "".join(display_card_items) or '<p class="muted">Noch kein M5Paper im Netzwerk registriert. Nach der WLAN-Einrichtung erscheint es hier automatisch.</p>'
    module_cards = "".join(
        f'<div class="module"><span>{escape(item.get("name", item["id"]))}</span><small>v{escape(item.get("version", "1.0"))}</small></div>'
        for item in registry.manifests()
    ) or '<p class="muted">Module werden beim Serverstart geladen.</p>'
    notice = f'<div class="success"><b>Gespeichert</b><br>{escape(message)}</div>' if message else ""
    warning = f'<div class="error"><b>Nicht gespeichert</b><br>{escape(error)}</div>' if error else ""
    token_box = f'''<div class="token-result"><b>Diesen Schlüssel jetzt kopieren:</b><code id="resultToken">{escape(revealed_token)}</code><button type="button" onclick="copyToken('resultToken')">Kopieren</button></div>''' if revealed_token else ""
    current_field = '''<label>Bisheriger API-Schlüssel<input name="current_token" type="password" autocomplete="current-password" required placeholder="Einmalig für diese Browsersitzung"></label>''' if configured and not authenticated else ""
    title = "Servereinstellungen" if configured else "Server erstmals einrichten"
    environment_note = '<div class="error">Der Schlüssel wird durch die Docker-Umgebungsvariable <code>SHB_API_TOKEN</code> fest vorgegeben und kann hier nicht geändert werden.</div>' if ENV_API_TOKEN else ""
    token_required = "" if configured else "required"
    token_help = "Leer lassen, wenn der vorhandene Schlüssel beibehalten werden soll." if configured else "Dieser Schlüssel wird für die Verbindung mit der App benötigt."
    restart_notice = f'<div class="success"><b>Neuer Kommunikationsport: {restart_port}</b><br>Der Container startet neu. Diese Einrichtungsseite bleibt unter Port {SETUP_PORT} erreichbar.</div>' if restart_port else ""
    return f'''<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SmartHomeBoard Server</title><style>
:root{{--bg:#edf2f7;--card:#fff;--text:#172033;--muted:#657086;--accent:#1677ff;--ok:#16834b;--bad:#b42318;--line:#d8e0ea}}
@media(prefers-color-scheme:dark){{:root{{--bg:#0c1421;--card:#151f2e;--text:#eef4ff;--muted:#9cabc2;--line:#2b3a50}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:16px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
main{{max-width:920px;margin:auto;padding:28px 18px 60px}}header{{display:flex;gap:14px;align-items:center;margin-bottom:24px}}.logo{{width:54px;height:54px;border-radius:16px;background:linear-gradient(145deg,#38bdf8,#2563eb);display:grid;place-items:center;color:white;font-size:25px;box-shadow:0 10px 30px #1677ff40}}h1{{font-size:25px;margin:0}}h2{{font-size:18px;margin:0 0 16px}}p{{line-height:1.5}}.muted,small{{color:var(--muted)}}.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:18px}}.stat,.card{{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:18px;box-shadow:0 8px 25px #0000000a}}.stat b{{display:block;font-size:26px}}.card{{margin-bottom:18px}}form{{display:grid;gap:14px}}label{{display:grid;gap:7px;font-weight:600}}input,select{{width:100%;border:1px solid var(--line);border-radius:11px;padding:12px 13px;font:inherit;color:var(--text);background:var(--bg)}}button{{border:0;border-radius:11px;padding:12px 16px;background:var(--accent);color:#fff;font-weight:700;font-size:15px;cursor:pointer}}.secondary{{background:#64748b}}.success,.error,.token-result{{border-radius:12px;padding:13px;margin-bottom:14px}}.success{{background:#16a34a20;color:var(--ok)}}.error{{background:#ef444420;color:var(--bad)}}.token-result{{background:#1677ff18;display:grid;gap:10px}}code{{font-family:ui-monospace,SFMono-Regular,monospace;overflow-wrap:anywhere}}.token-result code{{display:block;background:var(--bg);padding:12px;border-radius:9px;user-select:all}}.modules{{display:grid;grid-template-columns:repeat(2,1fr);gap:8px}}.module{{border:1px solid var(--line);border-radius:11px;padding:11px;display:flex;justify-content:space-between;gap:12px}}.module span,.module small{{display:block}}.display-card{{border:1px solid var(--line);border-radius:14px;padding:14px;margin-top:10px;display:grid;gap:13px}}.display-card small{{display:block;margin-top:4px}}.pair-form{{grid-template-columns:1fr 1fr auto;align-items:end}}.pair-form>label{{min-width:0}}.paired{{color:var(--ok);font-weight:700}}details.protocol{{margin-top:16px}}details.protocol summary{{cursor:pointer;font-weight:700;color:var(--accent)}}.events{{display:grid;gap:7px;margin-top:12px;max-height:390px;overflow:auto;padding-right:5px}}.event{{display:grid;grid-template-columns:130px 1fr;gap:12px;padding:10px;border-left:4px solid #64748b;background:var(--bg);border-radius:8px}}.event.success{{border-left-color:#16a34a}}.event.warning{{border-left-color:#f59e0b}}.event.error{{border-left-color:#ef4444}}.event small{{display:block;margin-top:3px}}@media(max-width:720px){{.grid{{grid-template-columns:repeat(2,1fr)}}.pair-form{{grid-template-columns:1fr}}}}@media(max-width:620px){{.grid{{grid-template-columns:1fr}}.modules{{grid-template-columns:1fr}}.event{{grid-template-columns:1fr}}main{{padding-top:18px}}}}
</style></head><body><main>
<header><div class="logo">⌂</div><div><h1>SmartHomeBoard Server</h1><div class="muted">Version {VERSION} · lokale Laufzeitumgebung</div></div></header>
<section class="grid"><div class="stat"><b>{len(registry.modules)}</b><span class="muted">Module</span></div><div class="stat"><b>{len(integrations)}</b><span class="muted">Integrationen</span></div><div class="stat"><b>{len(nodes)}</b><span class="muted">Geräte</span></div><div class="stat"><b>{len(displays)}</b><span class="muted">M5Paper</span></div></section>
<section class="card"><h2>{title}</h2>{notice}{warning}{environment_note}{restart_notice}{token_box}
<p class="muted">Der Setup-Port ist fest auf <b>{SETUP_PORT}</b> eingestellt. Der Kommunikationsport der App und der API kann hier frei vergeben werden. Beide Einstellungen bleiben bei Updates erhalten.</p>
<form method="post" action="/setup">{current_field}
<label>Kommunikationsport der App<input name="server_port" type="number" inputmode="numeric" required min="1024" max="65535" value="{current_port}"><small>Port {SETUP_PORT} ist reserviert. Nach einer Änderung startet der Container automatisch neu.</small></label>
<label>Neuer API-Schlüssel<input id="newToken" name="new_token" type="password" autocomplete="new-password" {token_required} minlength="16" value="{escape(suggested)}"><small>{token_help}</small></label>
<label>Neuen Schlüssel wiederholen<input id="confirmToken" name="confirm_token" type="password" autocomplete="new-password" {token_required} minlength="16" value="{escape(suggested)}"></label>
<button type="submit">Servereinstellungen speichern</button></form></section>
<section class="card"><h2>M5Paper-Displays · {len(displays)}</h2><p class="muted">Neu registrierte Displays hier mit dem sechsstelligen Code koppeln, der auf dem M5Paper angezeigt wird.</p>{display_cards}</section>
<section class="card"><h2>Geladene Module</h2><div class="modules">{module_cards}</div></section>
<section class="card"><h2>Server-Automationen · {automation_status['count']}</h2><p class="muted">Diese Regeln sind dauerhaft im Container gespeichert und werden auch ohne geöffnete App ausgeführt.</p><div class="modules">{automation_cards}</div>
<h2 style="margin-top:22px">Automation testen</h2><p class="muted">Der Test führt die Aktionen wirklich aus. UND-Bedingungen werden wie beim Play-Button der App geprüft; die Mindestpause wird für den manuellen Test ignoriert.</p><form method="post" action="/setup/automations/test">{automation_test_token}<label>Automation<select name="rule_id" required>{automation_options}</select></label><button type="submit" {'disabled' if not automation_options else ''}>Ausgewählte Automation testen</button></form>
<details class="protocol"><summary>Ausführungsprotokoll · letzte 12 Einträge</summary><p class="muted">Der Server speichert höchstens 50 Einträge. Hier werden nur die neuesten 12 angezeigt.</p><a href="/setup"><button type="button" class="secondary">Protokoll aktualisieren</button></a><div class="events">{automation_events}</div></details></section>
<section class="card"><h2>Modbus-Geräteprofile</h2><p>Mitgelieferte Modbus-Templates ansehen, miteinander vergleichen oder ein eigenes Profil nach demselben Muster erstellen.</p><a href="/setup/modbus"><button type="button">Modbus-Templates verwalten</button></a></section>
<section class="card"><h2>EnOcean-Geräte</h2><p>USB300-Anlernmodus starten, erkannte Geräte benennen, EEP-Profile zuordnen oder Geräte dauerhaft entfernen.</p><a href="/setup/enocean"><button type="button">EnOcean verwalten</button></a></section>
<section class="card"><h2>Nächster Schritt</h2><p>Öffne in der App <b>Einstellungen → Lokaler Server</b>, trage <code>http://IP-DES-PI:{restart_port or current_port}</code> und den API-Schlüssel ein und aktiviere den Servermodus.</p></section>
</main><script>function copyToken(id){{navigator.clipboard.writeText(document.getElementById(id).textContent);}}</script></body></html>'''


def display_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def authenticated_display(display_id: str, token: str):
    display = database.display(display_id, include_credentials=True)
    if not display:
        raise HTTPException(status_code=404, detail="Display wurde nicht gefunden")
    if not token or not compare_digest(display["device_token_hash"], display_token_hash(token)):
        raise HTTPException(status_code=401, detail="Display-Token ist ungültig")
    return display


def resolved_display_render(configuration: dict):
    widgets = []
    nodes_by_id = {int(node.get("id", 0)): node for node in database.nodes()}
    for source in configuration.get("widgets", [])[:8]:
        try:
            node_id = int(source.get("node_id", 0))
            attribute_id = int(source.get("attribute_id", 0))
        except (TypeError, ValueError):
            node_id, attribute_id = 0, 0
        node = nodes_by_id.get(node_id, {})
        attribute = next(
            (item for item in node.get("attributes", []) if int(item.get("id", 0)) == attribute_id),
            {},
        )
        resolved_unit = decoded_homee_text(source.get("unit") or attribute.get("unit") or "").strip()
        if resolved_unit.casefold() == "text":
            text_value = decoded_homee_text(attribute.get("data")).strip()
            available = bool(text_value)
            formatted = text_value if available else "--"
            resolved_unit = ""
        else:
            raw_value = attribute.get("current_value")
            available = isinstance(raw_value, (int, float))
            decimals = max(0, min(3, int(source.get("decimals", 1))))
            formatted = f"{float(raw_value):.{decimals}f}" if available else "--"
            if available and decimals > 0:
                formatted = formatted.rstrip("0").rstrip(".")
        widgets.append({
            "id": str(source.get("id", f"{node_id}-{attribute_id}")),
            "label": str(source.get("label")) if source.get("label") else decoded_homee_text(attribute.get("name") or node.get("name") or "Wert"),
            "value": formatted,
            "unit": resolved_unit,
            "available": available,
        })
    sleep_minutes = configuration.get("sleep_minutes", 5)
    try:
        sleep_minutes = max(1, min(1440, int(sleep_minutes)))
    except (TypeError, ValueError):
        sleep_minutes = 5
    return {
        "title": str(configuration.get("title") or "SmartHomeBoard"),
        "layout": "grid" if configuration.get("layout") == "grid" else "list",
        "sleep_minutes": sleep_minutes,
        "widgets": widgets,
        "generated_at": display_generated_at(),
    }


def display_generated_at(now=None):
    try:
        timezone = ZoneInfo(os.getenv("SHB_TIMEZONE", "Europe/Berlin"))
    except ZoneInfoNotFoundError:
        timezone = dt.timezone.utc
    moment = now or dt.datetime.now(dt.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(timezone).isoformat(timespec="minutes")


def decoded_homee_text(value):
    return unquote(str(value or ""))


@app.post("/api/v1/displays/register")
async def register_display(payload: DisplayRegistrationPayload, request: Request):
    existing = database.display(payload.device_id, include_credentials=True)
    issued_token = ""
    if existing:
        if not payload.device_token or not compare_digest(
            existing["device_token_hash"], display_token_hash(payload.device_token)
        ):
            raise HTTPException(status_code=401, detail="Dieses Display ist bereits registriert")
        database.touch_display(
            payload.device_id,
            request.client.host if request.client else "",
            payload.firmware_version,
        )
    else:
        issued_token = secrets.token_urlsafe(32)
        database.register_display({
            "id": payload.device_id,
            "name": payload.name,
            "model": payload.model,
            "firmware_version": payload.firmware_version,
            "ip_address": request.client.host if request.client else "",
            "pairing_code": f"{secrets.randbelow(1_000_000):06d}",
            "device_token_hash": display_token_hash(issued_token),
        })
    display = database.display(payload.device_id)
    return {
        "device_id": display["id"],
        "status": display["status"],
        "pairing_code": display["pairing_code"],
        "device_token": issued_token,
        "configuration_version": display["configuration_version"],
        "poll_seconds": 60,
    }


@app.get("/api/v1/displays/device/{display_id}/configuration")
async def display_configuration(display_id: str, x_display_token: Optional[str] = Header(default=None)):
    display = authenticated_display(display_id, x_display_token or "")
    database.touch_display(display_id)
    return {
        "status": display["status"],
        "configuration_version": display["configuration_version"],
        "configuration": display["configuration"] if display["status"] == "paired" else {},
        "render": resolved_display_render(display["configuration"]) if display["status"] == "paired" else {},
    }


@app.post("/api/v1/displays/device/{display_id}/heartbeat")
async def display_heartbeat(
    display_id: str,
    payload: DisplayHeartbeatPayload,
    request: Request,
    x_display_token: Optional[str] = Header(default=None),
):
    authenticated_display(display_id, x_display_token or "")
    database.touch_display(
        display_id,
        request.client.host if request.client else "",
        payload.firmware_version,
    )
    return {"ok": True}


@app.get("/api/v1/displays")
async def displays():
    return {"displays": database.displays()}


@app.post("/api/v1/displays/{display_id}/pair")
async def pair_display(display_id: str, payload: DisplayPairingPayload):
    display = database.display(display_id)
    if not display:
        raise HTTPException(status_code=404, detail="Display wurde nicht gefunden")
    if display["status"] != "pending":
        raise HTTPException(status_code=409, detail="Display ist bereits gekoppelt")
    if not compare_digest(display["pairing_code"], payload.pairing_code.strip()):
        raise HTTPException(status_code=403, detail="Kopplungscode ist nicht korrekt")
    name = payload.name.strip() or display["name"]
    return database.pair_display(display_id, name)


@app.put("/api/v1/displays/{display_id}/configuration")
async def update_display_configuration(display_id: str, payload: DisplayConfigurationPayload):
    if not database.display(display_id):
        raise HTTPException(status_code=404, detail="Display wurde nicht gefunden")
    return database.save_display_configuration(display_id, payload.configuration)


@app.delete("/api/v1/displays/{display_id}")
async def delete_display(display_id: str):
    if not database.delete_display(display_id):
        raise HTTPException(status_code=404, detail="Display wurde nicht gefunden")
    return {"ok": True}


@app.get("/api/v1/health")
async def health():
    return {"status": "ok", "version": VERSION, "modules": len(registry.modules), "instances": len(database.integrations()), "nodes": len(database.nodes()), "displays": len(database.displays())}


@app.get("/api/v1/modules")
async def modules():
    return {"modules": registry.manifests()}


@app.get("/api/v1/modbus/profiles")
async def modbus_profiles():
    profiles = []
    for record in modbus_profile_records():
        profile = json.loads(json.dumps(record["profile"]))
        profile.setdefault("default_unit_id", 1)
        profile.setdefault("minimum_poll_seconds", 2)
        profile.setdefault("node_profile", 0)
        for index, mapping in enumerate(profile.get("registers", []), start=1):
            mapping.setdefault("id", f"register-{mapping.get('address', index)}-{index}")
            mapping.setdefault("register_type", "holding")
            mapping.setdefault("data_type", "uint16")
            mapping.setdefault("word_order", "bigEndian")
            mapping.setdefault("scale", 1)
            mapping.setdefault("offset", 0)
            mapping.setdefault("writable", False)
            mapping.setdefault("unavailable_value_policy", "none")
        profiles.append(profile)
    custom_ids = [item["profile"]["id"] for item in modbus_profile_records() if item["custom"]]
    return {"profiles": profiles, "custom_profile_ids": custom_ids}


@app.post("/api/v1/modbus/profiles")
async def save_modbus_profile_api(payload: ModbusProfilePayload):
    try:
        profile = validate_modbus_profile(payload.profile)
        if profile["id"] in built_in_modbus_profile_ids():
            raise ValueError("Ein mitgeliefertes Profil kann nicht überschrieben werden. Bitte eine eigene Profil-ID verwenden.")
        write_custom_modbus_profile(profile)
    except (TypeError, ValueError, OSError) as error:
        raise HTTPException(status_code=400, detail=str(error))
    await reload_modbus_runtime()
    return {"profile": profile}


@app.delete("/api/v1/modbus/profiles/{profile_id}")
async def delete_modbus_profile_api(profile_id: str):
    if not re.fullmatch(r"[a-zA-Z0-9._-]{3,80}", profile_id):
        raise HTTPException(status_code=400, detail="Die Profil-ID ist ungültig")
    if any(item["module_id"] == "modbus-tcp" and item.get("configuration", {}).get("profile") == profile_id for item in database.integrations()):
        raise HTTPException(status_code=409, detail="Das Profil wird noch von einer Serverintegration verwendet")
    path = custom_modbus_profile_path(profile_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Das eigene Modbus-Profil wurde nicht gefunden")
    path.unlink()
    await reload_modbus_runtime()
    return {"ok": True}


async def reload_modbus_runtime():
    registry.load()
    for integration in database.integrations():
        if integration["module_id"] == "modbus-tcp":
            await runtime.restart_instance(integration)


@app.post("/api/v1/modules/reload")
async def reload_modules():
    return {"modules": registry.load()}


@app.get("/api/v1/integrations")
async def integrations():
    return {"integrations": database.integrations()}


@app.get("/api/v1/integrations/{integration_id}/state")
async def integration_state(integration_id: str):
    if not database.integration(integration_id):
        raise HTTPException(status_code=404, detail="Integration wurde nicht gefunden")
    return {"state": database.setting(f"module_state:{integration_id}", {})}


@app.post("/api/v1/integrations")
async def create_integration(payload: IntegrationPayload):
    item = payload.model_dump()
    item["id"] = str(uuid.uuid4())
    if item["module_id"] not in registry.modules:
        raise HTTPException(status_code=400, detail="Das gewählte Servermodul ist nicht installiert")
    saved = database.save_integration(item)
    runtime.schedule_restart(saved)
    return database.integration(saved["id"])


@app.put("/api/v1/integrations/{integration_id}")
async def update_integration(integration_id: str, payload: IntegrationPayload):
    if not database.integration(integration_id):
        raise HTTPException(status_code=404, detail="Integration wurde nicht gefunden")
    item = payload.model_dump()
    item["id"] = integration_id
    saved = database.save_integration(item)
    runtime.schedule_restart(saved)
    return database.integration(integration_id)


@app.delete("/api/v1/integrations/{integration_id}")
async def delete_integration(integration_id: str):
    await runtime.cancel_scheduled_restart(integration_id)
    await runtime.stop_instance(integration_id)
    database.delete_integration(integration_id)
    await runtime.broadcast_snapshot()
    return {"ok": True}


@app.post("/api/v1/integrations/{integration_id}/test")
async def test_integration(integration_id: str):
    item = database.integration(integration_id)
    if not item:
        raise HTTPException(status_code=404, detail="Integration wurde nicht gefunden")
    try:
        await runtime.test_instance(item)
        database.set_integration_state(integration_id, "Verbindung erfolgreich", None)
    except Exception as error:
        database.set_integration_state(integration_id, "Fehler", str(error))
        raise HTTPException(status_code=502, detail=str(error))
    return database.integration(integration_id)


@app.post("/api/v1/integrations/{integration_id}/actions/{action_id}")
async def perform_integration_action(integration_id: str, action_id: str, command: ModuleActionPayload = ModuleActionPayload()):
    if not database.integration(integration_id):
        raise HTTPException(status_code=404, detail="Integration wurde nicht gefunden")
    try:
        result = await runtime.integration_action(integration_id, action_id, command.payload)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        raise HTTPException(status_code=502, detail=str(error))
    return {"ok": True, "result": result}


@app.get("/api/v1/nodes")
async def nodes():
    return {"sequence": runtime.sequence, "nodes": database.nodes()}


@app.put("/api/v1/nodes/{node_id}/attributes/{attribute_id}")
async def set_attribute(node_id: int, attribute_id: int, command: AttributeCommand):
    try:
        await runtime.set_value(node_id, attribute_id, command.value)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error))
    except Exception as error:
        logging.getLogger("smarthomeboard.command").warning("Gerätebefehl fehlgeschlagen: %s", error)
        raise HTTPException(status_code=502, detail=str(error))
    return {"ok": True}


@app.put("/api/v1/automations")
async def save_automations(payload: AutomationPayload):
    automation_engine.replace(payload.automations)
    return {"ok": True}


@app.get("/api/v1/automations")
async def automations():
    return {"automations": automation_engine.rules}


@app.get("/api/v1/automations/status")
async def automation_status():
    return automation_engine.status()


@app.post("/api/v1/automations/{rule_id}/test")
async def test_automation(rule_id: str):
    result = await automation_engine.test(rule_id)
    if not result["ok"]:
        raise HTTPException(status_code=409, detail=result["message"])
    return result


@app.websocket("/api/v1/events")
async def events(websocket: WebSocket, token: str = ""):
    configured_token = effective_api_token()
    if configured_token and not compare_digest(token, configured_token):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    runtime.websockets.add(websocket)
    await websocket.send_json({"type": "snapshot", "sequence": runtime.sequence, "nodes": database.nodes()})
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        runtime.websockets.discard(websocket)
