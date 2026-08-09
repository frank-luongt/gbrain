#!/bin/zsh
set -euo pipefail

script_dir="${0:A:h}"
bin_dir="$HOME/.gbrain/bin"
data_root="${GBRAIN_EXTRACT_ROOT:-$HOME/gbrain-sources}"
agent_dir="$HOME/Library/LaunchAgents"
plist="$agent_dir/com.frankbrain.extract.plist"

mkdir -p "$bin_dir" "$data_root/logs" "$data_root/staging" "$data_root/quarantine" "$agent_dir"
install -m 0755 "$script_dir/gbrain_extract.py" "$bin_dir/gbrain-extract"
install -m 0755 "$script_dir/gbrain_source_reassign.py" "$bin_dir/gbrain-source-reassign"
install -m 0755 "$script_dir/gbrain_extract_health.py" "$bin_dir/gbrain-extract-health"
install -m 0755 "$script_dir/gbrain_heartbeat_wrapper.py" "$bin_dir/gbrain-founder-heartbeat"
install -m 0755 "$script_dir/gbrain_sync_staging.py" "$bin_dir/gbrain-extract-sync"
install -m 0755 "$script_dir/commit-staging.sh" "$bin_dir/gbrain-extract-commit-staging"
install -m 0755 "$script_dir/run-nightly.sh" "$bin_dir/gbrain-extract-nightly"

config_source="${GBRAIN_EXTRACT_CONFIG_SOURCE:-$script_dir/sources.example.json}"
if [[ ! -e "$data_root/sources.json" ]]; then
  install -m 0600 "$config_source" "$data_root/sources.json"
else
  print "preserving existing $data_root/sources.json"
fi

sed "s|__HOME__|$HOME|g" "$script_dir/com.frankbrain.extract.plist.template" >"$plist.tmp"
plutil -lint "$plist.tmp" >/dev/null
mv "$plist.tmp" "$plist"
chmod 0600 "$plist"

print "installed gbrain-extract to $bin_dir"
print "configuration: $data_root/sources.json"
print "LaunchAgent prepared at $plist"
print "activate after backup and migration: launchctl bootstrap gui/$UID $plist"
