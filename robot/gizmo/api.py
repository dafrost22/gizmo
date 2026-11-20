"""The console's API: everything the web console (static/) shows and changes, on the app's port 8042.

Kept apart from main.py (the robot's runtime) so the console can be worked on and tested against these routes
without a robot: add_routes(app, robot) only needs the objects in Robot.

    GET  /api/state                      live: status, the personality in use, what's thinking and speaking, the log
    GET  /api/personalities              every personality, in full, plus what there is to pick from
    PUT  /api/personalities/{key}        create or change one (name, prompt, delivery, greeting, voice, tools)
    POST /api/personality                use one
    GET  /api/servers                    MCP servers: tools, the skills they serve, problems
    PUT  /api/servers                    add or change one;  POST /api/servers/test  tries one without keeping it
    POST /api/servers/{name}/call        run one of its tools by hand
    POST /api/servers/{name}/skill       fetch and check one of its skills, as the model would
    GET  /api/settings                   model provider, speech, listening, add-ins, versions
"""

from __future__ import annotations

import asyncio
import base64
import importlib.metadata
import io
import json
import logging
import threading
import time
import wave
from dataclasses import dataclass
from typing import Callable

import numpy as np
import requests
from fastapi import HTTPException
from fastapi.responses import FileResponse, Response

from gizmo import power_switch
from gizmo.brain import BEDROCK_MODELS, HAIKU, PROVIDERS, ModelSpec, fallbacks, model_spec, openai_url, render_prompt
from gizmo.config import CONFIG
from gizmo.listening import MODES as LISTEN_MODES, Listening
from gizmo.local_session import LocalSession, ears_url
from gizmo.mcp_skills import SkillError
from gizmo.personalities import BUILTIN_VOICES, DEFAULT_PERSONALITY, GREETING, NOVA_VOICES, Personalities, VoiceRef
from gizmo.toolbox import MASK, Toolbox, mask
from gizmo.voice_clone import PREFER, VoiceError

logger = logging.getLogger("gizmo.api")

EARS_END_MS = 600  # home-box ears: the pause that ends a turn (Nova's is ~1.5 s)
SPEECH = {"nova": "Nova Sonic", "local": "Home box (add-in)"}
MODEL_PROVIDERS = {**PROVIDERS, "agentcore": "Bedrock AgentCore"}


@dataclass
class Robot:
    """What the routes work on. main.py builds it; `holder` fills in as the app comes up (toolbox, brain, agent,
    loop, power, motor_watch, daemon_url)."""
    state: dict
    settings: dict
    save: Callable[[dict], None]
    log: object  # robot_io.ConversationLog
    mic: object
    speaker: object
    body: object | None  # None when the app couldn't start (no AWS credentials)
    people: Personalities
    listening: Listening
    holder: dict
    apply_voice: Callable[[str], None]  # speak as this personality from the next sentence on


def agentcore_arn(settings: dict) -> str:
    """The AgentCore runtime to think on: set on the console, else in config.toml (agentcore/deploy.py prints it)."""
    return str(((settings.get("model") or {}).get("agentcore") or {}).get("arn") or CONFIG["agentcore_runtime_arn"] or "").strip()


def wanted_mode(state: dict, settings: dict) -> str:
    """Which pipeline the settings ask for: local (home-box ears and voice), agentcore, or claude (Nova Sonic
    hears and speaks, the Strands brain on the robot thinks)."""
    if state.get("speech") == "local":
        return "local"
    return "agentcore" if (settings.get("model") or {}).get("provider") == "agentcore" else "claude"


