#!/usr/bin/env python3
"""Bounded, sequential gbrain sync for the generated staging sources."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

SOURCES = ("gdrive-workspaces", "faos-projects")


def resolve_gbrain() -> str | None:
    binary = shutil.which("gbrain")
    if binary:
        return binary
    candidate = Path.home() / ".bun" / "bin" / "gbrain"
    return str(candidate) if candidate.exists() else None


def run_source(source_id: str, timeout: int, no_embed: bool) -> dict[str, object]:
    binary = resolve_gbrain()
    if not binary:
        raise RuntimeError("gbrain is not on PATH")
    command = [binary, "sync", "--source", source_id, "--no-pull", "--skip-failed", "--no-extract", "--json", "--yes"]
    if no_embed:
        command.append("--no-embed")
    started = time.monotonic()
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        return {"source_id": source_id, "status": "timeout", "elapsed_seconds": round(time.monotonic() - started, 2), "stderr_tail": stderr[-1000:]}
    result: dict[str, object] = {
        "source_id": source_id, "status": "success" if process.returncode == 0 else "failed",
        "returncode": process.returncode, "elapsed_seconds": round(time.monotonic() - started, 2),
        "stderr_tail": stderr[-1000:],
    }
    try:
        result["sync"] = json.loads(stdout)
    except json.JSONDecodeError:
        result["stdout_tail"] = stdout[-1000:]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=SOURCES, action="append")
    parser.add_argument("--timeout", type=int, default=900, help="seconds per source")
    parser.add_argument(
        "--slices", type=int, default=1,
        help="bounded resume attempts per selected source; timeout slices resume gbrain's durable checkpoint",
    )
    parser.add_argument("--no-embed", action="store_true")
    args = parser.parse_args()
    if args.slices < 1:
        parser.error("--slices must be at least 1")
    selected = tuple(args.source or SOURCES)
    results: list[dict[str, object]] = []
    for source in selected:
        slices: list[dict[str, object]] = []
        for attempt in range(1, args.slices + 1):
            outcome = run_source(source, args.timeout, args.no_embed)
            outcome["slice"] = attempt
            slices.append(outcome)
            # A completed source is converged. A non-timeout is actionable and
            # retrying it would only repeat a deterministic failure.
            if outcome["status"] != "timeout":
                break
        final_status = str(slices[-1]["status"])
        results.append({
            "source_id": source,
            "status": final_status,
            "slices": slices,
            "completed_slices": sum(item["status"] == "success" for item in slices),
        })
    print(json.dumps({"schema_version": 2, "sources": results}, sort_keys=True))
    return 0 if all(item["status"] == "success" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
