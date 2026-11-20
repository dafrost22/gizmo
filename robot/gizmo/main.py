"""Gizmo: a Strands voice agent on a Reachy Mini's own mic, speaker, camera and body, for trying out MCP
servers and Agent Skills by talking to them.

Two Strands agents share each turn:
- Nova Sonic (a BidiAgent, agent.py) hears and speaks. It doesn't decide anything: its one tool, think, hands what
  it heard to the brain.
- The brain (an Agent, brain.py) decides what to say and do, with the tools of the personality in use: MCP tools,
  skills served over MCP, skills kept on the robot, the camera (toolbox.py, personalities.py).

What thinks is a setting (Settings on the console, saved across restarts): Claude on Amazon Bedrock by default,
any OpenAI-compatible endpoint, or the same brain hosted on Bedrock AgentCore Runtime (agentcore/app.py), with the
robot's own brain standing by. With the home-box add-in, a box on your network can hear and speak instead of Nova
Sonic (local_session.py); it falls back to Nova Sonic while the box is unreachable.

The console is on port 8042 (api.py, static/). Credentials and addresses: config.py.
"""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import os
import re
import signal
import socket
import sys
import threading
import time
import wave

import boto3
import botocore.exceptions
import requests
from reachy_mini import ReachyMini, ReachyMiniApp

from gizmo.agent import PROFILE, REGION, VOICE_MODEL, build_agent
from gizmo.agentcore_brain import AgentCoreBrain
from gizmo.api import EARS_END_MS, Robot, add_routes, agentcore_arn, wanted_mode
from gizmo.brain import Brain, make_think_tool, model_spec, render_prompt, voice_only_prompt
from gizmo.config import CONFIG, load_settings, save_settings
from gizmo.direction import MIN_TURN_DEG, DoATracker, offset_deg
from gizmo.listening import FOLLOW_UP_S, Listening
from gizmo.local_session import LocalSession, ears_url
from gizmo.personalities import DEFAULT_PERSONALITY, GREETING, NOVA_VOICES, Personalities
from gizmo.robot_io import VOICE_DBFS, ConversationLog, RobotMic, RobotSpeaker
from gizmo.robot_tools import Body
from gizmo.sounds import Clips
from gizmo.toolbox import Toolbox
from gizmo.voice_clone import CloneSpeech, VoiceError, sentences

logger = logging.getLogger("gizmo")

LOCAL_RETRY_S = 30  # how often a fallen-back session checks whether the home box is back
LOCAL_COOLDOWN_S = 60  # after a home-box session fails, how long to stay on Nova Sonic
VOLUME = 100  # the hardware max; set at every start (Gizmo once turned itself down to 40 when it misheard)


class PowerWatch:
    """Polls the Pi's throttling flags: under-voltage is what reset Gizmo mid-conversation (2026-10-25).

    Bits: 0 under-voltage now, 2 throttled now, 16 under-voltage since boot, 18 throttled since boot.
    """

    def __init__(self, log: ConversationLog, period_s: float = 2.0) -> None:
        self.log = log
        self.period_s = period_s
        self.state = {"raw": None, "undervoltage_now": False, "undervoltage_since_boot": False}
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, name="power-watch", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        import subprocess

        while not self._stop.wait(self.period_s):
            try:
                out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3).stdout
                flags = int(out.strip().split("=")[1], 16)
            except Exception:
                continue
            now = bool(flags & 0x1)
            if now and not self.state["undervoltage_now"]:
                logger.warning(f"POWER: under-voltage right now (throttled={hex(flags)})")
                self.log.add("power", text="⚠ Under-voltage right now: plug in the 7 V charger or lower the volume")
            self.state = {"raw": hex(flags), "undervoltage_now": now, "undervoltage_since_boot": bool(flags & 0x10000)}


MOTORS = ["body_rotation", *(f"stewart_{i}" for i in range(1, 7)), "right_antenna", "left_antenna"]


