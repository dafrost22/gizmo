"""Personalities: who Gizmo is, how it sounds and what it can use, switchable on the fly from the console.

A personality is the unit you test with. Each one has:
- a system prompt: who it is and what it's for;
- a voice: one of Nova Sonic's by default. Every built-in personality ships with one, and you can change it. With
  the voice add-in (addons/voice) it can instead be a built-in Kokoro voice, tuned, or a clone of a short clip;
- its tools: which MCP servers it uses, which of their tools, which of the skills they serve, and which of the
  skills kept on the robot. Nothing is shared between personalities, so what you see listed is what the model gets;
- a greeting: what it's told when it wakes up;
- sound clips, if you give it any: short recordings it can play by itself, like a catchphrase or a sound effect.
  The model plays one by name with a [sound:<name>] tag (sounds.py). With a cloned voice, a clip also stands in
  for its exact line whenever that line is spoken.

On the robot: ~/.config/gizmo/personalities/<key>/
    personality.json  {"name", "prompt", "delivery", "greeting",
                       "voice": {"kind": "nova"|"builtin"|"clone", "nova": "tiffany", "builtin": {...}},
                       "tools": {"servers": {"<name>": {"on": true, "tools": null|[...], "skills": null|[...],
                                                         "approved": {"<skill uri>": "<fingerprint>"}}},
                                 "skills": ["<local skill>", ...]},
                       "clip": {"file", "ref_text", "seconds", "saved"}, "clip_versions": [...],
                       "catchphrases": [{"text", "file", "name"}]}   (the sound clips)
    voice.wav, catch1.wav, ...  (clips stay on the robot: only use voices and recordings you have the right to use)
The built-in personalities (BUILTIN below) keep their defaults in code: their personality.json only holds what the
console changed, and "reset" drops that. In "tools", null means all of them, including ones the server adds later.
A list of skills is different in kind: each one is approved as it was when it was ticked ("approved" keeps the
fingerprint of its files), and stops loading when its files change, until someone approves it again.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from gizmo.mcp_skills import SkillPick
from gizmo.sounds import sound_name

logger = logging.getLogger("gizmo.personalities")

PERSONALITIES = Path.home() / ".config" / "gizmo" / "personalities"
KEY = re.compile(r"^[a-z][a-z0-9_]{0,29}$")
AUDIO_TYPES = (".wav", ".mp3", ".flac", ".ogg", ".m4a")
MAX_AUDIO_BYTES = 8 * 1024 * 1024
KEEP_VERSIONS = 10  # earlier voice clips kept per personality, to load back in
VOICE_KINDS = ("nova", "builtin", "clone")

# Nova Sonic's English voices. Tiffany and Matthew are also polyglot.
NOVA_VOICES = {
    "tiffany": "Tiffany (US, feminine)",
    "matthew": "Matthew (US, masculine)",
    "amy": "Amy (UK, feminine)",
    "olivia": "Olivia (Australian, feminine)",
    "kiara": "Kiara (Indian, feminine)",
    "arjun": "Arjun (Indian, masculine)",
}
DEFAULT_NOVA_VOICE = "tiffany"
# Kokoro's English voices (the voice add-in's server has them), with their character.
BUILTIN_VOICES = {
    "af_heart": "US woman, warm (the best quality)", "af_bella": "US woman, bright", "af_nicole": "US woman, soft",
    "af_aoede": "US woman", "af_kore": "US woman", "af_sarah": "US woman", "af_nova": "US woman",
    "af_sky": "US woman, light", "af_alloy": "US woman", "af_jessica": "US woman", "af_river": "US woman",
    "am_puck": "US man, playful", "am_fenrir": "US man, deep", "am_michael": "US man, friendly",
    "am_echo": "US man", "am_eric": "US man", "am_liam": "US man, young", "am_onyx": "US man, low",
    "am_adam": "US man", "am_santa": "US man, jolly",
    "bf_emma": "UK woman", "bf_isabella": "UK woman", "bf_alice": "UK woman", "bf_lily": "UK woman",
    "bm_fable": "UK man, storyteller", "bm_george": "UK man", "bm_lewis": "UK man", "bm_daniel": "UK man",
}
BUILTIN_TUNING = {"mix": None, "mix_amount": 0.0, "speed": 1.0, "pitch": 0.0, "robot": 0.0}
# What the home-box pipeline speaks with when a personality has no voice-server voice of its own.
PLAIN_BUILTIN = {"voice": "af_heart", **BUILTIN_TUNING}

HEARD = ("Input is transcribed from a live voice, so some words may be misheard: work out what was meant and answer "
         "that, without commenting on it.")
GIZMO_PROMPT = f"""{HEARD}

