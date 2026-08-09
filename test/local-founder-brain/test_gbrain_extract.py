from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
import tempfile
import types
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
notebooklm = load_module("gbrain_notebooklm", ROOT / "scripts/local-founder-brain/gbrain_notebooklm.py")


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

    def test_document_source_discovers_direct_text_without_relaxing_exclusions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "notes.md").write_text("# Notes\n", encoding="utf-8")
            (root / "evidence.jsonl").write_text('{"event":"decision"}\n', encoding="utf-8")
            (root / "settings.yaml").write_text("enabled: true\n", encoding="utf-8")
            (root / "blob.bin").write_bytes(b"\0not direct text")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "ignored.js").write_text("ignored", encoding="utf-8")
            source = extract.Source("gdrive-workspaces", root, "document")
            discovered = {path.name for path in extract.iter_files(source)}
            self.assertTrue({"notes.md", "evidence.jsonl", "settings.yaml"}.issubset(discovered))
            self.assertNotIn("blob.bin", discovered)
            self.assertNotIn("ignored.js", discovered)

    def test_ocr_budget_exhaustion_preserves_partial_coverage(self):
        with tempfile.TemporaryDirectory() as temp:
            db = sqlite3.connect(":memory:")
            db.row_factory = sqlite3.Row
            extract.ensure_schema(db)
            content_hash = "a" * 64
            db.execute(
                "INSERT INTO ocr_ranges(sha256,first_page,last_page,output_text,completed_at) VALUES(?,?,?,?,?)",
                (content_hash, 1, 2, "## Page 1\n\nEnough extracted evidence for the test.", extract.now_iso()),
            )
            pdf = Path(temp) / "fixture.pdf"
            pdf.write_bytes(b"fixture")
            original = extract.PDFTOPPM, extract.TESSERACT, extract.pdf_page_count
            try:
                extract.PDFTOPPM = str(pdf)
                extract.TESSERACT = str(pdf)
                extract.pdf_page_count = lambda _path: 5
                result, used = extract.extract_ocr_window(pdf, db, content_hash, 0)
            finally:
                extract.PDFTOPPM, extract.TESSERACT, extract.pdf_page_count = original
            self.assertEqual(used, 0)
            self.assertEqual(result.status, "partial")
            self.assertEqual(result.page_range, "1-2")
            self.assertEqual(result.error_code, "ocr_incomplete")
            db.close()

    def test_partial_ocr_checkpoint_is_cumulative_and_materialized_exactly(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_root = root / "drive"
            source_root.mkdir()
            (source_root / "scan.pdf").write_text("placeholder", encoding="utf-8")
            config = root / "sources.json"
            config.write_text(json.dumps({"schema_version": 1, "sources": [{
                "id": "gdrive-workspaces", "root": str(source_root), "pipeline": "document", "enabled": True,
            }]}), encoding="utf-8")
            db_path = root / "state.sqlite3"
            original = extract.DATA_ROOT, extract.DEFAULT_LOGS, extract.DEFAULT_CORPUS, extract.route_extract
            try:
                extract.DATA_ROOT = root
                extract.DEFAULT_LOGS = root / "logs"
                extract.DEFAULT_CORPUS = root / "corpus"

                def partial_window(_path, db, content_hash, _budget):
                    done = extract.completed_ocr_pages(db, content_hash)
                    first = max(done, default=0) + 1
                    last = first + 19
                    db.execute(
                        "INSERT INTO ocr_ranges(sha256,first_page,last_page,output_text,completed_at) VALUES(?,?,?,?,?)",
                        (content_hash, first, last, f"## Page {first}\n\nOCR evidence long enough for validation.", extract.now_iso()),
                    )
                    db.commit()
                    completed = extract.completed_ocr_pages(db, content_hash)
                    return extract.Extraction(
                        "OCR evidence long enough for validation.\n", "ocr:tesseract-vie+eng", "partial",
                        "ocr_incomplete", page_count=60, page_range=extract.format_page_ranges(completed),
                    ), 20

                extract.route_extract = partial_window
                args = types.SimpleNamespace(
                    source="gdrive-workspaces", document=None, time_limit=60, ocr_page_budget=100,
                    max_retries=3, config=str(config), db=str(db_path),
                )
                sources = extract.load_sources(config)
                extract.run_extract(args, sources)
                extract.run_extract(args, sources)
                manifests = list((root / "logs").glob("run-*.json"))
                latest_manifest = json.loads(max(manifests, key=lambda path: path.stat().st_mtime).read_text(encoding="utf-8"))
            finally:
                extract.DATA_ROOT, extract.DEFAULT_LOGS, extract.DEFAULT_CORPUS, extract.route_extract = original
            with extract.connect(db_path) as db:
                row = db.execute("SELECT * FROM docs").fetchone()
                self.assertEqual(row["state"], "partial")
                self.assertEqual(row["completed_pages"], 40)
                self.assertEqual(row["completed_page_ranges"], "1-40")
                rendered = extract.render_markdown(row, "gdrive-workspaces", "Evidence", [], 1, 1)
            self.assertIn('page_range: "1-40"', rendered)
            self.assertIn("outputs", latest_manifest)
            self.assertIn("failures", latest_manifest)
            self.assertTrue(latest_manifest["outputs"][0]["output_hash"])

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
            self.assertEqual(len(list(Path(temp).glob(f"state.sqlite3.pre-v{extract.SCHEMA_VERSION}-*.bak"))), 1)
            current = extract.migrate_state(db_path, sources, False)
            self.assertEqual(current["already_current"], 1)
            self.assertEqual(current["backup_created"], 0)

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
            db.close()

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
        self.assertIn("embedding IS NOT NULL", sql)
        self.assertIn("embedding IS NULL", sql)
        self.assertIn("raw_aliases", sql)

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
        self.assertIn("embedded_chunks", source)
        self.assertIn("missing_embeddings", source)

    def test_provenance_owner_is_deterministic_and_unknown_is_quarantined(self):
        drive = "/Users/example/Library/CloudStorage/GoogleDrive-user/My Drive/1 Workspaces/note.md"
        faos = "/Users/example/Projects/FAOS/project/readme.md"
        self.assertEqual(reassign.deterministic_owner(reassign.provenance_targets(drive)), "gdrive-workspaces")
        self.assertEqual(reassign.deterministic_owner(reassign.provenance_targets(faos)), "faos-projects")
        self.assertEqual(
            reassign.deterministic_owner(reassign.provenance_targets(faos, [drive])), "gdrive-workspaces",
        )
        self.assertIsNone(reassign.deterministic_owner(reassign.provenance_targets("/tmp/unclassified.md")))

    def test_quarantine_report_is_atomic_and_records_no_guess(self):
        with tempfile.TemporaryDirectory() as temp:
            report_path = Path(temp) / "logs" / "quarantine.json"
            rows = [{"page_id": 7, "slug": "unknown", "current_source": "default"}]
            reassign.write_quarantine_report(report_path, rows)
            self.assertEqual(json.loads(report_path.read_text(encoding="utf-8"))["pages"], rows)
            self.assertFalse(list(report_path.parent.glob("*.tmp")))

    def test_owner_reconciliation_quarantines_hash_only_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "state.sqlite3"
            with sqlite3.connect(state_path) as state:
                state.execute("CREATE TABLE docs (sha256 TEXT, source_id TEXT)")
                state.execute("INSERT INTO docs VALUES (?,?)", ("a" * 64, "gdrive-workspaces"))
            original = reassign.run_psql_text
            try:
                reassign.run_psql_text = lambda _sql, _url: (
                    f"7\tdefault\tunknown\t{'a' * 64}\t/tmp/no-provenance.md\t[]\n"
                )
                report = reassign.owner_reconciliation("postgresql://example.test/db", state_path, False)
            finally:
                reassign.run_psql_text = original
            self.assertEqual(report["candidate_pages"], 0)
            self.assertEqual(report["quarantined_pages"], 1)
            self.assertEqual(report["quarantine"][0]["page_id"], 7)
            self.assertEqual(report["quarantine"][0]["reason"], "missing_provenance")

    def test_owner_reconciliation_quarantines_provenance_owner_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "state.sqlite3"
            with sqlite3.connect(state_path) as state:
                state.execute("CREATE TABLE docs (sha256 TEXT, source_id TEXT)")
                state.execute("INSERT INTO docs VALUES (?,?)", ("b" * 64, "gdrive-workspaces"))
            original = reassign.run_psql_text
            try:
                reassign.run_psql_text = lambda _sql, _url: (
                    f"8\tdefault\tfaos-only\t{'b' * 64}\t/Users/example/Projects/FAOS/readme.md\t[]\n"
                )
                report = reassign.owner_reconciliation("postgresql://example.test/db", state_path, False)
            finally:
                reassign.run_psql_text = original
            self.assertEqual(report["candidate_pages"], 0)
            self.assertEqual(report["quarantined_pages"], 1)
            self.assertEqual(report["quarantine"][0]["declared_owner"], "faos-projects")
            self.assertEqual(report["quarantine"][0]["reason"], "provenance_owner_mismatch")

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
        supervisor = (ROOT / "scripts/local-founder-brain/gbrain_nightly.py").read_text()
        self.assertIn("http://127.0.0.1:3131/health", source)
        self.assertIn("http://127.0.0.1:11434/api/tags", source)
        self.assertIn("pg_isready -h 127.0.0.1 -p 5432", source)
        self.assertNotIn("gbrain doctor", source)
        self.assertIn("gbrain-extract-nightly-cycle --wall-clock 14400", source)
        self.assertIn("os.killpg", supervisor)
        self.assertIn("start_new_session=True", supervisor)
        self.assertIn('"--ocr-page-budget", "2000"', supervisor)

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


class NotebookLMAdapterContractTest(unittest.TestCase):
    def config_file(self, root: Path, *, extra: dict | None = None) -> Path:
        config = {
            "schema_version": 1,
            "accounts": [
                {"id": "notebooklm-personal", "profile": "personal", "enabled": True},
                {"id": "notebooklm-faosx", "profile": "faosx", "enabled": True},
            ],
        }
        if extra:
            config.update(extra)
        path = root / "notebooklm.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def test_requires_both_scoped_accounts_and_rejects_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = self.config_file(root)
            loaded = notebooklm.load_config(config)
            self.assertEqual(set(loaded["accounts_by_id"]), {
                "notebooklm-personal", "notebooklm-faosx",
            })
            config.write_text(json.dumps({
                "schema_version": 1,
                "accounts": [{"id": "notebooklm-personal", "profile": "personal"}],
            }), encoding="utf-8")
            with self.assertRaisesRegex(notebooklm.AdapterError, "requires_personal"):
                notebooklm.load_config(config)
            config = self.config_file(root, extra={"token": "must-not-be-here"})
            with self.assertRaisesRegex(notebooklm.AdapterError, "credential_key"):
                notebooklm.load_config(config)

    def test_query_passes_exactly_one_configured_profile_and_returns_reference(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = notebooklm.load_config(self.config_file(root))
            account = notebooklm.selected_account(config, "notebooklm-personal")
            captured = []
            original = notebooklm.run_skill
            try:
                def fake_run_skill(selected, *args, **_kwargs):
                    captured.append((selected, args))
                    return types.SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps({
                            "status": "ok", "answer": "Grounded answer", "profile": "personal",
                            "notebook_id": "n-1", "notebook_name": "Personal notebook",
                            "notebook_url": "https://notebook.google.com/notebook/n-1",
                        }), stderr="",
                    )
                notebooklm.run_skill = fake_run_skill
                with tempfile.TemporaryFile(mode="w+") as output:
                    original_stdout = sys.stdout
                    try:
                        sys.stdout = output
                        code = notebooklm.query(config, "notebooklm-personal", "Question", "n-1", False)
                    finally:
                        sys.stdout = original_stdout
                    output.seek(0)
                    result = json.loads(output.read())
            finally:
                notebooklm.run_skill = original
            self.assertEqual(code, 0)
            self.assertEqual(captured[0][0], account)
            self.assertEqual(captured[0][1][0], "bridge.py")
            self.assertEqual(result["account_scope"], "notebooklm-personal")
            self.assertFalse(result["cross_account_mixing"])
            self.assertEqual(result["references"][0]["profile"], "personal")

    def test_profile_mismatch_is_unavailable_and_cross_scope_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            config = notebooklm.load_config(self.config_file(Path(temp)))
            with self.assertRaisesRegex(notebooklm.AdapterError, "one_of"):
                notebooklm.selected_account(config, "notebooklm-personal,notebooklm-faosx")
            original = notebooklm.run_skill
            try:
                notebooklm.run_skill = lambda *_args, **_kwargs: types.SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"status": "ok", "profile": "faosx", "answer": "wrong"}), stderr="",
                )
                with tempfile.TemporaryFile(mode="w+") as output:
                    original_stdout = sys.stdout
                    try:
                        sys.stdout = output
                        code = notebooklm.query(config, "notebooklm-personal", "Question", "n-1", False)
                    finally:
                        sys.stdout = original_stdout
                    output.seek(0)
                    result = json.loads(output.read())
            finally:
                notebooklm.run_skill = original
            self.assertEqual(code, 4)
            self.assertEqual(result["error"], "profile_scope_mismatch")

    def test_capture_is_atomic_review_inbox_and_never_canonical(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = self.config_file(root)
            raw = json.loads(config_path.read_text())
            raw["accounts"][0]["inbox_root"] = str(root / "inbox")
            config_path.write_text(json.dumps(raw), encoding="utf-8")
            config = notebooklm.load_config(config_path)
            with tempfile.TemporaryFile(mode="w+") as output:
                original_stdout = sys.stdout
                try:
                    sys.stdout = output
                    code = notebooklm.capture(config, "notebooklm-personal", "n-1", "Capture", "Answer", "Question")
                finally:
                    sys.stdout = original_stdout
                output.seek(0)
                result = json.loads(output.read())
            saved = json.loads(Path(result["path"]).read_text())
            self.assertEqual(code, 0)
            self.assertEqual(saved["status"], "review")
            self.assertTrue(saved["requires_founder_approval"])
            self.assertFalse(saved["ingest_to_canonical"])
            self.assertEqual(saved["source_id"], "notebooklm-personal")
            self.assertFalse(list((root / "inbox").rglob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
