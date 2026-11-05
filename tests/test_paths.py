"""运行路径：默认仍在项目内，只有显式设置 LUX_*_DIR 才搬家。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from core import paths

ROOT = Path(__file__).resolve().parent.parent


class PathDefaultsTests(unittest.TestCase):
    def test_defaults_stay_inside_project(self):
        for value in (paths.DATA_DIR, paths.CONFIG_DIR, paths.LOG_DIR):
            self.assertEqual(ROOT, Path(value).parent, f"{value} 应位于项目根目录")
        self.assertEqual(ROOT / ".env", paths.ENV_FILE)

    def test_helpers_join_under_their_directory(self):
        self.assertEqual(paths.DATA_DIR / "tasks.db", paths.data_path("tasks.db"))
        self.assertEqual(paths.CONFIG_DIR / "mail.json", paths.config_path("mail.json"))
        self.assertEqual(paths.LOG_DIR / "a.log", paths.log_path("a.log"))

    def test_describe_lists_every_root(self):
        described = paths.describe()
        self.assertEqual({"project_root", "data_dir", "config_dir", "log_dir", "env_file"},
                         set(described))
        secret = os.getenv("TAVILY_API_KEY", "")
        if secret:
            self.assertNotIn(secret, json.dumps(described, ensure_ascii=False))


class PathOverrideTests(unittest.TestCase):
    def _run(self, **env):
        code = ("import json;from core.paths import DATA_DIR, CONFIG_DIR, LOG_DIR, ENV_FILE;"
                "print(json.dumps([str(DATA_DIR),str(CONFIG_DIR),str(LOG_DIR),str(ENV_FILE)]))")
        merged = {k: v for k, v in os.environ.items()
                  if k not in {"LUX_DATA_DIR", "LUX_CONFIG_DIR", "LUX_LOG_DIR", "LUX_ENV_FILE"}}
        merged.update(env)
        out = subprocess.run([sys.executable, "-X", "utf8", "-c", code], cwd=ROOT,
                             env=merged, capture_output=True, text=True, check=True)
        return [Path(p) for p in json.loads(out.stdout)]

    def test_env_variables_move_each_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data, config, logs, env_file = self._run(
                LUX_DATA_DIR=str(base / "d"), LUX_CONFIG_DIR=str(base / "c"),
                LUX_LOG_DIR=str(base / "l"), LUX_ENV_FILE=str(base / "keys.env"))
            self.assertEqual(base / "d", data)
            self.assertEqual(base / "c", config)
            self.assertEqual(base / "l", logs)
            self.assertEqual(base / "keys.env", env_file)

    def test_blank_variable_falls_back_to_default(self):
        data, _config, _logs, _env = self._run(LUX_DATA_DIR="   ")
        self.assertEqual(ROOT / "data", data)


if __name__ == "__main__":
    unittest.main()