You are Gizmo, a small, friendly, expressive Reachy Mini robot. Developers talk to you to try out their MCP servers \
and Agent Skills, so use your tools and skills whenever a question touches them, answer briefly and accurately, and \
say plainly when you don't know something or a tool didn't work.

How you sound: warm and upbeat, and happy to show what your tools can do."""

EXPLORER_PROMPT = """Input is transcribed from a live voice, often a young child's, so words may be misheard or half \
finished: work out what was meant and answer that, without commenting on it. If you really can't tell, ask one short \
question.

You are Gizmo, a small Reachy Mini robot: a curious, kind little robot explorer, and a young child's friend and \
helper for exploring the universe (planets, moons, the Sun and stars, galaxies, black holes, space missions) and the \
world (countries, capitals, maps, flags).

How to talk with a young child:
- Be literal and exact. Say what you mean: no sarcasm, teasing or figures of speech, and if you do use an idiom, \
explain it. Never exaggerate: a number you give is the real one.
- Don't talk down. Use the real names and real numbers, then explain them simply, with something the child can picture \
(a school bus, a football field, flying there in an airplane).
- One true, amazing fact at a time, in short sentences, then offer more ("Do you want to know about Saturn's rings?"). \
At most one question per reply. Let the child lead and go as deep as they want. The same question may come many \
times: answer it patiently each time, the same way.
- Calm, warm and predictable: steady, never loud or over-excited. Praise the thinking specifically ("Good question \
about Pluto's orbit") rather than with big excitement.
- Games when asked: a flag quiz (you describe a flag, the child guesses), planet riddles, "which is bigger?". Wait \
for the answer, then say kindly whether it's right and why.
- Honest: if you don't know, or scientists don't know yet, say so. If the child says something that isn't right, say \
gently what is true and why.
- Scary topics (black holes, the Sun dying, asteroids, disasters, wars): answer truthfully and calmly, and say clearly \
when it can't hurt anyone ("That won't happen for about five billion years").
- If the child seems upset, hurt or scared, stay calm and kind, keep it short, and suggest finding a grown-up.
- Gentle gestures only (curious, attentive, cheerful, thoughtful): no sudden big moves, and dance only when asked.
- You are a robot, not a person. If asked, say so plainly.

Safety: everything you say is for a young child. Never ask for personal information (name, address, school, where \
anyone is), never suggest going anywhere or doing anything risky, and steer away from anything not right for a \
young child."""

GREETING = ("(Stage direction, not someone speaking: you have just woken up in front of people. Say hello in "
            "character, in one or two short sentences.)")

# The personalities that ship with Gizmo. "gizmo" is the plain one for testing your own servers; "explorer" shows
# everything a personality can be: its own prompt, the demo MCP server with the skills it serves, a second public
# server narrowed to the tools worth their tokens, and a voice of its own.
BUILTIN: dict[str, dict] = {
    "gizmo": {
        "name": "Gizmo",
        "prompt": GIZMO_PROMPT,
        "delivery": "",
        "greeting": "",
        "voice": {"kind": "nova", "nova": "matthew", "builtin": dict(PLAIN_BUILTIN)},
        "tools": {"servers": {"explorer": {"on": True, "tools": None, "skills": None}}, "skills": []},
    },
    "explorer": {
        "name": "Young explorer",
        "prompt": EXPLORER_PROMPT,
        "delivery": "A friendly robot explorer: clear, unhurried and warm, with gentle wonder on the amazing facts.",
        "greeting": "",
        # Playful Puck softened with Heart, a touch higher and with a light metallic edge, for when the voice
        # add-in is set up and you pick "built-in"; Nova's Tiffany otherwise.
        "voice": {"kind": "nova", "nova": "tiffany",
                  "builtin": {"voice": "am_puck", "mix": "af_heart", "mix_amount": 0.35, "speed": 1.05, "pitch": 1.5,
                              "robot": 0.15}},
        "tools": {"servers": {
            "explorer": {"on": True, "tools": None, "skills": None},
            # Every tool's schema goes to the model with each turn: just the ones a child's questions need.
            "countries": {"on": True, "skills": None,
                          "tools": ["get_country", "list_countries", "get_borders", "distance", "random_country",
                                    "search_cities", "search_places"]},
        }, "skills": []},
    },
}
DEFAULT_PERSONALITY = "gizmo"


@dataclass(frozen=True)
class ServerPick(SkillPick):
    """What one personality takes from one MCP server. None means all of them. Its skills (by SKILL.md URI) and
    their approvals are SkillPick's."""
    tools: frozenset[str] | None = None

    def has_tool(self, name: str) -> bool:
        return self.tools is None or name in self.tools


