#!/usr/bin/env python3
"""Heartbeat probe for the local founder-brain extraction status marker."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def probe(path: Path, max_age_hours: float) -> dict[str, object]:
    result: dict[str, object] = {"path": str(path), "threshold_hours": max_age_hours}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        last_run = payload.get("last_run") or {}
        completed = last_run.get("completed_at")
        if not completed:
            raise ValueError("last_run.completed_at is missing")
        timestamp = datetime.fromisoformat(str(completed).replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - timestamp).total_seconds() / 3600
        stale = age > max_age_hours or last_run.get("status") not in {"success", "partial"}
        result.update({"ready": not stale, "age_hours": round(age, 2), "stale": stale, "last_run": last_run})
    except Exception as error:
        result.update({"ready": False, "stale": True, "error": str(error)})
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", default=str(Path("~/gbrain-sources/logs/status.json").expanduser()))
    parser.add_argument("--max-age-hours", type=float, default=24.0)
    args = parser.parse_args()
    result = probe(Path(args.status), args.max_age_hours)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
