"""Gizmo without the robot: the same console and the same Strands brain on your laptop, with typed turns.

    python -m gizmo.desk [--port 8042]

For trying an MCP server, and the skills it serves, when you don't have a Reachy Mini (or it's across the room).
What the brain does is all real: the personality's prompt, the model, the MCP tools, skills over MCP, and the
trace of every turn. What's missing is the body: there's no voice, so you type a line on the console's Talk view
and read the reply, and gestures show in the trace and go nowhere.

It keeps its settings where the robot does (~/.config/gizmo/), and needs the same credentials for the model:
AWS for Bedrock (aws_profile in config.toml, else your default chain), or nothing but a URL for an
OpenAI-compatible endpoint such as Ollama.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import threading
import time
from collections import deque
from pathlib import Path

import boto3
import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from gizmo import demo_server
from gizmo.api import Robot, add_routes
from gizmo.brain import Brain, model_spec, sounds_in
from gizmo.config import CONFIG, load_settings, save_settings
from gizmo.listening import Listening
from gizmo.personalities import DEFAULT_PERSONALITY, GREETING, Personalities
from gizmo.toolbox import Toolbox
from gizmo.voice_clone import CloneVoiceClient

logger = logging.getLogger("gizmo.desk")

STATIC = Path(__file__).parent / "static"
# The gestures a brain here may write, so its prompt reads as on the robot (where the list is what's installed).
EMOTIONS = ["attentive", "cheerful", "confused", "curious", "laughing", "proud", "sad", "surprised", "thoughtful",
            "welcoming"]
DANCES = ["groovy"]


class Log:
    """What was said and done, newest first, for the console (robot_io.ConversationLog without the audio)."""

    def __init__(self, maxlen: int = 120) -> None:
        self.events: deque[dict] = deque(maxlen=maxlen)
        self.last_user_at: float | None = None

    def add(self, kind: str, **fields) -> None:
        self.events.appendleft({"time": time.strftime("%H:%M:%S"), "kind": kind, **fields})


class NoMic:
    muted, level_dbfs, running = True, -90.0, False


class NoSpeaker:
    """Nothing to play through. The voice add-in's client is here because the console's settings show it."""
    speaking, speaking_until, own_voice, mute_nova, boost_db = False, 0.0, False, False, 0.0

    def __init__(self) -> None:
        self.clone = CloneVoiceClient()

    def clear(self) -> None:
        pass

    def levels(self) -> dict:
        return {}


class NoBody:
    """Gestures are named in the trace and played nowhere."""
    emotions, dances, tracking, want_asleep = EMOTIONS, DANCES, False, False

    def play(self, *args, **kwargs) -> None:
        pass

    def look(self, *args) -> None:
        pass

    def stop(self) -> None:
        pass

    def hide(self) -> None:
        pass

    def reveal(self) -> None:
        pass

    def track_faces(self, on: bool) -> bool:
        return False


def aws_session() -> boto3.Session:
    """The profile in config.toml if it exists here, else boto3's default chain."""
    try:
        return boto3.Session(profile_name=CONFIG["aws_profile"] or None, region_name=CONFIG["aws_region"])
    except Exception as e:  # a profile that's on the robot but not on this machine
        logger.warning(f"AWS profile {CONFIG['aws_profile']!r} isn't usable here ({e}): using the default credentials")
        return boto3.Session(region_name=CONFIG["aws_region"])


