import unittest

from server.setup_portal import _pretty_protocol_message, automations_page, display_text, displays_page, integrations_page


class SetupPortalTests(unittest.TestCase):
    def test_automation_editor_offers_control_push_and_recipients(self):
        html = automations_page(
            "0.15.4",
            {"count": 1, "automations": [{"id": "rule-1", "name": "Licht", "enabled": True}],
             "recent_events": [], "push": {"configured": True, "device_count": 1,
                                               "devices": [{"id": "ipad-1", "name": "Flur iPad"}]}},
            nodes=[], rules=[{"id": "rule-1", "name": "Licht"}],
        )
        self.assertIn("Andere Automation steuern", html)
        self.assertIn("Push-Nachricht vom Server", html)
        self.assertIn("Flur iPad", html)
        self.assertIn("pushDeviceIDs", html)
        self.assertIn("/setup/automations/status", html)
        self.assertIn("setInterval(refreshAutomationOverview,2000)", html)

    def test_display_text_decodes_url_encoding_repeatedly_and_safely(self):
        self.assertEqual("Wohnzimmer Temperatur °C", display_text("Wohnzimmer%2520Temperatur%2520%25C2%25B0C"))
        self.assertEqual("100%", display_text("100%"))

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

    def test_module_action_can_request_a_temporary_payload_field(self):
        manifests = [{
            "id": "zwave", "name": "Z-Wave", "description": "Test", "fields": [],
            "actions": [{
                "id": "enter_pin", "title": "S2-PIN bestätigen",
                "fields": [{"key": "pin", "title": "Fünfstellige S2-PIN", "type": "password", "required": True, "pattern": "[0-9]{5}"}],
            }],
        }]
        integrations = [{
            "id": "zwave-1", "module_id": "zwave", "name": "Z-Wave",
            "enabled": True, "configuration": {}, "device_count": 0,
            "status": "S2-PIN erforderlich", "error": None,
        }]

        html = integrations_page("0.15.0", manifests, integrations, selected_id="zwave-1")

        self.assertIn('name="payload__pin"', html)
        self.assertIn('pattern="[0-9]{5}"', html)
        self.assertIn("S2-PIN bestätigen", html)

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

    def test_web_portal_decodes_device_attribute_unit_and_choice_labels(self):
        displays = [{
            "id": "paper-1", "name": "Flur", "model": "M5Paper",
            "status": "paired", "configuration": {"widgets": []},
        }]
        nodes = [{
            "id": 11, "name": "Wohnzimmer%20Sensor", "integration_module": "roborock",
            "attributes": [{
                "id": 101, "name": "Au%C3%9Fen%20Temperatur", "unit": "%C2%B0C", "editable": True,
                "data": '{"label":"K%C3%BCche%20oben","options":[{"value":1,"label":"K%C3%BCche%20oben"}]}',
            }],
        }]

        display_html = displays_page("0.14.1", displays, nodes, selected_id="paper-1")
        automation_html = automations_page("0.14.1", {"count": 0, "automations": [], "recent_events": []}, nodes=nodes)

        self.assertIn("Wohnzimmer Sensor · Außen Temperatur (°C)", display_html)
        self.assertIn("displayDecode(node.name", automation_html)
        self.assertIn("displayDecode(attribute.unit)", automation_html)
        self.assertIn("displayDecode(item.label", automation_html)

    def test_automation_page_contains_persistent_no_code_editor(self):
        nodes = [{
            "id": 11, "name": "Lokales Licht", "integration_module": "demo",
            "attributes": [{"id": 101, "name": "Schalter", "editable": True, "minimum": 0, "maximum": 1}],
        }]
        rule = {
            "id": "server-rule", "name": "Abendlicht", "isEnabled": True,
            "cooldownSeconds": 30, "conditionValidation": "triggerTime",
            "triggers": [{"id": "trigger", "kind": "timeDaily", "minuteOfDay": 1110}],
            "conditions": [],
            "actions": [{"id": "action", "kind": "toggleAttribute", "nodeID": 11, "attributeID": 101}],
        }
        status = {"count": 1, "automations": [{
            "id": "server-rule", "name": "Abendlicht", "enabled": True, "origin": "server",
            "trigger_count": 1, "condition_count": 0, "action_count": 1,
        }], "recent_events": []}

        html = automations_page("0.14.0", status, nodes=nodes, rules=[rule], selected_id="server-rule")

        self.assertIn("Automation bearbeiten", html)
        self.assertIn("Automation speichern", html)
        self.assertIn("Soll sie nur unter bestimmten Bedingungen laufen?", html)
        self.assertIn("Roborock reinigen", html)
        self.assertIn("Lokales Licht", html)
        self.assertIn('action="/setup/automations/delete"', html)


if __name__ == "__main__":
    unittest.main()
