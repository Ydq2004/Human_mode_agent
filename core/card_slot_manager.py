"""可替换角色实例槽位管理（框架层）。

``Card_slot`` 不是某个角色的固定名字，而是一个完整的实例数据目录。
槽位注册表放在实例目录之外，避免切换槽位后连注册表本身也被切走。

这里负责目录身份和生命周期，不负责加载 persona、打开数据库或创建 Agent。
这样可以在桌面设置页中安全地列出、创建、切换和移除槽位，运行时再根据
选中的路径重建自己的服务。
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SLOT_ID = "default"
DEFAULT_SLOT_NAME = "柳如烟（默认）"
DEFAULT_SLOT_PATH = PROJECT_ROOT / "Card_slot"
DEFAULT_REGISTRY_PATH = PROJECT_ROOT / ".human_mode" / "card_slots.json"
SLOT_ENV_VAR = "HUMAN_MODE_CARD_SLOT"
_SLOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class CardSlotError(ValueError):
    """槽位配置无效或操作违反生命周期边界。"""


@dataclass(frozen=True)
class CardSlot:
    """一个可选择的角色实例槽位。"""

    slot_id: str
    name: str
    path: Path
    protected: bool = False
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot_id": self.slot_id,
            "name": self.name,
            "path": str(self.path),
            "protected": self.protected,
            "created_at": self.created_at,
        }


def _now() -> str:
    return datetime.now().isoformat()


def _normalise_path(path: str | Path) -> Path:
    value = Path(path).expanduser()
    if not value.is_absolute():
        value = PROJECT_ROOT / value
    return value.resolve(strict=False)


def _validate_slot_id(slot_id: str) -> str:
    value = str(slot_id or "").strip()
    if not _SLOT_ID_RE.fullmatch(value):
        raise CardSlotError(
            "slot_id 只能包含字母、数字、下划线和连字符，长度为 1 到 64"
        )
    return value


def _require_persona(path: Path) -> None:
    if not path.is_dir():
        raise CardSlotError(f"角色槽位目录不存在：{path}")
    persona_path = path / "persona_config.json"
    if not persona_path.is_file():
        raise CardSlotError(f"角色槽位缺少 persona_config.json：{path}")
    try:
        value = json.loads(persona_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CardSlotError(f"角色槽位的 persona_config.json 无法读取：{exc}") from exc
    if not isinstance(value, dict):
        raise CardSlotError("persona_config.json 必须是 JSON object")


def _default_slot() -> CardSlot:
    return CardSlot(
        slot_id=DEFAULT_SLOT_ID,
        name=DEFAULT_SLOT_NAME,
        path=DEFAULT_SLOT_PATH.resolve(strict=False),
        protected=True,
        created_at="",
    )


def _decode_registry(raw: Any) -> tuple[str, list[CardSlot]]:
    if not isinstance(raw, dict):
        raise CardSlotError("角色槽位注册表必须是 JSON object")

    active_slot_id = _validate_slot_id(raw.get("active_slot_id", DEFAULT_SLOT_ID))
    raw_slots = raw.get("slots")
    if not isinstance(raw_slots, list):
        raise CardSlotError("角色槽位注册表的 slots 必须是数组")

    slots: list[CardSlot] = []
    seen: set[str] = set()
    for item in raw_slots:
        if not isinstance(item, dict):
            raise CardSlotError("角色槽位注册表包含无效项目")
        slot_id = _validate_slot_id(item.get("slot_id"))
        if slot_id in seen:
            raise CardSlotError(f"角色槽位重复：{slot_id}")
        path = _normalise_path(item.get("path", ""))
        name = str(item.get("name") or slot_id).strip()
        if not name:
            raise CardSlotError(f"角色槽位名称不能为空：{slot_id}")
        slots.append(CardSlot(
            slot_id=slot_id,
            name=name,
            path=path,
            protected=bool(item.get("protected", False)),
            created_at=str(item.get("created_at") or ""),
        ))
        seen.add(slot_id)

    default = _default_slot()
    existing = next((item for item in slots if item.slot_id == DEFAULT_SLOT_ID), None)
    if existing is None:
        slots.insert(0, default)
    elif existing.path != default.path or not existing.protected:
        raise CardSlotError("default 槽位必须指向项目 Card_slot 且受保护")

    if active_slot_id not in {item.slot_id for item in slots}:
        raise CardSlotError(f"活动槽位不存在：{active_slot_id}")
    return active_slot_id, slots


def _read_registry(registry_path: str | Path = DEFAULT_REGISTRY_PATH) -> tuple[str, list[CardSlot]]:
    path = _normalise_path(registry_path)
    if not path.is_file():
        return DEFAULT_SLOT_ID, [_default_slot()]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CardSlotError(f"角色槽位注册表无法读取：{exc}") from exc
    return _decode_registry(raw)


def _write_registry(
    active_slot_id: str,
    slots: list[CardSlot],
    registry_path: str | Path = DEFAULT_REGISTRY_PATH,
) -> None:
    path = _normalise_path(registry_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "active_slot_id": active_slot_id,
        "slots": [item.to_dict() for item in slots],
    }
    # 同目录临时文件 + 替换，避免设置页中途关闭留下半个 JSON。
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    try:
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def list_slots(*, registry_path: str | Path = DEFAULT_REGISTRY_PATH) -> list[CardSlot]:
    """列出所有已登记槽位；结果不打开任何角色数据库。"""
    return list(_read_registry(registry_path)[1])


def get_active_slot(*, registry_path: str | Path = DEFAULT_REGISTRY_PATH) -> CardSlot:
    """读取当前活动槽位。环境变量仅用于外部启动器的临时覆盖。"""
    override = os.environ.get(SLOT_ENV_VAR, "").strip()
    active_id, slots = _read_registry(registry_path)
    if override:
        override_path = _normalise_path(override)
        _require_persona(override_path)
        return CardSlot(
            slot_id="env_override",
            name=override_path.name or "外部槽位",
            path=override_path,
            protected=False,
            created_at="",
        )
    return next(item for item in slots if item.slot_id == active_id)


def resolve_active_slot_path(*, registry_path: str | Path = DEFAULT_REGISTRY_PATH) -> Path:
    """供 config.py 在进程启动时解析实例根目录。"""
    slot = get_active_slot(registry_path=registry_path)
    _require_persona(slot.path)
    return slot.path


def register_slot(
    slot_id: str,
    path: str | Path,
    *,
    name: str | None = None,
    protected: bool = False,
    registry_path: str | Path = DEFAULT_REGISTRY_PATH,
) -> CardSlot:
    """登记一个已有的角色槽位，不复制或修改其中的运行数据。"""
    slot_id = _validate_slot_id(slot_id)
    path = _normalise_path(path)
    _require_persona(path)
    active_id, slots = _read_registry(registry_path)
    if any(item.slot_id == slot_id for item in slots):
        raise CardSlotError(f"角色槽位已存在：{slot_id}")
    if any(item.path == path for item in slots):
        raise CardSlotError(f"角色槽位路径已登记：{path}")
    slot = CardSlot(
        slot_id=slot_id,
        name=str(name or slot_id).strip() or slot_id,
        path=path,
        protected=bool(protected),
        created_at=_now(),
    )
    _write_registry(active_id, [*slots, slot], registry_path)
    return slot


def create_slot(
    slot_id: str,
    *,
    name: str | None = None,
    path: str | Path | None = None,
    template_slot_id: str = DEFAULT_SLOT_ID,
    registry_path: str | Path = DEFAULT_REGISTRY_PATH,
) -> CardSlot:
    """从模板创建新槽位，只复制角色配置和工作流，不复制历史状态。"""
    slot_id = _validate_slot_id(slot_id)
    active_id, slots = _read_registry(registry_path)
    if any(item.slot_id == slot_id for item in slots):
        raise CardSlotError(f"角色槽位已存在：{slot_id}")
    template = next((item for item in slots if item.slot_id == template_slot_id), None)
    if template is None:
        raise CardSlotError(f"模板槽位不存在：{template_slot_id}")
    _require_persona(template.path)

    target = _normalise_path(path or (PROJECT_ROOT / "Card_slots" / slot_id))
    if target.exists() and any(target.iterdir()):
        raise CardSlotError(f"新槽位目录必须为空：{target}")
    target.mkdir(parents=True, exist_ok=True)

    for filename in ("persona_config.json", "file_access_policy.json"):
        source = template.path / filename
        if source.is_file():
            shutil.copy2(source, target / filename)
    workflow_dir = template.path / "image_generation"
    if workflow_dir.is_dir():
        shutil.copytree(workflow_dir, target / "image_generation", dirs_exist_ok=True)
    (target / "memory_db").mkdir(exist_ok=True)
    (target / "generated_images").mkdir(exist_ok=True)

    return register_slot(
        slot_id,
        target,
        name=name,
        registry_path=registry_path,
    )


def set_active_slot(
    slot_id: str,
    *,
    registry_path: str | Path = DEFAULT_REGISTRY_PATH,
) -> CardSlot:
    """保存下一次运行使用的槽位；不会修改当前已打开的运行时。"""
    slot_id = _validate_slot_id(slot_id)
    _, slots = _read_registry(registry_path)
    selected = next((item for item in slots if item.slot_id == slot_id), None)
    if selected is None:
        raise CardSlotError(f"角色槽位不存在：{slot_id}")
    _require_persona(selected.path)
    _write_registry(slot_id, slots, registry_path)
    return selected


def delete_slot(
    slot_id: str,
    *,
    registry_path: str | Path = DEFAULT_REGISTRY_PATH,
) -> CardSlot:
    """从槽位列表移除角色，但保留目录，避免设置页误删历史数据。"""
    slot_id = _validate_slot_id(slot_id)
    active_id, slots = _read_registry(registry_path)
    selected = next((item for item in slots if item.slot_id == slot_id), None)
    if selected is None:
        raise CardSlotError(f"角色槽位不存在：{slot_id}")
    if selected.protected or slot_id == DEFAULT_SLOT_ID:
        raise CardSlotError("默认柳如烟槽位不能删除")
    if active_id == slot_id:
        raise CardSlotError("当前活动槽位不能删除，请先切换到其他槽位")
    remaining = [item for item in slots if item.slot_id != slot_id]
    _write_registry(active_id, remaining, registry_path)
    return selected


def ensure_default_registry(*, registry_path: str | Path = DEFAULT_REGISTRY_PATH) -> None:
    """显式创建注册表；导入 config.py 时不会产生这个副作用。"""
    path = _normalise_path(registry_path)
    if path.exists():
        _read_registry(path)
        return
    _write_registry(DEFAULT_SLOT_ID, [_default_slot()], path)


__all__ = [
    "CardSlot",
    "CardSlotError",
    "DEFAULT_REGISTRY_PATH",
    "DEFAULT_SLOT_ID",
    "DEFAULT_SLOT_PATH",
    "SLOT_ENV_VAR",
    "create_slot",
    "delete_slot",
    "ensure_default_registry",
    "get_active_slot",
    "list_slots",
    "register_slot",
    "resolve_active_slot_path",
    "set_active_slot",
]
