import unittest

from core.somatic_simulation import (
    SomaticState,
    adjudicate_somatic_event,
    apply_somatic_appraisal,
    create_somatic_interoception,
    create_somatic_response,
)


class SomaticSimulationTests(unittest.TestCase):
    def test_missing_confirmation_is_uncertain(self):
        self.assertEqual(adjudicate_somatic_event({"contact_object": "user"}).status, "uncertain")

    def test_unconfirmed_event_is_rejected(self):
        decision = adjudicate_somatic_event({"event_confirmed": False, "contact_object": "user", "contact_area": "hand", "intensity": "gentle", "duration": "brief"})
        self.assertEqual(decision.status, "rejected")

    def test_confirmed_event_requires_object_and_area(self):
        decision = adjudicate_somatic_event({"event_confirmed": True, "intensity": "gentle", "duration": "brief"})
        self.assertEqual(decision.status, "uncertain")

    def test_confirmed_event_is_structured(self):
        decision = adjudicate_somatic_event({"event_confirmed": True, "contact_object": "user", "contact_area": "hand", "intensity": "gentle", "duration": "brief", "uncertainty": "low"})
        self.assertEqual(decision.status, "occurred")
        self.assertEqual(decision.contact_area, "hand")

    def test_appraisal_uses_direction_and_clamps(self):
        state, changes = apply_somatic_appraisal(SomaticState({"arousal": 0.98, "sensitivity": 0.50}), {"arousal_change": "increase", "arousal_magnitude": "intense", "sensitivity_change": "decrease", "sensitivity_magnitude": "moderate"})
        self.assertEqual(state.values["arousal"], 1.0)
        self.assertEqual(state.values["sensitivity"], 0.4)
        self.assertEqual(changes["arousal"], 0.02)
        self.assertEqual(changes["sensitivity"], -0.1)

    def test_absolute_value_cannot_bypass_direction_protocol(self):
        state, changes = apply_somatic_appraisal(SomaticState({"arousal": 0.5}), {"arousal": 1.0, "arousal_change": "unknown"})
        self.assertEqual(state.values["arousal"], 0.5)
        self.assertEqual(changes, {})

    def test_results_reuse_experience_protocol(self):
        decision = adjudicate_somatic_event({"event_confirmed": True, "contact_object": "user", "contact_area": "hand", "intensity": "gentle", "duration": "brief"})
        state = SomaticState({"arousal": 0.55})
        observation = create_somatic_interoception(decision, state, "身体有些紧张。", triggered_by_event="evt-1")
        action = create_somatic_response("身体微微颤了一下。", state, triggered_by_event="evt-1")
        self.assertEqual(observation.modality, "interoception")
        self.assertEqual(action.action_type, "somatic_response")
        self.assertEqual(action.metadata["triggered_by_event"], "evt-1")

    def test_module_request_is_optional_in_understanding_contract(self):
        from core.perception import PerceptionFrame, create_perception_event
        from core.perception_understanding import _clean_understanding

        frame = PerceptionFrame(
            perception_event=create_perception_event("user", "text", "你好"),
        )
        result = _clean_understanding({"situated_understanding": "问候"}, frame)
        self.assertEqual(result["module_requests"], [])


if __name__ == "__main__":
    unittest.main()
