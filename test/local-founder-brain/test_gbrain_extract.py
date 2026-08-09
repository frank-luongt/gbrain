from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


extract = load_module("gbrain_extract", ROOT / "scripts/local-founder-brain/gbrain_extract.py")
reassign = load_module("gbrain_source_reassign", ROOT / "scripts/local-founder-brain/gbrain_source_reassign.py")
health = load_module("gbrain_extract_health", ROOT / "scripts/local-founder-brain/gbrain_extract_health.py")
syncer = load_module("gbrain_sync_staging", ROOT / "scripts/local-founder-brain/gbrain_sync_staging.py")
heartbeat = load_module("gbrain_heartbeat_wrapper", ROOT / "scripts/local-founder-brain/gbrain_heartbeat_wrapper.py")


class ExtractionContractTest(unittest.TestCase):
    def test_rejects_binary_and_embedded_payload(self):
        with self.assertRaisesRegex(ValueError, "binary_garbage"):
            extract.validate_text("hello" + "\x01" * 100)
        with self.assertRaisesRegex(ValueError, "embedded_payload"):
            extract.validate_text("data:image/png;base64," + "A" * 2000)

    def test_printable_pdf_font_garbage_routes_to_ocr(self):
        garbage = ('2+#)0;$#()$#06-7-(8#(2#5$,-(0($#0",#1/0&(-&$#%$74E' * 40)
        self.assertTrue(extract.has_pdf_encoding_garbage(garbage))
        self.assertFalse(extract.has_pdf_encoding_garbage("This is normal Vietnamese and English evidence. " * 80))

    def test_split_is_stable_and_bounded(self):
        text = "".join(f"## Section {index}\n\n" + "x" * 600 + "\n" for index in range(20))
        first = extract.split_text(text, 1500)
        second = extract.split_text(text, 1500)
        self.assertEqual(first, second)
        self.assertTrue(all(len(part.encode()) <= 1500 for part in first))
        self.assertEqual("".join(part.rstrip() + "\n" for part in first).replace("\n", ""), text.replace("\n", ""))

    def test_source_precedence_and_project_classification(self):
        sources = [
            extract.Source("faos-projects", Path("/tmp/Projects/FAOS"), "document-and-code"),
            extract.Source("gdrive-workspaces", Path("/tmp/Drive/1 Workspaces"), "document"),
        ]
        self.assertEqual(extract.classify_source("/tmp/Projects/FAOS/repo/a.md", sources), "faos-projects")
        self.assertEqual(extract.classify_source("/tmp/Drive/1 Workspaces/a.pdf", sources), "gdrive-workspaces")
        self.assertIsNone(extract.classify_source("/tmp/elsewhere/a.pdf", sources))

    def test_projection_and_generated_paths_are_excluded(self):
        root = Path("/tmp/Projects/FAOS/repo")
        self.assertTrue(extract.should_skip(root / "wiki/frankbrain/generated.md", root))
        self.assertTrue(extract.should_skip(root / ".claude/worktrees/x/file.md", root))
        self.assertTrue(extract.should_skip(root / "node_modules/pkg/index.js", root))
        self.assertFalse(extract.should_skip(root / "docs/research.jsonl", root))

    def test_state_migration_is_dry_run_first_and_backed_up(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "state.sqlite3"
            db = sqlite3.connect(db_path)
            db.executescript(
                """
                CREATE TABLE docs (
                  sha256 TEXT PRIMARY KEY, src_path TEXT NOT NULL, bytes INTEGER NOT NULL,
                  folder TEXT NOT NULL, state TEXT NOT NULL, extract_chars INTEGER DEFAULT 0,
                  out_path TEXT, extractor TEXT, reason TEXT, ext TEXT, magic TEXT, mtime REAL, updated_at TEXT
                );
                CREATE TABLE aliases (sha256 TEXT NOT NULL, src_path TEXT NOT NULL, PRIMARY KEY(sha256,src_path));
                CREATE TABLE path_cache (src_path TEXT PRIMARY KEY,bytes INTEGER,mtime REAL,sha256 TEXT);
                INSERT INTO docs VALUES('abc','/tmp/Projects/FAOS/a.pdf',1,'/tmp','extracted',10,'/tmp/a.md','ocr:tesseract-vie+eng:20p:TRUNCATED@20',NULL,'pdf','pdf',0,'now');
                INSERT INTO aliases VALUES('abc','/tmp/Projects/FAOS/a.pdf');
                """
            )
            db.commit()
            db.close()
            sources = [extract.Source("faos-projects", Path("/tmp/Projects/FAOS"), "document-and-code")]
            preview = extract.migrate_state(db_path, sources, True)
            self.assertEqual(preview["truncated_to_partial"], 1)
            with sqlite3.connect(db_path) as check:
                self.assertNotIn("source_id", {row[1] for row in check.execute("PRAGMA table_info(docs)")})
            extract.migrate_state(db_path, sources, False)
            with sqlite3.connect(db_path) as check:
                row = check.execute("SELECT state,source_id,document_id FROM docs").fetchone()
                self.assertEqual(row[0:2], ("partial", "faos-projects"))
                self.assertTrue(row[2].startswith("doc-"))
            self.assertEqual(len(list(Path(temp).glob("state.sqlite3.pre-v2-*.bak"))), 1)

    def test_frontmatter_is_governed(self):
        with tempfile.TemporaryDirectory() as temp:
            db = sqlite3.connect(":memory:")
            db.row_factory = sqlite3.Row
            extract.ensure_schema(db)
            db.execute(
                "INSERT INTO docs(sha256,src_path,bytes,folder,state,document_id,source_id,extraction_version,extractor) VALUES(?,?,?,?,?,?,?,?,?)",
                ("a" * 64, str(Path(temp) / "memo.pdf"), 1, temp, "extracted", "doc-test", "faos-projects", "v1", "pdftotext"),
            )
            row = db.execute("SELECT * FROM docs").fetchone()
            rendered = extract.render_markdown(row, "faos-projects", "Evidence", [], 1, 1)
            self.assertIn('sensitivity: "local_only"', rendered)
            self.assertIn("generated: true", rendered)
            self.assertIn("ingest_to_canonical: false", rendered)
            self.assertLess(len(rendered.encode()), extract.MAX_MARKDOWN_BYTES)

    def test_reconcile_contract_reports_all_invalid_text_classes(self):
        source = (ROOT / "scripts/local-founder-brain/gbrain_extract.py").read_text()
        self.assertIn('"invalid_output"', source)
        self.assertIn("validate_text(strip_legacy_frontmatter(text), 20)", source)
        self.assertIn("pdf_font_encoding_garbage", source)

    def test_budget_exhaustion_skips_pending_ocr_before_state_change(self):
        source = (ROOT / "scripts/local-founder-brain/gbrain_extract.py").read_text()
        budget_guard = 'row["state"] in {"partial", "ocr_pending"} and counts["ocr_pages"] >= args.ocr_page_budget'
        self.assertIn(budget_guard, source)
        self.assertLess(source.index(budget_guard), source.index("state='extracting'"))

    def test_ooxml_failure_is_structured(self):
        with tempfile.TemporaryDirectory() as temp:
            bad = Path(temp) / "bad.docx"
            bad.write_text("not a zip")
            result = extract.extract_ooxml(bad, "docx")
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.error_code, "invalid_zip")

    def test_reconcile_repairs_exhausted_ocr_budget_failures(self):
        source = (ROOT / "scripts/local-founder-brain/gbrain_extract.py").read_text()
        self.assertIn("recovered after exhausted OCR budget", source)
        self.assertIn("extractor LIKE '%TRUNCATED@20%' THEN 'partial' ELSE 'ocr_pending'", source)

    def test_materialization_preserves_legacy_relative_slug(self):
        source = (ROOT / "scripts/local-founder-brain/gbrain_extract.py").read_text()
        self.assertIn("legacy_relative = source_path.resolve().relative_to(legacy_root)", source)
        self.assertIn("relative_output = legacy_relative", source)

    def test_duplicate_legacy_title_is_removed(self):
        self.assertEqual(extract.strip_duplicate_title("# Memo.pdf\n\nEvidence\n"), "Evidence\n")

    def test_targeted_document_is_confined_to_configured_roots(self):
        source = (ROOT / "scripts/local-founder-brain/gbrain_extract.py").read_text()
        self.assertIn("Target document is outside configured source roots", source)
        self.assertIn('run.add_argument("--document"', source)

    def test_recovered_legacy_slug_wins_over_new_document_slug(self):
        source = (ROOT / "scripts/local-founder-brain/gbrain_extract.py").read_text()
        self.assertIn("recovered_untracked[0].relative_to", source)
        self.assertIn('["git", "-C", str(source_root), "ls-files"', source)


