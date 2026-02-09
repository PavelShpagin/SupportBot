#!/usr/bin/env python3
"""
Verify that Signal Desktop attachment files exist on disk.

Expected inputs:
- Export JSON: test/data/signal_messages.json (from python test/read_signal_db.py)
- Attachments folder: test/data/extracted/Signal1/attachments.noindex/

This checks that attachment `path` values in the JSON resolve to files under
attachments.noindex and prints a summary of found vs missing.

Outputs (gitignored under test/data/):
- test/data/attachment_files_report.json
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


RE_ATTACHMENTS_NOINDEX = re.compile(r"(?i)attachments\.noindex[\\/](.+)$")


@dataclass(frozen=True)
class ResolvedPath:
    raw: str
    rel: str
    abs_path: Path


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8", errors="ignore"))


def _iter_attachments(messages: Iterable[Dict[str, Any]]) -> Iterable[Tuple[str, Dict[str, Any]]]:
    for m in messages:
        mid = str(m.get("id") or "").strip()
        for a in (m.get("attachments") or []):
            if isinstance(a, dict):
                yield mid, a


def _normalize_rel_path(raw_path: str) -> Optional[str]:
    s = (raw_path or "").strip().strip('"').strip("'")
    if not s:
        return None

    # Normalize separators early.
    s = s.replace("\\", "/")

    # If it contains ".../attachments.noindex/<rel>", strip prefix.
    m = RE_ATTACHMENTS_NOINDEX.search(s.replace("/", "\\"))
    if m:
        s = m.group(1).replace("\\", "/")

    # Drop Windows drive prefix if present (best-effort)
    if len(s) >= 3 and s[1] == ":" and s[2] == "/":
        # If the path was absolute, we still only want relative under attachments.noindex.
        # Keep it as-is but strip leading slashes after drive.
        s = s[3:]

    s = s.lstrip("/")

    # Prevent path traversal.
    parts = [p for p in s.split("/") if p not in ("", ".", "..")]
    if not parts:
        return None

    return "/".join(parts)


def _magic_kind(head: bytes) -> str:
    if head.startswith(b"\xFF\xD8\xFF"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "gif"
    if head.startswith(b"OggS"):
        return "ogg"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp":
        return "mp4/iso"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if head.startswith(b"SQLite format 3\x00"):
        return "sqlite"
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"{") or head.startswith(b"["):
        return "json/text?"
    return "unknown"


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    messages_json = repo / "test" / "data" / "signal_messages.json"
    attachments_root = repo / "test" / "data" / "extracted" / "Signal1" / "attachments.noindex"
    out_report = repo / "test" / "data" / "attachment_files_report.json"

    print("Verify attachments.noindex presence")
    print("=" * 50)
    print(f"messages_json:     {messages_json}")
    print(f"attachments_root:  {attachments_root}")
    print("")

    if not messages_json.exists():
        raise SystemExit(f"Missing {messages_json}. Run: python test/read_signal_db.py")
    if not attachments_root.exists():
        raise SystemExit(f"Missing {attachments_root}. Copy in attachments.noindex and retry.")

    data = _read_json(messages_json)
    messages: List[Dict[str, Any]] = data.get("messages") or []

    # Collect unique raw paths
    raw_paths: List[str] = []
    by_ct: Dict[str, int] = {}
    total_attachments = 0
    for _, a in _iter_attachments(messages):
        total_attachments += 1
        raw = a.get("path")
        if raw:
            raw_paths.append(str(raw))
        ct = (a.get("content_type") or "").strip()
        if ct:
            by_ct[ct] = by_ct.get(ct, 0) + 1

    unique_raw = sorted(set(p.strip() for p in raw_paths if p.strip()))
    resolved: List[ResolvedPath] = []
    missing: List[ResolvedPath] = []

    magic_counts: Dict[str, int] = {}

    for raw in unique_raw:
        rel = _normalize_rel_path(raw)
        if not rel:
            continue
        abs_path = attachments_root / rel
        rp = ResolvedPath(raw=raw, rel=rel, abs_path=abs_path)
        if abs_path.exists():
            resolved.append(rp)
            try:
                head = abs_path.read_bytes()[:16]
            except Exception:
                head = b""
            mk = _magic_kind(head)
            magic_counts[mk] = magic_counts.get(mk, 0) + 1
        else:
            missing.append(rp)

    print(f"messages:                 {len(messages)}")
    print(f"attachments (rows):       {total_attachments}")
    print(f"attachment paths (uniq):  {len(unique_raw)}")
    print(f"files found:              {len(resolved)}")
    print(f"files missing:            {len(missing)}")
    print("")
    if magic_counts:
        top_magic = sorted(magic_counts.items(), key=lambda x: x[1], reverse=True)[:12]
        print("Detected file headers (sample-based):")
        for k, v in top_magic:
            print(f"- {k:10s}: {v}")
        print("")

    if missing:
        print("Missing sample (first 10):")
        for rp in missing[:10]:
            print(f"- {rp.rel}")
        print("")

    report = {
        "messages_json": str(messages_json),
        "attachments_root": str(attachments_root),
        "messages": len(messages),
        "attachments_rows": total_attachments,
        "unique_paths": len(unique_raw),
        "files_found": len(resolved),
        "files_missing": len(missing),
        "magic_counts": magic_counts,
        "missing_rel_paths": [rp.rel for rp in missing],
        "content_type_counts": dict(sorted(by_ct.items(), key=lambda x: x[1], reverse=True)),
    }
    out_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote report: {out_report}")

    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())

