"""Deletion pins tolerate only homee-owned changes from this exact batch."""

from copy import deepcopy
import json
import unittest

from server.homeegram_batch_deletion import _deletion_pin, _matches_deletion_pin


def original():
    return {
        "id": 850, "name": "Kellerlicht%20Lager", "added": 1789650898,
        "active": 0, "order": 10, "state": 1, "play": 0,
        "last_triggered": 1789650899,
        "triggers": {"switch_triggers": [{"id": 20, "homeegram_id": 850}],
                     "homeegram_triggers": []},
        "conditions": {"homeegram_conditions": []},
        "actions": {"homeegram_actions": [
            {"id": 30, "homeegram_id": 850, "target_homeegram_id": 849,
             "homeegram_event": 3, "branch": 1, "delay": 0},
            {"id": 31, "homeegram_id": 850, "target_homeegram_id": 851,
             "homeegram_event": 1, "branch": 2, "delay": 60}],
                    "webhook_actions": [{"id": 32, "url": "https://private.invalid/secret"}]},
    }


class BatchDeletionPinTests(unittest.TestCase):
    def setUp(self):
        self.baseline = original()
        self.pin = _deletion_pin(self.baseline)

    def matches(self, candidate, deleted_ids=None):
        return _matches_deletion_pin(candidate, self.pin,
                                     {849} if deleted_ids is None else deleted_ids)

    def test_unchanged_or_order_and_runtime_only_changes_match(self):
        self.assertTrue(self.matches(self.baseline))
        candidate = deepcopy(self.baseline)
        candidate.update(order=0, state=0, play=1, last_triggered=99999999)
        self.assertTrue(self.matches(candidate))

    def test_identity_activation_and_unknown_fields_remain_protected(self):
        for field, value in (("id", 851), ("name", "Other"), ("added", 1789650899),
                             ("active", 1), ("future_field", {"value": 1})):
            with self.subTest(field=field):
                candidate = deepcopy(self.baseline)
                candidate[field] = value
                self.assertFalse(self.matches(candidate))

    def test_exact_reference_removal_only_for_confirmed_deleted_target(self):
        candidate = deepcopy(self.baseline)
        del candidate["actions"]["homeegram_actions"][0]
        self.assertTrue(self.matches(candidate))
        self.assertFalse(self.matches(candidate, set()))
        self.assertFalse(self.matches(candidate, {851}))

    def test_unconfirmed_reference_cannot_be_removed(self):
        candidate = deepcopy(self.baseline)
        del candidate["actions"]["homeegram_actions"][1]
        self.assertFalse(self.matches(candidate))
        self.assertTrue(self.matches(candidate, {851}))

    def test_reference_to_deleted_hg_cannot_be_edited_in_place(self):
        for field, value in (("id", 99), ("homeegram_event", 4), ("branch", 0),
                             ("delay", 1), ("future_field", "changed")):
            with self.subTest(field=field):
                candidate = deepcopy(self.baseline)
                candidate["actions"]["homeegram_actions"][0][field] = value
                self.assertFalse(self.matches(candidate))

    def test_added_reference_even_to_deleted_target_is_rejected(self):
        candidate = deepcopy(self.baseline)
        added = deepcopy(candidate["actions"]["homeegram_actions"][0])
        added["id"] = 99
        candidate["actions"]["homeegram_actions"].append(added)
        self.assertFalse(self.matches(candidate))
        candidate["actions"]["homeegram_actions"][-1] = deepcopy(
            candidate["actions"]["homeegram_actions"][0])
        self.assertFalse(self.matches(candidate))

    def test_reference_reordering_is_rejected_even_if_both_targets_deleted(self):
        candidate = deepcopy(self.baseline)
        candidate["actions"]["homeegram_actions"].reverse()
        self.assertFalse(self.matches(candidate, {849, 851}))

    def test_all_original_references_can_disappear_after_both_targets_deleted(self):
        candidate = deepcopy(self.baseline)
        candidate["actions"]["homeegram_actions"] = []
        self.assertTrue(self.matches(candidate, {849, 851}))
        self.assertFalse(self.matches(candidate, {849}))

    def test_cleanup_supported_for_all_known_reference_collections(self):
        for section, kind, field in (("triggers", "homeegram_triggers", "source_homeegram_id"),
                                     ("conditions", "homeegram_conditions", "target_homeegram_id"),
                                     ("actions", "homeegram_actions", "target_homeegram_id")):
            with self.subTest(section=section):
                baseline = original()
                baseline[section][kind] = [{"id": 100, field: 849, "future": "unchanged"}]
                candidate = deepcopy(baseline)
                candidate[section][kind] = []
                self.assertTrue(_matches_deletion_pin(candidate, _deletion_pin(baseline), {849}))
                self.assertFalse(_matches_deletion_pin(candidate, _deletion_pin(baseline), set()))

    def test_invalid_or_missing_target_ids_never_become_removable(self):
        for target in (None, False, True, 0, -1, 849.0, "849"):
            with self.subTest(target=target):
                baseline = original()
                baseline["actions"]["homeegram_actions"] = [{"id": 100, "target_homeegram_id": target}]
                candidate = deepcopy(baseline)
                candidate["actions"]["homeegram_actions"] = []
                self.assertFalse(_matches_deletion_pin(candidate, _deletion_pin(baseline), {849}))

    def test_collection_presence_and_unknown_collection_content_are_protected(self):
        candidate = deepcopy(self.baseline)
        del candidate["triggers"]["homeegram_triggers"]
        self.assertFalse(self.matches(candidate))
        candidate = deepcopy(self.baseline)
        candidate["actions"]["future_homeegram_actions"] = []
        self.assertFalse(self.matches(candidate))
        candidate = deepcopy(self.baseline)
        candidate["actions"]["webhook_actions"][0]["url"] += "/changed"
        self.assertFalse(self.matches(candidate))

    def test_pin_contains_no_private_original_values_and_does_not_mutate_input(self):
        baseline = deepcopy(self.baseline)
        baseline["actions"]["homeegram_actions"][0]["private_extension"] = "https://private.invalid/ref-secret"
        before = deepcopy(baseline)
        encoded = json.dumps(_deletion_pin(baseline))
        self.assertEqual(before, baseline)
        for private in ("private.invalid", "ref-secret", "Kellerlicht", "private_extension"):
            self.assertNotIn(private, encoded)


if __name__ == "__main__":
    unittest.main()