def wav_bytes(pcm: np.ndarray, rate: int) -> bytes:
    """float32 mono -> a WAV file, for the console's voice previews."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(pcm, -1, 1) * 32767).astype(np.int16).tobytes())
    return buf.getvalue()


def version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def add_routes(app, robot: Robot) -> None:
    state, settings, log, mic, speaker, body = robot.state, robot.settings, robot.log, robot.mic, robot.speaker, robot.body
    people, listening, holder = robot.people, robot.listening, robot.holder

    def save() -> None:
        robot.save(settings)

    def on_loop(coro) -> None:
        loop = holder.get("loop")
        if loop is None:
            raise RuntimeError("agent not running yet")
        asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=10)

    def toolbox() -> Toolbox:
        box = holder.get("toolbox")
        if box is None:
            raise HTTPException(503, "Gizmo is still starting")
        return box

    def personality(key: str) -> str:
        if not people.exists(key):
            raise HTTPException(404, f"no personality called {key}")
        return key

    def rebuild(why: str) -> None:
        """A prompt, tool, voice or model changed: rebuild the session (a couple of seconds) to pick it up."""
        state["config_rev"] += 1
        busy()
        log.add("system", text=f"{why}: reloading…")

    def busy() -> None:
        """The session is about to be rebuilt: say so now, before the session loop gets to it, so a line typed
        in that moment is held off instead of being sent into a closing session."""
        if state["status"] == "listening":
            state["status"] = "warming up"

    # --- live state ----------------------------------------------------------------------------

    def model_view() -> dict:
        """The model settings as the console edits them (the API key masked)."""
        chosen = settings.get("model") or {}
        o = chosen.get("openai") or {}
        return {
            "provider": chosen.get("provider") if chosen.get("provider") in MODEL_PROVIDERS else "bedrock",
            "providers": MODEL_PROVIDERS,
            "bedrock": {"model": (chosen.get("bedrock") or {}).get("model") or HAIKU, "models": BEDROCK_MODELS},
            "openai": {"base_url": o.get("base_url", ""), "model": o.get("model") or CONFIG["local_model"],
                       "api_key": mask(o["api_key"]) if o.get("api_key") else "", "params": o.get("params") or {},
                       "auto": bool(CONFIG["box_hosts"])},  # no base URL: LM Studio on the home box
            "agentcore": {"arn": agentcore_arn(settings)},
        }

    def now_using() -> dict:
        """What Gizmo hears, thinks and speaks with right now, in words."""
        want = wanted_mode(state, settings)
        mode = state.get("mode") or want
        spec = model_spec(settings)
        brain = holder.get("brain")
        own = getattr(brain, "fallback", None) or brain  # on AgentCore, the robot's brain stands by
        who = spec.label
        if mode == "agentcore":
            thinks = f"{who}, in a Strands agent on AgentCore (the robot's own brain if it fails)"
        elif own is not None and getattr(own, "agents", None) and own.agents[0] is None:
            who = fallbacks(spec)[0].label
            thinks = f"{who} on Amazon Bedrock: {spec.label} isn't answering"
        else:
            thinks = f"{who} on {PROVIDERS[spec.provider]} ({fallbacks(spec)[0].label} if it's slow)"
        voice = people.get(state["personality"])["voice"]
        nova = f"Nova Sonic, {NOVA_VOICES[voice['nova']].split(' (')[0]}"
        server = speaker.clone.where() is not None
        if mode == "local":
            speaks = "A voice server" if server else "Nothing: no voice server answers (the cloud takes over)"
            short_voice = "voice server"
        elif voice["kind"] != "nova" and server:
            speaks = f"Its {'cloned' if voice['kind'] == 'clone' else 'built-in'} voice, from a voice server"
            short_voice = "its own voice"
        else:
            speaks = nova + ("" if voice["kind"] == "nova" else " (no voice server answers for its own voice)")
            short_voice = nova
        note = ""
        if state.get("desk"):  # python -m gizmo.desk: the brain without the robot
            return {"hears": "Typed lines (no robot here)", "thinks": thinks, "speaks": "Nothing: read the reply",
                    "note": "", "short": " · ".join([PROVIDERS[spec.provider], who, "typed turns"])}
        if want == "local" and mode != "local":
            note = "The home box isn't answering, so Nova Sonic is standing in (checked every 30 s)."
        elif want == "agentcore" and mode != "agentcore":
            note = "No AgentCore runtime is set, so the brain on the robot is thinking. Add the ARN under Settings."
        return {"hears": "The home box (Whisper)" if mode == "local" else "Nova Sonic", "thinks": thinks,
                "speaks": speaks, "note": note,
                "short": " · ".join(["AgentCore" if mode == "agentcore" else PROVIDERS[spec.provider], who, short_voice])}

    @app.get("/api/state")
    def get_state() -> dict:
        brain = holder.get("brain")
        return {
            **state,
            "personalities": people.names(),
            "muted": mic.muted,
            "speaking": speaker.speaking,
            "mic_dbfs": round(mic.level_dbfs, 1),
            "tracking": bool(body and body.tracking),
            "power": holder["power"].state if "power" in holder else None,
            # The optional add-ins (addons/ in the repo): the console only shows their controls once they're set up.
            "addons": {"voice": bool(speaker.clone.servers or speaker.clone.live_macs()), "box": bool(CONFIG["box_hosts"])},
            "now": now_using(),
            "tools": holder["toolbox"].summary() if "toolbox" in holder else None,
            "skills_loaded": brain.loaded_skills() if brain else [],  # in the brain's conversation right now
            "listening": listening.state(),
            "greet": bool(settings.get("greet", True)),  # introduce itself when revealed
            # The words being spoken and how far into them the voice is (robot_io.Captions); None with no speaker.
            "captions": speaker.caption_state() if hasattr(speaker, "caption_state") else None,
            "events": list(log.events),
        }

    @app.post("/api/conversation/new")
    def new_conversation() -> dict:
        """Start again from nothing. The console's turns go, and the session is rebuilt (a few seconds), so
        neither the brain nor the voice has a conversation behind it: a skill it had loaded loads again."""
        speaker.clear()
        log.events.clear()
        state["last_turn"] = None
        state["config_rev"] += 1
        busy()
        return get_state()

    @app.post("/api/mute")
    def mute() -> dict:
        mic.muted = True
        log.add("system", text="mic muted")
        return get_state()

    @app.post("/api/unmute")
    def unmute() -> dict:
        mic.muted = False
        log.add("system", text="mic unmuted")
        return get_state()

    @app.post("/api/interrupt")
    def interrupt() -> dict:
        speaker.clear()
        log.add("system", text="stopped talking (button)")
        return get_state()

    @app.post("/api/say")
    def say(payload: dict) -> dict:
        """A typed line: the same turn as a spoken one, without the microphone."""
        text = str(payload.get("text", "")).strip()
        agent = holder.get("agent")
        if state["off"]:
            log.add("system", text="Gizmo is switched off: Start up first")
        elif state["asleep"]:
            log.add("system", text="Gizmo is hidden (asleep): reveal it first")
        elif state["status"] != "listening":
            log.add("system", text="Gizmo is still getting ready: try that line again in a moment")
        elif text and agent is not None:
            log.add("typed", text=text)
            listening.typed(text)  # through even in wake-word mode, also when Nova hands it on
            log.last_user_at = time.monotonic()  # time the reply from the typed line, not the last spoken one
            try:
                on_loop(agent.send(text))
            except Exception as e:  # the session closed under it (a switch just started): say so, don't fail
                logger.warning(f"Couldn't send the typed line: {e!r}")
                log.add("system", text="Gizmo was switching sessions and missed that line: try it again")
        return get_state()

    @app.post("/api/introduce")
    def introduce() -> dict:
        """Have Gizmo introduce itself now, in character: its personality's introduction, as when it's revealed."""
        if state["off"]:
            log.add("system", text="Gizmo is switched off: Start up first")
        elif state["asleep"]:
            log.add("system", text="Gizmo is hidden (asleep): reveal it first")
        elif holder.get("agent") is not None:
            holder["greet"] = True  # the session loop sends the line, as it does after a reveal
        return get_state()

    @app.post("/api/track")
    def track(payload: dict) -> dict:
        if body is not None:
            body.track_faces(bool(payload.get("on")))
        return get_state()

    @app.post("/api/motors/recover")
    def recover_motors() -> dict:
        """The 'Sit up' button: re-torque every motor and return to neutral (a limp head)."""
        if watch := holder.get("motor_watch"):
            watch.recover()
        return get_state()

    # --- personalities -------------------------------------------------------------------------

    @app.get("/api/personalities")
    def get_personalities() -> dict:
        """Every personality in full, and what there is to choose from when editing one."""
        box = holder.get("toolbox")
        everyone = people.all()
        for p in everyone.values():
            # The whole prompt the model gets, for "see it all".
            p["rendered"] = render_prompt(p["prompt"], sounds=people.sound_tags(p["key"]))
        return {"personalities": everyone, "active": state["personality"], "nova_voices": NOVA_VOICES,
                "builtin_voices": BUILTIN_VOICES, "greeting_default": GREETING,
                "servers": box.servers() if box else [], "local_skills": box.local_skills() if box else []}

    def bind_approvals(key: str, tools: dict) -> dict:
        """Ticking a skill on its own approves it as its server lists it now: the fingerprint of its files goes
        with the tick. One that was already ticked keeps the approval it had, so saving some other change can't
        approve an edit nobody looked at. All of a server's skills ticked is trust in the server, with nothing
        to bind."""
        before = people.get(key)["tools"]["servers"] if people.exists(key) else {}
        box = holder.get("toolbox")
        for name, pick in (tools.get("servers") or {}).items():
            pick = dict(pick or {})
            pick.pop("approved", None)  # the console can't set approvals: they're taken here, from the listing
            if pick.get("skills") is not None:
                was = before.get(name) or {}
                ticked_before = None if was.get("skills") is None else set(was["skills"])
                held, listed = was.get("approved") or {}, (box.fingerprints(name) if box else {})
                approved = {}
                for uri in pick["skills"]:
                    if ticked_before is not None and uri in ticked_before:
                        if uri in held:
                            approved[uri] = held[uri]
                    elif listed.get(uri):
                        approved[uri] = listed[uri]
                pick["approved"] = approved
            tools["servers"][name] = pick
        return tools

    @app.put("/api/personalities/{key}")
    def save_personality(key: str, payload: dict) -> dict:
        """Create a personality, or change any of: name, prompt, delivery, greeting, voice, tools."""
        key = key.strip().lower()
        try:
            if isinstance(payload.get("tools"), dict):
                payload = {**payload, "tools": bind_approvals(key, dict(payload["tools"]))}
            saved = people.save(key, payload)
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e)) from e
        if key == state["personality"]:
            robot.apply_voice(key)
            rebuild(f"{saved['name']} changed")
        return get_personalities()

    @app.post("/api/personalities/{key}/approve")
    def approve_skill(key: str, payload: dict) -> dict:
        """Approve a ticked skill again as its server lists it now: {server, uri}. For a skill whose files
        changed after it was ticked."""
        server, uri = str(payload.get("server", "")), str(payload.get("uri", ""))
        mark = toolbox().fingerprints(server).get(uri)
        if mark is None:
            raise HTTPException(400, "That server doesn't list that skill with its files' digests, so there's nothing "
                                     "to approve. Ask the server again, or switch on all of its skills.")
        try:
            people.approve(personality(key), server, uri, mark)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if key == state["personality"]:
            rebuild(f"{uri.removesuffix('/SKILL.md').rsplit('/', 1)[-1]} approved")
        return get_personalities()

    @app.post("/api/personalities/{key}/reset")
    def reset_personality(key: str) -> dict:
        """A built-in personality back to how it shipped (its voice clips stay)."""
        people.reset(personality(key))
        if key == state["personality"]:
            robot.apply_voice(key)
            rebuild(f"{people.get(key)['name']} reset")
        return get_personalities()

    @app.delete("/api/personalities/{key}")
    def delete_personality(key: str) -> dict:
        try:
            people.delete(personality(key))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if state["personality"] == key:
            state["personality"] = settings["personality"] = DEFAULT_PERSONALITY
            robot.apply_voice(DEFAULT_PERSONALITY)
            save()
        return get_personalities()

    @app.post("/api/personality")
    def use_personality(payload: dict) -> dict:
        """Switch personality: its voice from the next sentence, its prompt and tools once the session restarts
        (a couple of seconds)."""
        key = str(payload.get("key", "")).lower()
        if people.exists(key) and key != state["personality"]:
            state["personality"] = settings["personality"] = key
            save()
            busy()
            robot.apply_voice(key)
            log.add("system", text=f"now {people.get(key)['name']}…")
        return get_state()

    # --- a personality's own voice (the voice add-in) --------------------------------------------

    def voice_changed(key: str) -> str:
        """After a clip, transcript or catchphrase change: use it now, and teach the voice server now so a clip
        it can't use shows up here rather than mid-conversation."""
        if state["personality"] == key:
            robot.apply_voice(key)
        ref = people.voice_ref(key, fallback=False)
        if ref is None or ref.builtin:
            return "Saved. Add a clip and its transcript, and set the voice to Cloned, to speak with it."
        if not speaker.clone.url_for(ref):
            return "Saved. No voice server that can clone is answering now: it learns the voice when one does."
        try:
            seconds = speaker.clone.ensure_voice(ref)
        except VoiceError as e:
            raise HTTPException(400, f"Saved, but the voice server can't use this clip: {e}") from e
        except requests.RequestException as e:
            return f"Saved. The voice server didn't answer ({e}); it learns the voice next time."
        if seconds:
            people.note_seconds(key, seconds)
        return "Saved, and the voice server learned the voice" + (f" ({seconds:.1f} s clip)." if seconds else ".")

    @app.get("/api/personalities/{key}/file/{name}")
    def personality_file(key: str, name: str):
        """A personality's voice clip or catchphrase, for the console's players."""
        try:
            return FileResponse(people.file(personality(key), name))
        except (FileNotFoundError, ValueError) as e:
            raise HTTPException(404, "no such file") from e

    @app.put("/api/personalities/{key}/clip")
    def set_clip(key: str, payload: dict) -> dict:
        """A new voice clip ({filename, data: base64}) and/or its transcript ({ref_text})."""
        data = base64.b64decode(payload["data"]) if payload.get("data") else None
        try:
            people.set_clip(personality(key), str(payload.get("ref_text", "")), payload.get("filename"), data)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {**get_personalities(), "notice": voice_changed(key)}

    @app.post("/api/personalities/{key}/clip/versions/{version}/restore")
    def restore_clip(key: str, version: str) -> dict:
        """Load a saved clip (and its transcript) back in; the current one becomes a saved version."""
        try:
            people.restore_clip(personality(key), version)
        except ValueError as e:
            raise HTTPException(404, str(e)) from e
        return {**get_personalities(), "notice": voice_changed(key)}

    @app.delete("/api/personalities/{key}/clip/versions/{version}")
    def delete_clip_version(key: str, version: str) -> dict:
        people.delete_clip_version(personality(key), version)
        return get_personalities()

    def sounds_changed(key: str) -> str:
        """After a sound clip was added or removed: the model is told its clips by name, so the session in use
        reloads; and a voice server that clones this voice learns the clip as a line to play as recorded."""
        if state["personality"] == key:
            rebuild(f"{people.get(key)['name']}'s sound clips changed")
        ref = people.voice_ref(key, fallback=False)
        return voice_changed(key) if ref is not None and not ref.builtin else "Saved."

    @app.post("/api/personalities/{key}/catchphrases")
    def add_catchphrase(key: str, payload: dict) -> dict:
        """A sound clip, which the model plays with [sound:<name>]: {text, filename, data: base64, name?}. With a
        cloned voice it also stands in for that exact line."""
        try:
            people.add_catchphrase(personality(key), str(payload.get("text", "")), payload.get("filename", ""),
                                   base64.b64decode(payload.get("data") or ""), name=str(payload.get("name") or ""))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {**get_personalities(), "notice": sounds_changed(key)}

    @app.delete("/api/personalities/{key}/catchphrases/{index}")
    def delete_catchphrase(key: str, index: int) -> dict:
        people.delete_catchphrase(personality(key), index)
        return {**get_personalities(), "notice": sounds_changed(key)}

    @app.post("/api/personalities/{key}/preview")
    def preview_voice(key: str, payload: dict) -> Response:
        """One line in this personality's voice-server voice, as a WAV for the console to play (Gizmo stays
        quiet). {"builtin": {...}} tries unsaved built-in voice settings."""
        text = str(payload.get("text", "")).strip()[:300] or "Testing, one two three."
        ref = people.voice_ref(personality(key))
        if isinstance(payload.get("builtin"), dict):
            ref = VoiceRef(id=key, ref_text="", audio=None, catchphrases=(), hash="preview", builtin=payload["builtin"])
        if ref is None or not speaker.clone.url_for(ref):
            raise HTTPException(503, "no voice server that can speak this voice is answering (Settings → Add-ins)")
        try:
            pcm = speaker.clone.synth(text, ref)
        except VoiceError as e:
            raise HTTPException(400, f"the voice server can't use this clip: {e}") from e
        except requests.RequestException as e:
            raise HTTPException(502, f"the voice server failed: {e}") from e
        return Response(wav_bytes(pcm, 16000), media_type="audio/wav",
                        headers={"X-Gen-Ms": f"{speaker.clone.last_gen_ms or 0:.0f}"})

    @app.get("/api/personalities/{key}/preview")
    def preview_voice_url(key: str, text: str = "", builtin: str = "") -> Response:
        """The same preview as a URL an <audio> element plays directly. The console starts it inside the click:
        an embedded browser may not let a sound start once the click has been followed by a fetch."""
        payload: dict = {"text": text}
        if builtin:
            try:
                payload["builtin"] = json.loads(builtin)
            except ValueError as e:
                raise HTTPException(400, "builtin isn't JSON") from e
        return preview_voice(key, payload)

    # --- MCP servers and skills ------------------------------------------------------------------

    def used_by() -> dict[str, list[str]]:
        """server -> the personalities that have it switched on."""
        out: dict[str, list[str]] = {}
        for key, name in people.names().items():
            for server in people.selection(key).servers:
                out.setdefault(server, []).append(name)
        return out

    @app.get("/api/servers")
    def get_servers() -> dict:
        box = toolbox()
        return {"servers": box.servers(), "local_skills": box.local_skills(), "used_by": used_by(),
                "active": state["personality"], "active_name": people.get(state["personality"])["name"]}

    @app.post("/api/servers/test")
    def test_server(payload: dict) -> dict:
        """Connect to a server once and say what it offers, without keeping it."""
        return toolbox().test_server(payload)

    @app.put("/api/servers")
    def save_server(payload: dict) -> dict:
        """Add or change a server ({"server": {name, url, headers}, "original_name"}). A new one is switched on
        for the personality in use, so you can talk to it straight away."""
        box, original = toolbox(), payload.get("original_name")
        try:
            name = box.save_server(payload.get("server") or {}, original)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if original and original != name:
            people.forget(server=original, renamed_to=name)
        elif not original:
            people.use(state["personality"], server=name)
        box.reload()
        rebuild(f"MCP server {name} saved")
        return get_servers()

    @app.delete("/api/servers/{name}")
    def delete_server(name: str) -> dict:
        box = toolbox()
        box.delete_server(name)
        people.forget(server=name)
        box.reload()
        rebuild(f"MCP server {name} removed")
        return get_servers()

    @app.post("/api/servers/{name}/refresh")
    def refresh_server(name: str) -> dict:
        """Ask a server again for its tools and skills (after you change one on it)."""
        box = toolbox()
        try:
            box.refresh(name)
        except KeyError:
            box.reload()  # it wasn't connected: try the lot again
        except Exception as e:
            raise HTTPException(502, f"{name} didn't answer: {type(e).__name__}: {e}") from e
        rebuild(f"{name} refreshed")
        return get_servers()

    @app.post("/api/servers/{name}/call")
    def call_tool(name: str, payload: dict) -> dict:
        """Run one of a server's tools by hand: {"tool": "...", "arguments": {...}}."""
        try:
            return toolbox().call_tool(name, str(payload.get("tool", "")), payload.get("arguments") or {})
        except KeyError as e:
            raise HTTPException(404, f"{name} isn't connected") from e
        except Exception as e:
            return {"ok": False, "ms": None, "text": f"{type(e).__name__}: {e}"}

    @app.post("/api/servers/{name}/skill")
    def read_skill(name: str, payload: dict) -> dict:
        """Fetch one of a server's skills and check it, exactly as when the model loads it: {"uri": "skill://..."}.
        Says whether it matched the listing; a failure is an answer too, not an error."""
        host = toolbox().hosts.get(name)
        if host is None:
            raise HTTPException(404, f"{name} isn't connected")
        uri = str(payload.get("uri", ""))
        try:
            entry = next((s for s in toolbox().skills.get(name, []) if s.uri == uri), None) or host.get(uri)
            loaded = host.load(entry)
        except SkillError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "text": loaded.text, "verified": loaded.verified, "bytes": len(loaded.text.encode()),
                "entry": loaded.entry.as_dict()}  # the server's current entry, if the skill changed since listing

    @app.post("/api/servers/{name}/file")
    def read_skill_file(name: str, payload: dict) -> dict:
        """One of a skill's other files: {"skill": "<its SKILL.md uri>", "uri": "<the file>"}."""
        host = toolbox().hosts.get(name)
        entry = next((s for s in toolbox().skills.get(name, []) if s.uri == payload.get("skill")), None)
        if host is None or entry is None:
            raise HTTPException(404, "no such skill")
        try:
            return {"ok": True, "text": host.read_file(entry, str(payload.get("uri", "")))}
        except SkillError as e:
            return {"ok": False, "error": str(e)}

    @app.get("/api/skills/{name}")
    def get_skill(name: str) -> dict:
        try:
            return {"name": name, "content": toolbox().read_local_skill(name)}
        except KeyError as e:
            raise HTTPException(404, f"no skill called {name} on this robot") from e

    @app.put("/api/skills/{name}")
    def put_skill(name: str, payload: dict) -> dict:
        """Create or replace a skill kept on the robot: {"content": "<the SKILL.md>"}. A new one is switched on
        for the personality in use."""
        box = toolbox()
        new = name not in {s["name"] for s in box.local_skills()}
        try:
            box.write_local_skill(name, str(payload.get("content", "")))
        except Exception as e:
            raise HTTPException(400, str(e)) from e
        if new:
            people.use(state["personality"], skill=name)
        rebuild(f"skill {name} saved")
        return get_servers()

    @app.delete("/api/skills/{name}")
    def delete_skill(name: str) -> dict:
        try:
            toolbox().delete_local_skill(name)
        except KeyError as e:
            raise HTTPException(404, f"no skill called {name} on this robot") from e
        people.forget(skill=name)
        rebuild(f"skill {name} deleted")
        return get_servers()

    @app.post("/api/skills/upload")
    def upload_skill(payload: dict) -> dict:
        """A packaged skill ({filename, data: base64}): a .skill or .zip of a skill folder, or a SKILL.md."""
        try:
            name = toolbox().install(str(payload.get("filename", "")), base64.b64decode(payload.get("data") or ""))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        people.use(state["personality"], skill=name)
        rebuild(f"skill {name} installed")
        return {**get_servers(), "installed": name}

    # --- settings ----------------------------------------------------------------------------------

    @app.get("/api/settings")
    def get_settings() -> dict:
        return {
            "model": model_view(),
            "speech": state["speech"], "speech_choices": SPEECH,
            "addons": {"voice": bool(speaker.clone.servers or speaker.clone.live_macs()), "box": bool(CONFIG["box_hosts"])},
            "voice_servers": speaker.clone.servers,
            "clone": {"url": speaker.clone.url, "where": speaker.clone.where(), "prefer": speaker.clone.prefer,
                      "laptops": speaker.clone.live_macs(), "last_gen_ms": speaker.clone.last_gen_ms,
                      "nfe": speaker.clone.nfe},
            "listening": listening.state(),
            "ears_end_ms": int(settings.get("ears_end_ms", EARS_END_MS)),
            "barge_in": bool(settings.get("barge_in", True)),
            "greet": bool(settings.get("greet", True)),
            "show_ignored": bool(settings.get("show_ignored", False)),
            "face_speaker": bool(settings.get("face_speaker", True)),
            "boost_db": speaker.boost_db,
            "audio": speaker.levels(),
            "versions": {"strands-agents": version("strands-agents"), "mcp": version("mcp"),
                         "gizmo": version("gizmo-robot")},
            "aws": {"profile": CONFIG["aws_profile"] or "default", "region": CONFIG["aws_region"]},
        }

    @app.post("/api/settings/model")
    def set_model(payload: dict) -> dict:
        """Which model thinks: {"provider": "bedrock"|"openai"|"agentcore", "bedrock": {"model"},
        "openai": {"base_url", "model", "api_key", "params"}, "agentcore": {"arn"}}. Each provider keeps its own
        settings, so switching back finds them."""
        chosen = dict(settings.get("model") or {})
        provider = str(payload.get("provider") or chosen.get("provider") or "bedrock")
        if provider not in MODEL_PROVIDERS:
            raise HTTPException(400, f"the provider is one of {', '.join(MODEL_PROVIDERS)}")
        if isinstance(payload.get("bedrock"), dict):
            chosen["bedrock"] = {"model": str(payload["bedrock"].get("model") or HAIKU).strip()}
        if isinstance(payload.get("openai"), dict):
            new, old = payload["openai"], chosen.get("openai") or {}
            url, key = str(new.get("base_url") or "").strip().rstrip("/"), str(new.get("api_key") or "")
            if url and not url.startswith(("http://", "https://")):
                raise HTTPException(400, "the base URL starts with http:// or https:// (and usually ends in /v1)")
            if not isinstance(new.get("params") or {}, dict):
                raise HTTPException(400, "extra parameters are a JSON object, like {\"reasoning_effort\": \"none\"}")
            chosen["openai"] = {"base_url": url, "model": str(new.get("model") or "").strip(),
                                "api_key": old.get("api_key", "") if key.startswith(MASK) else key,
                                "params": new.get("params") or {}}
        if isinstance(payload.get("agentcore"), dict):
            arn = str(payload["agentcore"].get("arn") or "").strip()
            if arn and not arn.startswith("arn:aws:bedrock-agentcore:"):
                raise HTTPException(400, "that isn't an AgentCore runtime ARN (arn:aws:bedrock-agentcore:...)")
            chosen["agentcore"] = {"arn": arn}
        if provider == "openai" and not (chosen.get("openai") or {}).get("model"):
            raise HTTPException(400, "name the model, as the endpoint calls it")
        chosen["provider"] = provider
        settings["model"] = chosen
        save()
        rebuild(f"model: {model_spec(settings).label} on {MODEL_PROVIDERS[provider]}")
        return get_settings()

    @app.post("/api/settings/model/test")
    def test_model(payload: dict) -> dict:
        """Does an OpenAI-compatible endpoint answer, and which models does it have? {"base_url", "api_key"}."""
        saved = (settings.get("model") or {}).get("openai") or {}
        key = str(payload.get("api_key") or "")
        spec = ModelSpec("openai", "", str(payload.get("base_url") or "").strip(),
                         saved.get("api_key", "") if key.startswith(MASK) else key)
        url = openai_url(spec)
        if url is None:
            return {"ok": False, "error": "nothing answered there"}
        try:
            resp = requests.get(f"{url}/models", timeout=4,
                                headers={"Authorization": f"Bearer {spec.api_key}"} if spec.api_key else {})
            resp.raise_for_status()
            return {"ok": True, "url": url, "models": sorted(m["id"] for m in resp.json().get("data", []))}
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            return {"ok": False, "url": url, "error": f"it answered, but not with a model list: {e}"[:200]}

    @app.post("/api/settings/speech")
    def set_speech(payload: dict) -> dict:
        """Who hears and speaks: "nova" (Nova Sonic) or "local" (the home-box add-in: its ears and a voice server)."""
        speech = str(payload.get("speech", ""))
        if speech in SPEECH and speech != state["speech"] and (speech == "nova" or CONFIG["box_hosts"]):
            state["speech"] = settings["speech"] = speech
            save()
            rebuild(f"speech: {SPEECH[speech]}")
        return get_settings()

    @app.post("/api/boost")
    def boost(payload: dict) -> dict:
        db = max(0.0, min(15.0, float(payload.get("db", 0))))
        speaker.boost_db = settings["boost_db"] = db
        save()
        log.add("system", text=f"voice boost {db:g} dB")
        return get_settings()

    @app.post("/api/greet")
    def greet(payload: dict) -> dict:
        settings["greet"] = bool(payload.get("on"))
        save()
        return get_settings()

    @app.post("/api/face-speaker")
    def set_face_speaker(payload: dict) -> dict:
        """Look at whoever speaks to it: {"on": bool}. {"flip": true} when it turns the wrong way: which side
        of the mic array is left differs between robots, so it's set once by trying it."""
        if "on" in payload:
            settings["face_speaker"] = bool(payload["on"])
            log.add("system", text="looks at whoever speaks to it" if settings["face_speaker"] else "no longer turns toward voices")
        if payload.get("flip"):
            settings["doa_sign"] = -1.0 if float(settings.get("doa_sign", 1.0)) >= 0 else 1.0
            log.add("system", text="left and right swapped for turning toward a voice")
        save()
        return get_settings()

    @app.post("/api/listening")
    def set_listening(payload: dict) -> dict:
        """Free talk, or a turn needs Gizmo's name: {"mode": "free"|"wake", "names": [...], "follow_up_s"}.
        Takes effect at once."""
        mode = str(payload.get("mode", listening.mode))
        if mode in LISTEN_MODES:
            listening.mode = settings["listen_mode"] = mode
        if isinstance(payload.get("names"), list):
            names = [str(n).strip()[:30] for n in payload["names"] if str(n).strip()][:8]
            if names:
                listening.names = settings["wake_names"] = names
        if payload.get("follow_up_s") is not None:
            listening.follow_up_s = settings["follow_up_s"] = max(0.0, min(120.0, float(payload["follow_up_s"])))
        if "show_ignored" in payload:  # the words of speech that wasn't for Gizmo: kept only when asked for
            settings["show_ignored"] = bool(payload["show_ignored"])
        save()
        log.add("system", text="listening: free talk" if listening.mode == "free" else
                f"listening: wake word ({', '.join(listening.names)}), follow-ups for {listening.follow_up_s:g} s")
        return get_settings()

    @app.post("/api/barge-in")
    def barge_in(payload: dict) -> dict:
        """Home-box ears: may people talk over Gizmo (it stops to listen)? Takes effect at once."""
        on = settings["barge_in"] = bool(payload.get("on"))
        save()
        agent = holder.get("agent")
        if isinstance(agent, LocalSession):
            agent.barge_in = on
        log.add("system", text=f"interrupting Gizmo {'allowed' if on else 'off: it finishes before listening'}")
        return get_settings()

    @app.post("/api/ears")
    def ears_config(payload: dict) -> dict:
        """Home-box ears: how long a pause ends the person's turn (ms). Shorter is snappier but can cut people off."""
        ms = settings["ears_end_ms"] = max(300, min(2000, int(payload.get("end_silence_ms", EARS_END_MS))))
        save()
        agent = holder.get("agent")
        if isinstance(agent, LocalSession):
            on_loop(agent.set_end_silence(ms))
        log.add("system", text=f"end of turn after {ms} ms of silence")
        return get_settings()

    # --- the voice add-in's servers ----------------------------------------------------------------

    @app.post("/api/clone/announce")
    def clone_announce(payload: dict) -> dict:
        """A laptop's voice server (addons/voice, started with --robot) announcing its address every 10 s."""
        url = str(payload.get("url", ""))
        if url.startswith("http://"):
            speaker.clone.announce(url)
        return {"ok": True}

    @app.post("/api/voice-servers")
    def set_voice_servers(payload: dict) -> dict:
        """Where the voice add-in's servers are: {"urls": ["http://host:8770", ...]}, or [] for none (Nova only).
        Each is checked, and the reply says what it can do (cloning, built-in voices)."""
        urls = [str(u).strip().rstrip("/") for u in payload.get("urls") or [] if str(u).strip()]
        bad = [u for u in urls if not u.startswith(("http://", "https://"))]
        if bad:
            raise HTTPException(400, f"a voice server address starts with http:// or https://: {bad[0]}")
        settings["voice_servers"] = urls
        save()
        speaker.clone.set_servers(urls)
        robot.apply_voice(state["personality"])  # find a server and teach it the personality's voice now
        tested = {u: speaker.clone.info(u) for u in urls}
        ok = [u for u, t in tested.items() if t["ok"]]
        log.add("system", text=f"voice servers: {', '.join(urls) or 'none (Nova speaks)'}"
                               + (f" ({len(ok)} answering)" if urls else ""))
        return {**get_settings(), "tested": tested}

    @app.post("/api/clone/prefer")
    def clone_prefer(payload: dict) -> dict:
        """Which voice server to use first: auto (the home box at home, else a laptop), box or mac."""
        prefer = str(payload.get("prefer", ""))
        if prefer in PREFER:
            speaker.clone.set_prefer(prefer)
            settings["clone_prefer"] = prefer
            save()
            robot.apply_voice(state["personality"])
            log.add("system", text=f"voice server: {prefer}")
        return get_settings()

    @app.post("/api/clone/quality")
    def clone_quality(payload: dict) -> dict:
        """F5 steps for a cloned voice: 16 is fastest (~0.7 s a sentence on a 3090), 32 the best (~1.1 s)."""
        nfe = speaker.clone.nfe = settings["clone_nfe"] = max(8, min(64, int(payload.get("nfe", 16))))
        save()
        log.add("system", text=f"cloned voice: {nfe} steps")
        return get_settings()

    # --- health --------------------------------------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict:
        """Is each piece answering?"""
        def probe(url: str) -> bool:
            try:
                return requests.get(url, timeout=1.5).ok
            except requests.RequestException:
                return False

        spec = model_spec(settings)
        motors = None
        if holder.get("daemon_url"):
            try:
                motors = requests.get(f"{holder['daemon_url']}/api/motors/status", timeout=2).json().get("mode")
            except (requests.RequestException, ValueError):
                pass
        endpoint = openai_url(spec) if spec.provider == "openai" else None
        return {
            "session": {"ok": state["session"] is not None, "mode": state["session"], "status": state["status"],
                        "error": state["error"]},
            "model": {"provider": spec.provider, "model": spec.model, "ok": spec.provider == "bedrock" or endpoint is not None,
                      "url": endpoint},
            "servers": holder["toolbox"].summary()["servers"] if "toolbox" in holder else {},
            "robot": not state.get("desk"),  # False: there are no motors or power to report
            "motors": motors,
            "power": holder["power"].state if "power" in holder else None,
            "voice_server": {"ok": bool(speaker.clone.url) and probe(f"{speaker.clone.url}/health"),
                             "url": speaker.clone.url} if (speaker.clone.servers or speaker.clone.live_macs()) else None,
            "box_ears": {"ok": (ears := ears_url()) is not None, "url": ears} if CONFIG["box_hosts"] else None,
        }

    # --- hide and reveal, shut down and start up -------------------------------------------------------

    def do_hide() -> None:
        speaker.clear()
        holder["mic_was_muted"] = mic.muted
        mic.muted = True
        log.add("system", text="hiding: going to the power-down pose")
        try:
            body.hide()
            log.add("system", text="asleep: hidden and quiet until you reveal Gizmo")
        except Exception as e:
            log.add("power", text=f"Couldn't finish the sleep pose: {e!r}")

    def do_reveal(greet: bool) -> None:
        log.add("system", text="revealing: waking up")
        try:
            body.reveal()
        except Exception as e:
            log.add("power", text=f"Couldn't finish waking up: {e!r}")
        mic.muted = holder.pop("mic_was_muted", False)
        state.update(asleep=False, status="listening")
        settings["asleep"] = False
        save()
        if greet:
            holder["greet"] = True  # the session loop sends the personality's introduction once it's listening

    @app.post("/api/hide")
    def hide() -> dict:
        """Into the power-down pose and quiet (mic off, the cloud paused) until Reveal: for unveiling Gizmo."""
        if body is not None and not state["asleep"] and not state["off"]:
            body.want_asleep = True
            state.update(asleep=True, status="asleep")
            settings["asleep"] = True
            save()
            threading.Thread(target=do_hide, name="hide", daemon=True).start()
        return get_state()

    @app.post("/api/reveal")
    def reveal(payload: dict | None = None) -> dict:
        """Wake up and listen again; greet (default: the console setting) opens with a line in character."""
        greet = (payload or {}).get("greet", settings.get("greet", True))
        if body is not None and state["asleep"] and not state["off"]:
            body.want_asleep = False
            threading.Thread(target=do_reveal, args=(bool(greet),), name="reveal", daemon=True).start()
        return get_state()

    def off_step(name: str, ok: bool | None, detail: str) -> None:
        """One line of the console's Shut down / Start up checklist (ok None: nothing to do)."""
        state["off_steps"].append({"name": name, "ok": ok, "detail": detail})

    def switch(run, what: str) -> None:
        """Run Shut down or Start up; whatever happens, it ends, so the console never sticks on it."""
        try:
            run()
        except Exception as e:
            logger.exception(f"{what} failed")
            off_step(what, False, f"stopped on an error: {e!r}")
        finally:
            state["switching"] = None

    def do_shutdown() -> None:
        speaker.clear()
        mic.muted = True
        log.add("system", text="shutting down: the cloud session, then the robot")
        # The session loop sees state["off"] and closes the session; wait for it to say so.
        deadline = time.monotonic() + 15
        while state["session"] is not None and time.monotonic() < deadline:
            time.sleep(0.2)
        off_step("Cloud", state["session"] is None, "no session open, so nothing calls AWS"
                 if state["session"] is None else f"the {state['session']} session is still closing")
        if state["asleep"]:
            off_step("Gizmo", True, "asleep with the motors off (it was hidden)")
        else:
            body.want_asleep = True
            try:
                body.hide()
                off_step("Gizmo", True, "asleep, motors off, mic muted")
            except Exception as e:
                off_step("Gizmo", False, f"couldn't finish the sleep pose: {e!r}")
        if speaker.clone.servers:
            off_step("Voice server", None, "still running, left on (it costs nothing)"
                     if power_switch.box_voice_up(speaker.clone.servers) else "not running")
        if speaker.clone.live_macs():
            off_step("Laptop voice", *power_switch.quit_macs(speaker.clone.live_macs()))
        log.add("system", text="off: no cloud session, nothing billing. Start up brings it all back")

    def do_startup() -> None:
        log.add("system", text="starting up: robot, then the session")
        state["off"] = settings["off"] = False  # the session loop opens a session again
        save()
        if state["asleep"]:
            off_step("Gizmo", True, "still hidden: Reveal wakes it")
        else:
            body.want_asleep = False
            try:
                body.reveal()
                off_step("Gizmo", True, "awake, motors on")
            except Exception as e:
                off_step("Gizmo", False, f"couldn't finish waking up: {e!r}")
            mic.muted = False
        if speaker.clone.servers:
            up = power_switch.box_voice_up(speaker.clone.servers)
            off_step("Voice server", True if up else None, "answering" if up else "not running")
        deadline = time.monotonic() + 45
        while state["status"] not in ("listening", "asleep", "error") and time.monotonic() < deadline:
            time.sleep(0.3)
        where = {"local": "home box ears and voice", "claude": "Nova Sonic, the brain on the robot",
                 "agentcore": "Nova Sonic, the brain on AgentCore"}
        off_step("Session", state["status"] in ("listening", "asleep"),
                 f"{where.get(state['session'], state['session'])}: {state['status']}" if state["session"]
                 else "hidden: no cloud session until Reveal" if state["asleep"] else f"not open yet ({state['status']})")

    @app.post("/api/shutdown")
    def shutdown() -> dict:
        """Everything off: no session (so nothing calls AWS), asleep with the motors off, mic muted. Survives app
        restarts and reboots, until Start up."""
        if body is not None and not state["off"] and not state["switching"]:
            state.update(off=True, status="off", switching="down", off_steps=[])
            settings["off"] = True
            save()
            threading.Thread(target=switch, args=(do_shutdown, "Shut down"), name="shutdown", daemon=True).start()
        return get_state()

    @app.post("/api/startup")
    def startup() -> dict:
        """Back on: the robot awake (unless hidden), then the session."""
        if body is not None and state["off"] and not state["switching"]:
            state.update(switching="up", off_steps=[])
            threading.Thread(target=switch, args=(do_startup, "Start up"), name="startup", daemon=True).start()
        return get_state()
