"""Exercise both authenticated transports against the real homee adapter.

The only fake is the in-memory WebSocket peer; no homee connection is opened.
"""

import importlib.util
import json
import tempfile
import unittest
import uuid
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from server.database import Database
from server.homeegrams import HomeegramAccess
from server.mcp_access import MCPAccess


MODULE_PATH = Path(__file__).parents[1] / "modules" / "homee" / "module.py"
SPEC = importlib.util.spec_from_file_location("homee_delete_transport_module", MODULE_PATH)
homee = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(homee)


class DatabaseContext:
    integration_id = "home"

    def __init__(self, database):
        self.database = database

    def nodes(self):
        return []

    def load_state(self, default=None):
        return self.database.setting("module_state:home", default)

    def save_state(self, value):
        self.database.set_setting("module_state:home", value)


class DirectDeleteReplySocket:
    """Return a complete list in DELETE itself, never an extra confirmation GET."""

    def __init__(self, adapter, originals):
        self.adapter = adapter
        self.remote = deepcopy(originals)
        self.messages = []
        self.expect_preflight_get = True
        self.backups_at_send = {}

    async def send(self, command):
        self.messages.append(command)
        if command == "GET:homeegrams":
            if not self.expect_preflight_get:
                raise AssertionError("Direct DELETE response must not require another GET")
            self.expect_preflight_get = False
            await self.adapter._handle_message(json.dumps({"homeegrams": self.remote}))
            return
        if not command.startswith("DELETE:homeegrams/"):
            raise AssertionError("Unexpected homee command: " + command)
        identifier = int(command.rsplit("/", 1)[-1])
        journal = self.adapter.context.load_state()["homeegram_deletions"]
        reserved = next(record for record in journal.values()
                        if record["homeegram_id"] == identifier)
        if reserved["status"] != "delete_reserved" or not isinstance(reserved.get("backup"), dict):
            raise AssertionError("Original must be durably backed up before DELETE")
        self.backups_at_send[identifier] = deepcopy(reserved["backup"])
        self.remote = [row for row in self.remote if row["id"] != identifier]
        await self.adapter._handle_message(json.dumps({"homeegrams": self.remote}))


