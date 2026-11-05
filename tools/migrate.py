"""数据库结构迁移：每个库各自记录已应用的版本，可先备份再升级。

    python tools/migrate.py status
    python tools/migrate.py up            # 应用所有待执行迁移
    python tools/migrate.py up --dry-run  # 只打印将要执行的迁移
    python tools/migrate.py up --to 1     # 只到指定版本
    python tools/migrate.py adopt         # 旧库：登记基线，不执行 SQL

约定：
- ``migrations/<别名>/0001_baseline.sql`` 描述该库的完整结构，全部使用
  ``CREATE TABLE IF NOT EXISTS``；
- 旧安装升级时，库里已有业务表但还没有 ``schema_migrations`` 记录，此时自动
  “认领”基线（只登记不执行），避免重复执行；新库则正常执行基线；
- 已应用的迁移文件被改动会报校验和不一致，需人工确认后才用 ``--allow-drift`` 放行。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.paths import DATA_DIR, PROJECT_ROOT
from core.sqlite_util import connect_db

MIGRATIONS_DIR = PROJECT_ROOT / "migrations"
TABLE = "schema_migrations"

# 别名 -> 数据目录下的数据库文件（多个模块可能共用一个库，迁移按库组织）
TARGETS: dict[str, str] = {
    "prompts": "prompts.db",
    "tasks": "tasks.db",
    "deliverables": "deliverables.db",
    "incidents": "incidents.db",
    "reviews": "reviews.db",
    "library": "file_library/library.db",
}


class MigrationError(RuntimeError):
    pass


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def list_migrations(alias: str) -> list[tuple[int, str, Path]]:
    folder = MIGRATIONS_DIR / alias
    items: list[tuple[int, str, Path]] = []
    for path in sorted(folder.glob("*.sql")):
        prefix = path.stem.split("_", 1)[0]
        if not prefix.isdigit():
            raise MigrationError(f"迁移文件名必须以版本号开头：{path.name}")
        items.append((int(prefix), path.stem, path))
    versions = [item[0] for item in items]
    if len(set(versions)) != len(versions):
        raise MigrationError(f"{alias} 存在重复的迁移版本号。")
    return items


def _existing_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {row[0] for row in rows}


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {TABLE}("
        "version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
        "checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )


def _applied(conn: sqlite3.Connection) -> dict[int, dict]:
    return {
        row["version"]: dict(row)
        for row in conn.execute(f"SELECT version,name,checksum,applied_at FROM {TABLE}")
    }


def _record(conn: sqlite3.Connection, version: int, name: str, path: Path) -> None:
    conn.execute(
        f"INSERT OR REPLACE INTO {TABLE}(version,name,checksum,applied_at) VALUES(?,?,?,?)",
        (version, name, _checksum(path), datetime.now().isoformat(timespec="seconds")),
    )


def _apply(conn: sqlite3.Connection, version: int, name: str, path: Path) -> None:
    """迁移与登记放在同一事务里：中途失败整条回滚，不留下“半套表”。

    sqlite3 的 executescript 会先提交挂起事务，所以事务边界写在脚本里；
    迁移文件不应自带 BEGIN/COMMIT。
    """
    sql = path.read_text(encoding="utf-8")
    try:
        conn.executescript("BEGIN;\n" + sql + "\n;")
        _record(conn, version, name, path)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _db_path(alias: str, data_dir: Path) -> Path:
    return Path(data_dir) / TARGETS[alias]


def status(data_dir: Path = DATA_DIR) -> list[dict]:
    """只读检查，不会创建数据库文件。"""
    report = []
    for alias, rel in TARGETS.items():
        path = _db_path(alias, data_dir)
        migrations = list_migrations(alias)
        applied: dict[int, dict] = {}
        drift: list[int] = []
        if path.exists():
            uri = f"{path.resolve().as_uri()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=5)
            try:
                conn.row_factory = sqlite3.Row
                if _existing_tables(conn) and TABLE in _existing_tables(conn):
                    applied = _applied(conn)
            finally:
                conn.close()
            for version, _name, file in migrations:
                row = applied.get(version)
                if row and row["checksum"] != _checksum(file):
                    drift.append(version)
        report.append({
            "alias": alias,
            "db": str(path),
            "exists": path.exists(),
            "applied": sorted(applied),
            "pending": [v for v, _n, _p in migrations if v not in applied],
            "drift": drift,
        })
    return report


def migrate(alias: str, data_dir: Path = DATA_DIR, *, to: int | None = None,
            dry_run: bool = False, adopt: bool = False,
            allow_drift: bool = False) -> dict:
    path = _db_path(alias, data_dir)
    migrations = list_migrations(alias)
    result = {"alias": alias, "db": str(path), "applied": [], "adopted": [],
              "already": [], "pending": [], "drift": []}

    if not path.exists() and dry_run:
        result["pending"] = [v for v, _n, _p in migrations if to is None or v <= to]
        return result

    path.parent.mkdir(parents=True, exist_ok=True)
    with connect_db(path) as conn:
        _ensure_table(conn)
        applied = _applied(conn)
        for version, name, file in migrations:
            if version in applied:
                if applied[version]["checksum"] != _checksum(file):
                    result["drift"].append(version)
                result["already"].append(version)
                continue
            if to is not None and version > to:
                result["pending"].append(version)
                continue
            # 旧库：已有业务表却没有登记过迁移 —— 只认领第一条基线，不重复建表。
            legacy = not applied and bool(_existing_tables(conn) - {TABLE})
            if adopt or legacy:
                if not dry_run:
                    _record(conn, version, name, file)
                result["adopted"].append(version)
                applied[version] = {"version": version, "name": name}
                continue
            if dry_run:
                result["applied"].append(version)
                continue
            _apply(conn, version, name, file)
            applied[version] = {"version": version, "name": name}
            result["applied"].append(version)

    if result["drift"] and not allow_drift:
        raise MigrationError(
            f"{alias} 已应用的迁移文件被修改（版本 {result['drift']}）；"
            "确认无误后加 --allow-drift 重新运行。")
    return result


def _print_report(report: list[dict]) -> None:
    for item in report:
        state = "已存在" if item["exists"] else "未创建"
        print(f"\n[{item['alias']}] {item['db']}（{state}）")
        print(f"  已应用：{item['applied'] or '无'}")
        print(f"  待执行：{item['pending'] or '无'}")
        if item["drift"]:
            print(f"  校验和变化：{item['drift']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LUX 数据库迁移")
    parser.add_argument("command", choices=["status", "up", "adopt"])
    parser.add_argument("--data-dir", default=None, help="覆盖数据目录（默认读 LUX_DATA_DIR）")
    parser.add_argument("--to", type=int, default=None, help="只迁移到指定版本")
    parser.add_argument("--dry-run", action="store_true", help="只显示将要执行的迁移")
    parser.add_argument("--allow-drift", action="store_true", help="允许已应用文件被修改")
    parser.add_argument("--json", action="store_true", help="输出机器可读结果")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir).resolve() if args.data_dir else DATA_DIR
    try:
        if args.command == "status":
            report = status(data_dir)
            print(json.dumps(report, ensure_ascii=False, indent=2) if args.json
                  else "", end="")
            if not args.json:
                _print_report(report)
            return 0

        results = [migrate(alias, data_dir, to=args.to, dry_run=args.dry_run,
                           adopt=args.command == "adopt", allow_drift=args.allow_drift)
                   for alias in TARGETS]
    except (MigrationError, OSError, sqlite3.Error) as exc:
        print(f"迁移失败：{exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    for item in results:
        changed = item["applied"] + item["adopted"]
        line = "、".join(map(str, changed)) or "无变化"
        action = "将执行" if args.dry_run else "已应用"
        print(f"[{item['alias']}] {action}：{line}")
        if item["pending"]:
            print(f"  仍待执行：{item['pending']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
