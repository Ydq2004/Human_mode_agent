"""
向量存储初始化
加载 BGE 嵌入模型，连接或创建 ChromaDB 集合
"""

import os

from config import CHROMA_PERSIST_DIR, EMBEDDING_MODEL_NAME, EMBEDDING_LOCAL_FILES_ONLY

# 当前项目要求模型权重预先下载到本机。设置离线开关后，huggingface_hub
# 不会在每次启动时做联网检查；如果本地缺文件，会直接报清晰的加载错误。
if EMBEDDING_LOCAL_FILES_ONLY:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"

from langchain_huggingface import HuggingFaceEmbeddings
from langchain_chroma import Chroma

print("正在加载认知引擎（BGE 中文嵌入模型，本地离线模式）...")

embeddings = HuggingFaceEmbeddings(
    model_name=EMBEDDING_MODEL_NAME,
    model_kwargs={"local_files_only": EMBEDDING_LOCAL_FILES_ONLY},
)

vector_db = Chroma(
      collection_name="agent_memory",
      embedding_function=embeddings,
      persist_directory=CHROMA_PERSIST_DIR,
  )

print("认知引擎就绪：", CHROMA_PERSIST_DIR)
