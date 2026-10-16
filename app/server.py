"""本机 Web 应用入口（第一版）。

默认只监听 127.0.0.1。它是桌面壳的开发后端，不把 SQLite、Chroma 或
任意本地文件直接暴露给浏览器；浏览器只能通过这里的结构化接口读取历史和
受控图片资源。
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import secrets
import threading
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

try:
    import msvcrt
except ImportError:  # pragma: no cover - Windows is the primary target.
    msvcrt = None

from app.runtime import ApplicationRuntime
from config import DEFAULT_THREAD_ID
from core.vision_input import inspect_image
from core.card_slot_manager import (
    CardSlotError,
    create_slot,
    delete_slot,
    get_active_slot,
    list_slots,
    register_slot,
    set_active_slot,
)


STATIC_ROOT = Path(__file__).with_name("static")

# 浏览器刷新、关闭页面或桌面壳重载时，客户端可能在响应写回前主动
# 关闭 TCP 连接。这不是服务端处理失败；尤其不能在异常处理路径中
# 再次发送错误响应，否则会把同一个网络竞态打印成两段 traceback。
_CLIENT_DISCONNECT_ERRORS = (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)


class _SingleInstanceLock:
    """Keep one local Web backend per project directory."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="ascii")
        try:
            if msvcrt is not None:
                self.handle.seek(0)
                self.handle.write("0")
                self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("Human-mode Agent 已经在运行，请先关闭现有窗口") from exc
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()))
        self.handle.flush()

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            if msvcrt is not None:
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


class WebApplicationState:
    def __init__(self, *, thread_id: str = DEFAULT_THREAD_ID) -> None:
        self._lock = threading.RLock()
        self.runtime = ApplicationRuntime(thread_id=thread_id)

    def close(self) -> None:
        with self._lock:
            self.runtime.shutdown()

    def submit_text(self, content: str) -> dict[str, Any]:
        with self._lock:
            from core.perception import create_perception_event
            return self.runtime.submit_event_async(create_perception_event("user", "text", content))

    def async_status(self, event_id: str) -> dict[str, Any] | None:
        return self.runtime.async_status(event_id)

    def submit_image(self, path: str | Path) -> dict[str, Any]:
        with self._lock:
            from core.vision_input import create_image_event
            return self.runtime.submit_event_async(create_image_event(path))

    def switch_thread(self, thread_id: str) -> dict[str, Any]:
        with self._lock:
            self.runtime = self.runtime.restart_for_thread(thread_id)
            return self.runtime.status()

    def reload_runtime(self) -> dict[str, Any]:
        with self._lock:
            thread_id = self.runtime.thread_id
            self.runtime.shutdown()
            self.runtime = ApplicationRuntime(thread_id=thread_id)
            return self.runtime.status()

    def confirm_browser_action(self, confirmation_id: str, approved: bool) -> dict[str, Any]:
        with self._lock:
            return self.runtime.confirm_browser_action(confirmation_id, approved)


def _json_default(value: Any) -> Any:
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return str(value)


