"""备份与恢复：数据库用 SQLite 备份 API，密钥默认不进备份。"""
from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from core.sqlite_util import connect_db
from tools import backup_data


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.config = self.root / "config"
        self.out = self.root / "backups"
        self.data.mkdir()
        self.config.mkdir()
        self.db = self.data / "tasks.db"
        with connect_db(self.db) as conn:
            conn.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY, title TEXT)")
            conn.execute("INSERT INTO tasks VALUES('T-1','巡检')")
        (self.config / "redline_rules.json").write_text('{"a": 1}', encoding="utf-8")
        self.env_file = self.root / "keys.env"
        self.env_file.write_text("TAVILY_API_KEY=secret-value\n", encoding="utf-8")

    def _backup(self, **kwargs):
        kwargs.setdefault("data_dir", self.data)
        kwargs.setdefault("config_dir", self.config)
        kwargs.setdefault("out_dir", self.out)
        return backup_data.create_backup(**kwargs)

    def test_backup_contains_database_manifest_and_config(self):
        archive = self._backup()
        self.assertTrue(archive.is_file())
        with zipfile.ZipFile(archive) as zf:
            names = set(zf.namelist())
            self.assertIn("data/tasks.db", names)
            self.assertIn("config/redline_rules.json", names)
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        self.assertEqual(["data/tasks.db"], [db["path"] for db in manifest["databases"]])
        self.assertFalse(manifest["include_secrets"])

    def test_secrets_are_skipped_by_default(self):
        archive = self._backup()
        with zipfile.ZipFile(archive) as zf:
            self.assertNotIn("env/.env", zf.namelist())

    def test_secrets_can_be_included_explicitly(self):
        with patch.object(backup_data, "ENV_FILE", self.env_file):
            archive = self._backup(include_secrets=True)
        with zipfile.ZipFile(archive) as zf:
            self.assertIn("env/.env", zf.namelist())

    def test_backup_copies_uncheckpointed_database_content(self):
        # 用 SQLite 备份 API，WAL 里未落盘的数据也应出现在备份中。
        archive = self._backup()
        restored = self.root / "restored"
        with zipfile.ZipFile(archive) as zf:
            zf.extract("data/tasks.db", restored)
        with connect_db(restored / "data" / "tasks.db") as conn:
            self.assertEqual(("巡检",), tuple(conn.execute(
                "SELECT title FROM tasks WHERE id='T-1'").fetchone()))

    def test_restore_refuses_to_overwrite_without_force(self):
        archive = self._backup()
        with self.assertRaises(backup_data.BackupError):
            backup_data.restore_backup(archive, data_dir=self.data, config_dir=self.config)

    def test_restore_with_force_reinstates_files(self):
        archive = self._backup()
        (self.data / "tasks.db").unlink()
        with connect_db(self.data / "tasks.db") as conn:
            conn.execute("CREATE TABLE other(x)")
        result = backup_data.restore_backup(archive, data_dir=self.data,
                                            config_dir=self.config, force=True)
        self.assertIn(str(self.data / "tasks.db"), result["restored"])
        with connect_db(self.data / "tasks.db") as conn:
            self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
        self.assertEqual('{"a": 1}', (self.config / "redline_rules.json").read_text(encoding="utf-8"))

    def test_prune_keeps_requested_number(self):
        self.out.mkdir(parents=True, exist_ok=True)
        for index in range(4):
            (self.out / f"lux-backup-2026010{index + 1}-000000.zip").write_bytes(b"x")
        removed = backup_data.prune_backups(self.out, 2)
        self.assertEqual(2, len(removed))
        self.assertEqual(2, len(list(self.out.glob("lux-backup-*.zip"))))

    def test_list_reports_manifest_fields(self):
        archive = self._backup(note="部署前备份")
        rows = backup_data.list_backups(self.out)
        self.assertEqual(str(archive), rows[0]["path"])
        self.assertEqual("部署前备份", rows[0]["note"])
        self.assertTrue(rows[0]["created"])


if __name__ == "__main__":
    unittest.main()
