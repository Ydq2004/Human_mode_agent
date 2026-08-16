import sys
from pathlib import Path
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.commit_worker import CommitTask
from core.memory_commit import MemoryCommitService
from memory.schema import MemoryEntity


def _entity(concept_id="old", score=60.0):
    return MemoryEntity(
        concept_id=concept_id,
        canonical_name="旧认知",
        aliases=["旧认知"],
        memory_type="preference",
        identity_signature={"subject": "user", "relation": "likes", "object": "fruit"},
        summary="用户喜欢水果",
        emotion_score=score,
        emotion_label="中度正向",
    )


class MemoryCommitTests(unittest.TestCase):

    @staticmethod
    def _create_task():
        return CommitTask(
            "job-create", 1, "thread",
            {
                "status": "completed",
                "effects": {
                    "mood": {"mood_impact": 2},
                    "existing_memory_updates": [],
                    "new_memory_impressions": [{
                        "candidate_key": "candidate-1",
                        "emotion_score": 60.0,
                        "emotion_label": "轻微正向",
                        "candidate_valence": None,
                        "label_update": None,
                    }],
                },
                "appraisal": {
                    "emotion_assessment": {"event_valence": "mild_positive"},
                    "memory_assessment": {"memory_candidates": [{
                        "candidate_key": "candidate-1",
                        "operation": "create",
                        "target_concept_id": None,
                        "concept_name": "水果偏好",
                        "memory_type": "preference",
                        "identity_signature": {
                            "subject": "user",
                            "relation": "likes",
                            "object": "fruit",
                        },
                        "summary": "用户表示喜欢水果",
                        "aliases_add": [],
                        "tags": [],
                        "source": "user_told",
                    }]},
                },
            },
        )

    def test_existing_emotion_is_recomputed_from_current_entity(self):
        service = MemoryCommitService(identity_llm=None)
        task = CommitTask(
            "job-1", 1, "thread",
            {
                "status": "completed",
                "effects": {
                    "mood": {"mood_impact": 0},
                    "existing_memory_updates": [{
                        "concept_id": "old",
                        "change_direction": "slightly_positive",
                        "label_update": None,
                        "score_delta": 1.0,
                    }],
                },
                "appraisal": {"emotion_assessment": {}, "memory_assessment": {}},
            },
        )
        with patch("core.memory_commit.commit_mood_effect", return_value={}), \
             patch("core.memory_commit.read_entity", return_value=_entity(score=70.0)) as read, \
             patch("core.memory_commit.upsert_entity", side_effect=lambda entity: entity):
            result = service(task)

        self.assertEqual(result["emotion_updates"][0]["emotion_score"], 71.0)
        read.assert_called_with("old")

    def test_appraisal_failure_is_fail_forward_without_writes(self):
        service = MemoryCommitService(identity_llm=None)
        task = CommitTask("job-1", 1, "thread", {"status": "failed"})
        with patch("core.memory_commit.commit_mood_effect") as update:
            result = service(task)
        self.assertEqual(result["status"], "skipped_appraisal_failure")
        update.assert_not_called()

    def test_zero_mood_impact_is_delegated_to_atomic_commit(self):
        service = MemoryCommitService(identity_llm=None)
        task = CommitTask(
            "job-zero", 1, "thread",
            {
                "status": "completed",
                "effects": {"mood": {"mood_impact": 0}},
                "appraisal": {
                    "emotion_assessment": {},
                    "memory_assessment": {},
                },
            },
        )
        with patch(
            "core.memory_commit.commit_mood_effect",
            return_value={"baseline_regression": -1},
        ) as commit_mood:
            result = service(task)

        commit_mood.assert_called_once_with("thread", mood_impact=0)
        self.assertEqual(result["mood"]["baseline_regression"], -1)

    def test_nonzero_mood_impact_is_delegated_without_regression_logic(self):
        service = MemoryCommitService(identity_llm=None)
        task = self._create_task()
        with patch(
            "core.memory_commit.commit_mood_effect",
            return_value={
                "event_impact": 2,
                "baseline_regression": 0,
            },
        ) as commit_mood, patch(
            "core.memory_commit.resolve_identity_with_judge",
            return_value={"decision": "new"},
        ), patch(
            "core.memory_commit.upsert_entity",
            side_effect=lambda entity: entity,
        ):
            result = service(task)

        commit_mood.assert_called_once_with("thread", mood_impact=2)
        self.assertEqual(result["mood"]["baseline_regression"], 0)

    def test_same_candidate_reinforces_and_preserves_emotion_signal(self):
        service = MemoryCommitService(identity_llm=None)
        current = _entity(score=60.0)
        reinforced = _entity(score=60.0)
        reinforced.mention_count = 2

        with patch("core.memory_commit.commit_mood_effect", return_value={}), \
             patch("core.memory_commit.resolve_identity_with_judge", return_value={
                 "decision": "same",
                 "target_concept_id": "old",
                 "relation_type": "none",
                 "relation_direction": "none",
             }), \
             patch("core.memory_commit.reinforce_entity", return_value=reinforced), \
             patch("core.memory_commit.resolve_memory_revision", return_value={
                 "content_relation": "duplicate",
                 "revised_summary": "",
                 "reason": "同义重复",
             }), \
             patch("core.memory_commit.read_entity", return_value=current), \
             patch("core.memory_commit.upsert_entity", side_effect=lambda entity: entity):
            result = service(self._create_task())

        self.assertEqual(result["facts"][0]["status"], "reinforced")
        self.assertEqual(
            result["facts"][0]["content_relation"],
            "duplicate",
        )
        self.assertEqual(result["emotion_updates"][0]["emotion_score"], 61.0)

    def test_related_candidate_creates_entity_and_one_relation(self):
        service = MemoryCommitService(identity_llm=None)

        with patch("core.memory_commit.commit_mood_effect", return_value={}), \
             patch("core.memory_commit.resolve_identity_with_judge", return_value={
                 "decision": "related",
                 "target_concept_id": "old",
                 "relation_type": "belongs_to",
                 "relation_direction": "candidate_to_existing",
             }), \
             patch("core.memory_commit.read_entity", return_value=_entity()), \
             patch("core.memory_commit.upsert_entity", side_effect=lambda entity: entity), \
             patch("core.memory_commit.upsert_relation", side_effect=lambda relation: relation):
            result = service(self._create_task())

        self.assertEqual(result["facts"][0]["status"], "created")
        self.assertEqual(len(result["relations"]), 1)
        relation = result["relations"][0]
        self.assertEqual(relation["source_concept_id"], result["facts"][0]["concept_id"])
        self.assertEqual(relation["target_concept_id"], "old")

    def test_update_changes_fact_fields_and_increments_mention_count(self):
        service = MemoryCommitService(identity_llm=None)
        current = _entity()
        current.mention_count = 3
        task = CommitTask(
            "job-update", 1, "thread",
            {
                "status": "completed",
                "effects": {"mood": {"mood_impact": 0}},
                "appraisal": {
                    "emotion_assessment": {},
                    "memory_assessment": {"memory_candidates": [{
                        "candidate_key": "update-1",
                        "operation": "update",
                        "target_concept_id": "old",
                        "base_revision": 1,
                        "summary_update": {
                            "update_kind": "replace",
                            "new_information": "用户现在喜欢浆果",
                            "superseded_information": "用户喜欢水果",
                            "revised_summary": "用户表示现在喜欢浆果。",
                        },
                        "aliases_add": ["浆果偏好"],
                        "tags": ["偏好修正"],
                    }]},
                },
            },
        )
        with patch("core.memory_commit.commit_mood_effect", return_value={}), \
             patch("core.memory_commit.read_entity", return_value=current), \
             patch(
                 "core.memory_commit.update_entity_content",
                 side_effect=lambda entity, expected_revision: entity,
             ):
            result = service(task)

        self.assertEqual(result["facts"][0]["status"], "updated")
        self.assertEqual(current.summary, "用户表示现在喜欢浆果。")
        self.assertEqual(current.mention_count, 4)
        self.assertEqual(current.emotion_score, 60.0)

    def test_same_extend_uses_revision_resolver_and_keeps_old_information(self):
        service = MemoryCommitService(identity_llm=object())
        current = _entity()
        saved = _entity()
        saved.summary = "用户表示过去喜欢甜咖啡，现在偏好无糖咖啡。"
        saved.mention_count = 2
        saved.revision = 2
        task = self._create_task()
        task.appraisal_job["appraisal"]["memory_assessment"][
            "memory_candidates"
        ][0]["summary"] = "用户表示现在偏好无糖咖啡。"

        with patch("core.memory_commit.commit_mood_effect", return_value={}), \
             patch("core.memory_commit.resolve_identity_with_judge", return_value={
                 "decision": "same",
                 "target_concept_id": "old",
             }), \
             patch("core.memory_commit.read_entity", return_value=current), \
             patch("core.memory_commit.resolve_memory_revision", return_value={
                 "content_relation": "replace",
                 "new_information": "现在偏好无糖咖啡",
                 "superseded_information": "过去喜欢甜咖啡",
                 "revised_summary": saved.summary,
                 "reason": "用户明确说明口味变化",
             }) as resolver, \
             patch("core.memory_commit.update_entity_content", return_value=saved):
            result = service(task)

        resolver.assert_called_once()
        self.assertEqual(result["facts"][0]["status"], "updated")
        self.assertEqual(result["facts"][0]["revision"], 2)
        self.assertEqual(current.summary, saved.summary)

    def test_same_conflict_preserves_current_fact_without_reinforcing(self):
        service = MemoryCommitService(identity_llm=object())
        current = _entity()
        with patch("core.memory_commit.commit_mood_effect", return_value={}), \
             patch("core.memory_commit.resolve_identity_with_judge", return_value={
                 "decision": "same",
                 "target_concept_id": "old",
             }), \
             patch("core.memory_commit.read_entity", return_value=current), \
             patch("core.memory_commit.resolve_memory_revision", return_value={
                 "content_relation": "conflict",
                 "revised_summary": "",
                 "reason": "新旧来源冲突",
             }), \
             patch("core.memory_commit.reinforce_entity") as reinforce, \
             patch("core.memory_commit.update_entity_content") as update:
            result = service(self._create_task())

        self.assertEqual(result["facts"][0]["status"], "preserved")
        self.assertEqual(result["facts"][0]["content_relation"], "conflict")
        reinforce.assert_not_called()
        update.assert_not_called()

    def test_stale_update_revision_is_rejudged_against_latest_entity(self):
        service = MemoryCommitService(identity_llm=object())
        current = _entity()
        current.revision = 3
        task = CommitTask(
            "job-stale", 1, "thread",
            {
                "status": "completed",
                "effects": {"mood": {"mood_impact": 0}},
                "event_evidence": {"perception_event": {"content": "新信息"}},
                "appraisal": {
                    "emotion_assessment": {},
                    "memory_assessment": {"memory_candidates": [{
                        "candidate_key": "update-1",
                        "operation": "update",
                        "target_concept_id": "old",
                        "base_revision": 1,
                        "summary_update": {
                            "update_kind": "extend",
                            "new_information": "新增信息",
                            "superseded_information": "",
                            "revised_summary": "基于旧版本生成的摘要",
                        },
                    }]},
                },
            },
        )
        with patch("core.memory_commit.commit_mood_effect", return_value={}), \
             patch("core.memory_commit.read_entity", return_value=current), \
             patch("core.memory_commit.resolve_memory_revision", return_value={
                 "content_relation": "uncertain",
                 "revised_summary": "",
                 "reason": "无法与最新版本可靠合并",
             }) as resolver, \
             patch("core.memory_commit.update_entity_content") as update:
            result = service(task)

        resolver.assert_called_once()
        update.assert_not_called()
        self.assertEqual(result["facts"][0]["status"], "preserved")
        self.assertEqual(current.revision, 3)


if __name__ == "__main__":
    unittest.main()
