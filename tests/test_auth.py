from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.auth import UserStore, validate_password
from core.task_queue import TaskQueue


class UserStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "users.json"
        self.store = UserStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_password_is_hashed_and_authentication_works(self):
        self.store.add_user("zhangsan", "Strong#Pass2026", display_name="张三", role="operator")
        raw = self.path.read_text("utf-8")
        self.assertNotIn("Strong#Pass2026", raw)
        self.assertTrue(self.store.authenticate("ZHANGSAN", "Strong#Pass2026"))
        self.assertFalse(self.store.authenticate("zhangsan", "wrong-password"))
        record = json.loads(raw)["users"]["zhangsan"]
        self.assertIn("salt", record)
        self.assertIn("password_hash", record)

    def test_bootstrap_only_happens_on_empty_store(self):
        self.assertTrue(self.store.ensure_bootstrap("security", "legacy-password"))
        self.assertFalse(self.store.ensure_bootstrap("second", "another-password"))
        self.assertEqual(["security"], [u["username"] for u in self.store.list_users()])
        self.assertEqual("admin", self.store.get_public("security")["role"])

    def test_disabled_user_cannot_login(self):
        self.store.add_user("reviewer01", "Review#Pass2026", role="reviewer")
        self.store.set_enabled("reviewer01", False)
        self.assertFalse(self.store.authenticate("reviewer01", "Review#Pass2026"))

    def test_last_admin_cannot_be_disabled(self):
        self.store.add_user("admin01", "Admin#Pass2026", role="admin")
        with self.assertRaises(ValueError):
            self.store.set_enabled("admin01", False)

    def test_role_permissions(self):
        self.store.add_user("operator01", "Operator#2026", role="operator")
        self.store.add_user("reviewer01", "Reviewer#2026", role="reviewer")
        self.assertFalse(self.store.has_permission("operator01", "review_assets"))
        self.assertTrue(self.store.has_permission("reviewer01", "review_assets"))

    def test_weak_password_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_password("123456")


class TaskIdentityTests(unittest.TestCase):
    def test_created_by_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = TaskQueue(Path(tmp) / "tasks.db")
            task_id = queue.create("chat", {}, created_by="zhangsan")
            self.assertEqual("zhangsan", queue.get(task_id)["created_by"])


if __name__ == "__main__":
    unittest.main()
