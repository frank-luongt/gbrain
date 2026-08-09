#!/usr/bin/env python3
"""Offline, resumable document extraction for the local FrankBrain evidence corpus.

The utility deliberately has no Python package dependencies. External parsers are
invoked as bounded subprocesses and every source root is read-only.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import glob
import hashlib
import html
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Sequence
from xml.etree import ElementTree as ET

VERSION = "1.0.0"
SCHEMA_VERSION = 2
EXTRACTION_VERSION = "founder-local-v1"
MAX_MARKDOWN_BYTES = 3_500_000
MAX_ZIP_EXPANDED_BYTES = 512_000_000
DEFAULT_TIME_LIMIT = 4 * 60 * 60
DEFAULT_OCR_PAGE_BUDGET = 2_000
OCR_WINDOW_PAGES = 20

DATA_ROOT = Path(os.environ.get("GBRAIN_EXTRACT_ROOT", "~/gbrain-sources")).expanduser()
DEFAULT_CONFIG = DATA_ROOT / "sources.json"
DEFAULT_DB = DATA_ROOT / "extract-state.sqlite3"
DEFAULT_CORPUS = DATA_ROOT / "corpus-md-v2"
DEFAULT_STAGING = DATA_ROOT / "staging"
DEFAULT_LOGS = DATA_ROOT / "logs"
DEFAULT_QUARANTINE = DATA_ROOT / "quarantine"

PDFTOTEXT = shutil.which("pdftotext") or "/opt/homebrew/bin/pdftotext"
PDFINFO = shutil.which("pdfinfo") or "/opt/homebrew/bin/pdfinfo"
PDFTOPPM = shutil.which("pdftoppm") or "/opt/homebrew/bin/pdftoppm"
TESSERACT = shutil.which("tesseract") or "/opt/homebrew/bin/tesseract"
SOFFICE = shutil.which("soffice") or "/Applications/LibreOffice.app/Contents/MacOS/soffice"
TEXTUTIL = "/usr/bin/textutil"

SOURCE_DEFAULTS = (
    {
        "id": "gdrive-workspaces",
        "root": "~/Library/CloudStorage/GoogleDrive-*/My Drive/1 Workspaces",
        "pipeline": "document",
        "enabled": True,
    },
    {
        "id": "faos-projects",
        "root": "~/Projects/FAOS",
        "pipeline": "document-and-code",
        "enabled": True,
    },
)

SKIP_DIRS = {
    ".git", ".hg", ".svn", ".claude", ".turbo", ".mypy_cache", ".ruff_cache",
    ".pytest_cache", ".next", ".cache", "node_modules", "vendor", "dist", "build",
    "target", "coverage", "htmlcov", "venv", ".venv", "env", "__pycache__",
    "site-packages", "Pods", "DerivedData", "wiki/frankbrain", "corpus-md", "staging",
}
SKIP_PARTS = {(".claude", "worktrees"), (".claude", "audio"), ("wiki", "frankbrain")}
SKIP_SUFFIXES = {
    ".lock", ".map", ".pyc", ".pyo", ".class", ".o", ".a", ".dylib", ".so",
    ".woff", ".woff2", ".ttf", ".otf", ".bin", ".safetensors", ".gguf", ".ckpt",
    ".tmp", ".swp", ".bak", ".DS_Store",
}
DOCUMENT_EXTS = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".xlsm",
    ".rtf", ".odt", ".ods", ".odp", ".epub", ".zip", ".pages", ".key",
    ".numbers", ".wpd", ".xlsb",
}
TEXT_EXTS = {
    ".md", ".mdx", ".txt", ".rst", ".csv", ".tsv", ".json", ".jsonl", ".yaml",
    ".yml", ".toml", ".xml", ".html", ".htm", ".sql", ".graphql", ".py", ".pyi",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".kt", ".kts", ".go",
    ".rs", ".rb", ".php", ".swift", ".c", ".h", ".cpp", ".hpp", ".cs", ".sh",
    ".zsh", ".fish", ".ps1", ".vue", ".svelte", ".tex", ".ini", ".cfg", ".conf",
}

MAGIC_PDF = b"%PDF"
MAGIC_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
MAGIC_ZIP = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
DATA_URI_RE = re.compile(r"data:[\w.+-]+/[\w.+-]+;base64,[A-Za-z0-9+/=]{256,}", re.I)
BASE64_LINE_RE = re.compile(r"^[A-Za-z0-9+/]{1000,}={0,2}$")


@dataclass(frozen=True)
class Source:
    id: str
    root: Path
    pipeline: str
    enabled: bool = True


@dataclass
class Extraction:
    text: str
    extractor: str
    status: str = "extracted"
    error_code: str | None = None
    reason: str | None = None
    page_count: int | None = None
    page_range: str | None = None


class StopRequested(RuntimeError):
    pass


STOP = False


def request_stop(_signum: int, _frame: object) -> None:
    global STOP
    STOP = True


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_document_id(content_hash: str) -> str:
    return f"doc-{content_hash[:24]}"


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")[:80] or "document"


def sniff_magic(path: Path) -> str:
    try:
        head = path.open("rb").read(16)
    except OSError:
        return "unreadable"
    if head.startswith(MAGIC_PDF):
        return "pdf"
    if head.startswith(MAGIC_OLE):
        return "ole"
    if any(head.startswith(value) for value in MAGIC_ZIP):
        return "zip"
    if b"\x00" in head:
        return "binary"
    return "text"


def looks_binary(text: str) -> bool:
    if not text.strip():
        return False
    sample = text[:200_000]
    controls = sum(1 for char in sample if ord(char) < 32 and char not in "\n\r\t\f")
    replacement = sample.count("\ufffd")
    return (controls + replacement) / max(1, len(sample)) > 0.01


def has_embedded_payload(text: str) -> bool:
    if DATA_URI_RE.search(text):
        return True
    return any(BASE64_LINE_RE.match(line.strip()) for line in text.splitlines())


def has_pdf_encoding_garbage(text: str) -> bool:
    """Detect long font-decoding runs that are printable but not usable language."""
    compact = re.sub(r"\s", "", text)
    for offset in range(0, len(compact), 2_000):
        window = compact[offset:offset + 2_000]
        if len(window) < 1_000:
            continue
        letters = sum(char.isalpha() for char in window)
        punctuation_or_digits = sum(not char.isalpha() for char in window)
        if letters / len(window) < 0.15 and punctuation_or_digits / len(window) > 0.80:
            return True
    return False


def validate_text(text: str, min_chars: int = 20) -> str:
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    if looks_binary(text):
        raise ValueError("binary_garbage")
    if has_embedded_payload(text):
        raise ValueError("embedded_payload")
    if len(re.sub(r"\s", "", text)) < min_chars:
        raise ValueError("insufficient_text")
    return text.strip() + "\n"


def run_command(argv: Sequence[str], timeout: int = 180, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv), input=input_text, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, errors="replace", timeout=timeout, check=False,
    )


def load_sources(config_path: Path) -> list[Source]:
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json_dump({"schema_version": 1, "sources": SOURCE_DEFAULTS}) + "\n", encoding="utf-8")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if raw.get("schema_version") != 1 or not isinstance(raw.get("sources"), list):
        raise ValueError(f"Unsupported source registry: {config_path}")
    sources = []
    for item in raw["sources"]:
        root_pattern = os.path.expandvars(str(item["root"]))
        matches = [Path(value).resolve() for value in glob.glob(str(Path(root_pattern).expanduser()))]
        if len(matches) > 1:
            raise ValueError(f"Source root pattern is ambiguous for {item['id']}: {root_pattern}")
        root = matches[0] if matches else Path(root_pattern).expanduser().resolve()
        source = Source(str(item["id"]), root, str(item["pipeline"]), bool(item.get("enabled", True)))
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,62}", source.id):
            raise ValueError(f"Invalid source id: {source.id}")
        sources.append(source)
    if len({source.id for source in sources}) != len(sources):
        raise ValueError("Duplicate source ids")
    return sources


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS docs (
          sha256 TEXT PRIMARY KEY, src_path TEXT NOT NULL, bytes INTEGER NOT NULL,
          folder TEXT NOT NULL, state TEXT NOT NULL, extract_chars INTEGER DEFAULT 0,
          out_path TEXT, extractor TEXT, reason TEXT, ext TEXT, magic TEXT,
          mtime REAL, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS aliases (
          sha256 TEXT NOT NULL, src_path TEXT NOT NULL, PRIMARY KEY (sha256, src_path)
        );
        CREATE TABLE IF NOT EXISTS path_cache (
          src_path TEXT PRIMARY KEY, bytes INTEGER NOT NULL, mtime REAL NOT NULL, sha256 TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS source_memberships (
          sha256 TEXT NOT NULL, source_id TEXT NOT NULL, src_path TEXT NOT NULL,
          is_owner INTEGER NOT NULL DEFAULT 0, discovered_at TEXT NOT NULL,
          PRIMARY KEY (sha256, source_id, src_path)
        );
        CREATE TABLE IF NOT EXISTS ocr_ranges (
          sha256 TEXT NOT NULL, first_page INTEGER NOT NULL, last_page INTEGER NOT NULL,
          output_text TEXT NOT NULL, completed_at TEXT NOT NULL,
          PRIMARY KEY (sha256, first_page, last_page)
        );
        CREATE TABLE IF NOT EXISTS outputs (
          sha256 TEXT NOT NULL, source_id TEXT NOT NULL, part INTEGER NOT NULL,
          parts_total INTEGER NOT NULL, out_path TEXT NOT NULL, output_hash TEXT NOT NULL,
          bytes INTEGER NOT NULL, created_at TEXT NOT NULL,
          PRIMARY KEY (sha256, source_id, part)
        );
        CREATE TABLE IF NOT EXISTS runs (
          id TEXT PRIMARY KEY, started_at TEXT NOT NULL, completed_at TEXT,
          config_hash TEXT NOT NULL, extractor_version TEXT NOT NULL,
          status TEXT NOT NULL, manifest_path TEXT, counts_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_docs_state ON docs(state);
        CREATE INDEX IF NOT EXISTS idx_memberships_source ON source_memberships(source_id);
        """
    )
    additions = {
        "source_id": "TEXT", "document_id": "TEXT", "extraction_version": "TEXT",
        "extraction_hash": "TEXT", "error_code": "TEXT", "retry_count": "INTEGER NOT NULL DEFAULT 0",
        "page_count": "INTEGER", "completed_pages": "INTEGER NOT NULL DEFAULT 0",
    }
    columns = table_columns(db, "docs")
    for name, ddl in additions.items():
        if name not in columns:
            db.execute(f"ALTER TABLE docs ADD COLUMN {name} {ddl}")
    db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
    db.commit()


