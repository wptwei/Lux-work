"""工作流审计日志：只落元数据与摘要哈希，绝不保存业务原文。"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path


_LOG_LOCK = threading.Lock()


class AuditLogger:
    def __init__(self, log_dir: str | Path | None = None):
        self.log_dir = Path(log_dir) if log_dir else (
            Path(__file__).resolve().parent.parent / "logs"
        )
        self.log_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def hash_text(text: str | None, length: int = 12) -> str:
        if not text:
            return ""
        return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:length]

    def log(self, action: str, detail: str = "", **meta) -> dict:
        """写入一条审计记录。

        detail 是业务原文，只保存长度与摘要哈希；meta 只允许 stage/agent/level/
        score/action/status 等非敏感元数据。
        """
        entry = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "action": action,
            "detail_chars": len(detail or ""),
            "detail_sha256_12": self.hash_text(detail),
        }
        entry.update(meta)
        log_file = self.log_dir / f"audit_{datetime.now():%Y-%m-%d}.jsonl"
        with _LOG_LOCK:
            with open(log_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry
