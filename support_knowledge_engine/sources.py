from __future__ import annotations

import argparse
import hashlib
import json
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from .db import connect_database, init_database


DEFAULT_TIMEOUT = 10.0
DEFAULT_RETRIES = 2
DEFAULT_MAX_BYTES = 25 * 1024 * 1024
REQUIRED_FIELDS = {
    "source_id", "product_name", "document_type", "language",
    "expected_filename", "enabled", "notes",
}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_manifest(path: str | Path) -> list[dict]:
    manifest_path = Path(path)
    if manifest_path.suffix.lower() != ".json":
        raise ValueError("当前版本仅支持 UTF-8 JSON 清单。")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取资料清单：{exc}") from exc
    entries = payload.get("sources") if isinstance(payload, dict) else payload
    if not isinstance(entries, list) or not entries:
        raise ValueError("资料清单必须包含非空 sources 数组。")

    seen: set[str] = set()
    validated: list[dict] = []
    for index, raw in enumerate(entries, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index} 项必须是对象。")
        missing = sorted(REQUIRED_FIELDS - raw.keys())
        if missing:
            raise ValueError(f"第 {index} 项缺少字段：{', '.join(missing)}")
        source_id = str(raw["source_id"]).strip()
        if (not source_id or source_id in seen or source_id in {".", ".."}
                or not re.fullmatch(r"[\w.-]+", source_id, flags=re.UNICODE)):
            raise ValueError(f"第 {index} 项 source_id 为空、重复或包含不安全字符：{source_id!r}")
        seen.add(source_id)
        url = str(raw.get("url") or "").strip()
        local_path = str(raw.get("local_path") or "").strip()
        if bool(url) == bool(local_path):
            raise ValueError(f"{source_id} 必须且只能设置 url 或 local_path。")
        if url and not url.lower().startswith(("http://", "https://")):
            raise ValueError(f"{source_id} 仅允许 http/https URL。")
        filename = Path(str(raw["expected_filename"])).name
        if filename != str(raw["expected_filename"]) or not filename.lower().endswith(".pdf"):
            raise ValueError(f"{source_id} 的 expected_filename 必须是安全的 PDF 文件名。")
        if not isinstance(raw["enabled"], bool):
            raise ValueError(f"{source_id} 的 enabled 必须是布尔值。")
        item = dict(raw)
        item.update(source_id=source_id, url=url, local_path=local_path, expected_filename=filename)
        validated.append(item)
    return validated


def _validate_pdf(data: bytes, content_type: str, max_bytes: int) -> None:
    if len(data) > max_bytes:
        raise ValueError(f"文件超过大小限制 {max_bytes} 字节。")
    if "pdf" not in content_type.lower():
        raise ValueError(f"Content-Type 不是 PDF：{content_type or '缺失'}")
    if not data.startswith(b"%PDF-"):
        raise ValueError("文件签名不是 PDF。")


