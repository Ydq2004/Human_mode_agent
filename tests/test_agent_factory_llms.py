import sys
from pathlib import Path
import unittest
from unittest.mock import patch, sentinel

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import core.agent_factory as factory
from config import (
    APPRAISAL_LLM_TIMEOUT_SECONDS,
    APPRAISAL_LLM_MAX_TOKENS,
    APPRAISAL_LLM_TEMPERATURE,
    LLM_MAX_RETRIES,
    MAIN_LLM_TIMEOUT_SECONDS,
    UNDERSTANDING_LLM_TIMEOUT_SECONDS,
    UNDERSTANDING_LLM_MAX_TOKENS,
    UNDERSTANDING_LLM_TEMPERATURE,
)


class AgentFactoryLLMTests(unittest.TestCase):

    def test_background_llms_have_separate_configs(self):
        persona = {
            "agent_name": "测试角色",
            "emotion_profile": {
                "mood_reactivity": 1.0,
            },
        }
        model_config = {
            "provider": "deepseek",
            "model_name": "test-model",
            "base_url": "https://example.invalid",
            "temperature": 0.7,
        }

        clients = [
            sentinel.main_llm,
            sentinel.understanding_llm,
            sentinel.retry_understanding_llm,
            sentinel.appraisal_llm,
        ]

        with (
            patch.object(
                factory,
                "load_persona",
                return_value=persona,
            ),
            patch.object(
                factory,
                "get_model_config",
                return_value=model_config,
            ),
            patch.object(
                factory,
                "build_system_prompt",
                return_value="system prompt",
            ),
            patch.object(
                factory,
                "ChatOpenAI",
                side_effect=clients,
            ) as chat_openai,
            patch.object(
                factory.sqlite3,
                "connect",
                return_value=sentinel.connection,
            ),
            patch.object(
                factory,
                "SqliteSaver",
                return_value=sentinel.checkpointer,
            ),
            patch.object(
                factory,
                "make_dynamic_prompt",
                return_value=sentinel.dynamic_prompt,
            ),
            patch.object(
                factory,
                "SummarizationMiddleware",
                return_value=sentinel.summarizer,
            ),
            patch.object(
                factory,
                "create_agent",
                return_value=sentinel.agent,
            ),
        ):
            result = factory.create_agent_from_persona(tools=[])

        self.assertEqual(len(chat_openai.call_args_list), 4)

        main_call = chat_openai.call_args_list[0]
        self.assertEqual(
            main_call.kwargs["timeout"],
            MAIN_LLM_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            main_call.kwargs["max_retries"],
            LLM_MAX_RETRIES,
        )

        understanding_call = chat_openai.call_args_list[1]
        self.assertEqual(
            understanding_call.kwargs["temperature"],
            UNDERSTANDING_LLM_TEMPERATURE,
        )
        self.assertEqual(
            understanding_call.kwargs["max_tokens"],
            UNDERSTANDING_LLM_MAX_TOKENS,
        )
        self.assertEqual(
            understanding_call.kwargs["timeout"],
            UNDERSTANDING_LLM_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            understanding_call.kwargs["max_retries"],
            LLM_MAX_RETRIES,
        )

        retry_understanding_call = chat_openai.call_args_list[2]
        self.assertEqual(
            retry_understanding_call.kwargs["reasoning_effort"],
            "max",
        )
        self.assertEqual(
            retry_understanding_call.kwargs["max_tokens"],
            UNDERSTANDING_LLM_MAX_TOKENS,
        )
        self.assertEqual(
            retry_understanding_call.kwargs["timeout"],
            UNDERSTANDING_LLM_TIMEOUT_SECONDS,
        )

        appraisal_call = chat_openai.call_args_list[3]
        self.assertEqual(
            appraisal_call.kwargs["temperature"],
            APPRAISAL_LLM_TEMPERATURE,
        )
        self.assertEqual(
            appraisal_call.kwargs["max_tokens"],
            APPRAISAL_LLM_MAX_TOKENS,
        )
        self.assertEqual(
            appraisal_call.kwargs["timeout"],
            APPRAISAL_LLM_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            appraisal_call.kwargs["max_retries"],
            LLM_MAX_RETRIES,
        )

        self.assertIs(result[4], sentinel.understanding_llm)
        self.assertIs(result[5], sentinel.retry_understanding_llm)
        self.assertIs(result[6], sentinel.appraisal_llm)


if __name__ == "__main__":
    unittest.main()
