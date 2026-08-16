import sys
from pathlib import Path
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.experience import (
    create_agent_action,
    create_experience_slice,
)
from core.experience_appraisal import (
    ExperienceAppraisal,
    compute_appraisal_effects,
)
from core.perception import create_perception_event


class AppraisalEffectsTests(unittest.TestCase):

    def _create_experience(self):
        event = create_perception_event(
            source="user",
            modality="text",
            content="我很喜欢酸甜口的水果。",
        )

        action = create_agent_action(
            action_type="visible_response",
            content="我记住了。",
        )

        return create_experience_slice(
            perception_event=event,
            perception_understanding={},
            activated_memory_refs=[
                {
                    "concept_id": "cog_fruit",
                    "emotion_score": 60.0,
                },
            ],
            response_or_actions=[action],
            observations=[],
            state_snapshot={
                "owner": "agent",
                "mood": 60,
                "energy": 100,
            },
        )

    def test_computes_all_regions_without_writing(self):
        experience = self._create_experience()

        appraisal = ExperienceAppraisal(
            experience_review={},
            emotion_assessment={
                "event_relevance": "high",
                "event_valence": "mild_positive",
                "salience": "high",
                "affected_memories": [
                    {
                        "concept_id": "cog_fruit",
                        "change_direction": "slightly_positive",
                        "strength": "slight",
                        "label_update": {
                            "label": "更加喜欢",
                            "polarity": "positive",
                            "strength": "moderate",
                        },
                    },
                ],
            },
            memory_assessment={
                "new_memory_impressions": [
                    {
                        "candidate_key": "candidate_1",
                        "candidate_valence": None,
                        "persona_effect": "fitting",
                        "direct_related_concept_ids": [
                            "cog_fruit",
                        ],
                        "label_update": {
                            "label": "产生兴趣",
                            "polarity": "positive",
                            "strength": "moderate",
                        },
                        "fallback_to_neutral": False,
                    },
                ],
            },
        )

        before = appraisal.to_dict()

        result = compute_appraisal_effects(
            experience,
            appraisal,
            mood_reactivity=1.0,
        )

        self.assertEqual(result["mood"]["mood_impact"], 4)

        existing_update = result["existing_memory_updates"][0]
        self.assertEqual(
            existing_update["change_direction"],
            "slightly_positive",
        )
        self.assertEqual(existing_update["score_delta"], 1.0)
        self.assertEqual(
            existing_update["label_update"]["label"],
            "更加喜欢",
        )
        self.assertNotIn("emotion_score", existing_update)
        self.assertNotIn("emotion_label", existing_update)

        self.assertEqual(
            result["new_memory_impressions"][0]["emotion_score"],
            63.0,
        )
        self.assertEqual(
            result["new_memory_impressions"][0]["emotion_label"],
            "产生兴趣",
        )

        self.assertEqual(appraisal.to_dict(), before)

    def test_invalid_existing_memory_score_is_skipped(self):
        experience = self._create_experience()

        appraisal = ExperienceAppraisal(
            experience_review={},
            emotion_assessment={
                "event_relevance": "high",
                "event_valence": "mild_positive",
                "salience": "medium",
                "affected_memories": [
                    {
                        "concept_id": "cog_fruit",
                        "change_direction": "strengthened",
                        "strength": "strong",
                        "label_update": None,
                    },
                ],
            },
            memory_assessment={
                "new_memory_impressions": [],
            },
        )

        experience = create_experience_slice(
            perception_event=experience.perception_event,
            perception_understanding={},
            activated_memory_refs=[
                {
                    "concept_id": "cog_fruit",
                    "emotion_score": "invalid",
                },
            ],
            response_or_actions=list(
                experience.response_or_actions
            ),
            observations=[],
            state_snapshot=dict(
                experience.state_snapshot
            ),
        )

        result = compute_appraisal_effects(
            experience,
            appraisal,
            mood_reactivity=1.0,
        )

        self.assertEqual(
            result["existing_memory_updates"],
            [],
        )


if __name__ == "__main__":
    unittest.main()