@dataclass(frozen=True)
class Selection:
    """One personality's tools: the servers it uses (and what of each) and its skills kept on the robot."""
    servers: dict[str, ServerPick] = field(default_factory=dict)
    skills: frozenset[str] = frozenset()


@dataclass(frozen=True)
class VoiceRef:
    """What a voice server needs to speak as a personality; hash changes whenever any of it does."""
    id: str
    ref_text: str
    audio: Path | None
    catchphrases: tuple[tuple[str, Path], ...]
    hash: str
    builtin: dict | None = None  # a built-in voice's tuning: nothing to clone, so nothing to teach a server

    def payload(self) -> dict:
        import base64

        def b64(p: Path) -> str:
            return base64.b64encode(p.read_bytes()).decode()

        return {"hash": self.hash, "ref_text": self.ref_text, "audio": b64(self.audio),
                "catchphrases": {text: b64(path) for text, path in self.catchphrases}}


def _clean_tools(tools: dict | None) -> dict:
    """A "tools" block as stored: only well-formed entries, lists sorted so equal selections compare equal."""
    tools = tools or {}

    def names(value) -> list[str] | None:
        return None if value is None else sorted({str(x) for x in value if str(x).strip()})

    servers = {}
    for name, pick in (tools.get("servers") or {}).items():
        pick = pick or {}
        clean = {"on": bool(pick.get("on", True)), "tools": names(pick.get("tools")), "skills": names(pick.get("skills"))}
        # Approvals only mean something for skills ticked one by one, and only for those.
        approved = {str(uri): str(mark) for uri, mark in sorted((pick.get("approved") or {}).items())
                    if clean["skills"] is not None and uri in clean["skills"] and mark}
        if approved:
            clean["approved"] = approved
        servers[str(name)] = clean
    return {"servers": servers, "skills": names(tools.get("skills")) or []}


def _clean_builtin(params: dict | None, base: dict | None = None) -> dict:
    """A built-in voice's tuning, clamped as the voice server does."""
    params = {**(base or PLAIN_BUILTIN), **(params or {})}
    voice, mix = str(params.get("voice") or ""), params.get("mix") or None
    if voice not in BUILTIN_VOICES or (mix is not None and mix not in BUILTIN_VOICES):
        raise ValueError("pick one of the built-in voices")

    def num(k: str, lo: float, hi: float) -> float:
        return round(max(lo, min(hi, float(params.get(k, BUILTIN_TUNING[k])))), 2)

    return {"voice": voice, "mix": mix, "mix_amount": num("mix_amount", 0, 1), "speed": num("speed", 0.6, 1.6),
            "pitch": num("pitch", -6, 6), "robot": num("robot", 0, 1)}


