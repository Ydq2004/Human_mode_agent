import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.card_slot_manager import (  # noqa: E402
    CardSlotError,
    DEFAULT_SLOT_ID,
    create_slot,
    delete_slot,
    get_active_slot,
    list_slots,
    resolve_active_slot_path,
    set_active_slot,
)


class CardSlotManagerTests(unittest.TestCase):
    def test_default_slot_is_always_present_and_protected(self):
        with tempfile.TemporaryDirectory() as temp:
            registry = Path(temp) / "registry.json"
            slots = list_slots(registry_path=registry)

            self.assertEqual([item.slot_id for item in slots], [DEFAULT_SLOT_ID])
            self.assertTrue(slots[0].protected)
            self.assertEqual(
                get_active_slot(registry_path=registry).slot_id,
                DEFAULT_SLOT_ID,
            )

            with self.assertRaises(CardSlotError):
                delete_slot(DEFAULT_SLOT_ID, registry_path=registry)

    def test_create_slot_copies_configuration_but_not_history(self):
        with tempfile.TemporaryDirectory() as temp:
            registry = Path(temp) / "registry.json"
            target = Path(temp) / "role_a"
            slot = create_slot(
                "role_a",
                name="测试角色",
                path=target,
                registry_path=registry,
            )

            self.assertEqual(slot.name, "测试角色")
            self.assertTrue((target / "persona_config.json").is_file())
            self.assertTrue((target / "file_access_policy.json").is_file())
            self.assertFalse((target / "memory_db" / "sql_db.db").exists())
            self.assertFalse((target / "memory_db" / "checkpoint.db").exists())

            config = json.loads(
                (target / "persona_config.json").read_text(encoding="utf-8")
            )
            self.assertEqual(config["agent_name"], "柳如烟")

    def test_switch_and_remove_only_after_leaving_active_slot(self):
        with tempfile.TemporaryDirectory() as temp:
            registry = Path(temp) / "registry.json"
            target = Path(temp) / "role_b"
            create_slot("role_b", path=target, registry_path=registry)

            selected = set_active_slot("role_b", registry_path=registry)
            self.assertEqual(selected.slot_id, "role_b")
            self.assertEqual(
                resolve_active_slot_path(registry_path=registry),
                target.resolve(),
            )

            with self.assertRaises(CardSlotError):
                delete_slot("role_b", registry_path=registry)

            set_active_slot(DEFAULT_SLOT_ID, registry_path=registry)
            removed = delete_slot("role_b", registry_path=registry)
            self.assertEqual(removed.path, target.resolve())
            self.assertTrue(target.is_dir())
            self.assertEqual(
                [item.slot_id for item in list_slots(registry_path=registry)],
                [DEFAULT_SLOT_ID],
            )

    def test_environment_override_is_explicit_and_does_not_change_registry(self):
        with tempfile.TemporaryDirectory() as temp:
            registry = Path(temp) / "registry.json"
            target = Path(temp) / "external_role"
            create_slot("external_role", path=target, registry_path=registry)
            set_active_slot(DEFAULT_SLOT_ID, registry_path=registry)

            with patch.dict(os.environ, {"HUMAN_MODE_CARD_SLOT": str(target)}):
                active = get_active_slot(registry_path=registry)

            self.assertEqual(active.slot_id, "env_override")
            self.assertEqual(active.path, target.resolve())
            self.assertEqual(
                get_active_slot(registry_path=registry).slot_id,
                DEFAULT_SLOT_ID,
            )


if __name__ == "__main__":
    unittest.main()
