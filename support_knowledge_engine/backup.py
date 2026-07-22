from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .db import connect_database, init_database
from .migrations import MIGRATIONS, current_schema_version


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _timestamp() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S-%f")


def _inspect_database(path: str | Path) -> dict:
    database = Path(path)
    if not database.is_file():
        raise ValueError(f"数据库不存在：{database}")
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"SQLite 完整性检查失败：{integrity}")
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if not table:
            raise ValueError("备份缺少 schema_migrations，无法确认迁移版本。")
        version = current_schema_version(connection)
        supported = max(item[0] for item in MIGRATIONS)
        if version <= 0 or version > supported:
            raise ValueError(f"迁移版本不兼容：备份为 {version}，当前最高支持 {supported}。")
        return {"integrity": integrity, "schema_version": version, "sha256": file_sha256(database)}
    finally:
        connection.close()


def create_backup(database_path: str | Path, output_directory: str | Path) -> tuple[Path, dict]:
    source = Path(database_path)
    init_database(source)
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"knowledge-{_timestamp()}.sqlite3"
    temporary = destination.with_suffix(".sqlite3.part")
    source_connection = sqlite3.connect(source)
    target_connection = sqlite3.connect(temporary)
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()
    temporary.replace(destination)
    details = _inspect_database(destination)
    details.update(
        created_at=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        source_database=str(source.resolve()), backup_file=destination.name,
    )
    destination.with_suffix(destination.suffix + ".json").write_text(
        json.dumps(details, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return destination, details


def verify_backup(backup_path: str | Path) -> dict:
    backup = Path(backup_path)
    details = _inspect_database(backup)
    sidecar = backup.with_suffix(backup.suffix + ".json")
    if sidecar.is_file():
        manifest = json.loads(sidecar.read_text(encoding="utf-8"))
        expected = manifest.get("sha256")
        if expected and expected != details["sha256"]:
            raise ValueError("备份 SHA-256 与清单不一致。")
        details["manifest_verified"] = True
    else:
        details["manifest_verified"] = False
    return details


def restore_backup(
    backup_path: str | Path,
    database_path: str | Path,
    *,
    confirm: bool = False,
    safety_directory: str | Path | None = None,
) -> dict:
    if not confirm:
        raise ValueError("默认禁止覆盖当前数据库；请显式传入 --confirm。")
    backup = Path(backup_path)
    details = verify_backup(backup)
    target = Path(database_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    safety_backup: Path | None = None
    if target.exists():
        safety_backup, _ = create_backup(target, safety_directory or target.parent / "pre-restore")

        # A stale WAL beside a replaced main database can replay unrelated pages.
        # Require a brief exclusive lock, checkpoint it, then remove only this
        # database's known sidecars before the atomic replacement.
        current = sqlite3.connect(target, timeout=0.2)
        try:
            current.execute("PRAGMA busy_timeout = 200")
            checkpoint = current.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint and checkpoint[0] != 0:
                raise ValueError("当前数据库仍在使用，无法安全恢复。")
            current.execute("BEGIN EXCLUSIVE")
            current.rollback()
        except sqlite3.OperationalError as exc:
            raise ValueError("当前数据库仍在使用，无法安全恢复。") from exc
        finally:
            current.close()

    temporary = target.with_suffix(target.suffix + ".restore-part")
    source_connection = sqlite3.connect(backup)
    target_connection = sqlite3.connect(temporary)
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()
    try:
        _inspect_database(temporary)
        for suffix in ("-wal", "-shm"):
            Path(str(target) + suffix).unlink(missing_ok=True)
        temporary.replace(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {
        "restored_from": str(backup.resolve()),
        "database": str(target.resolve()),
        "safety_backup": str(safety_backup.resolve()) if safety_backup else None,
        **details,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Support Knowledge Engine SQLite 安全备份工具")
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("--database", required=True)
    backup_parser.add_argument("--output-dir", required=True)
    verify_parser = subparsers.add_parser("verify-backup")
    verify_parser.add_argument("--backup", required=True)
    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--backup", required=True)
    restore_parser.add_argument("--database", required=True)
    restore_parser.add_argument("--safety-dir")
    restore_parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()
    if args.command == "backup":
        path, result = create_backup(args.database, args.output_dir)
        result["path"] = str(path)
    elif args.command == "verify-backup":
        result = verify_backup(args.backup)
    else:
        result = restore_backup(
            args.backup, args.database, confirm=args.confirm, safety_directory=args.safety_dir
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
