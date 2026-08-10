# Local Founder Brain Extraction

This fork-only utility turns the founder's local document corpus into governed Markdown evidence
for gbrain. It is offline and deterministic: no document content is sent to a model during
ingestion. Human-approved FrankBrain publishing remains a separate workflow.

## Sources and boundaries

| Source | Input | gbrain staging |
|---|---|---|
| `gdrive-workspaces` | Google Drive `My Drive/1 Workspaces` | `~/gbrain-sources/staging/gdrive-workspaces` |
| `faos-projects` | `~/Projects/FAOS` | `~/gbrain-sources/staging/faos-projects` |

`default` retains Apple Notes and unrelated legacy pages. `frankbrain` accepts only approved
writeback. Personal and FAOSX NotebookLM profiles remain read-through/capture integrations; this
filesystem pipeline does not scrape them.

## NotebookLM read-through and capture

`gbrain-notebooklm` is a small local adapter over the existing NotebookLM skill. Its configuration
is `~/.gbrain/notebooklm.json`, created from `notebooklm.example.json` by the installer. It contains
only the two public account scopes and their skill profile names; browser sessions, cookies, and
other credentials stay solely inside the NotebookLM skill's profile directories.

```bash
gbrain-notebooklm status --account notebooklm-personal
gbrain-notebooklm query --account notebooklm-personal --notebook-id NOTEBOOK_ID --question "What decision is documented?"
gbrain-notebooklm capture --account notebooklm-faosx --notebook-id NOTEBOOK_ID \
  --title "Research capture" --answer "Explicitly captured answer" --question "Question asked"
```

Every operation requires exactly one of `notebooklm-personal` or `notebooklm-faosx`; an adapter
process invokes the skill wrapper with only that configured profile. It never authenticates an
account, exports a browser session, or combines account contexts. Query results retain the source,
profile, notebook identity, private sensitivity, and a structured reference. Explicit captures are
written atomically to `~/gbrain-sources/inbox/notebooklm/<source-id>/` with `status: review` and
`requires_founder_approval: true`; they never enter `frankbrain` automatically. NotebookLM content
is read-through only and must not be bulk-mirrored or filesystem-scraped.

The walker excludes build products, caches, model artifacts, Claude worktrees/audio, staging, and
`wiki/frankbrain/`. Generated pages are `local_only`, `generated: true`, and
`ingest_to_canonical: false`.

## Install and migrate

Run these commands from the designated gbrain fork:

```bash
scripts/local-founder-brain/install-local.sh
gbrain-extract migrate-state --dry-run
gbrain-extract migrate-state
gbrain-extract reconcile --dry-run
gbrain-extract materialize --dry-run
```

`migrate-state` creates a SQLite backup before changing the existing schema. The first real
materialization writes source-specific staging without altering the legacy `corpus-md` tree.

Register or safely repoint source paths after the dry runs are clean:

```bash
gbrain-source-reassign --prepare-sources
gbrain-source-reassign --prepare-sources --apply
```

The apply path refuses to repoint an existing target source that already owns active pages.

Create and restore-test a PostgreSQL backup before source reassignment. Then:

```bash
gbrain-source-reassign                 # mandatory dry-run
gbrain-source-reassign --apply         # refuses missing sources or slug conflicts
gbrain-source-reassign --reconcile-owner
gbrain-source-reassign --reconcile-owner --apply
```

The transaction changes page source ownership and frontmatter only. Page IDs stay stable, so
versions, chunks, embeddings, tags, timeline entries, and links remain attached. It rolls back if
page, chunk, embedded/missing-embedding, version, or link counts change.
The owner-reconciliation pass then applies the extractor's Google-Drive-first exact-hash dedup
decision, rather than guessing ownership from whichever alias happened to be imported first.

## Operations

```bash
gbrain-extract run --source gdrive-workspaces --time-limit 3600
gbrain-extract run --source faos-projects --time-limit 3600
gbrain-extract run --document /path/to/one/document.pdf --ocr-page-budget 20
gbrain-extract status --json
gbrain-extract reconcile --dry-run
gbrain-extract reconcile --fix-safe
gbrain-extract materialize
gbrain-extract-commit-staging
gbrain-extract-sync --timeout 900
```

OCR uses 20-page `vie+eng` Tesseract windows. The nightly run stops at 2,000 OCR pages or its time
limit and resumes from SQLite ranges. Documents remain `partial` until every page is complete.
Failed items retry at most three times per extraction version. Unsupported and encrypted files are
recorded explicitly and never crash the cycle.

`reconcile --fix-safe` moves unexplained output orphans into timestamped quarantine and marks
contaminated outputs for regeneration; it never permanently deletes source or corpus data.

Each source staging directory is a generated, local-only Git repository because native gbrain sync
uses the Git root as its slug root. `gbrain-extract-commit-staging` refuses a shared parent Git root
and any configured remote, preventing both slug-prefix drift and accidental evidence pushes. The
sync wrapper bounds each source subprocess and preserves gbrain's incremental checkpoint on timeout.

## LaunchAgent

The installer prepares, but does not activate, `com.frankbrain.extract`. Activate it only after
backup restore proof, state migration, source registration, and one manual bounded cycle:

```bash
launchctl bootstrap "gui/$UID" "$HOME/Library/LaunchAgents/com.frankbrain.extract.plist"
launchctl kickstart -k "gui/$UID/com.frankbrain.extract"
```

It runs at 02:30, uses a non-blocking process lock, and writes status and logs under
`~/gbrain-sources/logs/`. Each cycle performs extraction, deterministic reconciliation,
materialization, local staging commit, and source-scoped sync within one four-hour budget. Treat a
missing successful run within 24 hours as a heartbeat alert; a partial or failed cycle never refreshes
the successful-cycle timestamp.
`gbrain-extract-health` provides the machine-readable probe and exits non-zero when the marker is
missing, failed, or older than 24 hours.

`gbrain-founder-heartbeat` wraps the existing local heartbeat collector, preserves both its
canonical and Hermes-bridge reports, and adds a fail-closed `signals.founder_extraction` entry.
Configure the existing heartbeat LaunchAgent to invoke this wrapper only after the extraction
status marker has been created; no credential or document content is copied into the report.

## Recovery

- Restore extractor state from `extract-state.sqlite3.pre-v3-*.bak`.
- Restore PostgreSQL from the proven backup before repeating source reassignment.
- Quarantined output is recoverable under `~/gbrain-sources/quarantine/`.
- Re-running extraction and materialization is idempotent; unchanged inputs are not reprocessed.

Do not open an upstream PR for this local workflow. gbrain, LLM Wiki, and Hermes changes stay in
the designated forks.
