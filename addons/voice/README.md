# Voice add-in: cloned and built-in voices (optional)

Out of the box, Gizmo speaks with Amazon Nova Sonic's voices: each personality has one, and you can change it on the console. This add-in gives personalities voices of their own:

- **Cloned voices.** [F5-TTS](https://github.com/SWivid/F5-TTS) copies a voice from a 3–12 s clip and its exact transcript. The weights are hosted on Hugging Face at [SWivid/F5-TTS](https://huggingface.co/SWivid/F5-TTS), with the [charactr/vocos-mel-24khz](https://huggingface.co/charactr/vocos-mel-24khz) vocoder.
- **Built-in voices.** [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) has 28 English voices, which you can mix and tune for speed, pitch and a robot edge. We use its ONNX export from [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0).

Both run in one small web service, [voice_server.py](voice_server.py), which you host on any machine Gizmo can reach. Either half is optional: without F5 weights it only has built-in voices, and without the Kokoro files it only clones.

| Where you host it | Good for | Speed |
|---|---|---|
| A Linux PC with an NVIDIA GPU | Cloned and built-in voices, always on | about 0.7 s a sentence (F5, RTX 3090); about 0.5 s (Kokoro, CPU) |
| A laptop (Apple silicon works) | Cloned voices wherever you take Gizmo | 2–5 s a sentence on an M4 Pro, depending on the clip's length |

Gizmo asks for one sentence at a time and fetches the next while the current one plays, so only the first sentence's time is felt.

## Set it up

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

**Kokoro** (built-in voices). Use the ONNX export and its voice table. Never use Kokoro's `.pth` file: a downloaded pickle can run code.

```bash
mkdir -p models/kokoro && cd models/kokoro
R=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0
curl -fLO $R/kokoro-v1.0.onnx && curl -fLO $R/voices-v1.0.bin
curl -fLO https://huggingface.co/hexgrad/Kokoro-82M/resolve/main/config.json
```

**F5-TTS** (cloned voices). Download the weights from [SWivid/F5-TTS](https://huggingface.co/SWivid/F5-TTS) and [charactr/vocos-mel-24khz](https://huggingface.co/charactr/vocos-mel-24khz). Put them in `models/f5`, or point `--f5-models` / `GIZMO_F5_MODELS` at them:

```
models/f5/F5TTS_v1_Base/F5TTS_v1_Base/model_1250000.safetensors
models/f5/F5TTS_v1_Base/F5TTS_v1_Base/vocab.txt
models/f5/vocos/
```

Each model has its own licence, so check it before you use one. Only clone voices you have the right to use.

## Connect Gizmo to it

Start the server on the machine that hosts it: `.venv/bin/python voice_server.py`. You can also install it as a service with the [home box](../home-box/) scripts. Then tell Gizmo where it is, in one of three ways:

- **On the console:** Settings → **Voice add-in**. Type the server's address (`http://192.168.1.50:8770`; one per line for several) and press **Connect**. Gizmo checks each one and says what it can do: cloned voices, and how many built-in voices.
- **In the robot's config,** in `~/.config/gizmo/config.toml`. The console's setting overrides this:
  ```toml
  voice_servers = ["http://192.168.1.50:8770"]
  ```
- **A laptop that announces itself.** With `--robot`, the server tells Gizmo its address every 10 s, so it's found on any network:
  ```bash
  .venv/bin/python voice_server.py --robot http://reachy-mini.local:8042
  ```

Once Gizmo sees a voice server, each personality's Voice section on the console gains two choices next to Nova Sonic: **Built-in** (a Kokoro voice, with its tuning sliders and a Hear it button) and **Cloned**.

## Give a personality a cloned voice

On Personalities, pick the personality, then Voice → **Cloned**:

1. **Choose a clip** of 3–12 s, with one person speaking clearly and no music.
2. **Type its transcript** exactly, word for word.
3. **Press Save clip.** Gizmo uploads the clip to the voice server, which checks it right away. A clip it can't use shows up as an error then and there, not mid-conversation.
4. **Try it** with **Hear it**, which plays a line in the browser.
5. **Save the personality,** so Cloned is its voice from now on.

**Saved versions:** whenever you save a new clip, the one it replaces is kept with its transcript and date. Each saved version can be played, loaded back in (the current clip then becomes a saved version itself), or deleted. The last 10 are kept, on the robot in `~/.config/gizmo/personalities/<key>/`.

A personality set to Built-in or Cloned speaks with that voice whenever a voice server answers. When none does, Nova Sonic speaks instead, in the personality's Nova voice.

## The protocol

The docstring at the top of [voice_server.py](voice_server.py) is the reference:
- **`PUT /voices/{id}`:** Gizmo uploads a personality's clip the first time it needs it. The server caches it by a hash.
- **`POST /tts`:** one sentence. The voice is either `voice` plus `hash` (cloned) or `builtin` (a Kokoro tuning). It returns 16 kHz PCM.
- **`GET /health`:** lists the cloned voices the server holds and its built-in voices.
- **`POST /quit`:** only works for a copy started with `--robot`, and only from the robot itself. The console's Shut down uses it.

## Security

The server has no authentication. Keep port 8770 on your own network, and use a VPN to reach it from elsewhere.