def _ui_result(result: dict[str, Any]) -> dict[str, Any]:
    experience = result.get("experience_slice")
    event = getattr(experience, "perception_event", None)
    return {
        "status": "succeeded",
        "event_id": getattr(event, "event_id", ""),
        "slice_id": getattr(experience, "slice_id", ""),
        "event_sequence": result.get("event_sequence"),
        "visible_reply": str(result.get("visible_reply") or ""),
        "timings": result.get("timings") or {},
        "appraisal_job": result.get("appraisal_job") or {},
        "media": result.get("media_artifacts") or [],
    }


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "HumanModeLocal/0.1"

    @property
    def state(self) -> WebApplicationState:
        return self.server.app_state  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        # 由桌面壳/终端统一显示，避免把每次静态资源请求刷成聊天内容。
        return

    def _send_bytes(self, data: bytes, *, status: int, content_type: str) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except _CLIENT_DISCONNECT_ERRORS:
            # 客户端已经离开，继续写入或发送错误响应都没有意义。
            return

    def _send_json(self, payload: Any, *, status: int = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        self._send_bytes(data, status=status, content_type="application/json; charset=utf-8")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 2 * 1024 * 1024:
            raise ValueError("请求体超过限制")
        raw = self.rfile.read(length)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON object")
        return payload

    def _read_image_upload(self) -> tuple[str, bytes]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            raise ValueError("图片上传必须使用 multipart/form-data")
        boundary_marker = "boundary="
        if boundary_marker not in content_type:
            raise ValueError("图片上传缺少 boundary")
        boundary = content_type.split(boundary_marker, 1)[1].strip().strip('"')
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 20 * 1024 * 1024:
            raise ValueError("图片上传大小无效或超过 20 MB")
        body = self.rfile.read(length)
        marker = ("--" + boundary).encode("utf-8")
        sections = body.split(marker)
        for section in sections:
            if b"filename=" not in section:
                continue
            header, separator, data = section.partition(b"\r\n\r\n")
            if not separator:
                continue
            header_text = header.decode("utf-8", errors="replace")
            filename = "upload"
            for item in header_text.split(";"):
                if "filename=" in item:
                    filename = item.split("filename=", 1)[1].strip().strip('"')
                    break
            # multipart 分隔符前只有一个 CRLF；不能用 rstrip，否则合法图片
            # 末尾的 0x0a/0x2d 字节也可能被误删，导致哈希与用户文件不一致。
            if data.endswith(b"\r\n"):
                data = data[:-2]
            return Path(filename).name or "upload", data
        raise ValueError("没有找到图片文件")

    def _store_upload(self, filename: str, data: bytes) -> Path:
        root = self.state.runtime.slot_root / "attachments"
        root.mkdir(parents=True, exist_ok=True)
        target = root / f"{secrets.token_hex(12)}_{filename}"
        target.write_bytes(data)
        try:
            inspect_image(target)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return target

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/":
                self._send_bytes(
                    (STATIC_ROOT / "index.html").read_bytes(),
                    status=HTTPStatus.OK,
                    content_type="text/html; charset=utf-8",
                )
                return
            if path == "/api/status":
                self._send_json(self.state.runtime.status())
                return
            if path == "/api/history":
                self._send_json({"messages": self.state.runtime.history()})
                return
            if path.startswith("/api/event/"):
                event_id = unquote(path.rsplit("/", 1)[-1])
                value = self.state.async_status(event_id)
                if value is None:
                    self._send_json({"error": "event_not_found"}, status=HTTPStatus.NOT_FOUND)
                elif value.get("status") == "completed":
                    # _ui_result 的字段来自旧版同步接口，其中也有一个
                    # succeeded 状态；完成事件的轮询协议必须以后端任务状态为准。
                    self._send_json({**_ui_result(value["result"]), "status": "completed"})
                else:
                    self._send_json({k: v for k, v in value.items() if k != "result"})
                return
            if path == "/api/slots":
                active = get_active_slot()
                self._send_json({
                    "active_slot_id": active.slot_id,
                    "slots": [item.to_dict() for item in list_slots()],
                })
                return
            if path.startswith("/media/generated/"):
                self._serve_media(path, self.state.runtime.slot_root / "generated_images")
                return
            if path.startswith("/media/attachments/"):
                self._serve_media(path, self.state.runtime.slot_root / "attachments")
                return
            self._send_json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_json({"error": type(exc).__name__, "message": str(exc)}, status=500)

    def _serve_media(self, url_path: str, root: Path) -> None:
        filename = Path(unquote(url_path.rsplit("/", 1)[-1])).name
        target = (root / filename).resolve(strict=False)
        root = root.resolve(strict=False)
        try:
            target.relative_to(root)
        except ValueError:
            self._send_json({"error": "media_outside_root"}, status=403)
            return
        if not target.is_file():
            self._send_json({"error": "media_not_found"}, status=404)
            return
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send_bytes(target.read_bytes(), status=200, content_type=mime)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/message":
                payload = self._read_json()
                self._send_json(self.state.submit_text(payload.get("content", "")), status=HTTPStatus.ACCEPTED)
                return
            if parsed.path == "/api/image":
                filename, data = self._read_image_upload()
                target = self._store_upload(filename, data)
                response = self.state.submit_image(target)
                metadata = inspect_image(target)
                response["input_media"] = {
                    "filename": target.name,
                    "mime_type": metadata["mime_type"],
                    "url": f"/media/attachments/{target.name}",
                }
                response["input_media"] = {
                    "filename": target.name,
                    "mime_type": metadata["mime_type"],
                    "url": f"/media/attachments/{target.name}",
                }
                self._send_json(response, status=HTTPStatus.ACCEPTED)
                return
            if parsed.path == "/api/settings/thread":
                payload = self._read_json()
                self._send_json(self.state.switch_thread(payload.get("thread_id", "")))
                return
            if parsed.path == "/api/settings/reload":
                self._send_json(self.state.reload_runtime())
                return
            if parsed.path == "/api/browser/confirm":
                payload = self._read_json()
                self._send_json(self.state.confirm_browser_action(
                    payload.get("confirmation_id", ""),
                    bool(payload.get("approved", False)),
                ))
                return
            if parsed.path == "/api/choose_slot_folder":
                # 本机 Web 模式下由后端弹出 Windows 文件夹选择器；浏览器本身
                # 无法安全地提供用户文件夹的绝对路径。
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                selected = filedialog.askdirectory(title="选择包含 persona_config.json 的角色文件夹")
                root.destroy()
                if not selected:
                    self._send_json({"status": "cancelled"})
                else:
                    self._send_json({"status": "selected", "path": str(Path(selected).resolve())})
                return
            if parsed.path == "/api/shutdown":
                # 先返回确认，再由独立线程调用 shutdown；HTTP 请求线程不能
                # 在自己处理请求时同步关闭正在服务的 HTTPServer。
                self._send_json({
                    "status": "shutting_down",
                    "message": "应用正在关闭，后台任务会先完成收尾。",
                })
                threading.Thread(
                    target=self._shutdown_server,
                    args=(self.server,),
                    name="human-mode-server-shutdown",
                    daemon=True,
                ).start()
                return
            if parsed.path == "/api/slots/active":
                payload = self._read_json()
                selected = set_active_slot(payload.get("slot_id", ""))
                self._send_json({
                    "status": "accepted",
                    "restart_required": True,
                    "slot": selected.to_dict(),
                    "message": "活动槽位已保存，关闭并重新打开应用后生效。",
                })
                return
            if parsed.path == "/api/slots":
                payload = self._read_json()
                selected = create_slot(
                    payload.get("slot_id", ""),
                    name=payload.get("name"),
                    path=payload.get("path") or None,
                )
                self._send_json({"status": "created", "slot": selected.to_dict()}, status=201)
                return
            if parsed.path == "/api/slots/register":
                payload = self._read_json()
                path = str(payload.get("path") or "").strip()
                if not path:
                    raise ValueError("角色槽位路径不能为空")
                folder_name = Path(path.rstrip("\\/")).name or "slot"
                normalised = Path(path).expanduser().resolve(strict=False)
                existing = next((item for item in list_slots() if item.path == normalised), None)
                if existing is not None:
                    selected = existing
                else:
                    # 角色切换只需要一个稳定的内部登记名；用户不需要管理它。
                    # 内部 ID 永远使用 ASCII，角色文件夹名称只作为显示名，
                    # 避免中文或特殊字符触发槽位 ID 校验。
                    digest = hashlib.sha256(str(normalised).encode("utf-8")).hexdigest()[:16]
                    slot_id = f"folder_{digest}"
                    suffix = 2
                    existing_ids = {item.slot_id for item in list_slots()}
                    candidate = slot_id
                    while candidate in existing_ids:
                        candidate = f"{slot_id}_{suffix}"
                        suffix += 1
                    selected = register_slot(candidate, path, name=folder_name)
                selected = set_active_slot(selected.slot_id)
                self._send_json({"status": "registered", "slot": selected.to_dict()}, status=201)
                return
            self._send_json({"error": "not_found"}, status=404)
        except (ValueError, CardSlotError) as exc:
            self._send_json({"error": type(exc).__name__, "message": str(exc)}, status=400)
        except Exception as exc:
            self._send_json({"error": type(exc).__name__, "message": str(exc)}, status=500)

    @staticmethod
    def _shutdown_server(server) -> None:
        server.shutdown()

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        prefix = "/api/slots/"
        if not parsed.path.startswith(prefix):
            self._send_json({"error": "not_found"}, status=404)
            return
        try:
            selected = delete_slot(unquote(parsed.path[len(prefix):]))
            self._send_json({"status": "removed", "slot": selected.to_dict()})
        except (ValueError, CardSlotError) as exc:
            self._send_json({"error": type(exc).__name__, "message": str(exc)}, status=400)


def run_server(*, host: str = "127.0.0.1", port: int = 8765) -> None:
    instance_lock = _SingleInstanceLock(Path(__file__).resolve().parents[1] / ".human_mode" / "app.lock")
    instance_lock.acquire()
    state = WebApplicationState()
    try:
        server = ThreadingHTTPServer((host, port), RequestHandler)
    except Exception:
        state.close()
        instance_lock.release()
        raise
    server.app_state = state  # type: ignore[attr-defined]
    try:
        print(f"Human-mode Agent 已启动：http://{host}:{port}")
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nHuman-mode Agent 正在关闭...")
    finally:
        server.shutdown()
        state.close()
        server.server_close()
        instance_lock.release()


def main() -> int:
    parser = argparse.ArgumentParser(description="Human-mode Agent 本机桌面 Web 后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    run_server(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
