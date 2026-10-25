import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from server.database import Database
from server.homeegrams import HomeegramAccess


class HomeegramAPITests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.database = Database(self.folder.name)
        self.integration = dict(id="home", module_id="homee", name="homee", enabled=True,
                                configuration={"password": "private-password"})
        self.database.save_integration(self.integration)
        self.original = {"id": 849, "name": "Kellerlicht%20Hobbyraum", "active": 0,
                         "triggers": {}, "conditions": {}, "actions": {}}
        self.adapter = SimpleNamespace(socket=object(), resources={"homeegrams": [self.original]},
                                       _refresh_homeegram_snapshot=AsyncMock(),
                                       delete_homeegram_confirmed=AsyncMock(return_value={
                                           "homeegram_id": 849, "name": "Kellerlicht Hobbyraum",
                                           "deleted": True, "status": "deleted", "backup_saved": True,
                                           "replayed": False, "backup": {"secret": "private-webhook"}}))
        self.runtime = SimpleNamespace(adapters={"home": self.adapter})
        self.access = HomeegramAccess(self.database, self.runtime)

        async def admin(authorization: str | None = Header(default=None)):
            if authorization != "Bearer admin-test":
                raise HTTPException(status_code=401)

        self.app = FastAPI(dependencies=[Depends(admin)])
        self.app.include_router(self.access.router)
        self.client = TestClient(self.app)
        self.headers = {"Authorization": "Bearer admin-test"}
        self.payload = {"confirmation_name": "Kellerlicht Hobbyraum", "request_id": str(uuid.uuid4())}
        self.base = "/api/v1/integrations/home/homeegrams"

    def tearDown(self):
        self.client.close()
        self.database._connection.close()
        self.folder.cleanup()

    def post(self, payload=None, path=None):
        return self.client.post(path or self.base + "/849/delete", headers=self.headers,
                                json=self.payload if payload is None else payload)

    def test_admin_auth_required_for_list_and_delete_not_mcp_token(self):
        for headers in ({}, {"Authorization": "Bearer mcp-read-token"}):
            self.assertEqual(401, self.client.get(self.base, headers=headers).status_code)
            self.assertEqual(401, self.client.get(self.base + "/catalog", headers=headers).status_code)
            self.assertEqual(401, self.client.post(self.base + "/849/delete", headers=headers,
                                                 json=self.payload).status_code)
        self.adapter._refresh_homeegram_snapshot.assert_not_awaited()
        self.adapter.delete_homeegram_confirmed.assert_not_awaited()

    def test_list_reads_fresh_original_homeegrams_from_selected_source(self):
        response = self.client.get(self.base, headers=self.headers)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("no-store", response.headers["Cache-Control"])
        self.assertEqual({"integration_id": "home", "homeegrams": [self.original]}, response.json())
        self.adapter._refresh_homeegram_snapshot.assert_awaited_once_with(self.adapter.socket)
        self.adapter.delete_homeegram_confirmed.assert_not_awaited()
        self.assertNotIn("private-password", response.text)

    def test_catalog_is_source_local_native_cached_and_credential_free(self):
        self.adapter.context = SimpleNamespace(integration_id="home")
        self.adapter.nodes = {3357: {
            "id": 3357, "name": "Flur EG", "integration_id": "home", "integration_source": "server",
            "password": "private-node-password", "access_token": "private-node-token",
            "attributes": [{"id": 8674, "node_id": 3357, "type": 25, "name": "Bewegung",
                            "current_value": 1, "editable": False, "secret": "private-attribute-secret"},
                           {"id": 8675, "type": 0, "current_value": "private-string-value",
                            "data": "private-binary-value"}]
        }}
        self.adapter.resources.update({
            "groups": [{"id": 7, "name": "Flur", "type": 1, "token": "private-group-token"}],
            "users": [{"id": 58, "username": "Michael", "password": "private-user-password",
                       "access_token": "private-user-token", "sessions": ["private-session"]}],
            "plans": [{"id": 3, "name": "Heizen", "type": 2, "credentials": "private-plan-secret"}],
            "relationships": [{"id": 68, "group_id": 7, "node_id": 3357, "homeegram_id": 0,
                               "order": 0, "configuration": "private-relation-secret"}],
            "configuration": {"password": "private-resource-password"},
            "nodes": [{"id": 900, "name": "Nicht aus Resource-Nodes lesen"}]
        })
        self.runtime.adapters["other"] = SimpleNamespace(nodes={999: {"id": 999, "name": "Andere Quelle"}})
        response = self.client.get(self.base + "/catalog", headers=self.headers)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("no-store", response.headers["Cache-Control"])
        payload = response.json()
        self.assertEqual(("home", "homee", True), (payload["integration_id"], payload["source"], payload["cached"]))
        self.assertEqual([3357], [node["id"] for node in payload["nodes"]])
        self.assertEqual("homee", payload["nodes"][0]["integration_source"])
        self.assertEqual("home", payload["nodes"][0]["integration_id"])
        self.assertEqual(8674, payload["nodes"][0]["attributes"][0]["id"])
        self.assertEqual(3357, payload["nodes"][0]["attributes"][0]["node_id"])
        self.assertEqual(1, payload["nodes"][0]["attributes"][0]["current_value"])
        self.assertEqual([{"id": 58, "username": "Michael"}], payload["users"])
        self.assertEqual([{"id": 7, "name": "Flur", "type": 1}], payload["groups"])
        self.assertEqual([{"id": 3, "name": "Heizen", "type": 2}], payload["plans"])
        self.assertEqual([{"id": 68, "group_id": 7, "node_id": 3357, "homeegram_id": 0, "order": 0}], payload["relationships"])
        self.assertNotIn("private-", response.text)
        self.assertNotIn("homeegrams", payload)
        self.adapter._refresh_homeegram_snapshot.assert_not_awaited()
        self.adapter.delete_homeegram_confirmed.assert_not_awaited()

    def test_catalog_optional_empty_resources_do_not_block_old_firmware(self):
        self.adapter.nodes = {}
        response = self.client.get(self.base + "/catalog", headers=self.headers)
        self.assertEqual(200, response.status_code, response.text)
        for collection in ("nodes", "users", "groups", "plans", "relationships"):
            self.assertEqual([], response.json()[collection])

    def test_catalog_rejects_foreign_node_and_adapter_identity(self):
        self.adapter.nodes = {33: {"id": 33, "integration_id": "other"}}
        self.assertEqual(409, self.client.get(self.base + "/catalog", headers=self.headers).status_code)
        self.adapter.nodes = {}
        self.adapter.context = SimpleNamespace(integration_id="other")
        self.assertEqual(409, self.client.get(self.base + "/catalog", headers=self.headers).status_code)

    def test_catalog_rechecks_source_after_projection(self):
        project = self.access.catalog_snapshot

        def replace_source(integration_id, adapter):
            result = project(integration_id, adapter)
            self.runtime.adapters["home"] = SimpleNamespace(socket=object())
            return result

        self.access.catalog_snapshot = replace_source
        self.assertEqual(409, self.client.get(self.base + "/catalog", headers=self.headers).status_code)

    def test_catalog_rejects_malformed_collections_and_strips_nested_values(self):
        self.adapter.resources["groups"] = {"id": 7}
        self.assertEqual(502, self.client.get(self.base + "/catalog", headers=self.headers).status_code)
        self.adapter.resources["groups"] = [{"id": True}, {"name": "ohne ID"}, "falsch"]
        self.adapter.nodes = {33: {"id": 33, "name": {"token": "private-nested-token"},
                                   "attributes": [{"id": 5, "current_value": float("nan"),
                                                   "data": {"token": "private-nested-token"}}]}}
        response = self.client.get(self.base + "/catalog", headers=self.headers)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual([], response.json()["groups"])
        self.assertNotIn("private-nested-token", response.text)
        self.assertNotIn("current_value", response.json()["nodes"][0]["attributes"][0])

    def test_delete_independent_of_disabled_mcp_and_returns_only_confirmed_metadata(self):
        self.database.set_setting("mcp_access", {"enabled": False, "allow_homee_deletion": False})
        response = self.post()
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("no-store", response.headers["Cache-Control"])
        self.assertEqual((849, "Kellerlicht Hobbyraum", self.payload["request_id"]),
                         self.adapter.delete_homeegram_confirmed.await_args.args)
        self.assertTrue(self.adapter.delete_homeegram_confirmed.await_args.kwargs["authorize"]())
        self.assertEqual("home", response.json()["integration_id"])
        self.assertEqual(self.payload["request_id"], response.json()["request_id"])
        self.assertTrue(response.json()["deleted"])
        self.assertNotIn("backup", response.json())
        self.assertNotIn("private-webhook", response.text)
        self.adapter._refresh_homeegram_snapshot.assert_not_awaited()

    def test_delete_preserves_percent_sequences_and_spaces_in_confirmation(self):
        name = "  Licht %20  "
        self.adapter.delete_homeegram_confirmed.return_value["name"] = name
        response = self.post({**self.payload, "confirmation_name": name})
        self.assertEqual(200, response.status_code)
        self.assertEqual(name, self.adapter.delete_homeegram_confirmed.await_args.args[1])

    def test_request_uuid_and_input_validation_prevents_calls(self):
        bad = ({**self.payload, "request_id": "not-a-uuid"},
               {**self.payload, "confirmation_name": " "},
               {**self.payload, "confirmation_name": "x" * 501},
               {**self.payload, "command": "DELETE:all"})
        for payload in bad:
            with self.subTest(payload=payload):
                self.assertEqual(422, self.post(payload).status_code)
        for homeegram_id in ("0", "-1", "all", "1.5"):
            self.assertEqual(422, self.post(path=self.base + f"/{homeegram_id}/delete").status_code)
        self.adapter.delete_homeegram_confirmed.assert_not_awaited()

    def test_missing_foreign_and_disabled_sources_are_not_sent(self):
        self.database.save_integration({**self.integration, "id": "shelly", "module_id": "shelly"})
        self.database.save_integration({**self.integration, "id": "disabled", "enabled": False})
        for source, status in (("missing", 404), ("shelly", 400), ("disabled", 409)):
            base = f"/api/v1/integrations/{source}/homeegrams"
            self.assertEqual(status, self.client.get(base, headers=self.headers).status_code)
            self.assertEqual(status, self.client.get(base + "/catalog", headers=self.headers).status_code)
            self.assertEqual(status, self.post(path=base + "/849/delete").status_code)
        self.adapter.delete_homeegram_confirmed.assert_not_awaited()

    def test_disconnected_source_is_not_sent(self):
        for adapter in (None, SimpleNamespace(socket=None)):
            self.runtime.adapters["home"] = adapter
            self.assertEqual(503, self.post().status_code)
            self.assertEqual(503, self.client.get(self.base, headers=self.headers).status_code)
            self.assertEqual(503, self.client.get(self.base + "/catalog", headers=self.headers).status_code)
        self.adapter.delete_homeegram_confirmed.assert_not_awaited()

    def test_authorize_callback_checks_integration_and_adapter_again(self):
        response = self.post()
        self.assertEqual(200, response.status_code)
        authorize = self.adapter.delete_homeegram_confirmed.await_args.kwargs["authorize"]
        self.database.save_integration({**self.integration, "enabled": False})
        self.assertFalse(authorize())
        self.database.save_integration(self.integration)
        self.runtime.adapters["home"] = SimpleNamespace(socket=object())
        self.assertFalse(authorize())

    def test_refresh_rejects_changed_source_and_malformed_collection(self):
        async def replace_source(socket):
            self.runtime.adapters["home"] = SimpleNamespace(socket=object())

        self.adapter._refresh_homeegram_snapshot.side_effect = replace_source
        self.assertEqual(409, self.client.get(self.base, headers=self.headers).status_code)
        self.runtime.adapters["home"] = self.adapter
        self.adapter._refresh_homeegram_snapshot.side_effect = None
        self.adapter.resources["homeegrams"] = None
        self.assertEqual(502, self.client.get(self.base, headers=self.headers).status_code)

    def test_upstream_failures_are_not_reported_as_success(self):
        for error, status in ((TimeoutError("keine Antwort"), 504),
                              (ConnectionError("getrennt"), 503),
                              (ValueError("Name passt nicht"), 409),
                              (RuntimeError("Fehler"), 502)):
            self.adapter.delete_homeegram_confirmed.side_effect = error
            response = self.post()
            self.assertEqual(status, response.status_code, response.text)
            self.assertEqual(str(error), response.json()["detail"])

    def test_incomplete_or_wrong_confirmation_is_not_success(self):
        for overrides in ({"deleted": False}, {"status": "uncertain"},
                          {"homeegram_id": 850}, {"name": "anderes HG"}):
            self.adapter.delete_homeegram_confirmed.return_value = {
                "homeegram_id": 849, "name": "Kellerlicht Hobbyraum", "deleted": True,
                "status": "deleted", **overrides}
            self.assertEqual(502, self.post().status_code)


if __name__ == "__main__":
    unittest.main()
