#!/bin/zsh
set -euo pipefail

staging_root="${GBRAIN_EXTRACT_STAGING:-${GBRAIN_EXTRACT_ROOT:-$HOME/gbrain-sources}/staging}"
mkdir -p "$staging_root"

if [[ -d "$staging_root/.git" ]]; then
  print -u2 "refusing shared staging Git root; each gbrain source must own its slug root"
  exit 31
fi

for source_id in gdrive-workspaces faos-projects; do
  source_root="$staging_root/$source_id"
  mkdir -p "$source_root"
  if [[ ! -d "$source_root/.git" ]]; then
    git -C "$source_root" init -q
    git -C "$source_root" config user.name "FrankBrain Local Extractor"
    git -C "$source_root" config user.email "local-extractor@localhost"
  fi
  if [[ -n "$(git -C "$source_root" remote)" ]]; then
    print -u2 "refusing to commit: local-only staging repo has a remote ($source_id)"
    exit 30
  fi
  git -C "$source_root" add -A
  if git -C "$source_root" diff --cached --quiet; then
    print "$source_id unchanged"
    continue
  fi
  git -C "$source_root" commit -q -m "chore: refresh local founder evidence"
  print "$source_id committed: $(git -C "$source_root" rev-parse --short HEAD)"
done
