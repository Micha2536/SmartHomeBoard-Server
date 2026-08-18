import unittest

from server.setup_portal import _pretty_protocol_message, displays_page, integrations_page


class SetupPortalTests(unittest.TestCase):
    def test_homee_console_contains_templates_filters_and_escaped_messages(self):
        manifests = [{
            "id": "homee", "name": "homee", "description": "Test",
            "fields": [],
        }]
        integrations = [{
            "id": "homee-1", "module_id": "homee", "name": "Zuhause",
            "enabled": True, "configuration": {}, "device_count": 1,
            "status": "Verbunden", "error": None,
        }]
        html = integrations_page(
            "0.10.0", manifests, integrations, selected_id="homee-1",
            homee_protocol={"messages": [
                {
                    "timestamp": 1, "direction": "in", "category": "attribute",
                    "message": '<script>alert("x")</script>', "size": 27,
                    "truncated": False,
                },
                {
                    "timestamp": 2, "direction": "out", "category": "command",
                    "message": "GET:nodes", "size": 9, "truncated": False,
                },
            ]},
            protocol_filter="attribute",
        )
        self.assertIn("homee WebSocket-Konsole", html)
        self.assertIn("GET:all", html)
        self.assertIn("GET:analytics", html)
        self.assertIn('value="attribute" selected', html)
        self.assertNotIn('<script>alert("x")</script>', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<pre>GET:nodes</pre>', html)
        self.assertIn("1 Eintrag im gewählten Filter", html)
        self.assertIn("WebSocket-Livefeed", html)
        self.assertIn("setInterval(update,1500)", html)
        self.assertIn("Livefeed starten", html)
        self.assertIn("60000", html)
        self.assertIn("Im Hintergrund pausiert", html)
        self.assertNotIn("onchange=\"this.form.submit()\"", html)

    def test_protocol_payload_is_pretty_printed_as_json(self):
        formatted = _pretty_protocol_message('GET:nodes {"nodes":[{"id":1}]}')
        self.assertTrue(formatted.startswith("GET:nodes\n{"))
        self.assertIn('\n  "nodes": [', formatted)
        self.assertIn('\n      "id": 1', formatted)

    def test_module_actions_are_available_in_web_portal(self):
        manifests = [{
            "id": "roborock", "name": "Roborock", "description": "Test", "fields": [],
            "actions": [
                {"id": "request_code", "title": "Anmeldecode senden"},
                {"id": "logout", "title": "Anmeldung zurücksetzen", "role": "destructive"},
            ],
        }]
        integrations = [{
            "id": "robot-1", "module_id": "roborock", "name": "Sauger",
            "enabled": True, "configuration": {}, "device_count": 0,
            "status": "Anmeldung erforderlich", "error": None,
        }]

        html = integrations_page("0.11.0", manifests, integrations, selected_id="robot-1")

        self.assertIn('action="/setup/integrations/action"', html)
        self.assertIn('value="request_code"', html)
        self.assertIn("Anmeldecode senden", html)
        self.assertIn('class="danger"', html)

    def test_epaper_attribute_dropdown_is_filtered_by_device(self):
        displays = [{
            "id": "paper-1", "name": "Flur", "model": "M5Paper",
            "status": "paired", "configuration": {"widgets": []},
        }]
        nodes = [
            {"id": 11, "name": "Wohnzimmer Sensor", "attributes": [
                {"id": 101, "name": "Temperatur", "unit": "°C"},
                {"id": 102, "name": "Luftfeuchte", "unit": "%"},
            ]},
            {"id": 22, "name": "Küche Fenster", "attributes": [
                {"id": 201, "name": "Zustand", "unit": ""},
            ]},
        ]

        html = displays_page("0.10.5", displays, nodes, selected_id="paper-1")

        self.assertIn('placeholder="Gerät suchen …"', html)
        self.assertIn('data-device-search="wohnzimmer sensor 11"', html)
        self.assertIn('data-device-search="küche fenster 22"', html)
        self.assertIn("select.replaceChildren", html)
        self.assertIn("option.dataset.deviceSearch", html)
        self.assertNotIn("option.hidden", html)


if __name__ == "__main__":
    unittest.main()
