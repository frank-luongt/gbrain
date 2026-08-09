#!/usr/bin/env python3
"""Add founder-extraction freshness to the existing gbrain heartbeat report."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def atomic_write(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    gbrain_home = Path(os.environ.get("GBRAIN_HOME", str(Path.home() / ".gbrain")))
    hermes_home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".faos" / "hermes-frank")))
    original = gbrain_home / "bin" / "gbrain-heartbeat.py"
    probe = gbrain_home / "bin" / "gbrain-extract-health"
    status = Path.home() / "gbrain-sources" / "logs" / "status.json"
    outputs = [gbrain_home / "heartbeat" / "latest.json", hermes_home / "gbrain-heartbeat" / "latest.json"]

    baseline = subprocess.run([sys.executable, str(original)], capture_output=True, text=True, check=False)
    if baseline.returncode != 0:
        print(baseline.stderr, file=sys.stderr, end="")
        return baseline.returncode
    health_run = subprocess.run(
        [sys.executable, str(probe), "--status", str(status), "--max-age-hours", "24"],
        capture_output=True, text=True, check=False,
    )
    try:
        extraction = json.loads(health_run.stdout)
    except json.JSONDecodeError:
        extraction = {"ready": False, "stale": True, "error": health_run.stderr.strip() or "invalid extraction probe output"}

    for output in outputs:
        report = json.loads(output.read_text(encoding="utf-8"))
        report.setdefault("signals", {})["founder_extraction"] = extraction
        reasons = report.setdefault("red_reasons", [])
        reason = "no successful founder extraction cycle within 24 hours"
        if not extraction.get("ready") and reason not in reasons:
            reasons.append(reason)
        report["status"] = "RED" if reasons else "GREEN"
        atomic_write(output, report)
    print(json.dumps(json.loads(outputs[0].read_text(encoding="utf-8")), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
