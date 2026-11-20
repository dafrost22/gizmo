#!/usr/bin/env bash
# Make Gizmo's services on this box always on: systemd user services that start at boot and restart if
# they crash. Run on the box, from this folder: ./install-services.sh
#   gizmo-ears.service   ears_server.py: Silero VAD + Whisper (port 8771)
#   gizmo-voice.service  ../voice/voice_server.py, the voice add-in: F5-TTS cloned voices on the GPU,
#                        Kokoro built-in voices on the CPU (port 8770)
# Then, if LM Studio's CLI is installed, it loads the local brain model for the "Home box" backend.
#
# Settings (environment variables, all optional):
#   GIZMO_BOX_VENV     the Python venv with requirements.txt and ../voice/requirements.txt      (./.venv)
#   GIZMO_BOX_GPU      which GPU the two services use (CUDA_VISIBLE_DEVICES)                  (0)
#   GIZMO_LOCAL_MODEL  the LM Studio model for the robot's local brain                       (qwen/qwen3.8-27b)
#   GIZMO_LOCAL_CTX    its context length in tokens                                           (16384)
# For the services to run without anyone logged in: sudo loginctl enable-linger "$USER" (once).
set -euo pipefail
BOX_DIR="$(cd "$(dirname "$0")" && pwd)"
VOICE_DIR="$(cd "$BOX_DIR/../voice" && pwd)"
VENV="${GIZMO_BOX_VENV:-$BOX_DIR/.venv}"
GPU="${GIZMO_BOX_GPU:-0}"
MODEL="${GIZMO_LOCAL_MODEL:-qwen/qwen3.8-27b}"
CTX="${GIZMO_LOCAL_CTX:-16384}"
UNITS=~/.config/systemd/user
mkdir -p "$UNITS"
[ -x "$VENV/bin/python" ] || { echo "no Python venv at $VENV: see README.md (Setup)" >&2; exit 1; }

unit() {  # name description folder script
  cat > "$UNITS/$1.service" <<EOF
[Unit]
Description=$2
After=network-online.target

[Service]
WorkingDirectory=$3
Environment=CUDA_VISIBLE_DEVICES=$GPU
Environment=PYTHONUNBUFFERED=1
ExecStart=$VENV/bin/python $4
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
}
unit gizmo-ears "Gizmo ears (Silero VAD + Whisper, port 8771)" "$BOX_DIR" ears_server.py
unit gizmo-voice "Gizmo voices (F5-TTS and Kokoro, port 8770)" "$VOICE_DIR" voice_server.py

# Stop copies started by hand before the services take over the ports (bracket patterns, so pkill -f
# doesn't match this shell's own command line).
pkill -f "python [e]ars_server" || true
pkill -f "python [v]oice_server" || true
sleep 2
systemctl --user daemon-reload
systemctl --user enable --now gizmo-ears.service gizmo-voice.service
for svc in gizmo-ears gizmo-voice; do
  for i in $(seq 1 60); do journalctl --user -u $svc --since "-3min" --no-pager | grep -q -E "ready:|Traceback" && break; sleep 2; done
  echo "$svc: $(systemctl --user is-active $svc) | $(journalctl --user -u $svc --since '-3min' --no-pager | grep -E 'ready:|Traceback' | tail -1 | cut -c 1-140)"
done
loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q "Linger=yes" \
  || echo "note: lingering is off, so the services stop when you log out: sudo loginctl enable-linger $USER"

LMS=~/.lmstudio/bin/lms
if [ -x "$LMS" ]; then
  if $LMS ps 2>/dev/null | grep -q "$MODEL"; then
    $LMS unload "$MODEL" > /dev/null 2>&1 || true  # reload it with our settings and no idle timeout
  fi
  # One slot and a fixed context: LM Studio's default 4 parallel slots each keep their own context
  # snapshots in system RAM, which ran a 32 GB box out of memory. Gizmo only ever sends one request.
  $LMS load "$MODEL" -c "$CTX" --parallel 1 --identifier "$MODEL" --yes > /dev/null 2>&1 \
    && echo "$MODEL: loaded, $CTX context, no idle timeout" || echo "$MODEL: load failed (is LM Studio running, with the model downloaded?)"
  $LMS ps 2>/dev/null | grep -F "$MODEL" | cut -c 1-120 || true
else
  echo "LM Studio's CLI isn't at $LMS: skipping the local brain model (the robot thinks on Bedrock instead)"
fi
