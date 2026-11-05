"""区域差异化红线策略。配置独立于代码，便于安保负责人调整。"""
from __future__ import annotations

import json
from pathlib import Path
from core.paths import config_path


DEFAULT_PATH = config_path("region_policies.json")


class RegionPolicy:
    def __init__(self, path: str | Path = DEFAULT_PATH):
        self.path = Path(path)
        self.reload()

    def reload(self) -> None:
        with self.path.open(encoding="utf-8") as stream:
            self.data = json.load(stream)

    @property
    def default_region(self) -> str:
        return self.data.get("default_region", "H区")

    def names(self) -> list[str]:
        return list(self.data.get("regions", {}).keys())

    def get(self, region: str | None) -> dict:
        name = region if region in self.data.get("regions", {}) else self.default_region
        return {"name": name, **self.data["regions"][name]}

