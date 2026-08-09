#!/usr/bin/env python3
"""Dry-run-first source reassignment for legacy local-extractor gbrain pages."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

GDRIVE_PATTERN = "%/Library/CloudStorage/GoogleDrive-%/My Drive/1 Workspaces/%"
FAOS_PATTERN = "%/Projects/FAOS/%"
GDRIVE_STAGING = str(Path("~/gbrain-sources/staging/gdrive-workspaces").expanduser())
FAOS_STAGING = str(Path("~/gbrain-sources/staging/faos-projects").expanduser())
DEFAULT_QUARANTINE_REPORT = Path("~/gbrain-sources/logs/source-reassignment-quarantine.json").expanduser()


def resolve_database_url(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    config = Path("~/.gbrain/config.json").expanduser()
    try:
        return json.loads(config.read_text(encoding="utf-8")).get("database_url")
    except (OSError, json.JSONDecodeError):
        return None


def libpq_env(database_url: str) -> dict[str, str]:
    parsed = urlparse(database_url)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise ValueError("DATABASE_URL must be a postgres:// or postgresql:// URI")
    env = os.environ.copy()
    env.update({
        "PGHOST": parsed.hostname,
        "PGPORT": str(parsed.port or 5432),
        "PGDATABASE": unquote(parsed.path.lstrip("/")),
        "PGUSER": unquote(parsed.username or ""),
    })
    if parsed.password:
        env["PGPASSWORD"] = unquote(parsed.password)
    query = parse_qs(parsed.query)
    if query.get("sslmode"):
        env["PGSSLMODE"] = query["sslmode"][0]
    env.pop("DATABASE_URL", None)
    return env


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def provenance_targets(source_path: str | None, aliases: object = ()) -> set[str]:
    """Classify only declared local provenance; never infer an owner from a hash alone."""
    values = [source_path] if source_path else []
    if isinstance(aliases, list):
        values.extend(value for value in aliases if isinstance(value, str))
    targets: set[str] = set()
    for value in values:
        normalized = value.replace("\\", "/")
        if "/Library/CloudStorage/GoogleDrive-" in normalized and "/My Drive/1 Workspaces/" in normalized:
            targets.add("gdrive-workspaces")
        if "/Projects/FAOS/" in normalized:
            targets.add("faos-projects")
    return targets


def deterministic_owner(targets: set[str]) -> str | None:
    """Google Drive wins exact cross-source duplicates; no provenance means quarantine."""
    if "gdrive-workspaces" in targets:
        return "gdrive-workspaces"
    if "faos-projects" in targets:
        return "faos-projects"
    return None


def write_quarantine_report(path: Path, report: list[dict[str, object]]) -> None:
    """Persist unresolved source provenance without touching the affected pages."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump({"schema_version": 1, "reason": "ambiguous_provenance", "pages": report}, handle, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    os.replace(temporary, path)


def preserved_aliases_sql(page_alias: str) -> str:
    """Retain the declared path plus every existing alias during a source move."""
    return f"""(
      SELECT COALESCE(jsonb_agg(alias ORDER BY alias), '[]'::jsonb)
        FROM (
          SELECT DISTINCT alias
            FROM (
              SELECT NULLIF({page_alias}.frontmatter->>'source_path','') AS alias
              UNION ALL
              SELECT jsonb_array_elements_text(
                CASE WHEN jsonb_typeof({page_alias}.frontmatter->'aliases')='array'
                  THEN {page_alias}.frontmatter->'aliases' ELSE '[]'::jsonb END
              )
            ) raw_aliases
           WHERE alias IS NOT NULL AND alias <> ''
        ) normalized_aliases
    )"""


def mapping_cte() -> str:
    return f"""
      SELECT id AS page_id,
             CASE
               WHEN frontmatter->>'source_path' LIKE {sql_literal(GDRIVE_PATTERN)} THEN 'gdrive-workspaces'
               WHEN frontmatter->>'source_path' LIKE {sql_literal(FAOS_PATTERN)} THEN 'faos-projects'
             END AS target_source
        FROM pages
       WHERE source_id = 'default'
         AND deleted_at IS NULL
         AND (
           frontmatter->>'source_path' LIKE {sql_literal(GDRIVE_PATTERN)}
           OR frontmatter->>'source_path' LIKE {sql_literal(FAOS_PATTERN)}
         )
    """


