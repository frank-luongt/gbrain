#!/usr/bin/env python3
"""Account-scoped NotebookLM adapter for the local founder brain.

This adapter intentionally delegates browser/session handling to the locally
installed NotebookLM skill.  It neither authenticates nor stores Google
credentials.  A request must select exactly one configured account, so a
NotebookLM query can never combine personal and FAOSX browser contexts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VERSION = "1.0.0"
DEFAULT_CONFIG = Path(os.environ.get("GBRAIN_NOTEBOOKLM_CONFIG", "~/.gbrain/notebooklm.json")).expanduser()
DEFAULT_INBOX = Path(os.environ.get("GBRAIN_NOTEBOOKLM_INBOX", "~/gbrain-sources/inbox/notebooklm")).expanduser()
DEFAULT_SKILL = Path(os.environ.get("GBRAIN_NOTEBOOKLM_SKILL", "~/.claude/skills/notebooklm")).expanduser()
ACCOUNT_IDS = {"notebooklm-personal", "notebooklm-faosx"}
FORBIDDEN_CONFIG_KEYS = {
    "access_token", "api_key", "auth", "authorization", "cookie", "cookies",
    "credential", "credentials", "password", "secret", "session", "token",
}


class AdapterError(ValueError):
    """An expected governed adapter failure."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return code


def contains_forbidden_key(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).lower() in FORBIDDEN_CONFIG_KEYS:
                return str(key)
            nested_key = contains_forbidden_key(nested)
            if nested_key:
                return nested_key
    elif isinstance(value, list):
        for nested in value:
            nested_key = contains_forbidden_key(nested)
            if nested_key:
                return nested_key
    return None


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise AdapterError(f"configuration_missing:{path}")
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AdapterError(f"configuration_invalid:{exc.msg}") from exc
    forbidden = contains_forbidden_key(config)
    if forbidden:
        raise AdapterError(f"configuration_contains_credential_key:{forbidden}")
    if config.get("schema_version") != 1 or not isinstance(config.get("accounts"), list):
        raise AdapterError("configuration_schema_unsupported")
    accounts: dict[str, dict[str, Any]] = {}
    for item in config["accounts"]:
        if not isinstance(item, dict):
            raise AdapterError("configuration_account_invalid")
        account_id, profile = item.get("id"), item.get("profile")
        if account_id not in ACCOUNT_IDS or not isinstance(profile, str) or not profile:
            raise AdapterError("configuration_account_invalid")
        if account_id in accounts:
            raise AdapterError(f"configuration_account_duplicate:{account_id}")
        accounts[account_id] = {
            "id": account_id,
            "profile": profile,
            "enabled": bool(item.get("enabled", True)),
            "skill_root": str(item.get("skill_root", DEFAULT_SKILL)),
            "inbox_root": str(item.get("inbox_root", DEFAULT_INBOX)),
        }
    if set(accounts) != ACCOUNT_IDS:
        raise AdapterError("configuration_requires_personal_and_faosx_accounts")
    config["accounts_by_id"] = accounts
    return config


def selected_account(config: dict[str, Any], account_id: str) -> dict[str, Any]:
    # argparse supplies one scalar, but retain a defensive boundary for API callers.
    if account_id not in ACCOUNT_IDS or any(mark in account_id for mark in (",", "+", " ")):
        raise AdapterError("account_must_be_one_of:notebooklm-personal,notebooklm-faosx")
    account = config["accounts_by_id"].get(account_id)
    if account is None or not account["enabled"]:
        raise AdapterError(f"account_unavailable:{account_id}")
    return account


def skill_command(account: dict[str, Any], *args: str) -> list[str]:
    skill_root = Path(account["skill_root"]).expanduser()
    runner = skill_root / "scripts" / "run.py"
    if not runner.is_file():
        raise AdapterError(f"skill_runner_missing:{runner}")
    # run.py is the skill's required wrapper and receives a single profile.
    return [sys.executable, str(runner), "--profile", account["profile"], *args]


def run_skill(account: dict[str, Any], *args: str, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        skill_command(account, *args), text=True, capture_output=True,
        timeout=timeout, check=False,
    )


def reference(account: dict[str, Any], result: dict[str, Any]) -> dict[str, str]:
    notebook_id = str(result.get("notebook_id", ""))
    return {
        "title": str(result.get("notebook_name") or notebook_id or "NotebookLM notebook"),
        "source_id": account["id"],
        "account_scope": account["id"],
        "profile": account["profile"],
        "notebook_id": notebook_id,
        "notebook_url": str(result.get("notebook_url", "")),
    }


