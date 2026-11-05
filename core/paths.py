"""运行路径：代码走 Git 发布，业务数据、密钥配置和日志可放在仓库之外。

默认值与旧行为完全一致（都在仓库根目录下），只有显式设置环境变量时才搬家：

    LUX_DATA_DIR    业务数据（SQLite、用户文件、上传原件）
    LUX_CONFIG_DIR  可维护的运行配置（邮件、红线规则、区域策略等）
    LUX_LOG_DIR     运行日志
    LUX_ENV_FILE    密钥与开关所在的 .env（只放服务器本地，绝不进 Git）

这样 ``git checkout`` 更新代码时不会碰到数据目录；备份也只针对数据与配置。
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _dotted(env_name: str, default: Path) -> Path:
    raw = os.getenv(env_name, "").strip()
    return Path(raw).expanduser().resolve() if raw else default


DATA_DIR = _dotted("LUX_DATA_DIR", PROJECT_ROOT / "data")
CONFIG_DIR = _dotted("LUX_CONFIG_DIR", PROJECT_ROOT / "config")
LOG_DIR = _dotted("LUX_LOG_DIR", PROJECT_ROOT / "logs")
ENV_FILE = _dotted("LUX_ENV_FILE", PROJECT_ROOT / ".env")


def data_path(*parts: str) -> Path:
    return DATA_DIR.joinpath(*parts)


def config_path(*parts: str) -> Path:
    return CONFIG_DIR.joinpath(*parts)


def log_path(*parts: str) -> Path:
    return LOG_DIR.joinpath(*parts)


def ensure_dirs() -> None:
    """创建运行目录；启动脚本和迁移工具在写文件前调用一次。"""
    for directory in (DATA_DIR, CONFIG_DIR, LOG_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def describe() -> dict[str, str]:
    """供健康检查与部署脚本输出的路径概览（不含任何密钥）。"""
    return {
        "project_root": str(PROJECT_ROOT),
        "data_dir": str(DATA_DIR),
        "config_dir": str(CONFIG_DIR),
        "log_dir": str(LOG_DIR),
        "env_file": str(ENV_FILE),
    }
