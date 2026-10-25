import copy
import json
import unittest
from types import SimpleNamespace

from server.homeegram_groups import expand_group_element, project_homeegram_groups


class HomeegramGroupProjectionTests(unittest.TestCase):
    def setUp(self):
        self.adapter = SimpleNamespace(resources={
            "groups": [{"id": 7, "name": "Licht%20Flur", "private": "group-secret"}],
            "relationships": [
                {"id": 1, "group_id": 7, "node_id": 12, "homeegram_id": 0, "order": 2},
                {"id": 2, "group_id": 7, "node_id": 13, "homeegram_id": 0, "order": 1},
                {"id": 3, "group_id": 7, "node_id": 12, "homeegram_id": 0, "order": 3},
                {"id": 4, "group_id": 7, "node_id": 0, "homeegram_id": 20, "order": 4},
                {"id": 5, "group_id": 99, "node_id": 99, "homeegram_id": 0, "order": 0}],
            "homeegrams": [{"id": 20, "name": "Abend%20Licht", "actions": {"url": "private-url"}}]},
            nodes={
                12: {"id": 12, "name": "Lampe%20A", "password": "node-secret", "attributes": [
                    {"id": 55, "type": 1, "current_value": 0, "editable": True, "minimum": 0, "maximum": 1,
                     "data": "private-data", "description": "private-description"},
                    {"id": 56, "type": 3, "current_value": 10, "unit": "W", "editable": False}]},
                13: {"id": 13, "name": "Lampe B", "attributes": [
                    {"id": 57, "type": 1, "current_value": 1, "editable": False}]},
                99: {"id": 99, "name": "Andere Gruppe", "attributes": [{"id": 99, "type": 1, "current_value": 1}]}})
        self.row = {"id": 3, "group_id": 7, "attribute_type": 1, "value": 1,
                    "command": 1, "operator": 1, "operand": 1, "scope": 2,
                    "branch": 1, "check_moment": 3, "delay": 10}

    def expand(self, section="conditions", **changes):
        return expand_group_element(self.adapter, section, {**self.row, **changes})

    def test_members_are_source_local_ordered_deduplicated_and_keep_only_matching_types(self):
        result = self.expand()
        group = result["group"]
        self.assertEqual("Licht Flur", group["name"])
        self.assertEqual("An/Aus", group["attribute_name"])
        self.assertEqual([13, 12], [member["node_id"] for member in group["members"]])
        self.assertEqual([[57], [55]], [[attribute["id"] for attribute in member["attributes"]] for member in group["members"]])
        self.assertTrue(group["membership_complete"])
        self.assertFalse(group["missing"])
        self.assertNotIn(99, [member["node_id"] for member in group["members"]])

    def test_homeegram_members_are_distinct_and_never_treated_as_devices(self):
        group = self.expand()["group"]
        self.assertEqual([{"homeegram_id": 20, "name": "Abend Licht", "missing": False, "applicable": False}], group["homeegrams"])
        self.assertNotIn(0, [member["node_id"] for member in group["members"]])
        self.assertIn("keine Geräte", " ".join(group["warnings"]))

    def test_actions_require_editable_attributes_without_hiding_read_only_members(self):
        group = self.expand("actions")["group"]
        readonly, writable = group["members"]
        self.assertFalse(readonly["applicable"])
        self.assertFalse(readonly["attributes"][0]["applicable"])
        self.assertTrue(writable["applicable"])
        self.assertEqual("Aus", writable["attributes"][0]["current_value_label"])
        self.assertEqual("An", writable["attributes"][0]["target_value_label"])
        self.assertIn("nicht als schreibbar", " ".join(group["warnings"]))

    def test_private_fields_urls_and_string_values_never_cross_projection(self):
        self.adapter.nodes[12]["attributes"][0].update(current_value="https://secret.example", unit="https://unit.secret")
        row = {**self.row, "value": "https://value.secret", "url": "https://private.secret", "body": "private-body"}
        result = expand_group_element(self.adapter, "conditions", row)
        serialized = json.dumps(result)
        for private in ("private", "secret", "password", "https://"):
            self.assertNotIn(private, serialized)
        self.assertNotIn("value", result)
        self.assertEqual("Wert unbekannt", result["group"]["members"][1]["attributes"][0]["current_value_label"])

    def test_missing_group_relationships_node_attribute_and_hg_stay_explicit(self):
        del self.adapter.resources["groups"]
        result = self.expand()
        self.assertTrue(result["group"]["missing"])
        self.assertFalse(result["group"]["membership_complete"])
        del self.adapter.resources["relationships"]
        self.assertTrue(self.expand()["group"]["missing_relationships"])
        self.assertFalse(self.expand()["group"]["membership_complete"])
        self.adapter.resources["relationships"] = [{"group_id": 7, "node_id": 800, "homeegram_id": 999}]
        group = self.expand()["group"]
        self.assertTrue(group["members"][0]["missing"])
        self.assertTrue(group["members"][0]["missing_attribute"])
        self.assertIsNone(group["members"][0]["attributes"][0]["id"])
        self.assertFalse(group["members"][0]["attributes"][0]["applicable"])
        self.assertTrue(group["homeegrams"][0]["missing"])
        self.assertFalse(group["membership_complete"])

    def test_existing_node_with_wrong_type_is_visible_placeholder_not_an_assumed_target(self):
        group = self.expand(attribute_type=25)["group"]
        for member in group["members"]:
            self.assertFalse(member["missing"])
            self.assertTrue(member["missing_attribute"])
            self.assertFalse(member["applicable"])
            self.assertEqual("Bewegung", member["attributes"][0]["name"])

    def test_group_members_never_fall_back_to_other_adapter_with_same_id(self):
        other = SimpleNamespace(resources=copy.deepcopy(self.adapter.resources), nodes=copy.deepcopy(self.adapter.nodes))
        other.nodes[12]["name"] = "Andere Quelle"
        del self.adapter.nodes[12]
        own = self.expand()["group"]["members"][1]
        self.assertEqual("Gerät 12", own["name"])
        self.assertTrue(own["missing"])
        self.assertEqual("Andere Quelle", expand_group_element(other, "actions", self.row)["group"]["members"][1]["name"])

    def test_all_native_scopes_and_unknown_codes_preserve_numeric_semantics(self):
        for scope, label in ((1, "kein Gerät"), (2, "alle Geräte"), (3, "mindestens ein Gerät"), (4, "nicht alle Geräte")):
            result = self.expand(scope=scope, operator=6)
            self.assertEqual(scope, result["scope"])
            self.assertIn(label.lower(), result["summary"])
            self.assertIn("≠ An", result["summary"])
        unknown = self.expand(scope=88, operator=77, branch=9, check_moment=8)
        self.assertIn("unbekannter umfang 88", unknown["summary"])
        self.assertIn("Unbekannter Operator 77", unknown["summary"])
        self.assertEqual(9, unknown["branch"])
        self.assertEqual(8, unknown["check_moment"])

    def test_trigger_crossings_and_change_threshold_are_not_condition_comparisons(self):
        for operator, phrase in ((1, "steigt über"), (2, "fällt unter"), (3, "wechselt zu")):
            self.assertIn(phrase, self.expand("triggers", operator=operator)["summary"])
        self.assertIn("jede Änderung", self.expand("triggers", operator=4, value=0)["summary"])
        threshold = self.expand("triggers", operator=4, value=1)
        self.assertIn("ändert sich um mehr als 1", threshold["summary"])
        self.assertNotIn("mehr als An", threshold["summary"])
        self.assertIn("mindestens einem Gerät", threshold["summary"])

    def test_group_action_command_codes_and_source_references_are_not_reinterpreted(self):
        for command, phrase in ((1, "setzen auf An"), (2, "Aktuellen Wert von Attribut 987"),
                                (3, "Umschalten"), (4, "Erhöhen"), (5, "Verringern"),
                                (6, "Erhöhen"), (7, "Verringern"), (66, "Unbekannter Befehl 66")):
            result = self.expand("actions", command=command, source_attribute_id=987)
            self.assertEqual(command, result["command"])
            self.assertIn(phrase, result["summary"])
            self.assertIn("nach 10 Sek.", result["summary"])
        self.assertIn("Aktuellen Wert von Gerät 12", self.expand("actions", command=2, source_attribute_id=0, source_node_id=12)["summary"])
        reference = self.expand(operand=2, operand_id=987, value=1)
        self.assertEqual(987, reference["operand_id"])
        self.assertIn("Wert von Attribut 987", reference["summary"])
        self.assertNotIn("= An", reference["summary"])

    def test_weather_motion_contact_blind_labels_are_human_and_action_specific(self):
        cases = ((243, 7, "Bewölkt"), (246, 0, "Sonnig"), (25, 0, "Keine Bewegung"),
                 (25, 1, "Bewegung erkannt"), (10, 2, "Gekippt"), (135, 1, "Geschlossen"))
        for attribute_type, value, expected in cases:
            self.adapter.nodes[12]["attributes"] = [{"id": 55, "type": attribute_type, "current_value": value, "editable": True}]
            result = self.expand(attribute_type=attribute_type, value=value)
            attribute = result["group"]["members"][1]["attributes"][0]
            self.assertEqual(expected, attribute["current_value_label"])
            self.assertEqual(expected, attribute["target_value_label"])
        blind = self.expand("actions", attribute_type=135, value=1)["group"]["members"][1]["attributes"][0]
        self.assertEqual("Geschlossen", blind["current_value_label"])
        self.assertEqual("Schließen", blind["target_value_label"])

    def test_snapshot_and_unknown_data_are_not_mutated_or_evaluated(self):
        before = copy.deepcopy((self.adapter.resources, self.adapter.nodes, self.row))
        result = self.expand()
        result["group"]["members"][0]["attributes"][0]["current_value"] = 800
        self.assertEqual(before, (self.adapter.resources, self.adapter.nodes, self.row))
        self.assertNotIn("fulfilled", result)
        self.assertNotIn("evaluated", result)
        self.adapter.resources["relationships"] = []
        self.assertEqual([], self.expand()["group"]["members"])
        self.assertTrue(self.expand()["group"]["membership_complete"])

    def test_group_only_read_projection_never_copies_private_other_elements(self):
        homeegram = {"name": "HG", "conditions": {"group_conditions": [self.row],
                      "webhook_conditions": [{"url": "private"}]}, "actions": ["malformed"]}
        projected = project_homeegram_groups(self.adapter, homeegram)
        self.assertEqual({"triggers", "conditions", "actions"}, set(projected))
        self.assertEqual([], projected["actions"]["group_actions"])
        self.assertNotIn("private", json.dumps(projected))
        self.assertEqual(1, len(projected["conditions"]["group_conditions"]))
        with self.assertRaises(ValueError):
            self.expand("wrong")


if __name__ == "__main__":
    unittest.main()
