#!/usr/bin/env bash
# Restart one of Gizmo's box services (installed by install-services.sh) and wait until it's ready.
# Run on the box: ./restart.sh ears | voice
set -u
case "${1:-}" in
  ears|voice) unit="gizmo-$1" ;;
  *) echo "usage: restart.sh ears | voice" >&2; exit 2 ;;
esac
since=$(date "+%Y-%m-%d %H:%M:%S")  # only log lines from this restart count
systemctl --user restart "$unit"
for i in $(seq 1 60); do journalctl --user -u "$unit" --since "$since" --no-pager | grep -q -E "ready:|Traceback" && break; sleep 2; done
echo "$unit: $(systemctl --user is-active "$unit") | $(journalctl --user -u "$unit" --since "$since" --no-pager | grep -E 'ready:|Traceback' | tail -1 | cut -c 1-140)"
