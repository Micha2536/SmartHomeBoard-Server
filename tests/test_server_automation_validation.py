"""Regression coverage for extracting the portal's native rule validator.

The main entry-point check runs in a fresh process with a temporary database.
Neither the application lifespan nor an adapter or automation timer is started.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from server.automation_validation import validate_web_automation
from server.database import Database


ROOT = Path(__file__).resolve().parents[1]


class ServerAutomationValidationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.db = Database(self.folder.name)
        self.nodes = [
            {"id": 12, "name": "Licht", "integration_module": "homee", "attributes": [
                {"id": 55, "type": 1, "name": "An/Aus", "editable": True, "minimum": 0, "maximum": 1},
                {"id": 56, "type": 3, "name": "Leistung", "editable": False}]},
            {"id": 20, "name": "Roborock", "integration_module": "roborock", "attributes": []},
            {"id": 30, "name": "Sonos", "integration_module": "sonos", "attributes": []},
        ]
        self.groups = [{"id": "test-private-db-group", "name": "Test-Gruppe", "node_ids": [12]}]
        self.db.set_setting("device_groups", self.groups)

    def tearDown(self):
        self.db._connection.close()
        self.folder.cleanup()

    def full_rule(self):
        attribute = {"kind": "attribute", "nodeID": 12, "attributeID": 55,
                     "comparison": "equal", "value": 1, "holdSeconds": 2.5}
        group = {"kind": "groupAttribute", "groupID": "test-private-db-group", "attributeType": 1,
                 "attributeName": "An/Aus", "aggregation": "exactly", "count": 1,
                 "comparison": "notEqual", "value": 0}
        return {
            "id": "web-validation-regression", "name": "Portal-Automation", "isEnabled": True,
            "triggers": [dict(attribute, id="attribute-trigger"),
                {"id": "changed-trigger", "kind": "attributeChanged", "nodeID": 12, "attributeID": 55},
                {"id": "delta-trigger", "kind": "attributeChangedBy", "nodeID": 12, "attributeID": 56,
                 "comparison": "greater", "value": 12.5, "changeUnit": "percent"},
                dict(group, id="group-trigger"),
                {"id": "daily-trigger", "kind": "timeDaily", "minuteOfDay": 420},
                {"id": "once-trigger", "kind": "timeOnce", "scheduledAt": "2027-01-01T07:00:00+01:00",
                 "isConsumed": True}],
            "prerequisites": [dict(attribute, id="prerequisite")],
            "conditions": [dict(group, id="group-condition"),
                {"id": "after", "kind": "timeAfter", "minuteOfDay": 420},
                {"id": "before", "kind": "timeBefore", "minuteOfDay": 1320}],
            "elseConditions": [dict(attribute, id="else-condition", comparison="notEqual")],
            "alwaysActions": [{"id": "toggle", "kind": "toggleAttribute", "nodeID": 12,
                               "attributeID": 55, "delaySeconds": 1.25}],
            "actions": [{"id": "set", "kind": "setAttribute", "nodeID": 12, "attributeID": 55,
                         "value": 1, "delaySeconds": 2.25}],
            "directElseActions": [{"id": "roborock", "kind": "roborockCleaning", "nodeID": 20,
                "roborockTarget": "room", "roborockTargetValue": 3, "roborockCleaningType": 1,
                "roborockSuction": 2, "roborockWater": 3, "delaySeconds": 3.25}],
            "elseActions": [{"id": "sonos", "kind": "sonosSpeak", "nodeID": 30, "sonosText": "Test {value}",
                "sonosVolume": 25, "sonosRestorePlayback": False, "delaySeconds": 4.25}],
            "elseElseActions": [{"id": "automation", "kind": "controlAutomation", "automationID": "target",
                "automationOperation": "disable", "delaySeconds": 5.25},
                {"id": "push", "kind": "serverPushNotification", "title": "Hinweis", "message": "Test {value}",
                 "includeAttributeValue": True, "nodeID": 12, "attributeID": 56,
                 "pushDeviceIDs": ["phone"], "delaySeconds": 6.25}],
            "conditionValidation": "both", "cooldownSeconds": 12.5,
        }

    def test_all_native_paths_triggers_conditions_actions_and_fields_survive_extraction(self):
        source = self.full_rule()
        result = validate_web_automation(source, self.nodes, self.db)
        for field in ("id", "name", "isEnabled", "conditionValidation", "cooldownSeconds"):
            self.assertEqual(source[field], result[field], field)
        for section in ("triggers", "prerequisites", "conditions", "elseConditions", "alwaysActions",
                        "actions", "directElseActions", "elseActions", "elseElseActions"):
            self.assertEqual(len(source[section]), len(result[section]), section)
            for expected, actual in zip(source[section], result[section]):
                for field, value in expected.items():
                    self.assertEqual(value, actual[field], (section, field))
        self.assertEqual(6, len(result["triggers"]))
        self.assertEqual({"toggleAttribute", "setAttribute", "roborockCleaning", "sonosSpeak",
            "controlAutomation", "serverPushNotification"}, {action["kind"] for section in (
            "alwaysActions", "actions", "directElseActions", "elseActions", "elseElseActions") for action in result[section]})

    def test_main_wrapper_uses_injected_database_and_matches_helper_without_lifespan(self):
        source = self.full_rule()
        script = """
