#!/usr/bin/env python3
"""Run one founder-extraction cycle within one hard wall-clock budget."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
from pathlib import Path


def run_bounded(argv: list[str], seconds: float) -> None:
    if seconds <= 0:
        raise RuntimeError("nightly_deadline_exhausted")
    process = subprocess.Popen(argv, start_new_session=True)
    try:
        process.wait(timeout=seconds)
    except subprocess.TimeoutExpired as exc:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        raise RuntimeError(f"nightly_step_timeout:{argv[0]}") from exc
    if process.returncode:
        raise RuntimeError(f"nightly_step_failed:{argv[0]}:{process.returncode}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wall-clock", type=int, default=14_400)
    parser.add_argument("--status-file", required=True)
    args = parser.parse_args()
    deadline = time.monotonic() + args.wall_clock

    def remaining(reserve: int = 0) -> float:
        return max(0, deadline - time.monotonic() - reserve)

    # 30m is reserved for both source-scoped syncs, 10m for materialization,
    # and 5m for the local-only staging commit. Every child is independently
    # terminated at its share of the same global deadline.
    run_bounded(["gbrain-extract", "run", "--time-limit", str(int(min(12_600, remaining(2_700)))), "--ocr-page-budget", "2000"], remaining(2_700))
    run_bounded(["gbrain-extract", "materialize"], min(600, remaining(2_100)))
    run_bounded(["gbrain-extract-commit-staging"], min(300, remaining(1_800)))
    run_bounded(["gbrain-extract-sync", "--timeout", "900"], remaining())
    status_path = Path(args.status_file)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    output = subprocess.check_output(["gbrain-extract", "status", "--json"], text=True, timeout=max(1, remaining()))
    payload = json.loads(output)
    temporary = status_path.with_suffix(status_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, status_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
