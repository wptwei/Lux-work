"""更新前备份业务数据与运行配置，必要时从备份恢复。

    python tools/backup_data.py                  # 备份到 backups/，保留最近 10 份
    python tools/backup_data.py --list
    python tools/backup_data.py --keep 5
    python tools/backup_data.py restore backups\\lux-backup-20261031-120000.zip
    python tools/backup_data.py restore <zip> --force   # 覆盖现有数据

原则：
- 数据库用 SQLite 备份 API 复制（含 WAL 中尚未落盘的数据），不直接拷文件；
- ``.env`` 含密钥，默认不进备份，需显式 ``--include-secrets``；
- 恢复前会检查清单与目标目录，默认拒绝覆盖已有数据。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.paths import CONFIG_DIR, DATA_DIR, ENV_FILE, PROJECT_ROOT

_BACKUP_ENV = os.getenv("LUX_BACKUP_DIR", "").strip()
DEFAULT_BACKUP_DIR = Path(_BACKUP_ENV).expanduser().resolve() if _BACKUP_ENV else PROJECT_ROOT / "backups"
MANIFEST = "manifest.json"
PREFIX = "lux-backup-"


class BackupError(RuntimeError):
    pass


def _is_db(path: Path) -> bool:
    return path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}


def _copy_sqlite(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(str(src), timeout=10)
    try:
        target = sqlite3.connect(str(dest))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _collect(root: Path, archive_root: str, skip_dbs: bool,
             exclude: Path | None = None) -> list[tuple[Path, str]]:
    items: list[tuple[Path, str]] = []
    if not root.exists():
        return items
    skip_root = os.path.normcase(str(exclude.resolve())) if exclude else None
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if skip_root and os.path.normcase(str(path.resolve())).startswith(skip_root + os.sep):
            continue
        if path.suffix.lower() in {".db-wal", ".db-shm", ".wal", ".shm"}:
            continue
        if skip_dbs and _is_db(path):
            continue
        items.append((path, f"{archive_root}/{path.relative_to(root).as_posix()}"))
    return items


def create_backup(*, data_dir: Path = DATA_DIR, config_dir: Path = CONFIG_DIR,
                  out_dir: Path = DEFAULT_BACKUP_DIR, keep: int | None = 10,
                  include_secrets: bool = False, note: str = "") -> Path:
    data_dir, config_dir, out_dir = Path(data_dir), Path(config_dir), Path(out_dir)
    if not data_dir.exists():
        raise BackupError(f"数据目录不存在：{data_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = out_dir / f"{PREFIX}{stamp}.zip"

    skip_root = os.path.normcase(str(out_dir.resolve()))
    dbs = [p for p in data_dir.rglob("*")
           if p.is_file() and _is_db(p)
           and not os.path.normcase(str(p.resolve())).startswith(skip_root + os.sep)]
    files = (_collect(data_dir, "data", skip_dbs=True, exclude=out_dir)
             + _collect(config_dir, "config", skip_dbs=False, exclude=out_dir))
    secrets = []
    if include_secrets and ENV_FILE.is_file():
        secrets.append((ENV_FILE, "env/.env"))

    manifest = {
        "created": datetime.now().isoformat(timespec="seconds"),
        "data_dir": str(data_dir),
        "config_dir": str(config_dir),
        "note": note,
        "include_secrets": include_secrets,
        "files": [],
        "databases": [],
    }

    with tempfile.TemporaryDirectory(prefix="lux-backup-") as tmp:
        tmp = Path(tmp)
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for index, db in enumerate(dbs):
                snapshot = tmp / f"{index}-{db.name}"
                _copy_sqlite(db, snapshot)
                arcname = f"data/{db.relative_to(data_dir).as_posix()}"
                zf.write(snapshot, arcname)
                manifest["databases"].append({"path": arcname, "bytes": snapshot.stat().st_size})
            for path, arcname in files:
                zf.write(path, arcname)
                manifest["files"].append({"path": arcname, "bytes": path.stat().st_size})
            for path, arcname in secrets:
                zf.write(path, arcname)
                manifest["files"].append({"path": arcname, "bytes": path.stat().st_size,
                                          "secret": True})
            zf.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))

    if keep:
        prune_backups(out_dir, keep)
    return target


def list_backups(out_dir: Path = DEFAULT_BACKUP_DIR) -> list[dict]:
    out_dir = Path(out_dir)
    if not out_dir.exists():
        return []
    rows = []
    for path in sorted(out_dir.glob(f"{PREFIX}*.zip"), reverse=True):
        info = {"path": str(path), "bytes": path.stat().st_size, "note": "", "created": ""}
        try:
            with zipfile.ZipFile(path) as zf:
                if MANIFEST in zf.namelist():
                    manifest = json.loads(zf.read(MANIFEST).decode("utf-8"))
                    info["created"] = manifest.get("created", "")
                    info["note"] = manifest.get("note", "")
                    info["include_secrets"] = bool(manifest.get("include_secrets"))
        except (zipfile.BadZipFile, OSError, ValueError):
            info["note"] = "备份文件损坏"
        rows.append(info)
    return rows


def prune_backups(out_dir: Path, keep: int) -> list[Path]:
    keep = max(1, int(keep))
    archives = sorted(Path(out_dir).glob(f"{PREFIX}*.zip"))
    removed = []
    for path in archives[:-keep]:
        try:
            path.unlink()
            removed.append(path)
        except OSError:
            pass
    return removed


def restore_backup(archive: Path, *, data_dir: Path = DATA_DIR,
                   config_dir: Path = CONFIG_DIR, force: bool = False) -> dict:
    archive = Path(archive)
    if not archive.is_file():
        raise BackupError(f"找不到备份文件：{archive}")
    data_dir, config_dir = Path(data_dir), Path(config_dir)
    with zipfile.ZipFile(archive) as zf:
        if MANIFEST not in zf.namelist():
            raise BackupError("备份缺少 manifest.json，拒绝恢复。")
        manifest = json.loads(zf.read(MANIFEST).decode("utf-8"))
        entries = [m["path"] for m in manifest.get("files", [])] + \
                  [m["path"] for m in manifest.get("databases", [])]

        if not force:
            occupied = []
            for entry in entries:
                if entry.startswith("data/"):
                    if (data_dir / entry.split("/", 1)[1]).exists():
                        occupied.append(entry)
                elif entry.startswith("config/"):
                    if (config_dir / entry.split("/", 1)[1]).exists():
                        occupied.append(entry)
            if occupied:
                raise BackupError(
                    f"目标目录已有 {len(occupied)} 个同名文件（如 {occupied[0]}）；"
                    "确认要覆盖请加 --force。")

        restored, skipped = [], []
        with tempfile.TemporaryDirectory(prefix="lux-restore-") as tmp:
            tmp = Path(tmp)
            zf.extractall(tmp)
            for entry in entries:
                source = tmp / entry
                if not source.is_file():
                    continue
                if entry.startswith("env/"):
                    if manifest.get("include_secrets"):
                        ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, ENV_FILE)
                        restored.append(str(ENV_FILE))
                    else:
                        skipped.append(entry)
                elif entry.startswith("data/"):
                    dest = data_dir / entry.split("/", 1)[1]
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    for stale in (Path(str(dest) + "-wal"), Path(str(dest) + "-shm")):
                        stale.unlink(missing_ok=True)
                    shutil.copy2(source, dest)
                    restored.append(str(dest))
                elif entry.startswith("config/"):
                    dest = config_dir / entry.split("/", 1)[1]
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, dest)
                    restored.append(str(dest))
    return {"archive": str(archive), "restored": restored, "skipped": skipped,
            "created": manifest.get("created", "")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LUX 数据备份与恢复")
    parser.add_argument("command", nargs="?", default="create",
                        choices=["create", "list", "restore"])
    parser.add_argument("archive", nargs="?", help="restore 时的备份文件")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--config-dir", default=None)
    parser.add_argument("--out", default=None, help="备份目录（默认 backups/）")
    parser.add_argument("--keep", type=int, default=10, help="保留份数，0 表示不清理")
    parser.add_argument("--include-secrets", action="store_true", help="同时备份 .env（含密钥）")
    parser.add_argument("--note", default="", help="写入清单的备注")
    parser.add_argument("--force", action="store_true", help="恢复时覆盖已有数据")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir).resolve() if args.data_dir else DATA_DIR
    config_dir = Path(args.config_dir).resolve() if args.config_dir else CONFIG_DIR
    out_dir = Path(args.out).resolve() if args.out else DEFAULT_BACKUP_DIR

    try:
        if args.command == "list":
            rows = list_backups(out_dir)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                if not rows:
                    print(f"暂无备份：{out_dir}")
                for row in rows:
                    print(f"{row['path']}  {row['bytes'] / 1024 / 1024:.1f} MB  {row['created']}  {row['note']}")
            return 0
        if args.command == "restore":
            if not args.archive:
                raise BackupError("请指定要恢复的备份文件。")
            result = restore_backup(Path(args.archive), data_dir=data_dir,
                                    config_dir=config_dir, force=args.force)
            print(json.dumps(result, ensure_ascii=False, indent=2) if args.json
                  else f"已恢复 {len(result['restored'])} 个文件到 {data_dir} / {config_dir}")
            return 0
        path = create_backup(data_dir=data_dir, config_dir=config_dir, out_dir=out_dir,
                             keep=args.keep or None, include_secrets=args.include_secrets,
                             note=args.note)
    except (BackupError, OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
        print(f"备份失败：{exc}", file=sys.stderr)
        return 1

    print(json.dumps({"path": str(path), "bytes": path.stat().st_size}, ensure_ascii=False)
          if args.json else f"备份完成：{path}（{path.stat().st_size / 1024 / 1024:.1f} MB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