def classify_source(path: str, sources: Sequence[Source]) -> str | None:
    candidate = Path(path).expanduser()
    matches = [source for source in sources if candidate == source.root or source.root in candidate.parents]
    if not matches:
        return None
    matches.sort(key=lambda source: (0 if source.id == "gdrive-workspaces" else 1, -len(str(source.root))))
    return matches[0].id


def migration_preview(db: sqlite3.Connection, sources: Sequence[Source]) -> dict[str, int]:
    rows = db.execute("SELECT sha256,src_path,state,extractor,ext FROM docs").fetchall()
    return {
        "docs": len(rows),
        "source_classifiable": sum(classify_source(row["src_path"], sources) is not None for row in rows),
        "truncated_to_partial": sum("TRUNCATED@20" in (row["extractor"] or "") for row in rows),
        "ocr_pending_rename": sum(row["state"] == "ocr_needed" for row in rows),
    }


def migrate_state(db_path: Path, sources: Sequence[Source], dry_run: bool) -> dict[str, int]:
    if dry_run:
        uri = f"file:{db_path.resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as db:
            db.row_factory = sqlite3.Row
            return migration_preview(db, sources)
    with connect(db_path) as db:
        preview = migration_preview(db, sources)
        backup = db_path.with_name(f"{db_path.name}.pre-v{SCHEMA_VERSION}-{int(time.time())}.bak")
        with sqlite3.connect(backup) as target:
            db.backup(target)
        ensure_schema(db)
        for row in db.execute("SELECT sha256,src_path,state,extractor,ext FROM docs").fetchall():
            source_id = classify_source(row["src_path"], sources)
            state = "ocr_pending" if row["state"] == "ocr_needed" else row["state"]
            if "TRUNCATED@20" in (row["extractor"] or ""):
                state = "partial"
            if row["state"] == "unsupported" and row["ext"] in {"ppt", "xls", "doc"} and Path(SOFFICE).exists():
                state = "discovered"
            db.execute(
                """UPDATE docs SET source_id=?,document_id=?,extraction_version=COALESCE(extraction_version,?),state=?,
                   error_code=CASE WHEN ?='partial' THEN 'ocr_incomplete' ELSE error_code END,
                   completed_pages=CASE WHEN ?='partial' THEN MAX(completed_pages,20) ELSE completed_pages END
                   WHERE sha256=?""",
                (source_id, stable_document_id(row["sha256"]), EXTRACTION_VERSION, state, state, state, row["sha256"]),
            )
        paths = db.execute("SELECT sha256,src_path FROM aliases UNION SELECT sha256,src_path FROM docs").fetchall()
        for row in paths:
            source_id = classify_source(row["src_path"], sources)
            if source_id:
                db.execute(
                    "INSERT OR IGNORE INTO source_memberships(sha256,source_id,src_path,is_owner,discovered_at) VALUES(?,?,?,?,?)",
                    (row["sha256"], source_id, row["src_path"], 0, now_iso()),
                )
        db.execute("UPDATE source_memberships SET is_owner=0")
        db.execute(
            """UPDATE source_memberships SET is_owner=1 WHERE rowid IN (
              SELECT sm.rowid FROM source_memberships sm JOIN (
                SELECT sha256, MIN(CASE source_id WHEN 'gdrive-workspaces' THEN '0' ELSE '1' END || source_id || src_path) choice
                FROM source_memberships GROUP BY sha256
              ) x ON x.sha256=sm.sha256
              WHERE (CASE sm.source_id WHEN 'gdrive-workspaces' THEN '0' ELSE '1' END || sm.source_id || sm.src_path)=x.choice
            )"""
        )
        db.execute("UPDATE docs SET source_id=(SELECT source_id FROM source_memberships sm WHERE sm.sha256=docs.sha256 AND sm.is_owner=1 LIMIT 1)")
        db.commit()
        preview["backup_created"] = 1
        return preview