def preview_sql() -> str:
    return f"""
    WITH mapping AS ({mapping_cte()}), conflicts AS (
      SELECT m.page_id
        FROM mapping m JOIN pages p0 ON p0.id=m.page_id
        JOIN pages p1 ON p1.source_id=m.target_source AND p1.slug=p0.slug AND p1.id<>p0.id
    )
    SELECT json_build_object(
      'candidate_pages', (SELECT count(*) FROM mapping),
      'gdrive_workspaces', (SELECT count(*) FROM mapping WHERE target_source='gdrive-workspaces'),
      'faos_projects', (SELECT count(*) FROM mapping WHERE target_source='faos-projects'),
      'slug_conflicts', (SELECT count(*) FROM conflicts),
      'missing_sources', (
        SELECT json_agg(required.id) FROM (VALUES ('gdrive-workspaces'),('faos-projects')) required(id)
        LEFT JOIN sources s ON s.id=required.id WHERE s.id IS NULL
      ),
      'page_count_before', (SELECT count(*) FROM pages),
      'chunk_count_before', (SELECT count(*) FROM content_chunks),
      'embedded_chunk_count_before', (SELECT count(*) FROM content_chunks WHERE embedding IS NOT NULL),
      'missing_embedding_count_before', (SELECT count(*) FROM content_chunks WHERE embedding IS NULL),
      'version_count_before', (SELECT count(*) FROM page_versions),
      'link_count_before', (SELECT count(*) FROM links)
    )::text;
    """


def apply_sql() -> str:
    return f"""
    BEGIN;
    CREATE TEMP TABLE founder_source_mapping ON COMMIT DROP AS {mapping_cte()};

    DO $$
    DECLARE missing_count integer; conflict_count integer;
    BEGIN
      SELECT count(*) INTO missing_count FROM (VALUES ('gdrive-workspaces'),('faos-projects')) required(id)
        LEFT JOIN sources s ON s.id=required.id WHERE s.id IS NULL;
      IF missing_count <> 0 THEN
        RAISE EXCEPTION 'required target sources are not registered';
      END IF;
      SELECT count(*) INTO conflict_count
        FROM founder_source_mapping m JOIN pages p0 ON p0.id=m.page_id
        JOIN pages p1 ON p1.source_id=m.target_source AND p1.slug=p0.slug AND p1.id<>p0.id;
      IF conflict_count <> 0 THEN
        RAISE EXCEPTION 'source reassignment has % slug conflicts', conflict_count;
      END IF;
    END $$;

    CREATE TEMP TABLE founder_counts_before ON COMMIT DROP AS
      SELECT (SELECT count(*) FROM pages) pages,
             (SELECT count(*) FROM content_chunks) chunks,
             (SELECT count(*) FROM content_chunks WHERE embedding IS NOT NULL) embedded_chunks,
             (SELECT count(*) FROM content_chunks WHERE embedding IS NULL) missing_embeddings,
             (SELECT count(*) FROM page_versions) versions,
             (SELECT count(*) FROM links) links;

    UPDATE pages p
       SET source_id=m.target_source,
           frontmatter=jsonb_set(
             jsonb_set(p.frontmatter, '{{source_id}}', to_jsonb(m.target_source), true),
             '{{aliases}}', {preserved_aliases_sql('p')}, true
           )
      FROM founder_source_mapping m
     WHERE p.id=m.page_id;

    DO $$
    DECLARE before_row founder_counts_before%ROWTYPE;
    BEGIN
      SELECT * INTO before_row FROM founder_counts_before;
      IF before_row.pages <> (SELECT count(*) FROM pages)
         OR before_row.chunks <> (SELECT count(*) FROM content_chunks)
         OR before_row.embedded_chunks <> (SELECT count(*) FROM content_chunks WHERE embedding IS NOT NULL)
         OR before_row.missing_embeddings <> (SELECT count(*) FROM content_chunks WHERE embedding IS NULL)
         OR before_row.versions <> (SELECT count(*) FROM page_versions)
         OR before_row.links <> (SELECT count(*) FROM links) THEN
        RAISE EXCEPTION 'count reconciliation failed; transaction rolled back';
      END IF;
    END $$;

    SELECT json_build_object(
      'status','committed',
      'reassigned_pages',(SELECT count(*) FROM founder_source_mapping),
      'gdrive_workspaces',(SELECT count(*) FROM founder_source_mapping WHERE target_source='gdrive-workspaces'),
      'faos_projects',(SELECT count(*) FROM founder_source_mapping WHERE target_source='faos-projects'),
      'pages',(SELECT count(*) FROM pages),
      'chunks',(SELECT count(*) FROM content_chunks),
      'embedded_chunks',(SELECT count(*) FROM content_chunks WHERE embedding IS NOT NULL),
      'missing_embeddings',(SELECT count(*) FROM content_chunks WHERE embedding IS NULL),
      'versions',(SELECT count(*) FROM page_versions),
      'links',(SELECT count(*) FROM links)
    )::text;
    COMMIT;
    """


