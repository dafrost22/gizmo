#!/usr/bin/env bash
# Gizmo's on/off switch for its services on this box: down | up | up-home | status.
# Run on the box, from this folder: ./gizmo-ctl.sh <action>   (or over SSH from your laptop)
#   down     stop gizmo-voice and gizmo-ears, unload the local brain model (LM Studio itself stays open)
#   up       start gizmo-voice (the cloned and built-in voices, used on both backends)
#   up-home  also gizmo-ears and the local brain model, for the robot's "Home box" backend
#   status   one JSON line: {"voice": "active", "ears": "inactive", "local_model": false}
# GIZMO_LOCAL_MODEL and GIZMO_LOCAL_CTX as in install-services.sh.
set -u
cd "$(dirname "$0")"
LMS=~/.lmstudio/bin/lms
MODEL="${GIZMO_LOCAL_MODEL:-qwen/qwen3.8-27b}"
CTX="${GIZMO_LOCAL_CTX:-16384}"
action="${1:-}"

model_loaded() { [ -x "$LMS" ] && $LMS ps 2>/dev/null | grep -qF "$MODEL"; }

start() {  # ears | voice: restart.sh waits until the service is ready
  if systemctl --user is-active --quiet "gizmo-$1"; then echo "gizmo-$1: already running"; else ./restart.sh "$1"; fi
}

case "$action" in
  down)
    systemctl --user stop gizmo-voice gizmo-ears
    # Copies started by hand, outside the services (bracket patterns, or pkill -f matches this shell).
    pkill -f 'python [v]oice_server'; pkill -f 'python [e]ars_server'
    rc=0
    for unit in gizmo-voice gizmo-ears; do
      if systemctl --user is-active --quiet "$unit"; then echo "$unit: still running"; rc=1; else echo "$unit: stopped"; fi
    done
    # Only Gizmo's model: LM Studio may be serving other work on this box.
    if model_loaded; then $LMS unload "$MODEL" > /dev/null 2>&1; fi
    if model_loaded; then echo "$MODEL: still loaded"; rc=1; else echo "$MODEL: unloaded"; fi
    exit $rc ;;
  up|up-home)
    start voice
    if [ "$action" = up-home ]; then
      start ears
      if model_loaded; then echo "$MODEL: already loaded"
      elif [ ! -x "$LMS" ]; then echo "$MODEL: LM Studio's CLI isn't installed"
      # One slot: see install-services.sh.
      elif $LMS load "$MODEL" -c "$CTX" --parallel 1 --identifier "$MODEL" -y > /dev/null 2>&1; then echo "$MODEL: loaded, $CTX context"
      else echo "$MODEL: load failed (is LM Studio open?)"; fi
    fi ;;
  status)
    printf '{"voice": "%s", "ears": "%s", "local_model": %s}\n' "$(systemctl --user is-active gizmo-voice)" \
      "$(systemctl --user is-active gizmo-ears)" "$(model_loaded && echo true || echo false)" ;;
  *)
    echo "usage: gizmo-ctl.sh down | up | up-home | status" >&2
    exit 2 ;;
esac
