import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import main as app_main


class MainCliControlTests(unittest.TestCase):

    def test_exit_command_is_not_converted_to_perception_event(self):
        appraisal_worker = MagicMock()
        appraisal_worker.drain_finished.return_value = []
        commit_worker = MagicMock()
        commit_worker.drain_finished.return_value = []

        persona = {
            "agent_name": "测试角色",
            "exit_command": "退出",
        }
        agent_factory_result = (
            object(),
            {"configurable": {}},
            persona,
            "system prompt",
            object(),
            object(),
            object(),
        )

        with patch.object(
            app_main,
            "ensure_emotion_store",
        ), patch.object(
            app_main,
            "ensure_memory_store",
        ), patch.object(
            app_main,
            "create_agent_from_persona",
            return_value=agent_factory_result,
        ), patch.object(
            app_main,
            "initialize_genesis_memory",
        ), patch.object(
            app_main,
            "MemoryCommitService",
        ), patch.object(
            app_main,
            "CommitWorker",
            return_value=commit_worker,
        ), patch.object(
            app_main,
            "AppraisalWorker",
            return_value=appraisal_worker,
        ), patch.object(
            app_main,
            "create_perception_event",
        ) as create_event, patch.object(
            app_main,
            "process_perception_event",
        ) as process_event, patch(
            "builtins.input",
            return_value="退出",
        ), redirect_stdout(StringIO()):
            app_main.main()

        create_event.assert_not_called()
        process_event.assert_not_called()
        appraisal_worker.shutdown.assert_called_once_with(
            wait=True,
            cancel_futures=False,
        )
        commit_worker.shutdown.assert_called_once_with(wait=True)


if __name__ == "__main__":
    unittest.main()
