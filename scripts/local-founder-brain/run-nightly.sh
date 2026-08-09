#!/bin/zsh
set -euo pipefail

export PATH="$HOME/.gbrain/bin:$HOME/.bun/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
data_root="${GBRAIN_EXTRACT_ROOT:-$HOME/gbrain-sources}"
log_root="$data_root/logs"
mkdir -p "$log_root"

cycle_started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
status_file="$log_root/status.json"

for required_tool in pdftotext pdftoppm pdfinfo tesseract git curl pg_isready; do
  if ! command -v "$required_tool" >/dev/null; then
    print -u2 "required parser unavailable: $required_tool"
    exit 19
  fi
done
if [[ ! -w "$data_root/staging" ]]; then
  print -u2 "staging root is not writable: $data_root/staging"
  exit 19
fi
if ! curl -fsS --max-time 5 http://127.0.0.1:3131/health >/dev/null; then
  print -u2 "gbrain health endpoint is unavailable at $cycle_started"
  exit 20
fi
if ! curl -fsS --max-time 5 http://127.0.0.1:11434/api/tags >/dev/null; then
  print -u2 "Ollama is unavailable at $cycle_started"
  exit 20
fi
if ! pg_isready -h 127.0.0.1 -p 5432 >/dev/null; then
  print -u2 "PostgreSQL is unavailable at $cycle_started"
  exit 20
fi
# The generic doctor also evaluates unrelated resolver/skill policy. Those
# findings belong in maintenance reporting, not in this bounded ingestion
# readiness gate: the endpoint, PostgreSQL, Ollama, parsers, and staging
# checks above are the dependencies this cycle actually needs.

# The cycle supervisor owns one 4h deadline and terminates every child process
# group at its allocated share. It writes status only after all steps succeed.
gbrain-extract-nightly-cycle --wall-clock 14400 --status-file "$status_file"
print "founder extraction cycle complete: $cycle_started"