class ReassignmentContractTest(unittest.TestCase):
    def test_database_url_is_converted_without_leaking_uri(self):
        env = reassign.libpq_env("postgresql://brain-user:secret@example.test:5433/brain?sslmode=require")
        self.assertEqual(env["PGHOST"], "example.test")
        self.assertEqual(env["PGPORT"], "5433")
        self.assertEqual(env["PGPASSWORD"], "secret")
        self.assertNotIn("DATABASE_URL", env)

    def test_apply_sql_is_transactional_and_count_guarded(self):
        sql = reassign.apply_sql()
        self.assertIn("BEGIN;", sql)
        self.assertIn("COMMIT;", sql)
        self.assertIn("count reconciliation failed", sql)
        self.assertIn("slug conflicts", sql)
        self.assertIn("jsonb_set", sql)

    def test_source_preparation_refuses_repoint_with_pages(self):
        sql = reassign.prepare_apply_sql()
        self.assertIn("refusing to repoint a target source that already owns pages", sql)
        self.assertIn("gdrive-workspaces", sql)
        self.assertIn("faos-projects", sql)

    def test_owner_reconciliation_uses_content_hash_and_count_guard(self):
        source = (ROOT / "scripts/local-founder-brain/gbrain_source_reassign.py").read_text()
        self.assertIn("frontmatter->>'source_sha256'", source)
        self.assertIn("owner reconciliation count guard failed", source)
        self.assertIn("SELECT sha256,source_id FROM docs", source)

    def test_preview_has_no_mutation(self):
        sql = reassign.preview_sql().upper()
        for keyword in ("UPDATE ", "DELETE ", "INSERT ", "COMMIT"):
            self.assertNotIn(keyword, sql)