def prepare_preview_sql() -> str:
    return """
    SELECT json_build_object(
      'sources', COALESCE((SELECT json_agg(row_to_json(x)) FROM (
        SELECT id,name,local_path,(config->>'federated')::boolean federated,
               (SELECT count(*) FROM pages p WHERE p.source_id=s.id AND p.deleted_at IS NULL) page_count
          FROM sources s WHERE id IN ('gdrive-workspaces','faos-projects') ORDER BY id
      ) x), '[]'::json),
      'gdrive_target', %s,
      'faos_target', %s
    )::text;
    """ % (sql_literal(GDRIVE_STAGING), sql_literal(FAOS_STAGING))


def prepare_apply_sql() -> str:
    return """
    BEGIN;
    DO $$
    DECLARE conflicting integer;
    BEGIN
      SELECT count(*) INTO conflicting FROM sources s
       WHERE s.id IN ('gdrive-workspaces','faos-projects')
         AND s.local_path IS DISTINCT FROM CASE s.id
           WHEN 'gdrive-workspaces' THEN %s ELSE %s END
         AND EXISTS (SELECT 1 FROM pages p WHERE p.source_id=s.id AND p.deleted_at IS NULL);
      IF conflicting <> 0 THEN
        RAISE EXCEPTION 'refusing to repoint a target source that already owns pages';
      END IF;
    END $$;

    INSERT INTO sources(id,name,local_path,config)
      VALUES ('gdrive-workspaces','Google Drive Workspaces',%s,'{"federated":true}'::jsonb)
      ON CONFLICT(id) DO UPDATE SET
        local_path=EXCLUDED.local_path,
        config=jsonb_set(sources.config,'{federated}','true'::jsonb,true),
        archived=false,archived_at=NULL,archive_expires_at=NULL;
    INSERT INTO sources(id,name,local_path,config)
      VALUES ('faos-projects','FAOS Projects',%s,'{"federated":true}'::jsonb)
      ON CONFLICT(id) DO UPDATE SET
        local_path=EXCLUDED.local_path,
        config=jsonb_set(sources.config,'{federated}','true'::jsonb,true),
        archived=false,archived_at=NULL,archive_expires_at=NULL;

    SELECT json_build_object(
      'status','committed',
      'sources',(SELECT json_agg(row_to_json(x)) FROM (
        SELECT id,name,local_path,(config->>'federated')::boolean federated
          FROM sources WHERE id IN ('gdrive-workspaces','faos-projects') ORDER BY id
      ) x)
    )::text;
    COMMIT;
    """ % (
        sql_literal(GDRIVE_STAGING), sql_literal(FAOS_STAGING),
        sql_literal(GDRIVE_STAGING), sql_literal(FAOS_STAGING),
    )


