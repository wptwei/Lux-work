"""数据库迁移：新库建基线、旧库认领基线、版本推进与校验和防护。"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.sqlite_util import connect_db
from tools import migrate


def _tables(db: Path) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    finally:
        conn.close()


def _rows(db: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(db)
    try:
        return list(conn.execute(sql))
    finally:
        conn.close()


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        self.db = self.data / "tasks.db"

    def _custom_migrations(self, files: dict[str, str]) -> Path:
        base = self.root / "migrations" / "tasks"
        base.mkdir(parents=True, exist_ok=True)
        for name, sql in files.items():
            (base / name).write_text(sql, encoding="utf-8")
        return self.root / "migrations"

    def test_new_database_applies_repository_baseline(self):
        result = migrate.migrate("tasks", self.data)
        self.assertEqual([1], result["applied"])
        self.assertIn("tasks", _tables(self.db))
        self.assertIn("schema_migrations", _tables(self.db))
        self.assertEqual([(1,)], _rows(self.db, "SELECT version FROM schema_migrations"))

    def test_second_run_changes_nothing(self):
        migrate.migrate("tasks", self.data)
        result = migrate.migrate("tasks", self.data)
        self.assertEqual([], result["applied"])
        self.assertEqual([], result["adopted"])
        self.assertEqual([1], result["already"])

    def test_legacy_database_adopts_baseline_without_recreating(self):
        with connect_db(self.db) as conn:
            conn.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY, task_type TEXT)")
            conn.execute("INSERT INTO tasks VALUES('T-1','chat')")
        result = migrate.migrate("tasks", self.data)
        self.assertEqual([1], result["adopted"])
        self.assertEqual([], result["applied"])
        self.assertEqual([("T-1",)], _rows(self.db, "SELECT id FROM tasks"))

    def test_legacy_database_still_receives_later_migrations(self):
        migrations = self._custom_migrations({
            "0001_baseline.sql": "CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY);",
            "0002_add_title.sql": "ALTER TABLE tasks ADD COLUMN title TEXT DEFAULT '';",
        })
        with connect_db(self.db) as conn:
            conn.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY)")
            conn.execute("INSERT INTO tasks VALUES('T-1')")
        with patch.object(migrate, "MIGRATIONS_DIR", migrations):
            result = migrate.migrate("tasks", self.data)
        self.assertEqual([1], result["adopted"])
        self.assertEqual([2], result["applied"])
        columns = {row[1] for row in _rows(self.db, "PRAGMA table_info(tasks)")}
        self.assertIn("title", columns)

    def test_status_is_read_only(self):
        report = migrate.status(self.data)
        self.assertTrue(all(not item["exists"] for item in report))
        self.assertFalse(self.db.exists())
        self.assertIn("tasks", {item["alias"] for item in report})

    def test_dry_run_reports_pending_without_creating_files(self):
        result = migrate.migrate("tasks", self.data, dry_run=True)
        self.assertEqual([1], result["pending"])
        self.assertFalse(self.db.exists())

    def test_to_stops_at_requested_version(self):
        migrations = self._custom_migrations({
            "0001_baseline.sql": "CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY);",
            "0002_add_title.sql": "ALTER TABLE tasks ADD COLUMN title TEXT DEFAULT '';",
        })
        with patch.object(migrate, "MIGRATIONS_DIR", migrations):
            result = migrate.migrate("tasks", self.data, to=1)
            self.assertEqual([1], result["applied"])
            self.assertEqual([2], result["pending"])
            result = migrate.migrate("tasks", self.data)
        self.assertEqual([2], result["applied"])

    def test_edited_migration_is_rejected_unless_allowed(self):
        migrations = self._custom_migrations({
            "0001_baseline.sql": "CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY);",
        })
        with patch.object(migrate, "MIGRATIONS_DIR", migrations):
            migrate.migrate("tasks", self.data)
            (migrations / "tasks" / "0001_baseline.sql").write_text(
                "CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, extra TEXT);",
                encoding="utf-8")
            with self.assertRaises(migrate.MigrationError):
                migrate.migrate("tasks", self.data)
            result = migrate.migrate("tasks", self.data, allow_drift=True)
        self.assertEqual([1], result["drift"])

    def test_failed_migration_rolls_back_and_can_be_retried(self):
        migrations = self._custom_migrations({
            "0001_baseline.sql": ("CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY);\n"
                                  "CREATE TABLE broken(;"),
        })
        with patch.object(migrate, "MIGRATIONS_DIR", migrations):
            with self.assertRaises(sqlite3.OperationalError):
                migrate.migrate("tasks", self.data)
            # 失败后不能留下“半套表”，否则下次会被误判成旧库而认领基线。
            if self.db.exists():
                tables = _tables(self.db)
                self.assertNotIn("tasks", tables)
                if "schema_migrations" in tables:
                    self.assertEqual([], _rows(self.db, "SELECT version FROM schema_migrations"))
            (migrations / "tasks" / "0001_baseline.sql").write_text(
                "CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY);", encoding="utf-8")
            result = migrate.migrate("tasks", self.data)
        self.assertEqual([1], result["applied"])

    def test_duplicate_versions_are_rejected(self):
        migrations = self._custom_migrations({
            "0001_a.sql": "SELECT 1;",
            "0001_b.sql": "SELECT 2;",
        })
        with patch.object(migrate, "MIGRATIONS_DIR", migrations):
            with self.assertRaises(migrate.MigrationError):
                migrate.list_migrations("tasks")

    def test_every_repository_baseline_exists(self):
        for alias in migrate.TARGETS:
            self.assertTrue(migrate.list_migrations(alias), f"{alias} 缺少基线迁移")


class BaselineMatchesApplicationTests(unittest.TestCase):
    """基线必须至少覆盖程序自建的全部列，避免新库少字段。"""

    def test_baseline_covers_application_schema(self):
        from core.db import PromptDB
        from core.deliverable_store import DeliverableStore
        from core.incident_store import IncidentStore
        from core.pdf_library import PdfLibrary
        from core.reward_penalty_store import RewardPenaltyStore
        from core.review_queue import ReviewQueue
        from core.task_queue import TaskQueue

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app, migrated = root / "app", root / "migrated"
            app.mkdir()
            PromptDB(app / "prompts.db")
            TaskQueue(app / "tasks.db")
            DeliverableStore(app / "deliverables.db")
            IncidentStore(app / "incidents.db")
            RewardPenaltyStore(app / "incidents.db")
            ReviewQueue(app / "reviews.db", app / "review_files")
            PdfLibrary(app / "library")

            for alias in migrate.TARGETS:
                migrate.migrate(alias, migrated)

            pairs = {
                "prompts": (app / "prompts.db", migrated / "prompts.db"),
                "tasks": (app / "tasks.db", migrated / "tasks.db"),
                "deliverables": (app / "deliverables.db", migrated / "deliverables.db"),
                "incidents": (app / "incidents.db", migrated / "incidents.db"),
                "reviews": (app / "reviews.db", migrated / "reviews.db"),
                "library": (app / "library" / "library.db", migrated / "file_library" / "library.db"),
            }
            for alias, (app_db, migrated_db) in pairs.items():
                for table in _tables(app_db) - {"schema_migrations"}:
                    app_columns = {row[1] for row in _rows(app_db, f"PRAGMA table_info({table})")}
                    moved_columns = {row[1] for row in _rows(migrated_db, f"PRAGMA table_info({table})")}
                    self.assertTrue(
                        app_columns <= moved_columns,
                        f"{alias}.{table} 基线缺少列：{sorted(app_columns - moved_columns)}")


if __name__ == "__main__":
    unittest.main()
