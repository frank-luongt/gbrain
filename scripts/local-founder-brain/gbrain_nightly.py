#!/usr/bin/env python3
"""Run one founder-extraction cycle within one hard wall-clock budget."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def run_bounded(argv: list[str], seconds: float) -> dict[str, Any]:
    if seconds <= 0:
        raise RuntimeError("nightly_deadline_exhausted")
    started = time.monotonic()
    process = subprocess.Popen(argv, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        stdout, stderr = process.communicate(timeout=seconds)
    except subprocess.TimeoutExpired as exc:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        raise RuntimeError(f"nightly_step_timeout:{argv[0]}:{stderr[-500:]}") from exc
    if process.returncode:
        raise RuntimeError(f"nightly_step_failed:{argv[0]}:{process.returncode}:{stderr[-500:]}")
    result: dict[str, Any] = {
        "command": argv[0], "elapsed_seconds": round(time.monotonic() - started, 2),
    }
    try:
        result["result"] = json.loads(stdout)
    except json.JSONDecodeError:
        result["stdout_tail"] = stdout[-500:]
    if stderr:
        result["stderr_tail"] = stderr[-500:]
    return result


def previous_success(path: Path) -> str | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("last_success_at")
        return str(value) if value else None
    except (OSError, json.JSONDecodeError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wall-clock", type=int, default=14_400)
    parser.add_argument("--status-file", required=True)
    args = parser.parse_args()
    status_path = Path(args.status_file)
    deadline = time.monotonic() + args.wall_clock
    steps: list[dict[str, Any]] = []

    def remaining(reserve: int = 0) -> float:
        return max(0, deadline - time.monotonic() - reserve)

    try:
        # Keep every phase bounded inside the four-hour cycle. Large source
        # syncs bank completed paths in gbrain's durable checkpoint, so give
        # FAOS up to eight 15-minute resume slices rather than one monolithic
        # process or a once-per-day replay. The phase caps sum to the cycle:
        # extraction 45m, reconcile 10m, materialize 45m, staging commit 5m,
        # Drive sync 15m, and FAOS sync 120m. When the earlier phases finish
        # early, the remainder is a local Ollama stale-embedding backfill.
        steps.append(run_bounded(
            ["gbrain-extract", "run", "--time-limit", str(int(min(2_700, remaining(11_700)))), "--ocr-page-budget", "2000"],
            remaining(11_700),
        ))
        steps.append(run_bounded(["gbrain-extract", "reconcile", "--fix-safe"], min(600, remaining(11_100))))
        steps.append(run_bounded(["gbrain-extract", "materialize", "--time-limit", str(int(min(2_700, remaining(8_400))))], min(2_700, remaining(8_400))))
        steps.append(run_bounded(["gbrain-extract-commit-staging"], min(300, remaining(8_100))))
        steps.append(run_bounded(["gbrain-extract-sync", "--source", "gdrive-workspaces", "--timeout", "900"], min(900, remaining(7_200))))
        steps.append(run_bounded(["gbrain-extract-sync", "--source", "faos-projects", "--timeout", "900", "--slices", "8"], remaining()))
        # `embed --stale` is global in gbrain, but only operates on chunks
        # missing the active local Ollama vector. It is intentionally last so
        # every newly imported page is eligible and its bounded process group
        # cannot starve extraction, reconciliation, or source sync.
        # `--catch-up` is essential: the default command stops after one
        # batch, leaving a large imported source apparently fresh but only
        # partly embedded.  The enclosing four-hour deadline remains the
        # operational bound.
        steps.append(run_bounded(["gbrain", "embed", "--stale", "--catch-up"], remaining()))
        status_step = run_bounded(["gbrain-extract", "status", "--json"], max(1, remaining()))
        steps.append(status_step)
        last_run = status_step.get("result", {}).get("last_run", {})
        materialize_partial = bool(steps[2].get("result", {}).get("partial"))
        cycle_status = "success" if last_run.get("status") == "success" and not materialize_partial else "partial"
        last_success_at = now_iso() if cycle_status == "success" else previous_success(status_path)
        payload = {
            "schema_version": 2, "completed_at": now_iso(), "cycle_status": cycle_status,
            "last_success_at": last_success_at, "last_run": last_run, "steps": steps,
        }
        atomic_write(status_path, payload)
        return 0 if cycle_status == "success" else 2
    except Exception as error:
        atomic_write(status_path, {
            "schema_version": 2, "completed_at": now_iso(), "cycle_status": "failed",
            "last_success_at": previous_success(status_path), "error": str(error), "steps": steps,
        })
        print(f"gbrain-extract-nightly-cycle: {error}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