def query(config: dict[str, Any], account_id: str, question: str, notebook_id: str, show_browser: bool) -> int:
    account = selected_account(config, account_id)
    if not question.strip() or not notebook_id.strip():
        raise AdapterError("question_and_notebook_id_required")
    args = ["bridge.py", "query", "--question", question, "--notebook-id", notebook_id]
    if show_browser:
        args.append("--show-browser")
    completed = run_skill(account, *args)
    try:
        raw = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return emit({
            "status": "unavailable", "answer": "unavailable", "source_id": account["id"],
            "account_scope": account["id"], "cross_account_mixing": False,
            "error": "skill_bridge_invalid_response",
        }, 4)
    if raw.get("profile") != account["profile"]:
        return emit({
            "status": "unavailable", "answer": "unavailable", "source_id": account["id"],
            "account_scope": account["id"], "cross_account_mixing": False,
            "error": "profile_scope_mismatch",
        }, 4)
    status = "ok" if completed.returncode == 0 and raw.get("status") == "ok" else "unavailable"
    result = {
        "status": status,
        "answer": raw.get("answer", "unavailable") if status == "ok" else "unavailable",
        "grounding_source": account["id"],
        "source_id": account["id"],
        "account_scope": account["id"],
        "cross_account_mixing": False,
        "references": [reference(account, raw)] if raw.get("notebook_id") else [],
        "confidence": "source_grounded" if status == "ok" else "unavailable",
        "freshness": now_iso(),
        "sensitivity": "private",
    }
    if status != "ok":
        result["error"] = str(raw.get("error", "query_failed"))
    return emit(result, 0 if status == "ok" else 4)


def status(config: dict[str, Any], account_id: str) -> int:
    account = selected_account(config, account_id)
    completed = run_skill(account, "auth_manager.py", "status", timeout=30)
    return emit({
        "status": "ok" if completed.returncode == 0 else "unavailable",
        "source_id": account["id"], "account_scope": account["id"],
        "profile": account["profile"], "cross_account_mixing": False,
        "skill_status": completed.stdout.strip(),
        "skill_error": completed.stderr.strip(),
    }, 0 if completed.returncode == 0 else 3)


def capture(config: dict[str, Any], account_id: str, notebook_id: str, title: str, answer: str, question: str) -> int:
    account = selected_account(config, account_id)
    if not notebook_id.strip() or not title.strip() or not answer.strip():
        raise AdapterError("notebook_id_title_and_answer_required")
    inbox = Path(account["inbox_root"]).expanduser() / account["id"]
    inbox.mkdir(parents=True, exist_ok=True)
    capture_id = f"notebooklm-{uuid.uuid4().hex}"
    content_hash = hashlib.sha256(answer.encode("utf-8")).hexdigest()
    metadata = {
        "schema_version": 1, "capture_id": capture_id, "status": "review",
        "source_id": account["id"], "account_scope": account["id"], "profile": account["profile"],
        "notebook_id": notebook_id, "title": title, "question": question,
        "content_hash": content_hash, "captured_at": now_iso(), "sensitivity": "private",
        "generated": True, "ingest_to_canonical": False, "requires_founder_approval": True,
        "cross_account_mixing": False,
    }
    path = inbox / f"{capture_id}.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({**metadata, "answer": answer}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return emit({"status": "review", "capture": metadata, "path": str(path)})


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Account-scoped local NotebookLM adapter")
    result.add_argument("--config", default=str(DEFAULT_CONFIG))
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("status", "query", "capture"):
        command = commands.add_parser(name)
        command.add_argument("--account", required=True, choices=sorted(ACCOUNT_IDS))
    query_parser = commands.choices["query"]
    query_parser.add_argument("--question", required=True)
    query_parser.add_argument("--notebook-id", required=True)
    query_parser.add_argument("--show-browser", action="store_true")
    capture_parser = commands.choices["capture"]
    capture_parser.add_argument("--notebook-id", required=True)
    capture_parser.add_argument("--title", required=True)
    capture_parser.add_argument("--answer", required=True)
    capture_parser.add_argument("--question", default="")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = load_config(Path(args.config).expanduser())
        if args.command == "status":
            return status(config, args.account)
        if args.command == "query":
            return query(config, args.account, args.question, args.notebook_id, args.show_browser)
        return capture(config, args.account, args.notebook_id, args.title, args.answer, args.question)
    except (AdapterError, OSError, subprocess.TimeoutExpired) as exc:
        return emit({"status": "unavailable", "answer": "unavailable", "error": str(exc)}, 2)


if __name__ == "__main__":
    raise SystemExit(main())
