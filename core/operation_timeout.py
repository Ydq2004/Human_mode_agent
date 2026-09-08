"""为同步外部操作提供服务入口级超时。

超时只能让调用方停止等待；Python 线程中的底层调用本身仍需依赖客户端
超时才能真正结束。因此各服务仍应给自己的 LLM 客户端配置较短的请求超时。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable, TypeVar


T = TypeVar("T")


def run_with_timeout(fn: Callable[[], T], timeout_seconds: float | None) -> T:
    """运行一次同步操作；超时则抛出 TimeoutError。"""
    if timeout_seconds is None:
        return fn()
    timeout = float(timeout_seconds)
    if timeout <= 0:
        raise ValueError("timeout_seconds 必须是正数")
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="bounded-operation")
    future = executor.submit(fn)
    try:
        return future.result(timeout=timeout)
    finally:
        # 不等待已经阻塞的外部调用，避免服务层的超时又被 shutdown 卡住。
        executor.shutdown(wait=False, cancel_futures=True)


__all__ = ["run_with_timeout"]