import json, os, sys
from unittest.mock import patch
from server.database import Database
import server.main as main
payload = json.load(sys.stdin)
database = Database(os.environ['SHB_DATA_DIR'])
database.set_setting('device_groups', payload['groups'])
with patch.object(main, 'database', database):
    result = main.validate_web_automation(payload['source'], payload['nodes'])
print(json.dumps(result))
database._connection.close()
main.database._connection.close()
"""
        environment = {**os.environ, "SHB_DATA_DIR": self.folder.name, "SHB_MODULE_DIR": str(ROOT / "modules"),
            "SHB_API_TOKEN": "isolated-test-token", "SHB_DISABLE_SELF_RESTART": "1", "SHB_DISABLE_DISPLAY_DISCOVERY": "1"}
        process = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=environment,
            input=json.dumps({"source": source, "nodes": self.nodes, "groups": self.groups}),
            text=True, capture_output=True, timeout=30)
        self.assertEqual(0, process.returncode, process.stderr)
        self.assertEqual(validate_web_automation(source, self.nodes, self.db), json.loads(process.stdout))

    def test_web_legacy_coercion_defaults_and_clamps_are_not_mcp_strictness(self):
        source = {"id": "legacy", "name": "  Web-Regel  ", "conditionValidation": "legacy-value", "cooldownSeconds": 90000,
            "triggers": [{"id": "daily", "kind": "timeDaily", "minuteOfDay": 9000, "holdSeconds": -10}],
            "actions": [{"id": "set", "kind": "setAttribute", "nodeID": "12", "attributeID": "55",
                         "value": "1", "delaySeconds": 90000}]}
        result = validate_web_automation(source, self.nodes, self.db)
        self.assertTrue(result["isEnabled"])
        self.assertEqual("Web-Regel", result["name"])
        self.assertEqual("triggerTime", result["conditionValidation"])
        self.assertEqual(86400, result["cooldownSeconds"])
        self.assertEqual(1439, result["triggers"][0]["minuteOfDay"])
        self.assertEqual(0, result["triggers"][0]["holdSeconds"])
        self.assertEqual(86400, result["actions"][0]["delaySeconds"])
        self.assertEqual(12, result["actions"][0]["nodeID"])
        self.assertEqual(1, result["actions"][0]["value"])

    def test_web_manual_rules_all_validation_moments_and_each_control_command_still_work(self):
        for validation in ("triggerTime", "executionTime", "both"):
            for operation in ("play", "stop", "enable", "disable"):
                source = {"name": "Manuell", "triggers": [], "conditionValidation": validation,
                    "actions": [{"kind": "controlAutomation", "automationID": "existing", "automationOperation": operation}]}
                result = validate_web_automation(source, self.nodes, self.db)
                self.assertEqual([], result["triggers"])
                self.assertEqual(validation, result["conditionValidation"])
                self.assertEqual(operation, result["actions"][0]["automationOperation"])
                self.assertTrue(result["id"])
                self.assertTrue(result["actions"][0]["id"])

    def test_groups_are_resolved_from_each_call_database_not_global_state(self):
        source = self.full_rule()
        validate_web_automation(source, self.nodes, self.db)
        self.db.set_setting("device_groups", [])
        with self.assertRaisesRegex(ValueError, "Gerätegruppe"):
            validate_web_automation(source, self.nodes, self.db)

    def test_web_rejects_missing_devices_readonly_or_out_of_bounds_writes_and_no_actions(self):
        base = {"name": "Test", "actions": [{"kind": "setAttribute", "nodeID": 12, "attributeID": 55, "value": 1}]}
        bad = [{"actions": []}, {"actions": [{**base["actions"][0], "nodeID": 999}]},
            {"actions": [{**base["actions"][0], "attributeID": 56}]},
            {"actions": [{**base["actions"][0], "value": 2}]},
            {"actions": [{"kind": "showPopup", "message": "App-only"}]},
            {"triggers": [{"kind": "timeOnce", "scheduledAt": "invalid"}]},
            {"conditions": [{"kind": "attributeChanged", "nodeID": 12, "attributeID": 55}]}]
        for override in bad:
            with self.subTest(override=override), self.assertRaises(ValueError):
                validate_web_automation({**deepcopy(base), **override}, self.nodes, self.db)


if __name__ == "__main__":
    unittest.main()
