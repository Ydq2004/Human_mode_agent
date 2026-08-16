"""
  情绪状态数据模型
  SQLite 表 EmotionState，存储 AI Agent 的瞬时生理/心理参数
"""
from datetime import datetime
from pathlib import Path
from sqlmodel import Field,SQLModel,Session,create_engine

# 数据库引擎（全局共享，和config联动）
from config import SQLITE_DB_PATH

engine=create_engine(f"sqlite:///{SQLITE_DB_PATH}")

class EmotionState(SQLModel,table=True):
    """情绪状态表,按thread_id隔离不同会话"""
    __tablename__="emotion_state"
    thread_id:str = Field(primary_key=True)

    #===主要维度===
    mood:int=Field(default=50) #心情 0-100
    energy:int=Field(default=100) #体力 0-100
    last_active_time:str = Field(
        default_factory=lambda:datetime.now().isoformat()
    ) #上次活动时间
    # === 预留扩展字段（v2 多维池用，v1 不操作）===
      # curiosity: int = Field(default=50)
      # social_need: int = Field(default=50)
      # security: int = Field(default=50)

def ensure_emotion_store() -> None:
    """在应用启动阶段显式创建情绪表。

    以前模块一经 import（导入）就立即访问 SQLite，这会让纯规则测试也依赖
    当前实例目录。现在只有真正启动应用时才初始化存储；已有数据库不会被
    删除或覆盖，``create_all`` 只补不存在的表。
    """
    Path(SQLITE_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    SQLModel.metadata.create_all(engine)
