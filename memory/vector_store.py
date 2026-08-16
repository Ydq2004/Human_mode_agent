"""
向量存储初始化
加载 BGE 嵌入模型，连接或创建 ChromaDB 集合
"""

from langchain_huggingface import HuggingFaceEmbeddings
from langchain_chroma import Chroma

from config import CHROMA_PERSIST_DIR, EMBEDDING_MODEL_NAME

print("正在加载认知引擎（BGE 中文嵌入模型，首次运行需下载）...")

embeddings = HuggingFaceEmbeddings(
    model_name=EMBEDDING_MODEL_NAME,
    model_kwargs={"local_files_only": True},
)

vector_db = Chroma(
      collection_name="agent_memory",
      embedding_function=embeddings,
      persist_directory=CHROMA_PERSIST_DIR,
  )

print("认知引擎就绪：", CHROMA_PERSIST_DIR)