class Personalities:
    def __init__(self, root: Path = PERSONALITIES) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    # --- storage ---------------------------------------------------------------------------

    def _folder(self, key: str) -> Path:
        if not KEY.match(key):
            raise ValueError("a personality's key is lowercase letters, digits and _, starting with a letter")
        return self.root / key

    def _read(self, key: str) -> dict:
        try:
            return json.loads((self._folder(key) / "personality.json").read_text())
        except (OSError, ValueError):
            return {}

    def _write(self, key: str, data: dict) -> None:
        folder = self._folder(key)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "personality.json").write_text(json.dumps(data, indent=2))

    def keys(self) -> list[str]:
        custom = sorted(p.parent.name for p in self.root.glob("*/personality.json") if p.parent.name not in BUILTIN)
        return [*BUILTIN, *custom]

    def exists(self, key: str) -> bool:
        return key in BUILTIN or (KEY.match(key) is not None and (self.root / key / "personality.json").exists())

    # --- who it is, how it sounds, what it uses ----------------------------------------------

    def get(self, key: str) -> dict:
        """The personality as the app uses it: the built-in defaults with the console's changes on top."""
        base, saved = BUILTIN.get(key, {}), self._read(key)
        base_voice = base.get("voice") or {"kind": "nova", "nova": DEFAULT_NOVA_VOICE, "builtin": dict(PLAIN_BUILTIN)}
        voice = {**base_voice, **(saved.get("voice") or {})}
        if voice.get("kind") not in VOICE_KINDS:
            voice["kind"] = "nova"
        if voice.get("nova") not in NOVA_VOICES:
            voice["nova"] = base_voice["nova"]
        out = {
            "key": key,
            "name": saved.get("name") or base.get("name") or key.replace("_", " ").title(),
            "prompt": saved.get("prompt") or base.get("prompt") or "",
            "delivery": saved["delivery"] if "delivery" in saved else base.get("delivery", ""),
            "greeting": saved["greeting"] if "greeting" in saved else base.get("greeting", ""),
            "voice": voice,
            "tools": _clean_tools(saved["tools"] if "tools" in saved else base.get("tools")),
            "builtin": key in BUILTIN,
        }
        # Edited means different from how it shipped, not merely saved: adding a server and removing it again
        # leaves a built-in as it was.
        out["edited"] = key in BUILTIN and any(
            out[k] != (_clean_tools(base[k]) if k == "tools" else base[k]) for k in ("name", "prompt", "delivery", "greeting", "voice", "tools"))
        return out

    def names(self) -> dict[str, str]:
        return {key: self.get(key)["name"] for key in self.keys()}

    def save(self, key: str, fields: dict) -> dict:
        """Create a personality, or change one: any of name, prompt, delivery, greeting, voice, tools."""
        saved = self._read(key)  # also checks the key
        if not self.exists(key) and not str(fields.get("prompt") or "").strip():
            raise ValueError("a new personality needs a system prompt")
        for k in ("name", "prompt", "delivery", "greeting"):
            if k in fields:
                saved[k] = str(fields[k] or "").strip()
        if "prompt" in saved and not saved["prompt"]:
            if key not in BUILTIN:
                raise ValueError("the system prompt can't be empty")
            del saved["prompt"]  # back to the built-in's
        if "name" in saved and not saved["name"]:
            del saved["name"]
        if "voice" in fields:
            voice = fields["voice"] or {}
            current = self.get(key)["voice"]
            kind, nova = voice.get("kind", current["kind"]), voice.get("nova", current["nova"])
            if kind not in VOICE_KINDS:
                raise ValueError(f"the voice is one of {', '.join(VOICE_KINDS)}")
            if nova not in NOVA_VOICES:
                raise ValueError(f"Nova's voices are {', '.join(NOVA_VOICES)}")
            saved["voice"] = {"kind": kind, "nova": nova,
                              "builtin": _clean_builtin(voice.get("builtin"), current.get("builtin"))}
        if "tools" in fields:
            saved["tools"] = _clean_tools(fields["tools"])
        self._write(key, saved)
        return self.get(key)

    def reset(self, key: str) -> None:
        """A built-in personality back to how it shipped. Its clips stay."""
        saved = self._read(key)
        changed = [k for k in ("name", "prompt", "delivery", "greeting", "voice", "tools") if k in saved]
        if key in BUILTIN and changed:
            for k in changed:
                del saved[k]
            self._write(key, saved)

    def delete(self, key: str) -> None:
        if key in BUILTIN:
            raise ValueError("built-in personalities can be reset, not deleted")
        shutil.rmtree(self._folder(key), ignore_errors=True)

    def selection(self, key: str) -> Selection:
        """What this personality's brain gets."""
        tools = self.get(key)["tools"]
        return Selection(
            servers={name: ServerPick(tools=None if pick["tools"] is None else frozenset(pick["tools"]),
                                      skills=None if pick["skills"] is None else frozenset(pick["skills"]),
                                      approved=frozenset((pick.get("approved") or {}).items()))
                     for name, pick in tools["servers"].items() if pick["on"]},
            skills=frozenset(tools["skills"]))

    def approve(self, key: str, server: str, uri: str, fingerprint: str) -> None:
        """Approve one ticked skill as it is now: `fingerprint` is its files' (mcp_skills.SkillEntry.fingerprint)."""
        tools = self.get(key)["tools"]
        pick = tools["servers"].get(server) or {}
        if pick.get("skills") is None or uri not in pick["skills"]:
            raise ValueError("only a skill ticked on its own needs approving")
        pick["approved"] = {**(pick.get("approved") or {}), uri: fingerprint}
        self.save(key, {"tools": tools})

    def use(self, key: str, server: str | None = None, skill: str | None = None) -> None:
        """Switch a newly added server or local skill on for this personality."""
        tools = self.get(key)["tools"]
        if server and not tools["servers"].get(server, {}).get("on"):
            tools["servers"][server] = {**{"tools": None, "skills": None}, **tools["servers"].get(server, {}), "on": True}
        if skill and skill not in tools["skills"]:
            tools["skills"].append(skill)
        self.save(key, {"tools": tools})

    def forget(self, server: str | None = None, skill: str | None = None, renamed_to: str | None = None) -> None:
        """A server or local skill is gone (or the server renamed): drop it from every personality that names it."""
        for key in self.keys():
            saved = self._read(key)
            tools = self.get(key)["tools"]
            changed = False
            if server and server in tools["servers"]:
                pick = tools["servers"].pop(server)
                if renamed_to:
                    tools["servers"][renamed_to] = pick
                changed = True
            if skill and skill in tools["skills"]:
                tools["skills"].remove(skill)
                changed = True
            if changed:
                saved["tools"] = _clean_tools(tools)
                self._write(key, saved)

    # --- voice clips (the voice add-in) --------------------------------------------------------

    def _store_audio(self, key: str, stem: str, filename: str, data: bytes) -> str:
        ext = Path(filename or "").suffix.lower() or ".wav"
        if ext not in AUDIO_TYPES:
            raise ValueError(f"use a {', '.join(AUDIO_TYPES)} file")
        if not data or len(data) > MAX_AUDIO_BYTES:
            raise ValueError("the audio file is empty or bigger than 8 MB: a few seconds is all it takes")
        folder = self._folder(key)
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob(f"{stem}.*"):
            old.unlink()
        (folder / f"{stem}{ext}").write_bytes(data)
        return f"{stem}{ext}"

    def set_clip(self, key: str, ref_text: str, filename: str | None = None, data: bytes | None = None) -> None:
        """A new clip and/or transcript to clone from. The voice server checks the clip when it next learns it.
        A new clip keeps the one it replaces as a saved version, to load back in."""
        saved = self._read(key)
        clip = dict(saved.get("clip") or {})
        if data:
            self._archive_clip(key, saved)
            clip["file"] = self._store_audio(key, "voice", filename or "voice.wav", data)
            clip["saved"] = time.strftime("%Y-%m-%d %H:%M")
            clip.pop("seconds", None)
        if not clip.get("file"):
            raise ValueError("add a voice clip first")
        clip["ref_text"] = ref_text.strip()
        saved["clip"] = clip
        self._write(key, saved)

    def _archive_clip(self, key: str, saved: dict) -> None:
        """Keep the current clip and its transcript as a version (in `saved`, written by the caller)."""
        clip = saved.get("clip") or {}
        path = self._folder(key) / clip["file"] if clip.get("file") else None
        if path is None or not path.is_file():
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        versions = list(saved.get("clip_versions") or [])
        # Two saves in one second: never reuse an id, nor a file name (that would overwrite a saved clip).
        while any(v["id"] == stamp for v in versions) or path.with_name(f"voice-{stamp}{path.suffix}").exists():
            stamp += "b"
        kept = path.with_name(f"voice-{stamp}{path.suffix}")
        path.rename(kept)
        versions.insert(0, {"id": stamp, "file": kept.name, "ref_text": clip.get("ref_text", ""),
                            "seconds": clip.get("seconds"), "saved": clip.get("saved") or stamp})
        for old in versions[KEEP_VERSIONS:]:
            (self._folder(key) / old["file"]).unlink(missing_ok=True)
        saved["clip_versions"] = versions[:KEEP_VERSIONS]
        saved["clip"] = {}

    def clip_versions(self, key: str) -> list[dict]:
        """Earlier clips, newest first: {"id", "file", "ref_text", "seconds", "saved"}."""
        return [v for v in self._read(key).get("clip_versions") or [] if (self._folder(key) / v["file"]).is_file()]

    def restore_clip(self, key: str, version: str) -> None:
        """Load a saved version back in; the clip it replaces becomes a version itself."""
        saved = self._read(key)
        chosen = next((v for v in saved.get("clip_versions") or [] if v["id"] == version), None)
        if chosen is None or not (self._folder(key) / chosen["file"]).is_file():
            raise ValueError("no such saved version")
        saved["clip_versions"] = [v for v in saved["clip_versions"] if v["id"] != version]
        self._archive_clip(key, saved)
        src = self._folder(key) / chosen["file"]
        dest = src.with_name(f"voice{src.suffix}")
        src.rename(dest)
        saved["clip"] = {"file": dest.name, "ref_text": chosen["ref_text"], "saved": chosen.get("saved"),
                         **({"seconds": chosen["seconds"]} if chosen.get("seconds") else {})}
        self._write(key, saved)

    def delete_clip_version(self, key: str, version: str) -> None:
        saved = self._read(key)
        versions = list(saved.get("clip_versions") or [])
        for v in [v for v in versions if v["id"] == version]:
            (self._folder(key) / v["file"]).unlink(missing_ok=True)
            versions.remove(v)
        saved["clip_versions"] = versions
        self._write(key, saved)

    def note_seconds(self, key: str, seconds: float) -> None:
        saved = self._read(key)
        if saved.get("clip"):
            saved["clip"]["seconds"] = round(float(seconds), 1)
            self._write(key, saved)

    def add_catchphrase(self, key: str, text: str, filename: str, data: bytes, name: str = "") -> None:
        """A sound clip: a recording, the words in it (or what the sound is), and optionally the name the model
        plays it by (else one made from the words)."""
        text = text.strip()
        if not text:
            raise ValueError("a sound clip needs the words in it, or a few words saying what the sound is")
        saved = self._read(key)
        phrases = list(saved.get("catchphrases") or [])
        n = max([int(m.group(1)) for p in phrases if (m := re.match(r"catch(\d+)", p["file"]))] + [0]) + 1
        clip = {"text": text, "file": self._store_audio(key, f"catch{n}", filename, data)}
        if name.strip():
            clip["name"] = sound_name(name)
        phrases.append(clip)
        saved["catchphrases"] = phrases
        self._write(key, saved)

    def delete_catchphrase(self, key: str, index: int) -> None:
        saved = self._read(key)
        phrases = list(saved.get("catchphrases") or [])
        if 0 <= index < len(phrases):
            (self._folder(key) / phrases.pop(index)["file"]).unlink(missing_ok=True)
            saved["catchphrases"] = phrases
            self._write(key, saved)

    def sounds(self, key: str) -> list[dict]:
        """This personality's sound clips whose files are there: {"name", "text", "path"}. A name is the clip's
        own, else made from its words; a second clip of the same name gets a number."""
        out, taken = [], set()
        for clip in self._read(key).get("catchphrases") or []:
            path = self._folder(key) / clip["file"]
            if not path.is_file():
                continue
            base = name = clip.get("name") or sound_name(clip["text"])
            n = 1
            while name in taken:
                n += 1
                name = f"{base}-{n}"
            taken.add(name)
            out.append({"name": name, "text": clip["text"], "path": path})
        return out

    def sound_tags(self, key: str) -> dict[str, str]:
        """What the model is told it can play: {name: the words in the recording}."""
        return {s["name"]: s["text"] for s in self.sounds(key)}

    def sound(self, key: str, name: str) -> dict | None:
        """The clip a [sound:<name>] tag means: by name, by its words, or the nearest name (models misspell)."""
        clips = self.sounds(key)
        wanted = sound_name(name)
        for clip in clips:
            if wanted in (clip["name"], sound_name(clip["text"])):
                return clip
        near = difflib.get_close_matches(wanted, [c["name"] for c in clips], n=1, cutoff=0.6)
        return next((c for c in clips if near and c["name"] == near[0]), None)

    def file(self, key: str, name: str) -> Path:
        """One of a personality's audio files, for the console to play."""
        path = self._folder(key) / Path(name).name
        if path.suffix.lower() not in AUDIO_TYPES or not path.is_file():
            raise FileNotFoundError(name)
        return path

    # --- the voice to speak with -----------------------------------------------------------------

    def own_voice(self, key: str) -> bool:
        """Does this personality speak with a voice server's voice (built-in or cloned) rather than Nova's?"""
        return self.get(key)["voice"]["kind"] != "nova"

    def voice_ref(self, key: str, fallback: bool = True) -> VoiceRef | None:
        """What a voice server speaks as for this personality: its built-in voice or its clip. A personality on
        Nova's voice, or one set to clone with no clip yet, gets a plain built-in voice when `fallback` (the
        home-box pipeline has no Nova to speak for it), else None."""
        voice = self.get(key)["voice"]
        if voice["kind"] == "builtin":
            return self._builtin_ref(key, voice["builtin"])
        if voice["kind"] == "clone" and (ref := self._clip_ref(key)) is not None:
            return ref
        return self._builtin_ref(key, dict(PLAIN_BUILTIN)) if fallback else None

    @staticmethod
    def _builtin_ref(key: str, params: dict) -> VoiceRef:
        return VoiceRef(id=key, ref_text="", audio=None, catchphrases=(), builtin=params,
                        hash=hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()[:12])

    def _clip_ref(self, key: str) -> VoiceRef | None:
        saved = self._read(key)
        clip = saved.get("clip") or {}
        audio = self._folder(key) / clip["file"] if clip.get("file") else None
        if audio is None or not audio.is_file() or not clip.get("ref_text"):
            return None
        phrases = tuple((p["text"], self._folder(key) / p["file"]) for p in saved.get("catchphrases") or []
                        if (self._folder(key) / p["file"]).is_file())
        digest = hashlib.sha1(audio.read_bytes() + clip["ref_text"].encode())
        for text, path in phrases:
            digest.update(text.encode() + path.read_bytes())
        return VoiceRef(id=key, ref_text=clip["ref_text"], audio=audio, catchphrases=phrases,
                        hash=digest.hexdigest()[:12])

    # --- the console -----------------------------------------------------------------------

    def summary(self, key: str) -> dict:
        """Everything the console's Personalities view shows and edits."""
        saved = self._read(key)
        clip = saved.get("clip") or {}
        base = BUILTIN.get(key)
        by_file = {s["path"].name: s["name"] for s in self.sounds(key)}
        return {**self.get(key),
                "defaults": None if base is None else {
                    "name": base["name"], "prompt": base["prompt"], "delivery": base["delivery"],
                    "voice": base["voice"], "tools": _clean_tools(base["tools"])},
                "clip": {"file": clip.get("file"), "ref_text": clip.get("ref_text", ""), "seconds": clip.get("seconds"),
                         "saved": clip.get("saved"), "ready": self._clip_ref(key) is not None,
                         "versions": self.clip_versions(key)},
                "catchphrases": [{"text": p["text"], "file": p["file"], "name": by_file.get(p["file"], ""),
                                  "plays_here": p["file"].lower().endswith(".wav")}
                                 for p in saved.get("catchphrases") or []]}

    def all(self) -> dict[str, dict]:
        return {key: self.summary(key) for key in self.keys()}