def should_skip(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return True
    if any(part in SKIP_DIRS for part in parts[:-1]):
        return True
    if any(all(piece in parts for piece in pattern) for pattern in SKIP_PARTS):
        return True
    if path.name.startswith(".") or path.suffix.lower() in SKIP_SUFFIXES:
        return True
    return False


def iter_files(source: Source) -> Iterator[Path]:
    if not source.root.is_dir():
        return
    for current, dirs, files in os.walk(source.root, followlinks=False):
        current_path = Path(current)
        dirs[:] = [name for name in dirs if not should_skip(current_path / name / "x", source.root)]
        for name in files:
            path = current_path / name
            if path.is_symlink() or should_skip(path, source.root):
                continue
            ext = path.suffix.lower()
            if ext in DOCUMENT_EXTS or (source.pipeline == "document-and-code" and ext in TEXT_EXTS):
                yield path


def extract_ooxml(path: Path, kind: str) -> Extraction:
    values: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if kind == "docx":
                selected = [name for name in names if name.startswith("word/") and name.endswith(".xml")]
            elif kind in {"pptx", "odp"}:
                selected = sorted(name for name in names if (name.startswith("ppt/slides/") or name.startswith("content")) and name.endswith(".xml"))
            elif kind in {"xlsx", "xlsm", "ods"}:
                selected = [name for name in names if (name.startswith("xl/") or name.startswith("content")) and name.endswith(".xml")]
            else:
                selected = [name for name in names if name.endswith((".xhtml", ".html", ".xml")) and "META-INF" not in name]
            for name in selected:
                data = archive.read(name)
                try:
                    root = ET.fromstring(data)
                    text = " ".join(value.strip() for value in root.itertext() if value.strip())
                except ET.ParseError:
                    text = html.unescape(re.sub(r"<[^>]+>", " ", data.decode("utf-8", "replace")))
                if text.strip():
                    values.append(f"## {Path(name).stem}\n\n{text.strip()}")
        return Extraction(validate_text("\n\n".join(values)), f"ooxml-zip:{kind}")
    except zipfile.BadZipFile as error:
        return Extraction("", f"ooxml-zip:{kind}", "failed", "invalid_zip", str(error))
    except NotImplementedError as error:
        return Extraction("", f"ooxml-zip:{kind}", "excluded", "unsupported_zip_compression", str(error))
    except ValueError as error:
        status = "ocr_pending" if str(error) == "insufficient_text" else "excluded"
        return Extraction("", f"ooxml-zip:{kind}", status, str(error), None)


def extract_pdf_text(path: Path) -> Extraction:
    if not Path(PDFTOTEXT).exists():
        return Extraction("", "pdftotext", "failed", "missing_pdftotext", PDFTOTEXT)
    result = run_command([PDFTOTEXT, "-layout", str(path), "-"], timeout=300)
    if "password" in result.stderr.lower() or "encrypted" in result.stderr.lower():
        return Extraction("", "pdftotext", "excluded", "encrypted_document", result.stderr.strip()[:500])
    try:
        text = validate_text(result.stdout, 80)
        if has_pdf_encoding_garbage(text):
            raise ValueError("pdf_font_encoding_garbage")
        return Extraction(text, "pdftotext")
    except ValueError as error:
        return Extraction("", "pdftotext", "ocr_pending", str(error), result.stderr.strip()[:500])


def pdf_page_count(path: Path) -> int | None:
    if not Path(PDFINFO).exists():
        return None
    result = run_command([PDFINFO, str(path)], timeout=60)
    match = re.search(r"^Pages:\s+(\d+)", result.stdout, re.M)
    return int(match.group(1)) if match else None


def completed_ocr_pages(db: sqlite3.Connection, content_hash: str) -> set[int]:
    pages: set[int] = set()
    for row in db.execute("SELECT first_page,last_page FROM ocr_ranges WHERE sha256=?", (content_hash,)):
        pages.update(range(row[0], row[1] + 1))
    return pages


def extract_ocr_window(path: Path, db: sqlite3.Connection, content_hash: str, remaining_budget: int) -> tuple[Extraction, int]:
    if not all(Path(tool).exists() for tool in (PDFTOPPM, TESSERACT)):
        return Extraction("", "ocr:tesseract-vie+eng", "ocr_pending", "missing_ocr_tool", "pdftoppm or tesseract unavailable"), 0
    total = pdf_page_count(path)
    if not total:
        return Extraction("", "ocr:tesseract-vie+eng", "failed", "page_count_unknown", None), 0
    done = completed_ocr_pages(db, content_hash)
    pending = [page for page in range(1, total + 1) if page not in done]
    if not pending or remaining_budget <= 0:
        rows = db.execute("SELECT output_text FROM ocr_ranges WHERE sha256=? ORDER BY first_page", (content_hash,)).fetchall()
        return Extraction(validate_text("\n\n".join(row[0] for row in rows), 20), "ocr:tesseract-vie+eng", "extracted", page_count=total, page_range=f"1-{total}"), 0
    window = pending[: min(OCR_WINDOW_PAGES, remaining_budget)]
    first_page, last_page = window[0], window[-1]
    page_text: list[str] = []
    with tempfile.TemporaryDirectory(prefix="gbrain-ocr-") as temp:
        prefix = Path(temp) / "page"
        render = run_command([PDFTOPPM, "-f", str(first_page), "-l", str(last_page), "-r", "220", "-png", str(path), str(prefix)], timeout=900)
        if render.returncode != 0:
            return Extraction("", "ocr:tesseract-vie+eng", "failed", "ocr_render_failed", render.stderr.strip()[:500]), 0
        images = sorted(Path(temp).glob("page-*.png"))
        for offset, image in enumerate(images):
            result = run_command([TESSERACT, str(image), "stdout", "-l", "vie+eng", "--psm", "3"], timeout=300)
            if result.returncode != 0:
                return Extraction("", "ocr:tesseract-vie+eng", "failed", "tesseract_failed", result.stderr.strip()[:500]), offset
            page_text.append(f"## Page {first_page + offset}\n\n{result.stdout.strip()}")
    text = validate_text("\n\n".join(page_text), 20)
    db.execute(
        "INSERT OR REPLACE INTO ocr_ranges(sha256,first_page,last_page,output_text,completed_at) VALUES(?,?,?,?,?)",
        (content_hash, first_page, last_page, text, now_iso()),
    )
    db.commit()
    done.update(range(first_page, last_page + 1))
    all_text = "\n\n".join(row[0] for row in db.execute("SELECT output_text FROM ocr_ranges WHERE sha256=? ORDER BY first_page", (content_hash,)))
    status = "extracted" if len(done) >= total else "partial"
    return Extraction(validate_text(all_text, 20), "ocr:tesseract-vie+eng", status, None if status == "extracted" else "ocr_incomplete", page_count=total, page_range=f"1-{max(done)}"), len(window)


def extract_with_textutil(path: Path) -> Extraction:
    result = run_command([TEXTUTIL, "-convert", "txt", "-stdout", str(path)], timeout=180)
    try:
        return Extraction(validate_text(result.stdout), "textutil")
    except ValueError as error:
        return Extraction("", "textutil", "failed", str(error), result.stderr.strip()[:500])


def extract_with_libreoffice(path: Path) -> Extraction:
    if not Path(SOFFICE).exists():
        return Extraction("", "libreoffice", "unsupported", "missing_converter", "LibreOffice is not installed")
    target_ext = {".ppt": "pptx", ".xls": "xlsx", ".doc": "docx"}.get(path.suffix.lower())
    if not target_ext:
        return Extraction("", "libreoffice", "unsupported", "unsupported_legacy_format", path.suffix)
    with tempfile.TemporaryDirectory(prefix="gbrain-office-") as temp:
        result = run_command([SOFFICE, "--headless", "--convert-to", target_ext, "--outdir", temp, str(path)], timeout=300)
        converted = next(Path(temp).glob(f"*.{target_ext}"), None)
        if result.returncode != 0 or not converted:
            return Extraction("", "libreoffice", "failed", "conversion_failed", result.stderr.strip()[:500])
        return extract_ooxml(converted, target_ext)


def extract_zip_payloads(path: Path) -> Extraction:
    pieces: list[str] = []
    total = 0
    try:
      archive_context = zipfile.ZipFile(path)
    except zipfile.BadZipFile as error:
      return Extraction("", "zip", "failed", "invalid_zip", str(error))
    with archive_context as archive, tempfile.TemporaryDirectory(prefix="gbrain-zip-") as temp:
        for info in archive.infolist():
            if info.is_dir() or info.file_size <= 0:
                continue
            total += info.file_size
            if total > MAX_ZIP_EXPANDED_BYTES:
                return Extraction("", "zip", "excluded", "zip_expansion_limit", str(total))
            safe_name = Path(info.filename)
            if safe_name.is_absolute() or ".." in safe_name.parts:
                return Extraction("", "zip", "excluded", "zip_unsafe_path", info.filename)
            ext = safe_name.suffix.lower()
            if ext not in TEXT_EXTS and ext not in {".docx", ".pptx", ".xlsx", ".xlsm", ".epub"}:
                continue
            target = Path(temp) / f"{uuid.uuid4().hex}{ext}"
            try:
                with archive.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
            except (RuntimeError, NotImplementedError) as error:
                return Extraction("", "zip", "excluded", "unsupported_zip_compression", str(error))
            if ext in TEXT_EXTS:
                child = Extraction(validate_text(target.read_text(encoding="utf-8", errors="replace")), "text")
            else:
                child = extract_ooxml(target, ext.lstrip("."))
            pieces.append(f"# Archive item: {info.filename}\n\n{child.text}")
    if not pieces:
        return Extraction("", "zip", "excluded", "zip_no_readable_payload", None)
    return Extraction(validate_text("\n\n".join(pieces)), "zip:safe-leaf")


def route_extract(path: Path, db: sqlite3.Connection, content_hash: str, ocr_budget: int) -> tuple[Extraction, int]:
    ext, magic = path.suffix.lower(), sniff_magic(path)
    if magic == "pdf":
        result = extract_pdf_text(path)
        if result.status == "ocr_pending":
            return extract_ocr_window(path, db, content_hash, ocr_budget)
        return result, 0
    if magic == "ole":
        if ext == ".doc":
            result = extract_with_textutil(path)
            if result.status == "extracted":
                return result, 0
        return extract_with_libreoffice(path), 0
    if magic == "zip":
        if ext in {".docx", ".pptx", ".xlsx", ".xlsm", ".epub", ".odt", ".ods", ".odp"}:
            return extract_ooxml(path, ext.lstrip(".")), 0
        if ext == ".zip":
            return extract_zip_payloads(path), 0
        return Extraction("", "zip", "unsupported", "unsupported_container", ext), 0
    if ext in TEXT_EXTS and magic == "text":
        try:
            return Extraction(validate_text(path.read_text(encoding="utf-8", errors="replace")), "text"), 0
        except ValueError as error:
            return Extraction("", "text", "excluded", str(error), None), 0
    if ext in {".rtf", ".odt"}:
        return extract_with_textutil(path), 0
    return Extraction("", "none", "unsupported", "unsupported_format", f"{ext}:{magic}"), 0


def strip_legacy_frontmatter(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end >= 0:
            return text[end + 5 :].lstrip()
    return text


def strip_duplicate_title(text: str) -> str:
    return re.sub(r"^# [^\n]+\n+", "", text, count=1)


def split_text(text: str, max_bytes: int = MAX_MARKDOWN_BYTES) -> list[str]:
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]
    sections = re.split(r"(?=^#{1,3}\s)", text, flags=re.M)
    if len(sections) <= 1:
        sections = re.split(r"(?=^\s*$)", text, flags=re.M)
    parts: list[str] = []
    current = ""
    for section in sections:
        if not section:
            continue
        if len(section.encode("utf-8")) > max_bytes:
            encoded = section.encode("utf-8")
            while encoded:
                take = encoded[:max_bytes]
                while take:
                    try:
                        chunk = take.decode("utf-8")
                        break
                    except UnicodeDecodeError:
                        take = take[:-1]
                if current:
                    parts.append(current.rstrip() + "\n")
                    current = ""
                parts.append(chunk.rstrip() + "\n")
                encoded = encoded[len(take):]
            continue
        candidate = current + section
        if current and len(candidate.encode("utf-8")) > max_bytes:
            parts.append(current.rstrip() + "\n")
            current = section
        else:
            current = candidate
    if current:
        parts.append(current.rstrip() + "\n")
    return parts


def yaml_scalar(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_markdown(row: sqlite3.Row, source_id: str, text: str, aliases: list[str], part: int, total: int, extraction: Extraction | None = None) -> str:
    document_id = row["document_id"] or stable_document_id(row["sha256"])
    page_range = extraction.page_range if extraction else (f"1-{row['completed_pages']}" if row["state"] == "partial" and row["completed_pages"] else None)
    status = extraction.status if extraction else ("partial" if row["state"] == "partial" else "complete")
    fields: list[tuple[str, object]] = [
        ("schema_version", 1), ("document_id", document_id), ("source_id", source_id),
        ("source_path", row["src_path"]), ("content_hash", row["sha256"]),
        ("extraction_version", row["extraction_version"] or EXTRACTION_VERSION),
        ("extractor", row["extractor"] or "legacy"), ("extraction_status", "partial" if status == "partial" else "complete"),
    ]
    if page_range:
        fields.append(("page_range", page_range))
    if total > 1:
        fields.extend((("part", part), ("parts_total", total)))
        if part > 1:
            fields.append(("previous_part", f"{document_id}-part-{part-1:03d}"))
        if part < total:
            fields.append(("next_part", f"{document_id}-part-{part+1:03d}"))
    fields.extend((("aliases", aliases), ("sensitivity", "local_only"), ("generated", True), ("ingest_to_canonical", False)))
    frontmatter = "\n".join(f"{key}: {yaml_scalar(value)}" for key, value in fields)
    title = Path(row["src_path"]).name
    return f"---\n{frontmatter}\n---\n\n# {title}\n\n{text.strip()}\n"


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def write_corpus_output(db: sqlite3.Connection, row: sqlite3.Row, extraction: Extraction) -> Path:
    document_id = row["document_id"] or stable_document_id(row["sha256"])
    output = DEFAULT_CORPUS / row["sha256"][:2] / f"{document_id}.md"
    atomic_write(output, extraction.text)
    return output


def upsert_discovery(db: sqlite3.Connection, source: Source, path: Path) -> tuple[sqlite3.Row, bool]:
    stat = path.stat()
    cached = db.execute("SELECT sha256 FROM path_cache WHERE src_path=? AND bytes=? AND mtime=?", (str(path), stat.st_size, stat.st_mtime)).fetchone()
    content_hash = cached[0] if cached else sha256_file(path)
    if not cached:
        db.execute("INSERT OR REPLACE INTO path_cache(src_path,bytes,mtime,sha256) VALUES(?,?,?,?)", (str(path), stat.st_size, stat.st_mtime, content_hash))
    existing = db.execute("SELECT * FROM docs WHERE sha256=?", (content_hash,)).fetchone()
    is_new = existing is None
    if is_new:
        db.execute(
            """INSERT INTO docs(sha256,src_path,bytes,folder,state,ext,magic,mtime,updated_at,source_id,document_id,extraction_version,retry_count)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)""",
            (content_hash, str(path), stat.st_size, str(path.parent), "discovered", path.suffix.lower().lstrip("."), sniff_magic(path), stat.st_mtime, now_iso(), source.id, stable_document_id(content_hash), EXTRACTION_VERSION),
        )
    db.execute("INSERT OR IGNORE INTO aliases(sha256,src_path) VALUES(?,?)", (content_hash, str(path)))
    db.execute(
        "INSERT OR IGNORE INTO source_memberships(sha256,source_id,src_path,is_owner,discovered_at) VALUES(?,?,?,?,?)",
        (content_hash, source.id, str(path), 1 if is_new else 0, now_iso()),
    )
    db.commit()
    return db.execute("SELECT * FROM docs WHERE sha256=?", (content_hash,)).fetchone(), is_new


@contextlib.contextmanager
def process_lock() -> Iterator[None]:
    lock_path = DATA_ROOT / ".extract.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError(f"another extraction cycle holds {lock_path}") from error
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        yield


def run_extract(args: argparse.Namespace, sources: Sequence[Source]) -> dict[str, int]:
    selected = [source for source in sources if source.enabled and (not args.source or source.id == args.source)]
    targeted_path = Path(args.document).expanduser().resolve() if args.document else None
    if targeted_path:
        owner = next((source for source in sources if targeted_path == source.root or source.root in targeted_path.parents), None)
        if not owner:
            raise ValueError(f"Target document is outside configured source roots: {targeted_path}")
        if args.source and owner.id != args.source:
            raise ValueError(f"Target document belongs to {owner.id}, not {args.source}")
        selected = [owner]
    if args.source and not selected:
        raise ValueError(f"Unknown or disabled source: {args.source}")
    deadline = time.monotonic() + args.time_limit
    ocr_budget = args.ocr_page_budget
    counts = {"discovered": 0, "extracted": 0, "partial": 0, "skipped": 0, "failed": 0, "unsupported": 0, "excluded": 0, "ocr_pages": 0}
    config_hash = hashlib.sha256(Path(args.config).read_bytes()).hexdigest()
    run_id = uuid.uuid4().hex
    DEFAULT_LOGS.mkdir(parents=True, exist_ok=True)
    manifest_path = DEFAULT_LOGS / f"run-{run_id}.json"
    with process_lock(), connect(Path(args.db)) as db:
        ensure_schema(db)
        db.execute("INSERT INTO runs(id,started_at,config_hash,extractor_version,status,manifest_path) VALUES(?,?,?,?,?,?)", (run_id, now_iso(), config_hash, VERSION, "running", str(manifest_path)))
        db.commit()
        seen_hashes: set[str] = set()
        for source in selected:
            source_files: Iterable[Path] = [targeted_path] if targeted_path else iter_files(source)
            for path in source_files:
                if STOP or time.monotonic() >= deadline:
                    break
                row, is_new = upsert_discovery(db, source, path)
                counts["discovered"] += int(is_new)
                if row["sha256"] in seen_hashes:
                    counts["skipped"] += 1
                    continue
                seen_hashes.add(row["sha256"])
                if row["state"] in {"extracted", "unsupported", "excluded"} and row["extraction_version"] == EXTRACTION_VERSION:
                    counts["skipped"] += 1
                    continue
                if row["state"] in {"partial", "ocr_pending"} and counts["ocr_pages"] >= args.ocr_page_budget:
                    counts["skipped"] += 1
                    continue
                if row["retry_count"] >= args.max_retries and row["state"] == "failed":
                    counts["skipped"] += 1
                    continue
                db.execute("UPDATE docs SET state='extracting',updated_at=? WHERE sha256=?", (now_iso(), row["sha256"]))
                db.commit()
                try:
                    extraction, used_pages = route_extract(path, db, row["sha256"], max(0, ocr_budget - counts["ocr_pages"]))
                    output_path = None
                    if extraction.text:
                        output_path = write_corpus_output(db, row, extraction)
                    extraction_hash = hashlib.sha256((EXTRACTION_VERSION + "\0" + extraction.text).encode()).hexdigest() if extraction.text else None
                    db.execute(
                        """UPDATE docs SET state=?,extract_chars=?,out_path=?,extractor=?,reason=?,error_code=?,
                           extraction_version=?,extraction_hash=?,page_count=?,completed_pages=?,updated_at=? WHERE sha256=?""",
                        (extraction.status, len(extraction.text), str(output_path) if output_path else None, extraction.extractor,
                         extraction.reason, extraction.error_code, EXTRACTION_VERSION, extraction_hash, extraction.page_count,
                         used_pages if extraction.status == "partial" else extraction.page_count or 0, now_iso(), row["sha256"]),
                    )
                    if extraction.status == "failed":
                        db.execute("UPDATE docs SET retry_count=retry_count+1 WHERE sha256=?", (row["sha256"],))
                    db.commit()
                    counts[extraction.status] = counts.get(extraction.status, 0) + 1
                    counts["ocr_pages"] += used_pages
                except Exception as error:  # per-file isolation is intentional
                    db.execute("UPDATE docs SET state='failed',error_code='extract_exception',reason=?,retry_count=retry_count+1,updated_at=? WHERE sha256=?", (str(error)[:1000], now_iso(), row["sha256"]))
                    db.commit()
                    counts["failed"] += 1
            if STOP or time.monotonic() >= deadline:
                break
        terminal = "partial" if STOP or time.monotonic() >= deadline or counts["failed"] else "success"
        manifest = {"schema_version": 1, "run_id": run_id, "started_at": db.execute("SELECT started_at FROM runs WHERE id=?", (run_id,)).fetchone()[0], "completed_at": now_iso(), "status": terminal, "extractor_version": VERSION, "extraction_version": EXTRACTION_VERSION, "config_hash": config_hash, "sources": [source.id for source in selected], "counts": counts}
        atomic_write(manifest_path, json_dump(manifest) + "\n")
        db.execute("UPDATE runs SET completed_at=?,status=?,counts_json=? WHERE id=?", (manifest["completed_at"], terminal, json_dump(counts), run_id))
        db.commit()
    return counts


def aliases_for(db: sqlite3.Connection, content_hash: str) -> list[str]:
    return [row[0] for row in db.execute("SELECT src_path FROM aliases WHERE sha256=? ORDER BY src_path", (content_hash,))]


def untracked_staging_paths(source_root: Path) -> set[Path]:
    """Return generated files exported by gbrain but not yet owned by the staging repo."""
    if not (source_root / ".git").exists():
        return set()
    result = subprocess.run(
        ["git", "-C", str(source_root), "ls-files", "--others", "--exclude-standard", "-z"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        return set()
    return {
        (source_root / item.decode("utf-8", errors="surrogateescape")).resolve()
        for item in result.stdout.split(b"\0") if item
    }


def materialize(args: argparse.Namespace, sources: Sequence[Source]) -> dict[str, int]:
    counts = {"documents": 0, "parts": 0, "missing": 0, "unchanged": 0}
    staging_root = Path(args.staging)
    untracked_by_source = {
        source.id: untracked_staging_paths(staging_root / source.id)
        for source in sources
    }
    with connect(Path(args.db)) as db:
        ensure_schema(db)
        query = "SELECT * FROM docs WHERE state IN ('extracted','partial') AND out_path IS NOT NULL"
        params: list[object] = []
        if args.source:
            query += " AND source_id=?"
            params.append(args.source)
        for row in db.execute(query, params).fetchall():
            source_id = row["source_id"] or classify_source(row["src_path"], sources)
            if not source_id:
                counts["missing"] += 1
                continue
            source_path = Path(row["out_path"])
            if not source_path.exists():
                counts["missing"] += 1
                continue
            try:
                body = validate_text(strip_duplicate_title(strip_legacy_frontmatter(source_path.read_text(encoding="utf-8", errors="replace"))), 20)
            except ValueError:
                counts["missing"] += 1
                continue
            parts = split_text(body, MAX_MARKDOWN_BYTES - 8_000)
            document_id = row["document_id"] or stable_document_id(row["sha256"])
            legacy_root = (DATA_ROOT / "corpus-md").resolve()
            try:
                legacy_relative = source_path.resolve().relative_to(legacy_root)
            except ValueError:
                legacy_relative = None
            source_staging = staging_root / source_id
            recovered = sorted(source_staging.joinpath(row["sha256"][:2]).glob(f"{row['sha256'][:16]}-*.md"))
            recovered_untracked = [path for path in recovered if path.resolve() in untracked_by_source.get(source_id, set())]
            if recovered_untracked:
                # gbrain may export a DB-only legacy page during the first source sync.
                # Reuse that slug so the stable page ID survives normalization.
                legacy_relative = recovered_untracked[0].relative_to(source_staging)
            elif legacy_relative is None and recovered:
                legacy_relative = recovered[0].relative_to(source_staging)
            aliases = aliases_for(db, row["sha256"])
            for index, part_text in enumerate(parts, 1):
                if legacy_relative is not None:
                    if index == 1:
                        relative_output = legacy_relative
                    else:
                        relative_output = legacy_relative.with_name(f"{legacy_relative.stem}-part-{index:03d}.md")
                else:
                    slug = f"{document_id}-part-{index:03d}" if len(parts) > 1 else f"{document_id}-{slugify(Path(row['src_path']).stem)}"
                    relative_output = Path(row["sha256"][:2]) / f"{slug}.md"
                output = Path(args.staging) / source_id / relative_output
                content = render_markdown(row, source_id, part_text, aliases, index, len(parts))
                output_hash = hashlib.sha256(content.encode()).hexdigest()
                if output.exists() and hashlib.sha256(output.read_bytes()).hexdigest() == output_hash:
                    counts["unchanged"] += 1
                elif not args.dry_run:
                    atomic_write(output, content)
                if not args.dry_run:
                    db.execute(
                        "INSERT OR REPLACE INTO outputs(sha256,source_id,part,parts_total,out_path,output_hash,bytes,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (row["sha256"], source_id, index, len(parts), str(output), output_hash, len(content.encode()), now_iso()),
                    )
                counts["parts"] += 1
            counts["documents"] += 1
        if not args.dry_run:
            db.commit()
    return counts


def reconcile(args: argparse.Namespace) -> dict[str, object]:
    report: dict[str, object] = {
        "manifest_missing": [], "orphans": [], "invalid_output": [], "oversized": [],
        "partial": 0, "recoverable_budget_failures": 0, "unstructured_failures": 0,
    }
    corpus_roots = [DATA_ROOT / "corpus-md", DEFAULT_CORPUS]
    with connect(Path(args.db)) as db:
        ensure_schema(db)
        extractors_by_output = {
            str(Path(row[0]).resolve()): row[1] or ""
            for row in db.execute("SELECT out_path,extractor FROM docs WHERE out_path IS NOT NULL")
        }
        report["recoverable_budget_failures"] = db.execute(
            "SELECT COUNT(*) FROM docs WHERE state='failed' AND error_code='extract_exception' AND reason='insufficient_text'"
        ).fetchone()[0]
        report["unstructured_failures"] = db.execute(
            "SELECT COUNT(*) FROM docs WHERE state='failed' AND error_code='extract_exception' AND reason<>'insufficient_text'"
        ).fetchone()[0]
        referenced = {str(Path(row[0]).resolve()) for row in db.execute("SELECT out_path FROM docs WHERE out_path IS NOT NULL")}
        for path_string in referenced:
            if not Path(path_string).exists():
                report["manifest_missing"].append(path_string)
        for root in corpus_roots:
            if not root.exists():
                continue
            for path in root.rglob("*.md"):
                resolved = str(path.resolve())
                if resolved not in referenced:
                    report["orphans"].append(resolved)
                text = path.read_text(encoding="utf-8", errors="replace")
                try:
                    body = validate_text(strip_legacy_frontmatter(text), 20)
                    if extractors_by_output.get(resolved, "").startswith("pdftotext") and has_pdf_encoding_garbage(body):
                        raise ValueError("pdf_font_encoding_garbage")
                except ValueError as error:
                    report["invalid_output"].append({"path": resolved, "error_code": str(error)})
                if path.stat().st_size > MAX_MARKDOWN_BYTES:
                    report["oversized"].append(resolved)
        report["partial"] = db.execute("SELECT COUNT(*) FROM docs WHERE state='partial'").fetchone()[0]
        if args.fix_safe and not args.dry_run:
            quarantine = DEFAULT_QUARANTINE / datetime.now().strftime("%Y%m%d-%H%M%S")
            def move_to_quarantine(path: Path) -> None:
                if not path.exists():
                    return
                target = quarantine / path.parent.name / path.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(target))
            for value in report["orphans"]:
                move_to_quarantine(Path(value))
            for item in report["invalid_output"]:
                move_to_quarantine(Path(item["path"]))
                db.execute(
                    """UPDATE docs SET state=CASE WHEN ?='pdf_font_encoding_garbage' THEN 'ocr_pending' ELSE 'discovered' END,
                       out_path=NULL,error_code=?,reason='reconcile requested regeneration' WHERE out_path=?""",
                    (item["error_code"], item["error_code"], item["path"]),
                )
            db.execute(
                """UPDATE docs SET state=CASE WHEN extractor LIKE '%TRUNCATED@20%' THEN 'partial' ELSE 'ocr_pending' END,
                   error_code=CASE WHEN extractor LIKE '%TRUNCATED@20%' THEN 'ocr_incomplete' ELSE 'insufficient_text' END,
                   reason='recovered after exhausted OCR budget',
                   retry_count=CASE WHEN retry_count>0 THEN retry_count-1 ELSE 0 END
                   WHERE state='failed' AND error_code='extract_exception' AND reason='insufficient_text'"""
            )
            db.execute(
                "UPDATE docs SET error_code='invalid_zip' WHERE state='failed' AND error_code='extract_exception' AND reason LIKE 'File is not a zip file%'"
            )
            db.execute(
                "UPDATE docs SET state='excluded',error_code='unsupported_zip_compression' WHERE state='failed' AND error_code='extract_exception' AND reason LIKE 'File <ZipInfo%compress_type=%'"
            )
            db.execute(
                "UPDATE docs SET state='excluded',error_code='binary_garbage' WHERE state='failed' AND error_code='extract_exception' AND reason='binary_garbage'"
            )
            db.commit()
    return report


def status(db_path: Path, source_id: str | None) -> dict[str, object]:
    with connect(db_path) as db:
        ensure_schema(db)
        where = " WHERE source_id=?" if source_id else ""
        params = (source_id,) if source_id else ()
        states = {row[0]: row[1] for row in db.execute(f"SELECT state,COUNT(*) FROM docs{where} GROUP BY state ORDER BY state", params)}
        last_run = db.execute("SELECT id,started_at,completed_at,status,counts_json FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
        return {
            "schema_version": SCHEMA_VERSION, "extractor_version": VERSION, "extraction_version": EXTRACTION_VERSION,
            "source": source_id, "states": states,
            "memberships": db.execute(f"SELECT COUNT(*) FROM source_memberships{where}", params).fetchone()[0],
            "outputs": db.execute(f"SELECT COUNT(*) FROM outputs{where}", params).fetchone()[0],
            "last_run": dict(last_run) if last_run else None,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gbrain-extract", description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--source")
    run.add_argument("--document", help="process one configured source file")
    run.add_argument("--time-limit", type=int, default=DEFAULT_TIME_LIMIT)
    run.add_argument("--ocr-page-budget", type=int, default=DEFAULT_OCR_PAGE_BUDGET)
    run.add_argument("--max-retries", type=int, default=3)
    stat = sub.add_parser("status")
    stat.add_argument("--source")
    stat.add_argument("--json", action="store_true")
    rec = sub.add_parser("reconcile")
    rec.add_argument("--fix-safe", action="store_true")
    rec.add_argument("--dry-run", action="store_true")
    mat = sub.add_parser("materialize")
    mat.add_argument("--source")
    mat.add_argument("--staging", default=str(DEFAULT_STAGING))
    mat.add_argument("--dry-run", action="store_true")
    mig = sub.add_parser("migrate-state")
    mig.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    args = build_parser().parse_args(argv)
    sources = load_sources(Path(args.config))
    try:
        if args.command == "run":
            result = run_extract(args, sources)
        elif args.command == "status":
            result = status(Path(args.db), args.source)
        elif args.command == "reconcile":
            result = reconcile(args)
        elif args.command == "materialize":
            result = materialize(args, sources)
        elif args.command == "migrate-state":
            result = migrate_state(Path(args.db), sources, args.dry_run)
        else:
            raise AssertionError(args.command)
        if args.command == "status" and not args.json:
            for key, value in result.items():
                print(f"{key}: {value}")
        else:
            print(json_dump(result))
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        print(f"gbrain-extract: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