def run_psql(sql: str, database_url: str) -> dict[str, object]:
    result = subprocess.run(
        ["psql", "--no-psqlrc", "--set", "ON_ERROR_STOP=1", "--tuples-only", "--no-align", "--quiet"],
        input=sql, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=libpq_env(database_url), check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "psql failed")
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip().startswith("{")]
    if not lines:
        raise RuntimeError("psql returned no reconciliation result")
    return json.loads(lines[-1])


def run_psql_text(sql: str, database_url: str) -> str:
    result = subprocess.run(
        ["psql", "--no-psqlrc", "--set", "ON_ERROR_STOP=1", "--tuples-only", "--no-align", "--quiet"],
        input=sql, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=libpq_env(database_url), check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "psql failed")
    return result.stdout


def owner_reconciliation(
    database_url: str, state_db: Path, apply: bool, quarantine_report: Path = DEFAULT_QUARANTINE_REPORT,
) -> dict[str, object]:
    state = sqlite3.connect(state_db)
    try:
        desired = {row[0]: row[1] for row in state.execute("SELECT sha256,source_id FROM docs WHERE source_id IS NOT NULL")}
    finally:
        state.close()
    output = run_psql_text(
        "COPY (SELECT id,source_id,slug,COALESCE(frontmatter->>'source_sha256',frontmatter->>'content_hash',''), "
        "frontmatter->>'source_path',COALESCE(frontmatter->'aliases','[]'::jsonb)::text "
        "FROM pages WHERE deleted_at IS NULL) TO STDOUT WITH (FORMAT csv, DELIMITER E'\\t');",
        database_url,
    )
    mappings: list[tuple[int, str, str, str]] = []
    quarantined: list[dict[str, object]] = []
    for page_id, current, slug, content_hash, source_path, aliases_json in csv.reader(io.StringIO(output), delimiter="\t"):
        target = desired.get(content_hash)
        if not target or target == current:
            continue
        try:
            aliases = json.loads(aliases_json)
        except json.JSONDecodeError:
            aliases = []
        provenance_owner = deterministic_owner(provenance_targets(source_path, aliases))
        if provenance_owner != target:
            quarantined.append({
                "page_id": int(page_id), "slug": slug, "current_source": current,
                "content_hash": content_hash, "source_path": source_path,
                "declared_owner": provenance_owner, "requested_owner": target,
                "reason": "missing_provenance" if provenance_owner is None else "provenance_owner_mismatch",
            })
            continue
        mappings.append((int(page_id), current, target, slug))
    counts: dict[str, int] = {}
    for _, current, target, _ in mappings:
        key = f"{current}_to_{target}"
        counts[key] = counts.get(key, 0) + 1
    report: dict[str, object] = {
        "candidate_pages": len(mappings), "moves": counts,
        "quarantined_pages": len(quarantined), "quarantine": quarantined,
    }
    if apply and quarantined:
        write_quarantine_report(quarantine_report, quarantined)
        report["quarantine_report"] = str(quarantine_report)
    if not mappings:
        return {"status": "noop", **report}
    values = ",".join(f"({page_id},{sql_literal(target)})" for page_id, _, target, _ in mappings)
    conflict_sql = f"""
      WITH mapping(page_id,target_source) AS (VALUES {values})
      SELECT count(*)::text FROM mapping m JOIN pages p0 ON p0.id=m.page_id
      JOIN pages p1 ON p1.source_id=m.target_source AND p1.slug=p0.slug AND p1.id<>p0.id;
    """
    conflicts = int(run_psql_text(conflict_sql, database_url).strip())
    report["slug_conflicts"] = conflicts
    if not apply:
        return {"status": "dry_run", **report}
    if conflicts:
        raise RuntimeError(f"owner reconciliation has {conflicts} slug conflicts")
    sql = f"""
      BEGIN;
      CREATE TEMP TABLE founder_owner_mapping(page_id integer PRIMARY KEY,target_source text NOT NULL) ON COMMIT DROP;
      INSERT INTO founder_owner_mapping VALUES {values};
      CREATE TEMP TABLE founder_owner_counts ON COMMIT DROP AS
        SELECT (SELECT count(*) FROM pages) pages,(SELECT count(*) FROM content_chunks) chunks,
               (SELECT count(*) FROM content_chunks WHERE embedding IS NOT NULL) embedded_chunks,
               (SELECT count(*) FROM content_chunks WHERE embedding IS NULL) missing_embeddings,
               (SELECT count(*) FROM page_versions) versions,(SELECT count(*) FROM links) links;
      UPDATE pages p SET source_id=m.target_source,
        frontmatter=jsonb_set(
          jsonb_set(p.frontmatter,'{{source_id}}',to_jsonb(m.target_source),true),
          '{{aliases}}',{preserved_aliases_sql('p')},true
        )
        FROM founder_owner_mapping m WHERE p.id=m.page_id;
      DO $$ DECLARE b founder_owner_counts%ROWTYPE; BEGIN
        SELECT * INTO b FROM founder_owner_counts;
        IF b.pages<>(SELECT count(*) FROM pages) OR b.chunks<>(SELECT count(*) FROM content_chunks)
           OR b.embedded_chunks<>(SELECT count(*) FROM content_chunks WHERE embedding IS NOT NULL)
           OR b.missing_embeddings<>(SELECT count(*) FROM content_chunks WHERE embedding IS NULL)
           OR b.versions<>(SELECT count(*) FROM page_versions) OR b.links<>(SELECT count(*) FROM links) THEN
          RAISE EXCEPTION 'owner reconciliation count guard failed';
        END IF;
      END $$;
      SELECT json_build_object('status','committed','reassigned_pages',(SELECT count(*) FROM founder_owner_mapping),
        'pages',(SELECT count(*) FROM pages),'chunks',(SELECT count(*) FROM content_chunks),
        'embedded_chunks',(SELECT count(*) FROM content_chunks WHERE embedding IS NOT NULL),
        'missing_embeddings',(SELECT count(*) FROM content_chunks WHERE embedding IS NULL),
        'versions',(SELECT count(*) FROM page_versions),'links',(SELECT count(*) FROM links))::text;
      COMMIT;
    """
    return run_psql(sql, database_url)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="commit the preflighted transaction")
    parser.add_argument("--prepare-sources", action="store_true", help="register/repoint the two staging sources")
    parser.add_argument("--reconcile-owner", action="store_true", help="align page source with extractor dedup ownership")
    parser.add_argument("--state-db", default=str(Path("~/gbrain-sources/extract-state.sqlite3").expanduser()))
    parser.add_argument("--quarantine-report", default=str(DEFAULT_QUARANTINE_REPORT))
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args()
    database_url = resolve_database_url(args.database_url or os.environ.get("DATABASE_URL"))
    if not database_url:
        print("gbrain-source-reassign: DATABASE_URL is required", file=sys.stderr)
        return 2
    try:
        if args.prepare_sources:
            preview = run_psql(prepare_preview_sql(), database_url)
            if not args.apply:
                print(json.dumps({"status": "dry_run", **preview}, sort_keys=True))
                return 0
            print(json.dumps(run_psql(prepare_apply_sql(), database_url), sort_keys=True))
            return 0
        if args.reconcile_owner:
            print(json.dumps(owner_reconciliation(
                database_url, Path(args.state_db), args.apply, Path(args.quarantine_report),
            ), sort_keys=True))
            return 0
        preview = run_psql(preview_sql(), database_url)
        if not args.apply:
            print(json.dumps({"status": "dry_run", **preview}, sort_keys=True))
            return 0
        if preview.get("missing_sources") or preview.get("slug_conflicts"):
            raise RuntimeError(f"preflight is not clean: {json.dumps(preview, sort_keys=True)}")
        print(json.dumps(run_psql(apply_sql(), database_url), sort_keys=True))
        return 0
    except (ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"gbrain-source-reassign: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
