
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

from core.model_io import render_native_images_for_model, render_perception_times_for_model
from tools.vision_tools import (
    current_vision_event_id,
    render_native_vision_for_model,
)
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
    SOMATIC_LLM_TIMEOUT_SECONDS,
    SOMATIC_LLM_TEMPERATURE,
    SOMATIC_LLM_MAX_TOKENS,
    VISION_LLM_TIMEOUT_SECONDS,
    VISION_LLM_TEMPERATURE,
    VISION_LLM_MAX_TOKENS,
    VISION_LLM_CONFIG,
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
    rendered_messages = render_native_images_for_model(
        rendered_messages,
        current_event_id=current_vision_event_id(),
    )
    # 视觉工具返回的 native 引用只在这次模型请求中展开为图片块。
    # 具体 runtime 由 process_perception_event 在 agent.invoke 外层绑定。
    rendered_messages = render_native_vision_for_model(rendered_messages)

    model_request = request.override(
        messages=rendered_messages
    )

    return handler(model_request)


def create_agent_from_persona(
    tools: list = None,
    file_confirm=None,
    *,
    return_resources: bool = False,
):
      """
      读取角色卡 → 初始化 LLM → 组装 Agent。

      参数:
          tools: 工具列表。外部传入，方便在注册工具后再创建 Agent。
          file_confirm: 白名单外文件写入的外层确认回调；不传则不启用交互确认。

      返回:
          (agent, config, persona, system_prompt,
           understanding_llm, retry_understanding_llm, appraisal_llm,
          somatic_llm, vision_llm)
          When ``return_resources`` is true, an additional final dictionary
          contains the checkpoint connection so an application runtime can
          close it before rebuilding a slot or thread.
      """
      if tools is None:
          tools = []

      # 加载角色卡
      persona = load_persona()

      # 获取 LLM 配置
      model_cfg = get_model_config(persona)
      # 能力声明是适配器选择：明确 native 才把图片交给主模型；未声明或
      # 无法识别的值统一走独立视觉工具，避免工具注册但实际没有后端。
      requested_vision_mode = ((model_cfg.get("capabilities") or {}).get("vision"))
      vision_mode = "native" if requested_vision_mode == "native" else "tool"
      # 视觉工具在两种模式都可以注册：native 模式下它负责把 Agent 找到的
      # 路径临时送回主模型，tool 模式下才调用独立视觉模型。
      effective_tools = list(tools)
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

      # 7. 身体评价 LLM。它只解释已确认的身体事件，不参与事件裁决，
      # 也不能直接决定身体状态绝对值、同意、关系或人格。
      somatic_llm = ChatOpenAI(
          model=model_name,
          api_key=api_key,
          base_url=base_url,
          temperature=SOMATIC_LLM_TEMPERATURE,
          max_tokens=SOMATIC_LLM_MAX_TOKENS,
          timeout=SOMATIC_LLM_TIMEOUT_SECONDS,
          max_retries=LLM_MAX_RETRIES,
      )

      # 独立视觉模型：只有角色卡明确启用视觉工具时才创建，避免普通角色
      # 无故增加一次模型客户端；当前仍沿用同一 Gemini 兼容端点。
      vision_llm = None
      if vision_mode == "tool":
          vision_cfg = dict(VISION_LLM_CONFIG)
          # 角色卡只允许显式覆盖视觉能力模式，不把模型地址和密钥混入角色人格配置。
          vision_api_env = str(vision_cfg.get("api_key_env") or "").strip()
          vision_api_key = os.environ.get(vision_api_env, "").strip()
          if not vision_api_key:
              raise RuntimeError(f"环境变量 {vision_api_env} 未设置或为空")
          vision_llm = ChatOpenAI(
              model=vision_cfg["model_name"],
              api_key=vision_api_key,
              base_url=vision_cfg["base_url"],
              temperature=VISION_LLM_TEMPERATURE,
              max_tokens=VISION_LLM_MAX_TOKENS,
              timeout=VISION_LLM_TIMEOUT_SECONDS,
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
          tools=effective_tools,
          system_prompt=system_prompt,
          checkpointer=checkpointer,
          middleware=middleware,
      )
      config={
          "configurable": {"thread_id": "default_user"}
         }
      result = (
          agent,
          config,
          persona,
          system_prompt,
          understanding_llm,
          retry_understanding_llm,
          appraisal_llm,
          somatic_llm,
          vision_llm,
      )
      if return_resources:
          return (*result, {"checkpoint_connection": conn})
      return result
