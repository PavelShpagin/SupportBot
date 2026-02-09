#!/usr/bin/env python3
"""
Run a cost-controlled "scale smoke test" on the last N real messages, then cleanup.

Goals:
- Process a realistic slice of chat history (e.g. last 800 messages)
- Mine solved cases + structure + embed
- Run retrieval + respond + judge evaluation
- Keep your workspace clean: move any old default outputs aside and delete
  raw, sensitive experiment artifacts after summarizing.

This script DOES NOT touch docker-compose MySQL/Chroma state.

Usage:
  source .venv/bin/activate
  python test/run_scale_eval_subset.py

Env knobs (recommended defaults are safe):
- REAL_LAST_N_MESSAGES=800
- REAL_MAX_CASES=60
- REAL_EVAL_N=18
- REAL_EVAL_TOP_K=5
- SCALE_FORCE_CHEAP_MODE=1  (default; set to 0 to respect your own MODEL_*/JUDGE_MODEL env)
- SCALE_NO_CAPS=0           (default; set to 1 to disable caps on the values above)
- KEEP_ARTIFACTS=0  (default; set to 1 to keep raw JSON outputs)
- KEEP_PREV_OUTPUTS_BACKUP=0  (default; set to 1 to keep moved previous outputs)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional


def _now_tag() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def _cap_int_env(name: str, *, default: int, min_value: Optional[int] = None, max_value: Optional[int] = None) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        val = int(raw)
    except Exception:
        val = default

    if min_value is not None and val < min_value:
        val = min_value
    if max_value is not None and val > max_value:
        val = max_value

    os.environ[name] = str(val)
    return val


def _maybe_load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return
    for raw in dotenv_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip("\r")
        if not k:
            continue
        if (v.startswith("'") and v.endswith("'")) or (v.startswith('"') and v.endswith('"')):
            v = v[1:-1]
        os.environ.setdefault(k, v)


def _move_if_exists(src: Path, dst_dir: Path) -> None:
    if not src.exists():
        return
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    # Avoid overwrite
    if dst.exists():
        dst = dst_dir / f"{src.stem}__{_now_tag()}{src.suffix}"
    shutil.move(str(src), str(dst))


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8", errors="ignore"))


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    _maybe_load_dotenv(repo / ".env")

    # Ensure the knobs exist even if .env is missing; this script is meant to be cost-controlled.
    os.environ.setdefault("REAL_LAST_N_MESSAGES", "800")
    os.environ.setdefault("REAL_MAX_CASES", "60")
    os.environ.setdefault("REAL_EVAL_N", "18")
    os.environ.setdefault("REAL_EVAL_TOP_K", "5")

    # By default we cap these to the safe values above; set SCALE_NO_CAPS=1 to disable caps.
    no_caps = (os.environ.get("SCALE_NO_CAPS") or "").strip().lower() in {"1", "true", "yes", "y"}
    if not no_caps:
        _cap_int_env("REAL_LAST_N_MESSAGES", default=800, min_value=1, max_value=800)
        _cap_int_env("REAL_MAX_CASES", default=60, min_value=1, max_value=60)
        _cap_int_env("REAL_EVAL_N", default=18, min_value=1, max_value=18)
        _cap_int_env("REAL_EVAL_TOP_K", default=5, min_value=1, max_value=5)

    # --- experiment run directory (kept minimal / cleaned by default)
    n = int(os.environ.get("REAL_LAST_N_MESSAGES", "800") or "800")
    run_dir = repo / "test" / "data" / "experiments" / f"run_{_now_tag()}_last{n}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # --- "clear db before": move default outputs aside (these may contain sensitive chat excerpts)
    data_dir = repo / "test" / "data"
    backup_dir = run_dir / "_backup_prev_outputs"
    for name in ["signal_case_blocks.json", "signal_cases_structured.json", "real_quality_eval.json"]:
        _move_if_exists(data_dir / name, backup_dir)

    # --- cost control: force cheap models by default (can disable)
    force_cheap = (os.environ.get("SCALE_FORCE_CHEAP_MODE") or "").strip().lower() not in {"0", "false", "no"}
    cheap_overrides = {
        "MODEL_BLOCKS": "gemini-2.5-flash-lite",
        "MODEL_CASE": "gemini-2.5-flash-lite",
        "MODEL_DECISION": "gemini-2.5-flash-lite",
        "MODEL_RESPOND": "gemini-2.5-flash-lite",
        "JUDGE_MODEL": "gemini-2.5-flash-lite",
        "EMBEDDING_MODEL": "gemini-embedding-001",
    }
    for k, v in cheap_overrides.items():
        if force_cheap:
            os.environ[k] = v
        else:
            os.environ.setdefault(k, v)

    # Ensure we don't accidentally reuse full-history cached blocks from older runs.
    os.environ.setdefault("REAL_REUSE_BLOCKS", "0")

    # Outputs go into run_dir
    os.environ["REAL_OUT_DIR"] = str(run_dir)
    # Make sure mining actually uses the same subset size we named the directory with.
    os.environ["REAL_LAST_N_MESSAGES"] = str(n)

    # --- run mining + evaluation
    py = Path(sys.executable)

    print(f"Run dir: {run_dir}")
    print(f"Subset: last {n} messages")

    # 1) Mine cases
    subprocess.run([str(py), str(repo / "test" / "mine_real_cases.py")], check=True)

    # 2) Eval quality
    os.environ["REAL_CASES_PATH"] = str(run_dir / "signal_cases_structured.json")
    subprocess.run([str(py), str(repo / "test" / "run_real_quality_eval.py")], check=True)

    # --- summarize (redacted: only aggregated metrics)
    eval_path = run_dir / "real_quality_eval.json"
    data = _read_json(eval_path)
    summary = data.get("summary") or {}
    by_cat = (summary.get("by_category") or {}) if isinstance(summary, dict) else {}

    redacted_summary = {
        "run_dir": str(run_dir),
        "subset_last_n_messages": n,
        "k": summary.get("k"),
        "n_cases_total": summary.get("n_cases_total"),
        "n_scenarios": summary.get("n_scenarios"),
        "by_category": by_cat,
    }

    out_summary = run_dir / "summary_redacted.json"
    out_summary.write_text(json.dumps(redacted_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\nSummary (redacted) written to:", out_summary)
    print(json.dumps(redacted_summary, ensure_ascii=False, indent=2))

    # --- "clear db after": remove raw artifacts unless KEEP_ARTIFACTS=1
    keep = (os.environ.get("KEEP_ARTIFACTS") or "").strip().lower() in {"1", "true", "yes", "y"}
    if not keep:
        for p in [
            run_dir / "signal_case_blocks.json",
            run_dir / "signal_cases_structured.json",
            run_dir / "real_quality_eval.json",
        ]:
            if p.exists():
                p.unlink()

        # Remove backup of previous outputs too unless explicitly requested.
        keep_prev = (os.environ.get("KEEP_PREV_OUTPUTS_BACKUP") or "").strip().lower() in {"1", "true", "yes", "y"}
        if backup_dir.exists() and not keep_prev:
            shutil.rmtree(str(backup_dir))
        elif backup_dir.exists() and not any(backup_dir.iterdir()):
            backup_dir.rmdir()
        print("\nCleaned up raw artifacts (KEEP_ARTIFACTS=1 to keep them).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