def _read_limited(stream: BinaryIO, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = stream.read(min(1024 * 1024, max_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"文件超过大小限制 {max_bytes} 字节。")
    return b"".join(chunks)


@dataclass(frozen=True)
class FetchSummary:
    checked: int
    downloaded: int
    copied: int
    duplicates: int
    failed: int
    disabled: int


def _record(connection, entry: dict, metadata: dict) -> None:
    connection.execute(
        """INSERT INTO source_fetches
           (source_id, request_url, final_url, local_source_path, http_status,
            content_type, etag, last_modified, fetched_at, file_size, sha256,
            result, error_reason, saved_path, dry_run)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            entry["source_id"], entry.get("url") or None, metadata.get("final_url"),
            entry.get("local_path") or None, metadata.get("http_status"),
            metadata.get("content_type"), metadata.get("etag"), metadata.get("last_modified"),
            _now(), metadata.get("file_size"), metadata.get("sha256"), metadata["result"],
            metadata.get("error_reason"), metadata.get("saved_path"), int(metadata.get("dry_run", False)),
        ),
    )


def _fetch_url(entry: dict, *, timeout: float, retries: int, max_bytes: int, dry_run: bool) -> dict:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(
                entry["url"], headers={"User-Agent": "Support-Knowledge-Engine/0.3"}
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = int(response.status)
                content_type = response.headers.get("Content-Type", "")
                declared = response.headers.get("Content-Length")
                if status < 200 or status >= 300:
                    raise ValueError(f"HTTP 状态异常：{status}")
                if declared and int(declared) > max_bytes:
                    raise ValueError(f"Content-Length 超过大小限制 {max_bytes} 字节。")
                data = _read_limited(response, max_bytes)
                if declared and len(data) != int(declared):
                    raise ValueError(
                        f"下载中断：声明 {declared} 字节，实际收到 {len(data)} 字节。"
                    )
                _validate_pdf(data, content_type, max_bytes)
                return {
                    "final_url": response.geturl(), "http_status": status,
                    "content_type": content_type, "etag": response.headers.get("ETag"),
                    "last_modified": response.headers.get("Last-Modified"),
                    "file_size": len(data), "sha256": _sha256_bytes(data), "data": data,
                    "result": "checked" if dry_run else "downloaded", "dry_run": dry_run,
                }
        except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, TimeoutError,
                ConnectionError, OSError, ValueError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(min(0.1 * (attempt + 1), 0.3))
    raise ValueError(f"获取失败（已尝试 {retries + 1} 次）：{last_error}")


def acquire_sources(
    manifest_path: str | Path,
    database_path: str | Path,
    output_directory: str | Path,
    *,
    dry_run: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> FetchSummary:
    entries = load_manifest(manifest_path)
    init_database(database_path)
    output = Path(output_directory)
    counters = {key: 0 for key in ("checked", "downloaded", "copied", "duplicates", "failed", "disabled")}

    with connect_database(database_path) as connection:
        for entry in entries:
            if not entry["enabled"]:
                counters["disabled"] += 1
                _record(connection, entry, {"result": "disabled", "dry_run": dry_run})
                continue
            try:
                if entry["url"]:
                    metadata = _fetch_url(
                        entry, timeout=timeout, retries=retries, max_bytes=max_bytes, dry_run=dry_run
                    )
                    data = metadata.pop("data")
                else:
                    source = Path(entry["local_path"]).expanduser().resolve(strict=True)
                    if not source.is_file():
                        raise ValueError("local_path 不是文件。")
                    data = source.read_bytes()
                    _validate_pdf(data, "application/pdf", max_bytes)
                    metadata = {
                        "file_size": len(data), "sha256": _sha256_bytes(data),
                        "content_type": "application/pdf", "result": "checked" if dry_run else "copied",
                        "dry_run": dry_run,
                    }

                duplicate = connection.execute(
                    """SELECT saved_path FROM source_fetches
                       WHERE sha256 = ? AND result IN ('downloaded', 'copied', 'duplicate')
                             AND saved_path IS NOT NULL ORDER BY id DESC LIMIT 1""",
                    (metadata["sha256"],),
                ).fetchone()
                if duplicate and not Path(duplicate["saved_path"]).is_file():
                    duplicate = None
                if duplicate and not dry_run:
                    metadata.update(result="duplicate", saved_path=duplicate["saved_path"])
                    counters["duplicates"] += 1
                elif dry_run:
                    counters["checked"] += 1
                else:
                    destination = output / entry["source_id"] / entry["expected_filename"]
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    temporary = destination.with_suffix(destination.suffix + ".part")
                    temporary.write_bytes(data)
                    temporary.replace(destination)
                    metadata["saved_path"] = str(destination.resolve())
                    counters[metadata["result"]] += 1
                _record(connection, entry, metadata)
            except Exception as exc:
                counters["failed"] += 1
                _record(connection, entry, {
                    "result": "failed", "error_reason": str(exc), "dry_run": dry_run,
                })
            connection.commit()

    return FetchSummary(**counters)


def main() -> int:
    parser = argparse.ArgumentParser(description="按受控 JSON 清单获取 PDF，不进行链接发现。")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    args = parser.parse_args()
    summary = acquire_sources(
        args.manifest, args.database, args.output_dir, dry_run=args.dry_run,
        timeout=args.timeout, retries=max(0, args.retries), max_bytes=args.max_bytes,
    )
    print(json.dumps(summary.__dict__, ensure_ascii=False))
    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