def build_app() -> FastAPI:
    settings = load_settings()
    people = Personalities()
    chosen = settings.get("personality") or ""
    state: dict = {
        "status": "starting", "error": None, "desk": True,  # desk: the console hides what only a robot has
        "personality": chosen if people.exists(chosen) else DEFAULT_PERSONALITY,
        "config_rev": 0, "asleep": False, "off": False, "switching": None, "off_steps": [], "session": None,
        "speech": "nova", "mode": None, "last_turn": None,
    }
    log, body = Log(), NoBody()
    holder: dict = {}
    listening = Listening("free", None, 20.0)
    listening.speaking_until = lambda: 0.0
    session = aws_session()

    demo_server.start()  # the demo MCP server, so there's one to try
    demo_server.warm_up()  # where home is, looked up now and not during the first question
    toolbox = Toolbox()
    toolbox.start()
    holder["toolbox"] = toolbox

    def on_tool(kind: str, text: str, detail: dict | None = None) -> None:
        log.add(kind, text=text, detail=detail or {})

    async def turn(text: str) -> None:
        brain = holder.get("brain")
        if brain is None:
            log.add("system", text="still starting: try again in a moment")
            return
        try:
            result = await brain.think(text)
        except Exception as e:
            logger.exception("the brain failed")
            log.add("power", text=f"The brain failed: {type(e).__name__}: {e}"[:300])
            return
        state["last_turn"] = result
        acts = [f"{k}:{a}" if a else k for k, a in result["actions"]]
        name = result["model"].split("/")[-1].removeprefix("us.anthropic.")
        log.add("brain", text=f"{name} {result['seconds']}s", detail={"model": name, "seconds": result["seconds"], "actions": acts})
        for wanted in sounds_in(result["actions"]):  # no speaker here: say which recording a robot would have played
            found = people.sound(state["personality"], wanted)
            log.add("sound", text=f'would play "{found["text"]}"' if found else f'no recording called "{wanted}"',
                    detail={"name": found["name"], "words": found["text"]} if found else {"name": wanted, "error": "no such recording"})
        if result["say"]:
            log.add("assistant", text=result["say"])

    class Typed:
        """What the console's typed lines go to: one brain turn each."""

        async def send(self, text: str) -> None:
            asyncio.get_running_loop().create_task(turn(text))

    async def sessions() -> None:
        """One brain at a time, rebuilt when the personality, its tools or the model change."""
        while True:
            key, rev = state["personality"], state["config_rev"]
            state.update(status="warming up", session=None)
            holder.pop("brain", None)
            p, spec = people.get(key), model_spec(settings)
            try:
                brain = await asyncio.to_thread(Brain, session, body, lambda: None, toolbox, people.selection(key),
                                                p["prompt"], spec, on_tool, people.sound_tags(key))
                holder["brain"] = brain
                state.update(session="desk", mode="desk")
                state["brain_warmup_s"] = await brain.warm_up()
                state.update(status="listening", error=None)
                log.add("system", text=f"Ready as {p['name']}: {spec.label} thinking. No robot here: type your turns.")
            except Exception as e:  # no credentials, a model id that doesn't exist: say so, and wait for a change
                logger.exception("couldn't start the brain")
                state.update(status="error", error=f"Couldn't start {p['name']} on {spec.label}: {type(e).__name__}: {e}"[:400])
            while state["personality"] == key and state["config_rev"] == rev:
                if holder.pop("greet", False):  # the console's Introduce yourself
                    line = p["greeting"] or GREETING
                    log.add("typed", text=line, intro=True)
                    asyncio.get_running_loop().create_task(turn(line))
                await asyncio.sleep(0.2)

    loop = asyncio.new_event_loop()
    holder["loop"], holder["agent"] = loop, Typed()
    threading.Thread(target=loop.run_forever, name="desk-loop", daemon=True).start()
    asyncio.run_coroutine_threadsafe(sessions(), loop)

    app = FastAPI(title="Gizmo (desk)")
    add_routes(app, Robot(state=state, settings=settings, save=save_settings, log=log, mic=NoMic(), speaker=NoSpeaker(),
                          body=None, people=people, listening=listening, holder=holder, apply_voice=lambda key: None))
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.get("/")(lambda: FileResponse(STATIC / "index.html"))
    return app


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m gizmo.desk", description="Gizmo's console and brain without the robot.")
    parser.add_argument("--port", type=int, default=8042)
    parser.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to reach it from another machine (it has no login)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    for noisy in ("botocore", "urllib3", "httpx", "httpx2", "mcp", "strands"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    app = build_app()
    logger.info(f"Gizmo's console: http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
