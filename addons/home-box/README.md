# Home box add-in: local ears and a local brain (optional)

Gizmo runs fine on AWS alone: Nova Sonic hears and speaks, and Claude on Bedrock thinks, on the robot or on AgentCore.
The home box is an optional Linux machine with an NVIDIA GPU on the same network. It does the listening, the speaking
and the thinking locally, with no per-minute cost, and the delay from speech to voice drops to about 2 s. On the
console's Settings it adds two independent choices:

- **Who hears and speaks → Home box:** the box's ears and voice server in place of Nova Sonic. When the box doesn't
  answer, the robot falls back to Nova Sonic by itself and switches back when the box returns.
- **What thinks → OpenAI-compatible,** with the base URL left empty: LM Studio on the box. Claude on Bedrock stands by
  for any turn the local model is slow on. (Any other OpenAI-compatible endpoint works there too: type its URL.)

The box speaks through the [voice add-in](../voice/), so set that up on the box too.

| Service | What it does | Port | Runs on |
|---|---|---|---|
| `gizmo-ears` (`ears_server.py`) | Silero voice activity detection plus Whisper large-v3-turbo. The robot streams its mic over a WebSocket; the ears send back `speech_start` (for barge-in) and each finished turn's text. | 8771 | GPU (about 0.3 s a turn) |
| `gizmo-voice` ([`../voice/voice_server.py`](../voice/)) | The voice add-in: the personalities' voices, sentence by sentence. | 8770 | F5 on the GPU (about 0.7 s a sentence), Kokoro on the CPU (about 0.5 s) |
| LM Studio | The local brain model over its OpenAI-compatible API. The robot's Strands agent calls it like any other model. | 1234 | GPU |

Each part is optional. Without the box's ears the robot uses Nova Sonic. Without F5 weights the voice server only has
built-in voices. Without LM Studio the brain uses Claude on Bedrock.

## Hardware

The reference box is Ubuntu 22.04 with two RTX 3090s, 32 GB of RAM and an 8-core CPU:

- **The ears and the voices** share one GPU, and need under 4 GB of its memory.
- **Qwen 3.8 27B** in LM Studio uses about 16.5 GB, split across both GPUs.

A single 24 GB GPU runs everything with a smaller local model.

RAM is the tight part. LM Studio's engine, the two services and a desktop session fill 32 GB.

## Setup

Clone the repo onto the box, then from this folder:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r ../voice/requirements.txt
```

- **Whisper** ([openai/whisper-large-v3-turbo](https://huggingface.co/openai/whisper-large-v3-turbo), about 1.6 GB) downloads itself the first time the ears start.
- **The voices:** set up their models as in the [voice add-in](../voice/README.md).

### The local brain (LM Studio)

1. **Install and download.** Install LM Studio, download the model (the reference setup uses `qwen/qwen3.8-27b`), and
   turn on its server for the local network (port 1234).
2. **Load it with one slot:**

   ```bash
   ~/.lmstudio/bin/lms load qwen/qwen3.8-27b -c 16384 --parallel 1 --identifier qwen/qwen3.8-27b -y
   ```

   - **One slot (`--parallel 1`):** Gizmo only ever sends one request at a time. LM Studio's default of 4 slots keeps
     each slot's saved context snapshots in system RAM. With a hybrid model like Qwen 3.8, that grew the engine to
     about 10 GB and the kernel's out-of-memory killer stopped it.
   - **A 16k context:** room for the system prompt, a loaded skill and the conversation, at almost no extra GPU memory
     over 8k.
   - **No `--ttl`:** the model never unloads on its own.
3. **If the model unloads anyway** (a crash, or someone unloads it), LM Studio reloads it on Gizmo's next request with
   its own defaults: its default context, 4 slots and a 1-hour idle timeout. That cold load takes about 25 s, so the
   robot's first turns go to its cloud fallback. Run `./gizmo-ctl.sh up-home` to load it properly again.
4. **After a reboot,** LM Studio is a desktop app: start it with its server on.

### Always-on services

```bash
./install-services.sh               # systemd user services gizmo-ears and gizmo-voice (../voice), then loads the model
sudo loginctl enable-linger "$USER" # once: keep them running without anyone logged in
```

- **Settings:** set them in the environment before running it: `GIZMO_BOX_VENV` (the venv, default `./.venv`),
  `GIZMO_BOX_GPU` (`CUDA_VISIBLE_DEVICES`, default `0`), `GIZMO_LOCAL_MODEL` and `GIZMO_LOCAL_CTX`.
- **Behaviour:** the services start at boot and restart if they crash.

Then give the robot the box's address. On the robot, in `~/.config/gizmo/config.toml`:

```toml
box_hosts = ["192.168.1.50"]   # ears on 8771, LM Studio on 1234, and the voice server on 8770 unless voice_servers says otherwise
```

## Day to day

```bash
./gizmo-ctl.sh status      # {"voice": "active", "ears": "active", "local_model": true}
./gizmo-ctl.sh down        # stop both services and unload the model (LM Studio stays open)
./gizmo-ctl.sh up          # the voice server only (what a personality's own voice needs)
./gizmo-ctl.sh up-home     # voice, ears and the model, for hearing, speaking and thinking on the box
./restart.sh ears|voice    # restart one and wait until it's ready
journalctl --user -u gizmo-voice -f
```

`gizmo-ctl.sh` only touches Gizmo's own model in LM Studio, so other work on the box carries on.

## Security

None of these services has authentication. Keep ports 8770, 8771 and 1234 on your home network, never open them to
the internet, and use a VPN (WireGuard, for example) to reach the box from elsewhere.

`POST /quit` on the voice server only works for a copy started with `--robot`, and only from the robot's announced
address or the machine itself.

## Protocols

The docstring at the top of `ears_server.py` is the reference for the ears' WebSocket messages. The voice server's API is described in the [voice add-in](../voice/README.md).