class HealthContractTest(unittest.TestCase):
    def test_missing_status_is_fail_closed(self):
        result = health.probe(Path("/definitely/missing/status.json"), 24)
        self.assertFalse(result["ready"])
        self.assertTrue(result["stale"])

    def test_nightly_readiness_is_dependency_aware_and_four_hour_bounded(self):
        source = (ROOT / "scripts/local-founder-brain/run-nightly.sh").read_text()
        self.assertIn("http://127.0.0.1:3131/health", source)
        self.assertIn("http://127.0.0.1:11434/api/tags", source)
        self.assertIn("pg_isready -h 127.0.0.1 -p 5432", source)
        self.assertIn("doctor --json --fast", source)
        self.assertIn("--time-limit 12600", source)
        self.assertIn("--ocr-page-budget 2000", source)

    def test_heartbeat_wrapper_fails_closed_on_extraction_freshness(self):
        source = (ROOT / "scripts/local-founder-brain/gbrain_heartbeat_wrapper.py").read_text()
        self.assertIn('"founder_extraction"', source)
        self.assertIn("no successful founder extraction cycle within 24 hours", source)
        self.assertIn("os.replace", source)


class SyncContractTest(unittest.TestCase):
    def test_sync_is_source_scoped_and_bounded(self):
        source = (ROOT / "scripts/local-founder-brain/gbrain_sync_staging.py").read_text()
        self.assertIn("start_new_session=True", source)
        self.assertIn("os.killpg", source)
        self.assertIn('"--source", source_id', source)
        self.assertIn('Path.home() / ".bun" / "bin" / "gbrain"', source)
        self.assertNotIn('"--all"', source)

    def test_staging_commit_refuses_any_remote(self):
        source = (ROOT / "scripts/local-founder-brain/commit-staging.sh").read_text()
        self.assertIn("local-only staging repo has a remote", source)
        self.assertIn("git -C \"$source_root\" add -A", source)
        self.assertIn("refusing shared staging Git root", source)


if __name__ == "__main__":
    unittest.main()