class MotorWatch:
    """Switches the motors back on after a motor-bus dropout.

    2026-10-26 00:44: during speech plus a slow body-turn move the daemon logged "Motor
    communication error" for two motors, the servos reset with their torque off and the head
    went limp, while the daemon still reported motors enabled with no errors. So we watch the
    daemon's journal for that line, then re-torque every motor by name (set_mode/enabled would
    do nothing: the daemon thinks they're on) and sit up. Re-torquing healthy motors is harmless.
    """

    TRIGGER = "Motor communication error"

    def __init__(self, mini, body: Body, log: ConversationLog, cooldown_s: float = 10.0, asleep=None) -> None:
        self.mini = mini
        self.asleep = asleep or (lambda: None)  # why the motors are off on purpose (switched off, hidden), or None
        self.body = body
        self.log = log
        self.cooldown_s = cooldown_s
        self._quiet_until = 0.0
        self._proc = None

    def start(self) -> None:
        import subprocess

        try:
            self._proc = subprocess.Popen(
                ["journalctl", "-u", "reachy-mini-daemon", "-f", "-n", "0", "-o", "cat"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            )
        except OSError as e:
            logger.warning(f"Motor watch off: can't follow the daemon journal ({e})")
            return
        threading.Thread(target=self._run, name="motor-watch", daemon=True).start()
        logger.info("Motor watch on")

    def stop(self) -> None:
        if self._proc:
            self._proc.terminate()

    def _run(self) -> None:
        for line in self._proc.stdout:
            if self.TRIGGER in line and time.monotonic() > self._quiet_until:
                self._quiet_until = time.monotonic() + self.cooldown_s
                if why := self.asleep():
                    logger.info(f"Motor bus dropout in the daemon log, but Gizmo is {why}: motors stay off")
                    continue
                logger.warning("Motor bus dropout in the daemon log: re-enabling motors in 1.5 s")
                threading.Timer(1.5, self.recover, kwargs={"reason": "dropped out for a moment"}).start()

    def recover(self, reason: str = "re-enabled from the console") -> bool:
        if why := self.asleep():
            self.log.add("system", text=f"Motors left off: Gizmo is {why}")
            return False
        try:
            self.mini.enable_motors(ids=MOTORS)
            time.sleep(0.3)
            self.body.stop()  # ends any half-played move and sits up
        except Exception as e:
            logger.error(f"Motor recovery failed: {e!r}")
            self.log.add("power", text=f"⚠ Motors {reason} and didn't come back: run ./gizmo wake ({e})")
            return False
        self.log.add("power", text=f"⚠ Motors {reason}: switched them back on and sat up")
        return True


SHUTDOWN_GRACE_S = 10.0
# How long the agent may go without taking audio from the mic before its connection counts as dead. Healthy, it
# takes a chunk every few tens of milliseconds.
STALL_S = 8.0


def internet_up() -> bool:
    """Can a new connection reach AWS from here, right now?"""
    try:
        socket.create_connection((f"bedrock-runtime.{REGION}.amazonaws.com", 443), timeout=3).close()
        return True
    except OSError:
        return False
STOP_REQUESTED = threading.Event()  # the daemon asked us to stop (vs. a lost connection)


def exit_if_shutdown_hangs(stop_event: threading.Event) -> None:
    """2026-10-26 01:06 the app ignored the daemon's stop for 20 s and was killed, and the daemon then
    stuck in "stopping". So: if we're still alive 10 s after the stop, log every thread's stack
    (to find what hung) and exit."""
    import faulthandler

    stop_event.wait()
    time.sleep(SHUTDOWN_GRACE_S)
    logger.warning(f"Shutdown still running after {SHUTDOWN_GRACE_S:g}s: thread stacks follow, then exiting")
    faulthandler.dump_traceback(all_threads=True)
    logging.shutdown()
    sys.stdout.flush()
    os._exit(0)


class GizmoApp(ReachyMiniApp):
    custom_app_url: str | None = "http://0.0.0.0:8042"

    def run(self, reachy_mini: ReachyMini, stop_event: threading.Event) -> None:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
        for noisy in ("botocore", "urllib3", "httpx", "httpx2", "mcp", "strands"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        # The daemon stops apps with SIGINT. Let it set stop_event (our loop then winds down) rather
        # than raise KeyboardInterrupt inside asyncio, where a hung cleanup would never set it.
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: (STOP_REQUESTED.set(), stop_event.set()))
        threading.Thread(target=exit_if_shutdown_hangs, args=(stop_event,), name="shutdown-watchdog",
                         daemon=True).start()
        host = "127.0.0.1" if self.daemon_on_localhost else reachy_mini.client.get_status().wlan_ip
        daemon_url = f"http://{host}:8000"
        settings = load_settings()
        people = Personalities()  # who Gizmo is, how it sounds and what it can use (personalities.py)
        chosen = settings.get("personality") or ""
        state: dict = {
            "status": "starting", "error": None,
            "personality": chosen if people.exists(chosen) else DEFAULT_PERSONALITY,
            "config_rev": 0,  # bumped when a prompt, tool, voice or the model changes: the session rebuilds
            "asleep": bool(settings.get("asleep", False)),  # hidden in the power-down pose (survives restarts)
            # Shut down from the console: no session at all, so nothing calls AWS (survives restarts and boots).
            "off": bool(settings.get("off", False)),
            "switching": None,  # "down" or "up" while Shut down or Start up runs
            "off_steps": [],  # what the last Shut down or Start up did, step by step, for the console
            "session": None,  # the session open right now ("claude", "agentcore", "local"); None: nothing open
            # Who hears and speaks: Nova Sonic, or the home-box add-in when it's set up and chosen.
            "speech": "local" if settings.get("speech") == "local" and CONFIG["box_hosts"] else "nova",
            "mode": None,  # the pipeline running (it can differ from the one asked for while a fallback stands in)
            "last_turn": None,
        }
        log = ConversationLog()
        mic = RobotMic(reachy_mini)
        speaker = RobotSpeaker(reachy_mini, boost_db=float(settings.get("boost_db", 0.0)))
        speaker.clone.nfe = int(settings.get("clone_nfe", 16))
        speaker.clone.prefer = settings.get("clone_prefer", "auto")
        if "voice_servers" in settings:  # set on the console; else config.toml's
            speaker.clone.servers = list(settings["voice_servers"])
        speaker.mic = mic
        speaker.on_reply_start = lambda s: log.add("timing", text=f"mic-to-voice {s:.1f}s")
        holder: dict = {"mini": reachy_mini, "daemon_url": daemon_url}
        # Free talk, or a turn needs Gizmo's name.
        listening = Listening(settings.get("listen_mode", "free"), settings.get("wake_names"),
                              float(settings.get("follow_up_s", FOLLOW_UP_S)))
        listening.speaking_until = lambda: speaker.speaking_until
        speaker.interrupts = listening.addressed  # in wake-word mode only the name cuts Gizmo off
        log.nova_muted = lambda: speaker.mute_nova
        # In wake-word mode, what Gizmo overhears isn't kept unless the console asks to see it (for working out
        # why it missed its name).
        log.private = lambda text: (listening.mode == "wake" and not settings.get("show_ignored")
                                    and not listening.addressed(text))

        def apply_voice(key: str) -> None:
            """Speak as this personality from the next sentence on, and teach a voice server its voice now
            rather than on the first sentence."""
            own = people.voice_ref(key, fallback=False)
            speaker.own_voice = own is not None  # else Nova Sonic speaks, in the personality's Nova voice
            speaker.clone.set_voice(own or people.voice_ref(key))  # the home-box pipeline always needs one

            def prepare() -> None:
                try:
                    if speaker.clone.ensure_url() and (seconds := speaker.clone.ensure_voice()):
                        people.note_seconds(speaker.clone.voice.id, seconds)
                except (VoiceError, requests.RequestException, ValueError) as e:
                    logger.warning(f"Couldn't prepare the {key} voice: {e}")

            threading.Thread(target=prepare, name="voice-prepare", daemon=True).start()

        apply_voice(state["personality"])
        robot = Robot(state=state, settings=settings, save=save_settings, log=log, mic=mic, speaker=speaker,
                      body=None, people=people, listening=listening, holder=holder, apply_voice=apply_voice)
        power = PowerWatch(log)
        power.start()
        holder["power"] = power

        try:
            session = boto3.Session(profile_name=PROFILE, region_name=REGION)
            session.client("sts").get_caller_identity()
        except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as e:
            state.update(status="error", error=f"AWS credentials for profile '{PROFILE}' not usable: {e}")
            logger.error(state["error"])
            add_routes(self.settings_app, robot)
            stop_event.wait()
            return

        try:
            if state["asleep"] or state["off"]:
                logger.info("Gizmo is hidden or switched off: not waking up")
            elif requests.get(f"{daemon_url}/api/motors/status", timeout=3).json().get("mode") == "disabled":
                logger.info("Robot is asleep; enabling motors and waking up")
                reachy_mini.enable_motors()
                reachy_mini.wake_up()
        except (requests.RequestException, ValueError) as e:
            logger.warning(f"Couldn't read motor status: {e}")

        body = Body(reachy_mini, daemon_url)
        robot.body = body
        try:
            body.set_volume(VOLUME)
        except requests.RequestException as e:
            logger.warning(f"Couldn't set the volume: {e}")
        body.want_asleep = state["asleep"] or state["off"]
        if body.want_asleep:  # auto-wake at boot may have woken it: back into the pose, mic off
            mic.muted = True
            threading.Thread(target=body.hide, name="hide", daemon=True).start()
        motor_watch = MotorWatch(reachy_mini, body, log, asleep=lambda: (
            "switched off (Start up wakes it)" if state["off"] else "hidden (Reveal wakes it)" if state["asleep"] else None))
        motor_watch.start()
        holder["motor_watch"] = motor_watch
        # Where voices come from, for looking at whoever speaks. Gizmo's own voice doesn't count.
        doa = DoATracker(daemon_url, quiet=lambda: speaker.speaking, loud=lambda: mic.level_dbfs > VOICE_DBFS - 8)
        doa.start()
        from gizmo import demo_server  # after logging is set up: building its server calls basicConfig

        demo_server.start()  # the demo MCP server (everyday tools, space data, five skills), so there's one to try
        demo_server.warm_up()  # where home is, looked up now and not during the first question
        toolbox = Toolbox()  # MCP servers, the skills they serve, and skills kept on the robot
        toolbox.start()
        holder["toolbox"] = toolbox

        def camera() -> bytes | None:
            return reachy_mini.media.get_frame_jpeg()

        def on_tool(kind: str, text: str, detail: dict | None = None) -> None:
            log.add(kind, text=text, detail=detail or {})

        def on_brain_turn(result: dict) -> None:
            state["last_turn"] = result
            listening.replied(result["say"])
            acts = [f"{k}:{a}" if a else k for k, a in result["actions"]]
            name = result["model"].split("/")[-1].removeprefix("us.anthropic.")  # claude-haiku-5-5
            log.add("brain", text=f"{name} {result['seconds']}s {' '.join(f'[{a}]' for a in acts)}".strip(),
                    detail={"model": name, "seconds": result["seconds"], "actions": acts})

        clips = Clips()  # the personalities' sound clips, decoded once

        def queue_sound(name: str) -> None:
            """A reply's [sound:name]: put that recording on the speaker, where the words then follow it."""
            found = people.sound(state["personality"], name)
            if found is None:
                log.add("sound", text=f'no recording called "{name}"', detail={"name": name, "error": "no such recording"})
                return
            try:
                pcm = clips.get(found["path"], speaker.rate)
            except (OSError, EOFError, ValueError, wave.Error) as e:
                logger.warning(f"Couldn't play the sound clip {found['name']}: {e!r}")
                log.add("sound", text=f'couldn\'t play "{found["text"]}"', detail={"name": found["name"], "error": str(e)[:200]})
                return
            speaker.play_clone(pcm)
            log.add("sound", text=f'played "{found["text"]}"',
                    detail={"name": found["name"], "words": found["text"], "seconds": round(pcm.size / speaker.rate, 1)})

        def play_sounds(names: list[str]) -> None:
            """The sound clips of a whole reply, before any voice says its words."""
            speaker.clear()  # a new reply replaces whatever Gizmo was still saying
            for name in names:
                queue_sound(name)

        async def sounds_after_voice(names: list[str]) -> None:
            """A reply spoken by Nova Sonic that ends with a recording's own words: when its voice has finished
            the words before them, the recording. Nova's audio arrives in bursts ahead of playback, so "finished"
            is nothing playing and nothing new for a moment. A barge-in in between drops it."""
            gen, t0 = speaker.generation, time.monotonic()
            while time.monotonic() - t0 < 10 and speaker.last_audio_at <= t0:  # until the voice starts
                await asyncio.sleep(0.05)
            while speaker.speaking or time.monotonic() - speaker.last_audio_at < 0.6:  # until it's done
                await asyncio.sleep(0.05)
            if speaker.generation == gen and not (state["off"] or state["asleep"]):
                for name in names:
                    queue_sound(name)

        def play_sounds_after(names: list[str]) -> None:
            asyncio.get_running_loop().create_task(sounds_after_voice(names))

        async def speak_own(say: str, force: bool = False, started: bool = False) -> bool:
            """Say a reply in the personality's own voice from a voice server; False lets Nova Sonic speak it.

            force: the home-box pipeline, where a voice server is the only voice. started: this reply's sound
            clips are already on the speaker, so it mustn't be cleared.
            """
            if not ((force or speaker.own_voice) and say and await speaker.clone.available()):
                speaker.mute_nova = False
                return False
            if not started:
                speaker.clear()  # a new reply replaces whatever Gizmo was still saying
            gen = speaker.generation
            first, rest = await speaker.clone.first(say)
            if first is None or speaker.generation != gen:
                speaker.mute_nova = False
                return False
            speaker.mute_nova = True
            speaker.play_clone(first, sentences(say)[0])  # with its words, which the console lights up as it speaks
            log.add("assistant", text=say)
            if log.last_user_at is not None:
                log.add("timing", text=f"voice started {time.monotonic() - log.last_user_at:.1f}s after your turn "
                                       f"(first sentence {speaker.clone.last_gen_ms:.0f} ms on the voice server)")
                log.last_user_at = None
            asyncio.create_task(speaker.clone.rest(rest, speaker.play_clone, lambda: speaker.generation != gen))
            return True

        def addressed(heard: str) -> bool:
            """Wake-word mode with Nova Sonic listening: Nova hands every turn to the brain. One that isn't for
            Gizmo is logged as ignored, and Nova's own audio is dropped: it sometimes answered by itself."""
            if listening.addressed(heard):
                if not listening.was_typed(heard):  # a typed line comes from nowhere in the room
                    face_speaker()
                return True
            log.add("ignored", text=f'not for Gizmo (no "{listening.names[0]}")'
                                    + (f': "{heard.strip()}"' if settings.get("show_ignored") else f": {len(heard.split())} words"))
            speaker.mute_nova = True
            return False

        def face_speaker() -> None:
            """Look at whoever just spoke to Gizmo: turn toward where their voice came from, while the brain
            works on the answer. Off with the console's switch."""
            if not settings.get("face_speaker", True) or state["asleep"] or state["off"]:
                return
            now = time.monotonic()
            bearing = doa.bearing(now)
            flagged, loud = doa.heard(now)
            if bearing is None:  # say so in the turn's trace: it's how you find out the mic is too quiet for this
                log.add("body", text=f"stayed put: couldn't tell where the voice came from ({len(flagged)} speech "
                                     f"readings, {len(loud)} loud ones)", detail={"turned": False})
                return
            offset = offset_deg(bearing, -1.0 if float(settings.get("doa_sign", 1.0)) < 0 else 1.0)
            side = "left" if offset > 0 else "right"

            def turn() -> None:
                try:
                    facing = body.face(offset)
                except Exception as e:
                    logger.warning(f"Couldn't turn toward the voice: {e!r}")
                    return
                if facing is None:
                    why = "the voice was straight ahead" if abs(offset) < MIN_TURN_DEG else "it was busy with a move"
                    log.add("body", text=f"stayed put: {why} ({abs(offset):.0f}° to its {side})", detail={"turned": False})
                else:
                    log.add("body", text=f"turned {abs(offset):.0f}° to its {side}, toward the voice "
                                         f"({len(flagged) or len(loud)} readings)",
                            detail={"turned": True, "offset": round(offset), "facing": round(facing)})
                logger.info(f"Voice at {offset:+.0f}° ({len(flagged)} speech readings, {len(loud)} loud): "
                            f"{'turned to ' + format(facing, '.0f') + '°' if facing is not None else 'stayed put'}")

            threading.Thread(target=turn, name="face-speaker", daemon=True).start()

        def heard_locally() -> None:
            """Home-box ears: a turn for Gizmo just ended."""
            body.ack()
            face_speaker()

        async def local_reply(text: str) -> bool:
            """One home-box turn: the brain streams sentences straight into the voice server, so Gizmo starts
            talking while the rest is still being written. False if the voice server is down (the app then
            falls back to Nova Sonic)."""
            if not await speaker.clone.available():
                return False
            speaker.clear()  # a new reply replaces whatever Gizmo was still saying
            gen = speaker.generation
            speech = CloneSpeech(speaker.clone, speaker.play_clone, lambda: speaker.generation != gen)
            result = await holder["brain"].think_streaming(text, speech.say, queue_sound)
            on_brain_turn(result)
            await speech.close()
            if result["say"]:
                log.add("assistant", text=result["say"])
            if speech.first_at is not None and log.last_user_at is not None:
                log.add("timing", text=f"voice started {speech.first_at - log.last_user_at:.1f}s after your turn "
                                       f"(streamed; brain {result['seconds']}s in all)")
                log.last_user_at = None
            return speech.first_at is not None or not result["say"]

        def build(mode: str, key: str, ears: str | None = None):
            """The session for one personality: its brain, then who hears and speaks."""
            p, selection, spec = people.get(key), people.selection(key), model_spec(settings)
            sounds = people.sound_tags(key)  # its sound clips, which the model plays by name
            brain = Brain(session, body, camera, toolbox, selection, p["prompt"], spec=spec, on_tool=on_tool, sounds=sounds)
            if mode == "agentcore":  # the same brain on AgentCore, with this one standing by
                families = sorted({re.sub(r"\d+$", "", e) for e in body.emotions})
                # The runtime is a skills-over-MCP host of its own: it gets this personality's picks, and loads
                # served skills itself. Only the skills kept on the robot go there as text.
                skills = toolbox.local_skill_texts(selection)
                picks = {name: {"tools": None if pick.tools is None else sorted(pick.tools),
                                "skills": None if pick.skills is None else sorted(pick.skills),
                                "approved": dict(pick.approved)} for name, pick in selection.servers.items()}
                remote_prompt = render_prompt(p["prompt"], ", ".join(families), ", ".join(body.dances), robot_tools=False,
                                              has_mcp=bool(selection.servers),
                                              has_skills=bool(skills or toolbox.skills_for(selection)), sounds=sounds)
                brain = AgentCoreBrain(session, body, agentcore_arn(settings), remote_prompt, skills, picks,
                                       model="sonnet" if "sonnet" in spec.model else "haiku", fallback=brain,
                                       on_tool=on_tool, sounds=sounds)
            holder["brain"] = brain
            if mode == "local":
                return LocalSession(ears, mic, speaker, log, local_reply,
                                    end_silence_ms=int(settings.get("ears_end_ms", EARS_END_MS)), on_heard=heard_locally,
                                    barge_in=bool(settings.get("barge_in", True)), listening=listening)
            return build_agent(voice_only_prompt(p["delivery"]), session=session, voice=p["voice"]["nova"],
                               tools=[make_think_tool(brain, on_brain_turn, speak_own, addressed, play_sounds,
                                                      play_sounds_after, sounds)],
                               output_rate=speaker.rate)

        add_routes(self.settings_app, robot)
        reachy_mini.enable_wobbling()
        reachy_mini.media.start_recording()
        reachy_mini.media.start_playing()

        async def main() -> None:
            holder["loop"] = asyncio.get_running_loop()
            state["status"] = "warming up"
            local_down_until = 0.0
            left_behind: list[asyncio.Future] = []  # session stops that never finished (see below)
            while not stop_event.is_set():
                # Switched off, or hidden: no cloud session, nothing billed.
                if state["off"] or (state["asleep"] and state["speech"] != "local"):
                    state["status"] = "off" if state["off"] else "asleep"
                    await asyncio.sleep(0.5)
                    continue
                want, key, rev = wanted_mode(state, settings), state["personality"], state["config_rev"]
                state["status"] = "warming up"  # also while switching personality, model or speech
                mode, ears = want, None
                if want == "local":
                    ears = await asyncio.to_thread(ears_url) if time.monotonic() > local_down_until else None
                    if ears is None:
                        mode = "claude"
                        log.add("system", text="the home box isn't answering: Nova Sonic hears and speaks for now")
                if want == "agentcore" and not agentcore_arn(settings):
                    mode = "claude"
                    log.add("system", text="AgentCore isn't set up (no runtime ARN): the brain on the robot thinks for now")
                state["mode"] = mode
                try:
                    agent = build(mode, key, ears)
                except Exception as e:  # a bad model id, a plugin that wouldn't load: say so and try again
                    logger.exception("Couldn't build the session")
                    state.update(status="error", error=f"Couldn't start {people.get(key)['name']}: {e!r}", mode=None)
                    while not stop_event.is_set() and state["config_rev"] == rev and state["personality"] == key:
                        await asyncio.sleep(0.5)
                    continue
                holder["agent"] = agent
                state["session"] = mode  # from the warm-up on, this session may call AWS
                try:
                    state["brain_warmup_s"] = await holder["brain"].warm_up()
                    logger.info(f"Brain warmed up in {state['brain_warmup_s']}s")
                except Exception as e:  # the first turn will just be slower
                    logger.warning(f"Brain warm-up failed: {e!r}")
                voice = people.get(key)["voice"]["nova"]
                if mode == "local":
                    run = asyncio.create_task(agent.run())
                    ready = f"home-box ears and voice ({ears.split('/')[2]})"
                else:
                    run = asyncio.create_task(agent.run(inputs=[mic], outputs=[speaker, log]))
                    ready = f"Nova Sonic ({VOICE_MODEL}, {NOVA_VOICES[voice].split(' (')[0]})"
                while not (mic.running or run.done() or stop_event.is_set()):
                    await asyncio.sleep(0.05)
                state.update(status="asleep" if state["asleep"] else "listening", error=None)
                log.add("system", text=f"Ready as {people.get(key)['name']}: {ready}, {model_spec(settings).label} thinking"
                                       + (" on AgentCore" if mode == "agentcore" else ""))
                logger.info(f"Gizmo ready ({ready}): talk to it")
                retry_at = time.monotonic() + LOCAL_RETRY_S
                stalled, tick = False, time.monotonic()
                while (not stop_event.is_set() and not run.done()
                       and state["personality"] == key and state["config_rev"] == rev and not state["off"]
                       and not (state["asleep"] and mode != "local")):
                    # A network change underneath (the travel router swapping its uplink, say) leaves the stream
                    # to Nova Sonic open but dead: nothing errors for minutes, sends just never complete, and the
                    # agent stops taking audio. That's the one sign there is, so act on it.
                    now = time.monotonic()
                    if now - tick > 2.0:  # this loop was held up, so the agent's was too: not a dead connection
                        mic.pulled_at = now
                    tick = now
                    if mode != "local" and mic.running and now - mic.pulled_at > STALL_S:
                        stalled = True
                        break
                    if holder.pop("greet", False):  # just revealed, or asked to: introduce itself in character
                        log.last_user_at = None
                        line = people.get(key)["greeting"] or GREETING
                        log.add("typed", text=line, intro=True)  # the console shows it as "Introduce yourself"
                        listening.typed(line)
                        await agent.send(line)
                    if want == "local" and mode != want and time.monotonic() > max(retry_at, local_down_until):
                        retry_at = time.monotonic() + LOCAL_RETRY_S
                        if await asyncio.to_thread(ears_url):
                            log.add("system", text="the home box is back: switching to its ears and voice")
                            break
                    await asyncio.sleep(0.2)
                if stalled:
                    logger.warning(f"The session took no audio for {STALL_S:g} s: its connection is dead. Reconnecting")
                    log.add("power", text="Lost the connection to Nova Sonic (did the network change?): reconnecting")
                    state["status"] = "warming up"
                if mode == "local" and run.done() and agent.failed:
                    local_down_until = time.monotonic() + LOCAL_COOLDOWN_S
                    log.add("power", text=f"Home box: {agent.failed}. Using Nova Sonic for now.")
                if run.done() and run.exception():
                    state.update(status="error", error=repr(run.exception()))
                    logger.error(f"Agent stopped: {run.exception()!r}")
                    await asyncio.sleep(3)  # don't spin if the backend keeps failing
                # Stopping a session must never hold this loop up. Strands' stop waits for the session's tasks to
                # end, and swallows a cancellation while it waits, so wait_for can't bound it: a "New conversation"
                # pressed mid-reply once left the app at "warming up" until it was restarted. So the stop is a
                # task of its own, and after 5 s the loop goes on without it. (Its being a task also keeps a
                # CancelledError raised inside Strands, as when a session is stopped right after it opened, from
                # ending this loop.)
                stopping = asyncio.ensure_future(agent.stop())
                await asyncio.wait({stopping}, timeout=5)
                if not stopping.done():
                    logger.warning("Session stop took over 5 s: going on without it")
                    stopping.cancel()
                    left_behind.append(stopping)  # held, so it isn't collected while still pending
                elif stopping.cancelled():
                    logger.warning("Session stop was cancelled inside Strands: carrying on")
                elif stopping.exception() is not None:
                    logger.warning(f"Agent stop: {stopping.exception()!r}")
                if not run.done():
                    run.cancel()
                    await asyncio.wait({run}, timeout=3)
                state["session"] = None
                speaker.clear()
                if stalled:
                    # Whatever the network is doing (a router changing its uplink can take a minute), wait until
                    # a new connection gets out before opening anything. Then open the MCP servers' connections
                    # again, since they went the same way, and the next pass builds a fresh session.
                    waiting = time.monotonic()
                    while not stop_event.is_set() and not state["off"] and not await asyncio.to_thread(internet_up):
                        if waiting is not None and time.monotonic() - waiting > 4:
                            log.add("power", text="No internet from here: waiting for it to come back")
                            waiting = None
                        await asyncio.sleep(2)
                    try:
                        await asyncio.to_thread(toolbox.reload)
                    except Exception as e:
                        logger.warning(f"Couldn't reconnect the MCP servers: {e!r}")
                    logger.info("The network answers again: opening a new session")

        try:
            asyncio.run(main())
        finally:
            state["status"] = "stopped"
            motor_watch.stop()
            doa.stop()
            toolbox.stop()
            try:
                reachy_mini.disable_wobbling()
                reachy_mini.media.stop_playing()
                reachy_mini.media.stop_recording()
            except Exception:
                pass


CONNECT_ATTEMPTS = 6
CONNECT_RETRY_S = 5.0

if __name__ == "__main__":
    # For finding out where a stuck app is stuck: `kill -USR1 <pid>` prints every thread's stack to the log.
    faulthandler.register(signal.SIGUSR1, all_threads=True)
    # Same boot pattern as gizmo_ears: the daemon can launch us before its media stack answers,
    # and a failed connect leaves SDK threads that would keep a dead app "running".
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        app = GizmoApp()
        try:
            app.wrapped_run()
            break
        except KeyboardInterrupt:
            app.stop()
            break
        except (TimeoutError, ConnectionError) as e:
            # ConnectionError: the daemon dropped our SDK connection right after a quick restart
            # (seen 2026-10-26 01:57: stop and start racing). Same cure: a fresh app instance.
            if STOP_REQUESTED.is_set():
                break
            logger.warning(f"Couldn't connect to the daemon (attempt {attempt}/{CONNECT_ATTEMPTS}): {e!r}")
            if attempt == CONNECT_ATTEMPTS:
                logging.shutdown()
                sys.stdout.flush()
                os._exit(1)
            time.sleep(CONNECT_RETRY_S)
    logging.shutdown()
    sys.stdout.flush()
    os._exit(0)
