
"""
Agent 工厂
从角色卡配置动态创建 Agent
"""

import os
import sqlite3
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
from langgraph.checkpoint.sqlite import SqliteSaver
from langchain.agents.middleware import (
    dynamic_prompt,
    wrap_model_call,
)

from core.model_io import render_perception_times_for_model
from config import (
    APPRAISAL_LLM_TIMEOUT_SECONDS,
    APPRAISAL_LLM_MAX_TOKENS,
    APPRAISAL_LLM_TEMPERATURE,
    DEEPSEEK_API_KEY,
    LLM_MAX_RETRIES,
    MAIN_LLM_TIMEOUT_SECONDS,
    UNDERSTANDING_LLM_TIMEOUT_SECONDS,
    UNDERSTANDING_LLM_TEMPERATURE,
    CHECKPOINT_DB_PATH,
    UNDERSTANDING_LLM_MAX_TOKENS,
    WORKING_CONTEXT_TRIGGER_MESSAGES,
    WORKING_CONTEXT_KEEP_MESSAGES,
    WORKING_CONTEXT_MAX_SUMMARY_CHARS,
    WORKING_CONTEXT_LLM_TIMEOUT_SECONDS,
    WORKING_CONTEXT_LLM_TEMPERATURE,
    WORKING_CONTEXT_LLM_MAX_TOKENS,
)
from core.working_context import WorkingContextMiddleware
from persona.loader import load_persona, get_model_config, build_system_prompt
from tools.file_authorization import make_file_authorization_middleware

_RUNTIME_INJECTION = {"text": ""}


def set_current_injection(text: str) -> None:
    """设置本轮动态注入文本。该文本只进入模型请求，不作为用户消息存入 checkpoint。"""
    _RUNTIME_INJECTION["text"] = text or ""


def make_dynamic_prompt(system_prompt: str):
    @dynamic_prompt
    def runtime_prompt(request) -> str:
        injection = _RUNTIME_INJECTION.get("text", "")
        if not injection:
            return system_prompt
        return f"{system_prompt}\n\n{injection}\n\n"

    return runtime_prompt

@wrap_model_call
def render_event_times(request, handler):
    """
    每次真正调用主模型前，为历史感知事件渲染时间。

    request.override() 只替换本次模型请求中的消息，
    不会把渲染后的文本写回 checkpoint。
    """
    rendered_messages = render_perception_times_for_model(
        request.messages
    )

    model_request = request.override(
        messages=rendered_messages
    )

    return handler(model_request)


def create_agent_from_persona(tools: list = None, file_confirm=None):
      """
      读取角色卡 → 初始化 LLM → 组装 Agent。

      参数:
          tools: 工具列表。外部传入，方便在注册工具后再创建 Agent。
          file_confirm: 白名单外文件写入的外层确认回调；不传则不启用交互确认。

      返回:
          (agent, config, persona, system_prompt,
           understanding_llm, retry_understanding_llm, appraisal_llm)
      """
      if tools is None:
          tools = []

      # 加载角色卡
      persona = load_persona()

      # 获取 LLM 配置
      model_cfg = get_model_config(persona)
      provider = model_cfg.get("provider", "deepseek")

      default_base_url = "https://api.deepseek.com"
      default_model = "deepseek-v4-flash"
      

      # 根据 provider 决定 api_key
      base_url = model_cfg.get("base_url", default_base_url)
      model_name = model_cfg.get("model_name", default_model)
     
      api_key_env_name = str(
           model_cfg.get("api_key_env", "DEEPSEEK_API_KEY")
        ).strip()
      
      if not  api_key_env_name:
          api_key = DEEPSEEK_API_KEY

      api_key = os.environ.get(api_key_env_name, "").strip()

      if not api_key:
           raise RuntimeError(
           f"环境变量 {api_key_env_name} 未设置或为空"
        )

      # 2. 主 LLM (前台自然语言回复)
      llm = ChatOpenAI(
          model=model_name,
          api_key=api_key,
          base_url=base_url,
          temperature=model_cfg.get("temperature", 0.7),
          timeout=MAIN_LLM_TIMEOUT_SECONDS,
          max_retries=LLM_MAX_RETRIES,
      )

      # 3. 感知理解 LLM
      understanding_llm = ChatOpenAI(
          model=model_name,
          api_key=api_key,
          base_url=base_url,
          reasoning_effort="high",
          temperature=UNDERSTANDING_LLM_TEMPERATURE,
          max_tokens=UNDERSTANDING_LLM_MAX_TOKENS,
          timeout=UNDERSTANDING_LLM_TIMEOUT_SECONDS,
          max_retries=LLM_MAX_RETRIES,
      )

      # 4. 重试/深度感知理解 LLM
      retry_understanding_llm = ChatOpenAI(
         model=model_name,
          api_key=api_key,
          base_url=base_url,
          reasoning_effort="max",
          temperature=UNDERSTANDING_LLM_TEMPERATURE,
          max_tokens=UNDERSTANDING_LLM_MAX_TOKENS,
          timeout=UNDERSTANDING_LLM_TIMEOUT_SECONDS,
          max_retries=LLM_MAX_RETRIES,
      )

     # 5. 后台经验评估 LLM (Appraisal)
      appraisal_llm = ChatOpenAI(
         model=model_name,
          api_key=api_key,
          base_url=base_url,
          reasoning_effort="high",
          temperature=APPRAISAL_LLM_TEMPERATURE,
          max_tokens=APPRAISAL_LLM_MAX_TOKENS,
          timeout=APPRAISAL_LLM_TIMEOUT_SECONDS,
          max_retries=LLM_MAX_RETRIES,
      )

      # 6. 短期工作上下文总结 LLM。
      # 它只在 checkpoint 过长时整理旧对话，不参与主 Agent 回复、情绪评价
      # 或长期记忆写入，因此使用独立的低温度和输出上限。
      working_context_llm = ChatOpenAI(
          model=model_name,
          api_key=api_key,
          base_url=base_url,
          temperature=WORKING_CONTEXT_LLM_TEMPERATURE,
          max_tokens=WORKING_CONTEXT_LLM_MAX_TOKENS,
          timeout=WORKING_CONTEXT_LLM_TIMEOUT_SECONDS,
          max_retries=LLM_MAX_RETRIES,
      )

      

      # 对话持久化
      conn = sqlite3.connect(CHECKPOINT_DB_PATH, check_same_thread=False)
      checkpointer = SqliteSaver(conn)

      # 拼接 System Prompt
      # 角色卡负责人格、语气、知识边界和关系设定。
      # 当前主 Agent 使用普通自然语言回应，不额外强制 JSON 协议。
      system_prompt = build_system_prompt(persona)

      # 创建 Agent
      middleware = [
          make_dynamic_prompt(system_prompt),
          WorkingContextMiddleware(
              summary_llm=working_context_llm,
              trigger_messages=WORKING_CONTEXT_TRIGGER_MESSAGES,
              keep_messages=WORKING_CONTEXT_KEEP_MESSAGES,
              max_summary_chars=WORKING_CONTEXT_MAX_SUMMARY_CHARS,
          ),
          render_event_times,
      ]
      if file_confirm is not None:
          middleware.append(make_file_authorization_middleware(file_confirm))

      agent = create_agent(
          model=llm,
          tools=tools,
          system_prompt=system_prompt,
          checkpointer=checkpointer,
          middleware=middleware,
      )
      config={
          "configurable": {"thread_id": "default_user"}
         }
      return (
          agent,
          config,
          persona,
          system_prompt,
          understanding_llm,
          retry_understanding_llm,
          appraisal_llm,
          
      )
