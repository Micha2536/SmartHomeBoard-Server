import os
import sys
import tempfile
import unittest
import json
from unittest.mock import AsyncMock, patch
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
os.environ["SHB_DATA_DIR"] = tempfile.mkdtemp(prefix="shb-api-test-")
os.environ["SHB_MODULE_DIR"] = str(ROOT / "modules")
os.environ["SHB_API_TOKEN"] = "test-token"
os.environ["SHB_DISABLE_SELF_RESTART"] = "1"
os.environ["SHB_DISABLE_DISPLAY_DISCOVERY"] = "1"

from fastapi.testclient import TestClient
import server.main as main
from server.config import SETUP_PORT, load_server_config
from server.main import SETUP_SESSION_COOKIE, app, setup_app


class APITests(unittest.TestCase):
    def test_enocean_learning_form_has_searchable_ft55_variants(self):
        html = main.enocean_setup_html()
        self.assertIn('id="learningProfileSearch"', html)
        self.assertIn("FT55 Einfachwippe (1-fach)", html)
        self.assertIn("FT55 Doppelwippe (2-fach)", html)
        self.assertIn("filterLearningProfiles", html)
        self.assertIn("refreshEnOceanStatus", html)
        self.assertIn('id="enoceanLearningStatus"', html)
        self.assertIn('id="enoceanDevices"', html)

    def test_enocean_setup_status_returns_live_countdown_and_device_html(self):
        integration_id = "enocean-live-test"
        main.database.save_integration({
            "id": integration_id,
            "module_id": "enocean",
            "name": "EnOcean Live-Test",
            "enabled": True,
            "configuration": {},
            "status": "Verbunden",
            "error": None,
            "device_count": 1,
        })
        main.database.set_setting(f"module_state:{integration_id}", {
            "learning_until": main.time.time() + 30,
            "learning_profile_id": "F6-02-01-SINGLE",
            "devices": {
                "019ADAA0": {
                    "name": "FT55 Test",
                    "eep": "F6-02-01",
                    "profile_id": "F6-02-01-SINGLE",
                    "raw": "10",
                    "last_seen": 1,
                    "rssi": -55,
                }
            },
        })
        with TestClient(setup_app) as client:
            response = client.get(f"/setup/enocean/status?integration_id={integration_id}")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["learning"])
        self.assertGreater(payload["learning_seconds"], 0)
        self.assertEqual(payload["device_count"], 1)
        self.assertIn("FT55 Test", payload["devices_html"])

    def test_epaper_units_decode_percent_encoding(self):
        self.assertEqual(main.decoded_homee_text("kW%20h"), "kW h")
        self.assertEqual(main.decoded_homee_text("%25"), "%")

    def test_epaper_text_attribute_uses_data_instead_of_numeric_value(self):
        node_id = 450045
        main.database.save_node("epaper-text-test", {
            "id": node_id,
            "name": "Wechselrichter",
            "attributes": [{
                "id": 450046,
                "type": 45,
                "name": "Betriebsmodus",
                "unit": "text",
                "current_value": 2,
                "data": "Automatik%20Eco",
            }],
        })

        rendered = main.resolved_display_render({
            "widgets": [{
                "id": "operating-mode",
                "node_id": node_id,
                "attribute_id": 450046,
                "decimals": 1,
            }],
        })

        widget = rendered["widgets"][0]
        self.assertEqual(widget["value"], "Automatik Eco")
        self.assertEqual(widget["unit"], "")
        self.assertTrue(widget["available"])

    def test_epaper_timestamp_uses_configured_timezone_and_dst(self):
        with patch.dict(os.environ, {"SHB_TIMEZONE": "Europe/Berlin"}):
            summer = main.display_generated_at(
                main.dt.datetime(2026, 8, 18, 11, 0, tzinfo=main.dt.timezone.utc)
            )
            winter = main.display_generated_at(
                main.dt.datetime(2026, 1, 18, 11, 0, tzinfo=main.dt.timezone.utc)
            )

        self.assertEqual(summer, "2026-08-18T13:00+02:00")
        self.assertEqual(winter, "2026-01-18T12:00+01:00")

    def test_authentication_and_dynamic_module_catalog(self):
        main.ENV_API_TOKEN = ""
        main.database.set_setting("api_token", "")
        with TestClient(setup_app) as setup_client, TestClient(app) as client:
            self.assertEqual(setup_client.get("/setup").status_code, 200)
            self.assertEqual(client.get("/setup").status_code, 404)
            self.assertEqual(client.get("/api/v1/health").status_code, 200)
            token = "lokaler-test-schluessel-123456789"
            configured = setup_client.post("/setup", data={"server_port": "8787", "new_token": token, "confirm_token": token})
            self.assertEqual(configured.status_code, 200)
            self.assertIn(token, configured.text)
            self.assertIn(SETUP_SESSION_COOKIE, setup_client.cookies)
            self.assertNotEqual(setup_client.cookies[SETUP_SESSION_COOKIE], token)
            unlocked_setup = setup_client.get("/setup")
            self.assertNotIn("Bisheriger API-Schlüssel", unlocked_setup.text)
            self.assertEqual(client.get("/api/v1/health").status_code, 401)
            headers = {"Authorization": f"Bearer {token}"}
            health = client.get("/api/v1/health", headers=headers)
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["status"], "ok")
            modules = client.get("/api/v1/modules", headers=headers).json()["modules"]
            module_ids = {item["id"] for item in modules}
            self.assertTrue({"demo", "go-e", "modbus-tcp", "enocean", "homee"}.issubset(module_ids))
            modbus = next(item for item in modules if item["id"] == "modbus-tcp")
            profile_field = next(item for item in modbus["fields"] if item["key"] == "profile")
            profile_ids = {option["value"] for option in profile_field["options"]}
            self.assertTrue({"modbus.connection-test", "victron.gx-system.v3", "victron.gx-solarcharger.v3", "sma.sunny-boy.core", "tq.b-control.em300", "mennekes.amtron-4you500-4business700.v1_5"}.issubset(profile_ids))
            synchronized = client.get("/api/v1/modbus/profiles", headers=headers)
            self.assertEqual(synchronized.status_code, 200)
            synchronized_ids = {profile["id"] for profile in synchronized.json()["profiles"]}
            self.assertTrue(profile_ids.issubset(synchronized_ids))

            stored_homee = main.database.save_integration({
                "id": "homee-state-test", "module_id": "homee", "name": "homee Test",
                "enabled": False, "configuration": {}, "device_count": 0,
            })
            main.database.set_setting(
                f"module_state:{stored_homee['id']}",
                {"resources": {"groups": [{"id": 3, "name": "Wohnzimmer"}]}},
            )
            state_response = client.get(
                f"/api/v1/integrations/{stored_homee['id']}/state", headers=headers
            )
            self.assertEqual(state_response.status_code, 200)
            self.assertEqual(
                state_response.json()["state"]["resources"]["groups"][0]["name"],
                "Wohnzimmer",
            )
            main.database.delete_integration(stored_homee["id"])

            registration = client.post("/api/v1/displays/register", json={
                "device_id": "m5paper-3c6105091a68",
                "name": "M5Paper Flur",
                "model": "M5Paper",
                "firmware_version": "1.0.0",
            })
            self.assertEqual(registration.status_code, 200)
            registration_data = registration.json()
            self.assertEqual(registration_data["status"], "pending")
            self.assertEqual(len(registration_data["pairing_code"]), 6)
            self.assertTrue(registration_data["device_token"])

            self.assertEqual(
                client.post("/api/v1/displays/register", json={
                    "device_id": "m5paper-3c6105091a68",
                    "firmware_version": "1.0.1",
                }).status_code,
                401,
            )
            registered_again = client.post("/api/v1/displays/register", json={
                "device_id": "m5paper-3c6105091a68",
                "firmware_version": "1.0.1",
                "device_token": registration_data["device_token"],
            })
            self.assertEqual(registered_again.status_code, 200)
            self.assertEqual(registered_again.json()["device_token"], "")

            display_headers = {"X-Display-Token": registration_data["device_token"]}
            pending_configuration = client.get(
                "/api/v1/displays/device/m5paper-3c6105091a68/configuration",
                headers=display_headers,
            )
            self.assertEqual(pending_configuration.status_code, 200)
            self.assertEqual(pending_configuration.json()["status"], "pending")

            display_list = client.get("/api/v1/displays", headers=headers)
            self.assertEqual(display_list.status_code, 200)
            self.assertEqual(len(display_list.json()["displays"]), 1)
            display_setup = setup_client.get("/setup/displays")
            self.assertIn("E-Paper", display_setup.text)
            self.assertIn("m5paper-3c6105091a68", display_setup.text)
            self.assertIn("Kopplungscode", display_setup.text)
            automation_setup = setup_client.get("/setup/automations")
            self.assertIn("Letzte 12 Ereignisse", automation_setup.text)
            self.assertEqual(
                client.post(
                    "/api/v1/displays/m5paper-3c6105091a68/pair",
                    headers=headers,
                    json={"pairing_code": "000000", "name": "Flur"},
                ).status_code,
                403,
            )
            paired = setup_client.post(
                "/setup/displays/pair",
                data={
                    "display_id": "m5paper-3c6105091a68",
                    "pairing_code": registration_data["pairing_code"],
                    "name": "Flur",
                },
            )
            self.assertEqual(paired.status_code, 200)
            self.assertIn("wurde gekoppelt", paired.text)
            self.assertIn("Gekoppelt", paired.text)

            layout = {
                "sleep_minutes": 5,
                "title": "Energie",
                "layout": "grid",
                "widgets": [{
                    "id": "power",
                    "label": "Leistung",
                    "node_id": 999999,
                    "attribute_id": 999999,
                    "decimals": 0,
                }],
            }
            configured_display = client.put(
                "/api/v1/displays/m5paper-3c6105091a68/configuration",
                headers=headers,
                json={"configuration": layout},
            )
            self.assertEqual(configured_display.status_code, 200)
            delivered = client.get(
                "/api/v1/displays/device/m5paper-3c6105091a68/configuration",
                headers=display_headers,
            )
            self.assertEqual(delivered.json()["configuration"], layout)
            self.assertGreater(delivered.json()["configuration_version"], 1)
            self.assertEqual(delivered.json()["render"]["title"], "Energie")
            self.assertEqual(delivered.json()["render"]["layout"], "grid")
            self.assertEqual(delivered.json()["render"]["widgets"][0]["value"], "--")

            web_saved_display = setup_client.post(
                "/setup/displays/save",
                data={
                    "display_id": "m5paper-3c6105091a68",
                    "name": "Flur Paper",
                    "title": "Hauswerte",
                    "sleep_minutes": "7",
                    "layout": "list",
                },
            )
            self.assertEqual(web_saved_display.status_code, 200)
            stored_display = main.database.display("m5paper-3c6105091a68")
            self.assertEqual(stored_display["name"], "Flur Paper")
            self.assertEqual(stored_display["configuration"]["sleep_minutes"], 7)

            fake_saved = {"id": "homee-test"}
            with patch.object(main, "call_local_api", AsyncMock(return_value=fake_saved)) as local_api:
                web_saved_homee = setup_client.post(
                    "/setup/integrations/save",
                    data={
                        "module_id": "homee",
                        "name": "homee Zuhause",
                        "enabled": "1",
                        "config__host": "192.168.1.10",
                        "config__port": "7681",
                        "config__username": "server",
                        "config__password": "geheim",
                    },
                )
                self.assertEqual(web_saved_homee.status_code, 200)
                payload = local_api.await_args.args[1]
                self.assertEqual(payload["module_id"], "homee")
                self.assertEqual(payload["configuration"]["port"], 7681)
                self.assertEqual(local_api.await_args.kwargs["method"], "POST")

            with client.websocket_connect(f"/api/v1/events?token={token}") as websocket:
                snapshot = websocket.receive_json()
                self.assertEqual(snapshot["type"], "snapshot")
                self.assertIn("nodes", snapshot)

            templates = setup_client.get("/setup/modbus")
            self.assertEqual(templates.status_code, 200)
            self.assertIn("Victron Energy", templates.text)
            enocean_page = setup_client.get("/setup/enocean")
            self.assertEqual(enocean_page.status_code, 200)
            self.assertIn("EnOcean-Geräte", enocean_page.text)
            self.assertIn("Eltako FT55", enocean_page.text)
            self.assertIn("Rauchmelder", enocean_page.text)
            self.assertIn("Der erste neue Sender", enocean_page.text)
            self.assertIn("Anlernen starten", enocean_page.text)
            custom_profile = {
                "id": "test.custom-meter.v1", "manufacturer": "Test", "model": "Zähler",
                "default_unit_id": 1, "registers": [
                    {"address": 10, "register_type": "input", "data_type": "float32", "attribute_type": 3, "name": "Leistung", "unit": "W"}
                ]
            }
            # Das zuvor gesetzte HttpOnly-Cookie schaltet auch andere Setup-Seiten frei.
            saved_profile = setup_client.post("/setup/modbus", data={"profile_json": json.dumps(custom_profile)})
            self.assertEqual(saved_profile.status_code, 200)
            self.assertTrue((Path(os.environ["SHB_DATA_DIR"]) / "modbus-profiles" / "test.custom-meter.v1.json").exists())
            main.registry.load()
            refreshed_modules = client.get("/api/v1/modules", headers=headers).json()["modules"]
            refreshed_modbus = next(item for item in refreshed_modules if item["id"] == "modbus-tcp")
            refreshed_profiles = next(item for item in refreshed_modbus["fields"] if item["key"] == "profile")["options"]
            self.assertTrue(any(option["value"] == "test.custom-meter.v1" for option in refreshed_profiles))

            app_profile = dict(custom_profile)
            app_profile["id"] = "test.app-editor.v1"
            api_saved = client.post("/api/v1/modbus/profiles", headers=headers, json={"profile": app_profile})
            self.assertEqual(api_saved.status_code, 200)
            self.assertTrue((Path(os.environ["SHB_DATA_DIR"]) / "modbus-profiles" / "test.app-editor.v1.json").exists())
            api_deleted = client.delete("/api/v1/modbus/profiles/test.app-editor.v1", headers=headers)
            self.assertEqual(api_deleted.status_code, 200)
            self.assertFalse((Path(os.environ["SHB_DATA_DIR"]) / "modbus-profiles" / "test.app-editor.v1.json").exists())

            changed = setup_client.post("/setup", data={"current_token": token, "server_port": "8877", "new_token": "", "confirm_token": ""})
            self.assertEqual(changed.status_code, 200)
            self.assertEqual(load_server_config()["port"], 8877)
            self.assertIn("Einrichtungsseite bleibt unter Port 8400", changed.text)

            reserved = setup_client.post("/setup", data={"current_token": token, "server_port": str(SETUP_PORT), "new_token": "", "confirm_token": ""})
            self.assertEqual(reserved.status_code, 400)
            self.assertEqual(load_server_config()["port"], 8877)


if __name__ == "__main__":
    unittest.main()
