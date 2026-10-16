import json
import unittest

from core.somatic_appraisal import appraise_somatic_event, clean_somatic_appraisal
from core.somatic_simulation import SomaticState, adjudicate_somatic_event


class FakeResponse:
    def __init__(self, content):
        self.content = content


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload

    def bind(self, **kwargs):
        return self

    def invoke(self, prompt):
        return FakeResponse(json.dumps(self.payload, ensure_ascii=False))


def decision():
    return adjudicate_somatic_event({
        "event_confirmed": True,
        "contact_object": "user",
        "contact_area": "hand",
        "intensity": "gentle",
        "duration": "brief",
    })


class SomaticAppraisalTests(unittest.TestCase):
    def test_valid_payload_is_cleaned(self):
        result = appraise_somatic_event(
            llm=FakeLLM({
                "arousal_change": "increase",
                "arousal_magnitude": "slight",
                "sensitivity_change": "unchanged",
                "sensitivity_magnitude": "slight",
                "visible_reaction": "手指轻轻收紧",
                "interoception_description": "掌心感到温热",
            }),
            decision=decision(),
            state=SomaticState({"arousal": 0.5}),
            event_context={},
        )
        self.assertEqual(result.status, "normal")

    def test_forbidden_absolute_or_relationship_field_fails(self):
        with self.assertRaises(ValueError):
            clean_somatic_appraisal({
                "arousal": 0.9,
                "arousal_change": "increase",
                "arousal_magnitude": "slight",
                "sensitivity_change": "unchanged",
                "sensitivity_magnitude": "slight",
                "visible_reaction": "x",
                "interoception_description": "y",
            })

    def test_llm_failure_is_explicit(self):
        class Failing:
            def bind(self, **kwargs):
                return self

            def invoke(self, prompt):
                raise RuntimeError("offline")

        result = appraise_somatic_event(
            llm=Failing(),
            decision=decision(),
            state=SomaticState({"arousal": 0.5}),
            event_context={},
        )
        self.assertEqual(result.status, "failed")

    def test_non_occurred_event_never_calls_llm(self):
        result = appraise_somatic_event(
            llm=FakeLLM({}),
            decision=adjudicate_somatic_event({"event_confirmed": False, "contact_object": "user", "contact_area": "hand", "intensity": "gentle", "duration": "brief"}),
            state=SomaticState({"arousal": 0.5}),
            event_context={},
        )
        self.assertEqual(result.status, "failed")
