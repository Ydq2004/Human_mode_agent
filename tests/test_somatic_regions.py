import unittest
from datetime import datetime, timedelta, timezone

from core.somatic_extensions import DisabledSomaticExtension, SomaticExtensionResult, apply_extension_result
from core.somatic_simulation import (
    SomaticState,
    adjudicate_somatic_event,
    apply_somatic_appraisal,
    decay_somatic_state,
)


class SomaticRegionTests(unittest.TestCase):
    def test_multi_contact_event_keeps_only_registered_regions(self):
        decision = adjudicate_somatic_event({
            "event_confirmed": True,
            "contact_object": "user",
            "contacts": [
                {"region_id": "head_scalp", "contact_type": "stroke", "intensity": "gentle", "duration": "brief"},
                {"region_id": "unknown_region", "contact_type": "touch", "intensity": "gentle", "duration": "brief"},
                {"region_id": "hand", "contact_type": "hold", "intensity": "moderate", "duration": "sustained"},
            ],
        })
        self.assertEqual(decision.status, "occurred")
        self.assertEqual([item.region_id for item in decision.contacts], ["head_scalp", "hand"])

    def test_unknown_only_region_does_not_become_a_guessed_contact(self):
        decision = adjudicate_somatic_event({
            "event_confirmed": True,
            "contact_object": "user",
            "contacts": [{"region_id": "unknown_region", "intensity": "gentle", "duration": "brief"}],
        })
        self.assertEqual(decision.status, "uncertain")
        self.assertEqual(decision.contacts, ())

    def test_common_chinese_region_alias_is_normalized(self):
        decision = adjudicate_somatic_event({
            "event_confirmed": True,
            "contact_object": "user",
            "contacts": [{"region_id": "头皮", "intensity": "gentle", "duration": "brief"}],
        })
        self.assertEqual(decision.status, "occurred")
        self.assertEqual(decision.contacts[0].region_id, "head_scalp")

    def test_regional_update_does_not_change_unrelated_region(self):
        state, _ = apply_somatic_appraisal(
            SomaticState({"arousal": 0.5, "sensitivity": 0.5}),
            {
                "arousal_change": "unchanged",
                "arousal_magnitude": "slight",
                "sensitivity_change": "increase",
                "sensitivity_magnitude": "moderate",
            },
            contacts=adjudicate_somatic_event({
                "event_confirmed": True,
                "contact_object": "user",
                "contact_area": "head_scalp",
                "intensity": "moderate",
                "duration": "sustained",
            }).contacts,
        )
        self.assertIn("head_scalp", state.region_states())
        self.assertNotIn("hand", state.region_states())

    def test_decay_is_pure_and_moves_towards_profile_baseline(self):
        old = datetime.now(timezone.utc) - timedelta(minutes=180)
        state, _ = apply_somatic_appraisal(
            SomaticState({"regions": {"head_scalp": {
                "short_term_sensitivity": 1.0,
                "activation": 1.0,
                "last_stimulated_at": old.isoformat(),
            }}}),
            {"region_updates": []},
            contacts=(),
        )
        decayed = decay_somatic_state(state, now=datetime.now(timezone.utc))
        self.assertGreater(decayed.region_states()["head_scalp"].short_term_sensitivity, 0.5)
        self.assertLess(decayed.region_states()["head_scalp"].short_term_sensitivity, 1.0)
        self.assertEqual(state.region_states()["head_scalp"].short_term_sensitivity, 1.0)

    def test_disabled_extension_does_not_create_private_state(self):
        state = SomaticState({"arousal": 0.5})
        result = DisabledSomaticExtension().apply()
        self.assertEqual(result.status, "disabled")
        self.assertNotIn("extension_state", apply_extension_result(state, result).to_dict())

    def test_extension_cannot_write_generic_state(self):
        with self.assertRaises(ValueError):
            apply_extension_result(
                SomaticState({"arousal": 0.5}),
                SomaticExtensionResult(status="updated", state_patch={"arousal": 1.0}),
            )


if __name__ == "__main__":
    unittest.main()
