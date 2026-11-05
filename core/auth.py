"""本地多用户认证与最小角色权限。

账号文件只保存 PBKDF2 加盐哈希，不保存明文密码。该模块不依赖第三方包，
适合离线 Windows 专机部署；正式接入公司 SSO 后可替换认证入口而保留角色判断。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from core.paths import data_path


logger = logging.getLogger("lux.auth")
USERS_PATH = data_path("users.json")
PBKDF2_ITERATIONS = 310_000
ROLES = {
    "operator": "普通用户",
    "reviewer": "审核员",
    "supervisor": "主管",
    "admin": "系统管理员",
}
ROLE_PERMISSIONS = {
    "operator": {"use_assistant", "use_incident", "view_own_tasks"},
    "reviewer": {"use_assistant", "use_incident", "view_own_tasks", "review_assets"},
    "supervisor": {
        "use_assistant", "use_incident", "view_own_tasks", "review_assets",
        "manage_templates", "view_team_dashboard", "send_email", "manage_incidents",
    },
    "admin": {"*"},
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _normalize_username(username: str) -> str:
    value = (username or "").strip().lower()
    if not 3 <= len(value) <= 40:
        raise ValueError("账号长度必须为 3–40 个字符")
    if not all(ch.isalnum() or ch in "._-" for ch in value):
        raise ValueError("账号只能包含字母、数字、点、下划线和短横线")
    return value


def validate_password(password: str) -> None:
    if len(password or "") < 10:
        raise ValueError("密码至少需要 10 个字符")
    groups = sum(
        bool(check(password))
        for check in (
            lambda value: any(ch.islower() for ch in value),
            lambda value: any(ch.isupper() for ch in value),
            lambda value: any(ch.isdigit() for ch in value),
            lambda value: any(not ch.isalnum() for ch in value),
        )
    )
    if groups < 3:
        raise ValueError("密码需包含大小写字母、数字、符号中的至少三类")


def _password_hash(password: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations).hex()


class UserStore:
    def __init__(
        self,
        path: str | Path = USERS_PATH,
        *,
        max_failures: int | None = None,
        window_seconds: int | None = None,
        lock_seconds: int | None = None,
        clock=time.monotonic,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # 登录失败锁定：按账号名统计（不存在的账号同样统计，避免通过锁定状态枚举账号）。
        self.max_failures = max_failures or _env_int("LUX_LOGIN_MAX_FAILURES", 5)
        self.window_seconds = window_seconds or _env_int("LUX_LOGIN_WINDOW_SECONDS", 600)
        self.lock_seconds = lock_seconds or _env_int("LUX_LOGIN_LOCK_SECONDS", 900)
        self._clock = clock
        self._throttle = threading.Lock()
        self._attempts: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}
        if not self.path.exists():
            self._write({"version": 1, "users": {}})

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"账号库不可读取：{self.path}") from exc
        if not isinstance(data.get("users"), dict):
            raise RuntimeError("账号库格式无效")
        return data

    def _write(self, data: dict) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def add_user(
        self,
        username: str,
        password: str,
        *,
        display_name: str = "",
        role: str = "operator",
        enabled: bool = True,
        enforce_password_policy: bool = True,
    ) -> dict:
        username = _normalize_username(username)
        if role not in ROLES:
            raise ValueError(f"无效角色：{role}")
        if enforce_password_policy:
            validate_password(password)
        elif not password:
            raise ValueError("密码不能为空")
        with self._lock:
            data = self._read()
            if username in data["users"]:
                raise ValueError(f"账号 {username} 已存在")
            salt = secrets.token_bytes(16)
            data["users"][username] = {
                "display_name": (display_name or username).strip()[:80],
                "role": role,
                "enabled": bool(enabled),
                "salt": salt.hex(),
                "password_hash": _password_hash(password, salt),
                "iterations": PBKDF2_ITERATIONS,
                "created_at": _now(),
                "updated_at": _now(),
            }
            self._write(data)
        return self.get_public(username) or {}

    def ensure_bootstrap(self, username: str, password: str) -> bool:
        """仅在账号库为空时迁移旧 `.env` 共享账号为管理员。"""
        if not username or not password:
            return False
        with self._lock:
            if self._read()["users"]:
                return False
            self.add_user(
                username,
                password,
                display_name="初始管理员",
                role="admin",
                enforce_password_policy=False,
            )
            return True

    def authenticate(self, username: str, password: str) -> bool:
        try:
            normalized = _normalize_username(username)
        except ValueError:
            normalized = ""
        key = normalized or "<invalid>"
        if self.is_locked(key):
            _password_hash(password or "", bytes(16))  # 保持耗时接近，且锁定期间正确密码也不放行
            return False
        ok = self._verify(normalized, password)
        self._record_attempt(key, ok)
        return ok

    def _verify(self, normalized: str, password: str) -> bool:
        with self._lock:
            record = self._read()["users"].get(normalized)
        # 不存在的账号也执行一次哈希，降低账号枚举的计时差异。
        if not record:
            _password_hash(password or "", bytes(16))
            return False
        try:
            actual = _password_hash(
                password or "",
                bytes.fromhex(record["salt"]),
                int(record.get("iterations", PBKDF2_ITERATIONS)),
            )
        except (KeyError, TypeError, ValueError):
            return False
        return bool(record.get("enabled")) and hmac.compare_digest(actual, record.get("password_hash", ""))

    def is_locked(self, username: str) -> bool:
        key = (username or "").strip().lower() or "<invalid>"
        with self._throttle:
            return self._locked_until.get(key, 0.0) > self._clock()

    def unlock(self, username: str) -> None:
        """管理员手动解除锁定。"""
        key = (username or "").strip().lower()
        with self._throttle:
            self._attempts.pop(key, None)
            self._locked_until.pop(key, None)

    def _record_attempt(self, key: str, ok: bool) -> None:
        now = self._clock()
        with self._throttle:
            if ok:
                self._attempts.pop(key, None)
                self._locked_until.pop(key, None)
                return
            recent = [t for t in self._attempts.get(key, []) if now - t < self.window_seconds]
            recent.append(now)
            if len(recent) >= self.max_failures:
                self._locked_until[key] = now + self.lock_seconds
                self._attempts.pop(key, None)
                logger.warning("账号 %s 连续登录失败 %d 次，已锁定 %d 秒", key, self.max_failures, self.lock_seconds)
            else:
                self._attempts[key] = recent
            if len(self._attempts) > 2000:  # 防止被构造大量账号名撑爆内存
                for stale in sorted(self._attempts, key=lambda k: self._attempts[k][-1])[:500]:
                    self._attempts.pop(stale, None)

    def reset_password(self, username: str, new_password: str) -> None:
        username = _normalize_username(username)
        validate_password(new_password)
        with self._lock:
            data = self._read()
            if username not in data["users"]:
                raise ValueError("账号不存在")
            salt = secrets.token_bytes(16)
            record = data["users"][username]
            record.update({
                "salt": salt.hex(),
                "password_hash": _password_hash(new_password, salt),
                "iterations": PBKDF2_ITERATIONS,
                "updated_at": _now(),
            })
            self._write(data)

    def set_enabled(self, username: str, enabled: bool) -> None:
        username = _normalize_username(username)
        with self._lock:
            data = self._read()
            if username not in data["users"]:
                raise ValueError("账号不存在")
            if not enabled and data["users"][username].get("role") == "admin":
                active_admins = sum(
                    bool(item.get("enabled")) and item.get("role") == "admin"
                    for item in data["users"].values()
                )
                if active_admins <= 1:
                    raise ValueError("不能停用最后一个管理员账号")
            data["users"][username]["enabled"] = bool(enabled)
            data["users"][username]["updated_at"] = _now()
            self._write(data)

    def get_public(self, username: str) -> dict | None:
        try:
            username = _normalize_username(username)
        except ValueError:
            return None
        with self._lock:
            record = self._read()["users"].get(username)
        if not record:
            return None
        return {
            "username": username,
            "display_name": record.get("display_name") or username,
            "role": record.get("role", "operator"),
            "role_label": ROLES.get(record.get("role"), "普通用户"),
            "enabled": bool(record.get("enabled")),
            "created_at": record.get("created_at", ""),
        }

    def list_users(self) -> list[dict]:
        with self._lock:
            usernames = sorted(self._read()["users"])
        return [item for name in usernames if (item := self.get_public(name))]

    def has_enabled_users(self) -> bool:
        return any(item["enabled"] for item in self.list_users())

    def has_permission(self, username: str, permission: str) -> bool:
        user = self.get_public(username)
        if not user or not user["enabled"]:
            return False
        permissions = ROLE_PERMISSIONS.get(user["role"], set())
        return "*" in permissions or permission in permissions
