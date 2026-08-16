"""
  情绪状态管理器
  职责：查询、创建、更新情绪状态；离线恢复；回合后情感分析应用
  """

from datetime import datetime

from sqlmodel import Session, select

from config import (
      MOOD_BASELINE, MOOD_REGRESSION_RATE,
      MOOD_IMPACT_MAX, MOOD_IMPACT_MIN,
      ENERGY_RECOVERY_PER_MINUTE,
      OFFLINE_MOOD_REGRESSION_PER_MINUTE
  )
from emotion.appraisal_rules import compute_committed_mood_change
from emotion.models import EmotionState, engine
from emotion.translator import format_emotion_context
from math import log1p


def init_or_get_emotion(thread_id: str) -> dict:
    """
    兼容旧调用。新代码应使用 begin_perception_event。
    """
    return begin_perception_event(thread_id)

def begin_perception_event(thread_id: str) -> dict:
      """
      创建状态记录；
      执行离线恢复；
      更新 last_active_time；
      返回本轮开始时的 mood / energy。
      """
      now = datetime.now()

      with Session(engine) as session:
          statement = select(EmotionState).where(EmotionState.thread_id == thread_id)
          record = session.exec(statement).first()

          if not record:
              # 新用户：创建
              record = EmotionState(
                  thread_id=thread_id,
                  last_active_time=now.isoformat()
              )
              session.add(record)
              session.commit()
              session.refresh(record)
              return {"mood": record.mood, "energy": record.energy}

          
          try:
              # === 离线体力恢复 ===
              last_time = datetime.fromisoformat(record.last_active_time)
              offline_minutes = (now - last_time).total_seconds() / 60.0
              recovery = int(offline_minutes * ENERGY_RECOVERY_PER_MINUTE)
              if recovery > 0:
                  record.energy = min(100, record.energy + recovery)
              
              # === 离线心情回归 ===
              # 人离开一段时间后，情绪会自然向基线平复
              # 每离线 10 分钟向基线回归 10*OFFLINE_MOOD_REGRESSION_PER_MINUTE点 
              offline_mood_regression=int(offline_minutes*OFFLINE_MOOD_REGRESSION_PER_MINUTE)#每10分钟1点
              if offline_mood_regression>0:
                  if record.mood>MOOD_BASELINE:
                      #高于基准,向下跌
                      drop=min(offline_mood_regression,record.mood-MOOD_BASELINE)
                      record.mood-=drop
                      print(f" ↳ 离线心情回归: {-drop:+d}")
                  elif record.mood<MOOD_BASELINE:
                      #低于基准,往上涨
                      rise=min(offline_mood_regression,MOOD_BASELINE-record.mood)
                      record.mood+=rise
                      print(f" ↳ 离线心情回归: {rise:+d}")
          except (ValueError, TypeError):
              pass  # 时间戳解析失败，跳过恢复

         
          # 更新最后活跃时间
          record.last_active_time = now.isoformat()
          session.commit()

          return {"mood": record.mood, "energy": record.energy}

def read_emotion_snapshot(thread_id: str) -> dict:
    """
    只读取当前状态，不执行离线恢复，不更新 last_active_time，
    不产生任何数据库写入。
    """
    with Session(engine) as session:
        statement = select(
            EmotionState
        ).where(
            EmotionState.thread_id == thread_id
        )
        record = session.exec(statement).first()

        if not record:
            return {
                "mood": MOOD_BASELINE,
                "energy": 100,
            }

        return {
            "mood": record.mood,
            "energy": record.energy,
        }

def update_emotion(
      thread_id: str,
      mood_change: int = 0,
      energy_change: int = 0
  ) -> dict:
      """
      回合后由情感分析模块调用，程序化更新情绪。

      mood_change 已在外部经天花板/地板+幅度限制处理。
      energy_change v1 暂为 0，保留接口。

      返回: {"old_mood": int, "new_mood": int, "mood_change": int}
      """
      with Session(engine) as session:
          statement = select(EmotionState).where(EmotionState.thread_id == thread_id)
          record = session.exec(statement).first()

          if not record:
              return {"error": "thread_id not found"}

          old_mood = record.mood
          old_energy = record.energy

          # 更新并夹在 0-100
          record.mood = max(0, min(100, record.mood + mood_change))
          record.energy = max(0, min(100, record.energy + energy_change))
          record.last_active_time = datetime.now().isoformat()
          session.commit()

          print(
              f"\n📈 [情绪更新] mood: {old_mood} → {record.mood} "
              f"({mood_change:+d}) | "
              f"energy: {old_energy} → {record.energy} "
              f"({energy_change:+d})"
          )

          return {
              "old_mood": old_mood,
              "new_mood": record.mood,
              "mood_change": mood_change,
          }


def commit_mood_effect(thread_id: str, mood_impact: int) -> dict:
      """原子提交 Step 6 已经计算好的 mood 结果。

      “原子”表示读取当前 mood、决定是否基线回归、写入最终 mood 在同一个
      SQLite Session（数据库会话）中完成。调用方不会看见先写入 0、随后
      再回归一步的中间状态。整个函数只应由 CommitWorker 的有序任务调用；
      同一 job 是否重复提交仍由 CommitWorker 的幂等边界负责。
      """
      with Session(engine) as session:
          statement = select(EmotionState).where(
              EmotionState.thread_id == thread_id
          )
          record = session.exec(statement).first()
          if not record:
              return {"error": "thread_id not found"}

          result = compute_committed_mood_change(
              current_mood=record.mood,
              mood_impact=mood_impact,
          )
          record.mood = result["new_mood"]
          record.last_active_time = datetime.now().isoformat()
          session.commit()
          return result

def apply_baseline_regression(thread_id: str) -> int:
      """
      心情基线回归：每轮向 MOOD_BASELINE（50）靠近 MOOD_REGRESSION_RATE（1）点。
      防止情绪一直停留在高位或低位。

      返回: 本次回归量（正=向上回归，负=向下回归）
      """
      with Session(engine) as session:
          statement = select(EmotionState).where(EmotionState.thread_id == thread_id)
          record = session.exec(statement).first()
          if not record:
              return 0

          if record.mood > MOOD_BASELINE:
              delta = -MOOD_REGRESSION_RATE
          elif record.mood < MOOD_BASELINE:
              delta = MOOD_REGRESSION_RATE
          else:
              return 0

          record.mood += delta
          session.commit()
          return delta


def apply_post_turn_update(
      thread_id: str,
      mood_impact: int,
      concept_impacts: list = None,
      extensions: dict = None
  ):
      """
      回合后情感分析的完整应用流程：
      1. moodimpact连续边界衰减.(天花板/地板效应检查(旧方案废弃))
      2. 幅度钳制
      3. 更新 mood
      4. 基线回归
      5. concept_impacts 留给 memory 模块处理（外部调用）

      concept_impacts 和 extensions 由上层（main.py/context_builder）传入
      """
      if concept_impacts is None:
          concept_impacts = []

      # 1. 获取当前情绪状态
      state = init_or_get_emotion(thread_id)
      current_mood = state["mood"]

      # 2. 天花板/地板效应
      # 2. 天花板/地板效应 —— 连续衰减，不硬切
      # 越接近边界，剩余空间越小，衰减越强
      # 公式: log1p(剩余空间) / log1p(50) —— 和重要性评分的对数归一化思路一致
      mood_impact = _apply_mood_boundary_damping(current_mood, mood_impact)

      # 3. 幅度钳制（防漂移）
      mood_impact = max(MOOD_IMPACT_MIN, min(MOOD_IMPACT_MAX, mood_impact))

      # 4. 更新 mood
      update_result = update_emotion(thread_id, mood_change=mood_impact)

      # 5. 基线回归(新增条件没有情绪冲击时才触发)
      if mood_impact==0:
          regression = apply_baseline_regression(thread_id)
      else:
          regression=0

      # 6. concept_impacts 返回给上层处理（调 memory.upsert）
      print(f"📊 [回合情感] 冲击: {mood_impact:+d} | 回归: {regression:+d}")
      if concept_impacts:
          print(f"   ↳ 涉及概念好感度变化: {concept_impacts}")

      return {
          "mood": update_result,
          "regression": regression,
          "concept_impacts": concept_impacts,
      }


def _apply_mood_boundary_damping(current_mood: int, mood_impact: int) -> int:
      """
      情绪边界连续衰减函数。

      原理：越接近极限（0 或 100），剩余空间越小，继续被推向极限的难度越大。
      使用对数尺度（log1p）让衰减平滑，不出现"85 不打折、86 打五折"的突兀跳跃。

      效果示例（正向）：
        mood=50, +5 → +5  (剩余空间充裕，无衰减)
        mood=80, +5 → +4  (高位开始遇到阻力)
        mood=90, +5 → +3  (阻力明显)
        mood=98, +5 → +1  (接近满值，几乎推不动)

      负向同理：
        mood=50, -5 → -5
        mood=20, -5 → -4
        mood=10, -5 → -3
        mood=2,  -5 → -1
      """
      if mood_impact == 0:
          return 0

      if mood_impact > 0:
          # 正向冲击：剩余空间 = 100 - current_mood
          available_room = 100 - current_mood
      else:
          # 负向冲击：剩余空间 = current_mood
          available_room = current_mood

      # 对数衰减系数：剩余空间越小，系数越小
      # 除以 log1p(50) 归一化——50 是"一边到基线"的距离
      factor = log1p(available_room) / log1p(50)

      damped = int(mood_impact * factor)

      # 保底：非零冲击不要被压成 0，否则高位/低位会完全钝化
      if damped == 0:
          damped = 1 if mood_impact > 0 else -1

      if damped != mood_impact:
          print(f"   ↳ 边界衰减: {mood_impact} → {damped} (剩余空间={available_room}, 系数={factor:.2f})")

      return damped