class HomeegramDeletionTransportTests(unittest.TestCase):
    def test_mcp_and_admin_http_delete_use_real_adapter_and_direct_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Database(folder)
            try:
                database.save_integration({"id": "home", "module_id": "homee", "name": "Test homee",
                                           "enabled": True, "configuration": {}})
                originals = [
                    {"id": identifier, "name": name, "active": 0, "play": 0,
                     "triggers": {"switch_triggers": [{"id": identifier + 1000}]},
                     "conditions": {},
                     "actions": {"notification_actions": [{"message": "private-notification-text"}],
                                 "webhook_actions": [{"url": "https://example.invalid/private-webhook"}]}}
                    for identifier, name in ((849, "Kellerlicht%20Hobbyraum"), (850, "Kellerlicht%20Lager"))
                ]
                adapter = homee.HomeeAdapter({}, DatabaseContext(database))
                adapter.resources["homeegrams"] = deepcopy(originals)
                adapter.homeegram_wait_seconds = 0.05
                socket = DirectDeleteReplySocket(adapter, originals)
                adapter.socket = socket
                runtime = SimpleNamespace(adapters={"home": adapter}, visible_nodes=lambda: [])
                mcp = MCPAccess(database, runtime, lambda: "admin-test")
                api = HomeegramAccess(database, runtime)

                @asynccontextmanager
                async def lifespan(app):
                    async with mcp.server.session_manager.run():
                        yield

                app = FastAPI(lifespan=lifespan)
                app.include_router(mcp.router)
                app.include_router(api.router, dependencies=[Depends(mcp.require_admin)])
                app.mount("/mcp", mcp.http_app)
                admin = {"Authorization": "Bearer admin-test"}
                with TestClient(app) as client:
                    self.assertEqual(200, client.put("/api/v1/mcp", headers=admin,
                                                   json={"enabled": True}).status_code)
                    token = client.post("/api/v1/mcp/rotate-token", headers=admin).json()["token"]
                    mcp_headers = {"Authorization": "Bearer " + token,
                                   "Accept": "application/json, text/event-stream"}
                    mcp_request_id = str(uuid.uuid4())
                    arguments = {"integration_id": "home", "homeegram_id": 849,
                                 "confirmation_name": "Kellerlicht Hobbyraum", "request_id": mcp_request_id}

                    def delete_mcp():
                        return client.post("/mcp/", headers=mcp_headers, json={
                            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "delete_homeegram", "arguments": arguments}})

                    def tool_data(response):
                        result = response.json()["result"]
                        self.assertFalse(result.get("isError", False), result)
                        return result.get("structuredContent") or json.loads(result["content"][0]["text"])

                    # MCP authentication alone does not grant deletion or admin API access.
                    self.assertTrue(delete_mcp().json()["result"]["isError"])
                    admin_path = "/api/v1/integrations/home/homeegrams/850/delete"
                    admin_payload = {"confirmation_name": "Kellerlicht Lager", "request_id": str(uuid.uuid4())}
                    for headers in ({}, mcp_headers):
                        self.assertEqual(401, client.post(admin_path, headers=headers,
                                                         json=admin_payload).status_code)
                    self.assertEqual([], socket.messages)

                    self.assertEqual(200, client.put("/api/v1/mcp", headers=admin,
                        json={"enabled": True, "allow_homee_deletion": True}).status_code)
                    response = delete_mcp()
                    self.assertEqual(200, response.status_code, response.text)
                    public = tool_data(response)
                    self.assertEqual({"homeegram_id": 849, "name": "Kellerlicht Hobbyraum", "deleted": True,
                                      "status": "deleted", "backup_saved": True, "request_id": mcp_request_id,
                                      "replayed": False}, public)
                    self.assertEqual(["GET:homeegrams", "DELETE:homeegrams/849"], socket.messages)
                    self.assertNotIn("private-notification", response.text)
                    self.assertNotIn("private-webhook", response.text)
                    self.assertTrue(tool_data(delete_mcp())["replayed"])
                    self.assertEqual(1, socket.messages.count("DELETE:homeegrams/849"))

                    backup_path = "/api/v1/mcp/homeegram-deletions/" + mcp_request_id + "/backup?integration_id=home"
                    self.assertEqual(401, client.get(backup_path, headers=mcp_headers).status_code)
                    self.assertEqual(originals[0], client.get(backup_path, headers=admin).json())

                    # The normal app API remains usable with MCP completely disabled.
                    self.assertEqual(200, client.put("/api/v1/mcp", headers=admin,
                                                    json={"enabled": False}).status_code)
                    self.assertEqual(403, delete_mcp().status_code)
                    socket.expect_preflight_get = True
                    response = client.post(admin_path, headers=admin, json=admin_payload)
                    self.assertEqual(200, response.status_code, response.text)
                    self.assertEqual({"homeegram_id": 850, "name": "Kellerlicht Lager", "deleted": True,
                                      "status": "deleted", "backup_saved": True, "replayed": False,
                                      "integration_id": "home", "request_id": admin_payload["request_id"]}, response.json())
                    self.assertNotIn("private-notification", response.text)
                    self.assertNotIn("private-webhook", response.text)
                    self.assertTrue(client.post(admin_path, headers=admin, json=admin_payload).json()["replayed"])
                    self.assertEqual(["GET:homeegrams", "DELETE:homeegrams/849",
                                      "GET:homeegrams", "DELETE:homeegrams/850"], socket.messages)
                    self.assertEqual([], socket.remote)
                    self.assertEqual([], adapter.resources["homeegrams"])
                    journal = database.setting("module_state:home")["homeegram_deletions"]
                    for original, request_id in ((originals[0], mcp_request_id),
                                                 (originals[1], admin_payload["request_id"])):
                        self.assertEqual("deleted", journal[request_id]["status"])
                        self.assertEqual(original, journal[request_id]["backup"])
                        self.assertEqual(original, socket.backups_at_send[original["id"]])
            finally:
                database._connection.close()


if __name__ == "__main__":
    unittest.main()
