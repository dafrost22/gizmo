"""Gizmo's brain: a Strands Agent that decides what Gizmo says and does.

The model comes from the provider picked on the console: Claude on Amazon Bedrock by default, or any
OpenAI-compatible endpoint (Ollama, LM Studio, OpenAI, OpenRouter...). If the chosen model is slow or fails,
the next one in line takes the same turn from the same history. Every turn returns the exact words to speak
(Nova Sonic or a voice server says them). Gestures ride along as leading tags ("[emotion:cheerful] [look:left]
Hi there!") so most turns take a single model call. The camera is a tool; everything else the brain can use is
its personality's: MCP tools, skills served over MCP and skills kept on the robot (toolbox.py).

To add another Strands model provider, give it a branch in make_model() and a name in PROVIDERS.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field

import boto3
import requests
from strands import Agent, tool
from strands.agent.conversation_manager import SlidingWindowConversationManager
from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent
from strands.models import BedrockModel
from strands.models.openai import OpenAIModel

from gizmo.config import CONFIG, box_urls
from gizmo.robot_tools import DANCES, EMOTIONS, LOOK, match

logger = logging.getLogger("gizmo.brain")

HAIKU = "us.anthropic.claude-haiku-5-5"  # the default: Sonnet felt slow in conversation
SONNET = "us.anthropic.claude-sonnet-5-5"
# The console's short list. Any Bedrock model id or inference profile that can call tools works too.
BEDROCK_MODELS = {HAIKU: "Claude Haiku 5.5", SONNET: "Claude Sonnet 5.5"}
PROVIDERS = {"bedrock": "Amazon Bedrock", "openai": "OpenAI-compatible"}
# LM Studio's OpenAI-compatible API on the optional home box (config.toml box_hosts), at each of its addresses:
# where the OpenAI-compatible provider looks when no base URL is given.
LM_STUDIO_URLS = box_urls(1234, path="/v1")
BRAIN_TIMEOUT_S = float(os.environ.get("GIZMO_BRAIN_TIMEOUT_S", "6"))


@dataclass(frozen=True)
class ModelSpec:
    """Which model thinks, and where."""
    provider: str = "bedrock"
    model: str = HAIKU  # a Bedrock model id or inference profile, or the endpoint's name for the model
    base_url: str = ""  # openai: http://localhost:11434/v1 (Ollama), https://api.openai.com/v1, ...
    api_key: str = ""
    params: dict = field(default_factory=dict)  # openai: extra request parameters, e.g. {"reasoning_effort": "none"}

    @property
    def label(self) -> str:
        return BEDROCK_MODELS.get(self.model) or self.model.split("/")[-1]


def model_spec(settings: dict) -> ModelSpec:
    """The console's model choice (settings["model"]) as a spec. An AgentCore choice runs the same Bedrock model,
    with the brain on the robot standing by."""
    chosen = settings.get("model") or {}
    if chosen.get("provider") == "openai":
        o = chosen.get("openai") or {}
        return ModelSpec("openai", str(o.get("model") or CONFIG["local_model"]), str(o.get("base_url") or "").strip(),
                         str(o.get("api_key") or ""), dict(o.get("params") or {}))
    return ModelSpec("bedrock", str((chosen.get("bedrock") or {}).get("model") or HAIKU))


def fallbacks(spec: ModelSpec) -> list[ModelSpec]:
    """Who takes the turn if `spec` is slow or fails: the other Claude, and for a model somewhere else, the cloud."""
    if spec.provider != "bedrock":
        return [ModelSpec("bedrock", HAIKU), ModelSpec("bedrock", SONNET)]
    return [ModelSpec("bedrock", SONNET if spec.model == HAIKU else HAIKU)]


def thinking_off(model_id: str) -> dict | None:
    """Claude 5.5 on Bedrock thinks before it answers by default: ~3.5 s and hundreds of tokens a call, which blew
    the 6 s budget and the 500-token cap on skill-and-tool turns. Each model wants its own spelling of "don't"
    (Sonnet refuses "disabled", Haiku refuses "between_tools")."""
    if "claude-haiku" in model_id:
        return {"thinking": {"type": "disabled"}}
    if "claude-sonnet" in model_id:
        return {"thinking": {"type": "between_tools"}}
    return None


def openai_url(spec: ModelSpec) -> str | None:
    """The endpoint to use, if it answers: the given base URL, else the first LM Studio on the home box."""
    headers = {"Authorization": f"Bearer {spec.api_key}"} if spec.api_key else {}
    for url in ([spec.base_url.rstrip("/")] if spec.base_url else LM_STUDIO_URLS):
        try:
            requests.get(f"{url}/models", headers=headers, timeout=2)  # any answer will do: it's there
            return url
        except requests.RequestException:
            continue
    return None


def make_model(spec: ModelSpec, session: boto3.Session):
    """The Strands model for a spec, or None when its endpoint doesn't answer."""
    if spec.provider == "openai":
        url = openai_url(spec)
        if url is None:
            return None
        return OpenAIModel(client_args={"base_url": url, "api_key": spec.api_key or "none", "timeout": 30},
                           model_id=spec.model, params={"max_tokens": 500, **spec.params})
    # 300 tokens once cut off a tool call plus an in-character answer
    return BedrockModel(model_id=spec.model, boto_session=session, max_tokens=500,
                        additional_request_fields=thinking_off(spec.model))


# What every personality gets after its own system prompt: how the app works. Not editable, so a prompt edit on
# the console can't break the gestures.
MECHANICS = """HOW YOU WORK
You think; a voice model speaks your words aloud exactly as you write them. Write for the ear: one to three short \
spoken sentences, no markdown, lists, emoji or URLs. Say numbers the way a person would.
You can move. Put action tags at the very start of your reply, before the words:
  [emotion:<name>]  one of: {emotions}
  [look:<direction>] center, left, right, up, down, up left, up right, down left, down right
  [dance:<name>] one of: {dances}, or [dance:random]. Only when someone asks you to dance.
  [stop] stop moving and go back to neutral.
{sounds}Use an emotion for genuine moments (greetings, jokes, good news, surprise), roughly every few turns, not every reply.

Tools:
{tools}
Your volume is set from the console: never offer to change it.
Never make up facts: if you aren't sure, say so."""

# The tools only the robot has (a brain on AgentCore can't see through Gizmo's camera).
ROBOT_TOOLS = """- see(question): look through your camera. Use it whenever someone asks what you see, about their \
appearance, what they're holding, or anything visual. Never guess what you can't see.
- track_faces(enabled) when asked."""
MCP_TOOLS = "- Your MCP servers' tools, named <server>_<tool>: use them for anything they cover rather than memory."
SKILL_TOOLS = """- Skills: when a question is about something one of your skills covers, load that skill first and \
follow it, including which of your other tools to use."""

# What a personality with sound clips is told (sounds.py). The tag leads the reply like a gesture, and the
# recording plays before the words.
SOUNDS = """  [sound:<name>] play one of your recordings, ahead of your words: {clips}.
    A recording says its own words, so don't write them as well. (A reply that opens or ends with a
    recording's exact words, as a sentence of their own, plays the recording for them too.)
"""

# A leading [kind:arg] tag, or a bare [word] (Qwen sometimes writes [attentive] for [emotion:attentive]).
TAG = re.compile(r"^\s*\[(?:(emotion|look|dance|stop|sound)(?::([^\]]*))?|([a-z][a-z _-]{1,30}))\]\s*", re.IGNORECASE)


# The end of a spoken sentence in streamed text: punctuation (plus closing quotes) and then a space.
SENTENCE_END = re.compile(r"[.!?]+[\"')\]]*\s+")


def sentences_of(text: str) -> list[str]:
    """Leading tags stay with the first sentence (split_actions takes them off there)."""
    parts, start = [], 0
    for m in SENTENCE_END.finditer(text):
        parts.append(text[start: m.end()])
        start = m.end()
    if text[start:].strip():
        parts.append(text[start:])
    return parts


def split_actions(text: str) -> tuple[list[tuple[str, str]], str]:
    """'[emotion:cheerful] [look:left] Hi!' -> ([('emotion','cheerful'), ('look','left')], 'Hi!').

    A bare leading tag counts as an emotion (unknown names simply don't play).
    """
    actions = []
    while m := TAG.match(text):
        if m.group(3):
            actions.append(("emotion", m.group(3).strip()))
        else:
            actions.append((m.group(1).lower(), (m.group(2) or "").strip()))
        text = text[m.end():]
    # Speech never needs brackets: drop any tag the model put mid-sentence rather than read it aloud.
    spoken = re.sub(r"\[[^\]]{0,40}\]", "", text)
    return actions, re.sub(r"\s{2,}", " ", spoken).strip()


class Brain:
    def __init__(self, session: boto3.Session, body, camera, toolbox, selection, prompt: str,
                 spec: ModelSpec | None = None, on_tool=None, sounds: dict[str, str] | None = None) -> None:
        """sounds: the personality's sound clips, {name: its words}. The brain only names them in a reply
        ([sound:name] comes back among the actions): whoever speaks the reply plays them, in order."""
        self.spec = spec or ModelSpec()
        self.sounds = sounds or {}
        self.on_tool = on_tool  # (kind, text, detail) for each finished tool call: the console's trace
        self.body = body
        self.toolbox = toolbox  # MCP servers, the skills they serve, skills on the robot (toolbox.py)
        self.selection = selection  # which of them this personality gets (personalities.Selection)
        self.camera = camera  # () -> jpeg bytes or None
        self.session = session
        # One plugin for all of this brain's models: what was loaded from a server stays loaded when the
        # fallback model takes a turn.
        self.mcp_skills = toolbox.mcp_skills(selection)
        mcp_tools = toolbox.tools_for(selection)
        families = sorted({re.sub(r"\d+$", "", e) for e in body.emotions})
        self.prompt = render_prompt(prompt, ", ".join(families), ", ".join(body.dances), has_mcp=bool(mcp_tools),
                                    has_skills=bool(self.mcp_skills or selection.skills), sounds=sounds)
        self.tools = [*self._make_tools(), *mcp_tools]
        # The chosen model, then who takes its turn when it's slow or fails. An agent is None when its endpoint
        # didn't answer: its turns go straight to the next.
        self.specs = [self.spec, *fallbacks(self.spec)]
        self.agents: list[Agent | None] = [self._agent(s) for s in self.specs]
        self.last: dict = {}
        self.tools_running = 0  # time spent in tools (camera, MCP calls) doesn't count against the model

    def _agent(self, spec: ModelSpec) -> Agent | None:
        model = make_model(spec, self.session)
        if model is None:
            logger.warning(f"{spec.model}: no answer at {spec.base_url or LM_STUDIO_URLS}; its turns go to the next model")
            return None
        agent = Agent(
            model=model,
            system_prompt=self.prompt,
            tools=list(self.tools),
            # Strands' AgentSkills for the skills on the robot (per-agent state, so one each), ours for the
            # skills served over MCP.
            plugins=[p for p in (self.toolbox.local_plugin(self.selection), self.mcp_skills) if p is not None],
            conversation_manager=SlidingWindowConversationManager(window_size=24),
            callback_handler=None,
        )
        agent.add_hook(self._tool_started, BeforeToolCallEvent)
        agent.add_hook(self._tool_finished, AfterToolCallEvent)
        return agent

    def _line(self) -> list[tuple[ModelSpec, Agent]]:
        """The models that can take a turn now, the chosen one first."""
        return [(s, a) for s, a in zip(self.specs, self.agents) if a is not None]

    def loaded_skills(self) -> list[dict]:
        """Skills whose full instructions are in the conversation right now, oldest first: {"name", "server"}
        (server None for a skill kept on the robot).

        A skill is loaded when the brain calls a skill tool for it, and stays until its tool result slides out of
        the window (24 messages) or the session is rebuilt. Read from the messages rather than a plugin's own
        list, which never forgets.
        """
        line = self._line()
        if not line:
            return []
        calls, ok = {}, set()  # toolUseId -> skill; ids whose skill really loaded
        for message in list(line[0][1].messages):
            for block in message.get("content", []):
                if (use := block.get("toolUse")) and use.get("name") in ("skills", "read_skill"):
                    given = use.get("input") or {}
                    if use["name"] == "skills":
                        calls[use.get("toolUseId")] = (given.get("skill_name"), None)
                    else:  # skill://universe/SKILL.md -> universe
                        name = str(given.get("uri", "")).removesuffix("/SKILL.md").rsplit("/", 1)[-1]
                        calls[use.get("toolUseId")] = (name, given.get("server"))
                elif (res := block.get("toolResult")) and res.get("toolUseId") in calls:
                    text = "".join(c.get("text", "") for c in res.get("content", []))
                    missing = text.startswith("Error:") or (text.startswith("Skill '") and "' not found" in text)
                    if res.get("status") != "error" and not missing:
                        ok.add(res["toolUseId"])
        seen = dict.fromkeys(skill for i, skill in calls.items() if i in ok and skill[0])
        return [{"name": name, "server": server} for name, server in seen]

    def _tool_started(self, event: BeforeToolCallEvent) -> None:
        self.tools_running += 1

    def _tool_finished(self, event: AfterToolCallEvent) -> None:
        """Log each tool call (a skill loaded, an MCP tool, the camera) with its time, result and any failure."""
        self.tools_running = max(0, self.tools_running - 1)
        use = event.tool_use
        if getattr(event, "cancel_message", None):  # asked for beside a skill, and held until the skill was read
            logger.info(f"TOOL held until the skill is read: {use['name']}")  # (mcp_skills.McpSkills.skill_first)
            return
        result = event.result or {}
        kind, text, detail = self.toolbox.describe(use["name"], use.get("input") or {}, result, self.mcp_skills)
        if event.duration is not None:
            text += f" {event.duration:.1f}s"
            detail["ms"] = round(event.duration * 1000)
        if event.exception is not None or result.get("status") == "error" or detail.get("error"):
            why = event.exception or detail.get("error") or next(
                (c["text"] for c in result.get("content", []) if "text" in c), "")
            detail["error"] = str(why)[:300]
            text += f" failed: {str(why)[:120]}"
        logger.info(f"TOOL {kind}: {text}")
        if self.on_tool:
            self.on_tool(kind, text, detail)

    def _make_tools(self) -> list:
        brain = self

        @tool
        def see(question: str) -> dict:
            """Look through your camera to answer a visual question.

            Args:
                question: What to look for, e.g. "what colour is their shirt".
            """
            jpeg = brain.camera()
            if not jpeg:
                return {"status": "error", "content": [{"text": "The camera isn't available right now."}]}
            return {"status": "success", "content": [
                {"text": f"Camera view for: {question}"},
                {"image": {"format": "jpeg", "source": {"bytes": jpeg}}},
            ]}

        @tool
        def track_faces(enabled: bool) -> str:
            """Turn face tracking on or off.

            Args:
                enabled: true to keep looking at the person in front of you.
            """
            return "Following faces" if brain.body.track_faces(enabled) else "Not following faces"

        return [see, track_faces]

    async def warm_up(self) -> float:
        """Run a throwaway turn on the cloud models: the first call after a start took over 6 s."""
        t0 = time.perf_counter()

        async def one(agent: Agent) -> None:
            await agent.invoke_async("Warm-up check, not a real visitor. Reply with just: OK")
            agent.messages.clear()

        cloud = [(s, a) for s, a in self._line() if s.provider == "bedrock"]
        results = await asyncio.gather(*(one(a) for _, a in cloud), return_exceptions=True)
        for (spec, _), result in zip(cloud, results):
            if isinstance(result, Exception):
                if spec == self.spec:  # the chosen model itself can't be reached: that's worth stopping for
                    raise result
                logger.warning(f"{spec.model} (standing by) didn't warm up: {result!r}")  # no AWS credentials, say
        # A model somewhere else may first have to load: warm it in the background so the app starts listening
        # now (until it's ready, a slow turn just falls back). Only when it's the chosen one: the request is
        # what makes a local server load the model.
        if self.spec.provider != "bedrock" and self.agents[0] is not None:
            asyncio.get_running_loop().create_task(self._warm_slowly(self.agents[0], one))
        return round(time.perf_counter() - t0, 2)

    async def _warm_slowly(self, agent: Agent, one) -> None:
        t0 = time.perf_counter()
        try:
            await one(agent)
            logger.info(f"{self.spec.model} warmed up in {time.perf_counter() - t0:.1f}s")
        except Exception as e:
            logger.warning(f"{self.spec.model} warm-up failed: {e!r}")

    def _share(self, answered: Agent) -> None:
        """Every model continues from the same conversation."""
        for agent in self.agents:
            if agent is not None and agent is not answered:
                agent.messages = copy.deepcopy(answered.messages)

    async def think(self, heard: str) -> dict:
        """One conversational turn. Returns {'say', 'actions', 'model', 'seconds'} and starts the actions."""
        t0 = time.perf_counter()
        self.tools_running = 0
        (spec, primary), *rest = self._line()
        before = copy.deepcopy(primary.messages)
        try:
            result = await self._run_with_model_budget(primary, heard, t0)
            answered = primary
        except Exception as e:  # slow or failed: the next model takes the same turn from the same history
            if not rest:
                raise
            was, (spec, answered) = spec, rest[0]
            logger.warning(f"{was.model} {'timed out' if isinstance(e, asyncio.TimeoutError) else f'failed: {e!r}'}; "
                           f"using {spec.model}")
            answered.messages = before
            result = await answered.invoke_async(heard)
        self._share(answered)
        # Some local models write their reasoning inline; never read it aloud.
        actions, say = split_actions(re.sub(r"<think>.*?</think>", "", str(result), flags=re.DOTALL).strip())
        actions, say = lead_with_clip(actions, say, self.sounds)
        self._act(actions)
        self.last = {"say": say, "actions": actions, "model": spec.model, "seconds": round(time.perf_counter() - t0, 2)}
        logger.info(f"BRAIN {spec.model} {self.last['seconds']}s actions={actions} say={say!r}")
        return self.last

    async def think_streaming(self, heard: str, on_sentence, on_sound=None) -> dict:
        """think(), but each finished sentence goes to on_sentence(text) while the model still writes, and each
        [sound:name] tag to on_sound(name) as it arrives.

        The home-box pipeline speaks the first sentence about half a second sooner this way. Leading action tags
        of each model message start their gesture as soon as they're complete. Fallback as in think(): if the
        chosen model says nothing within its budget (paused during tools) or fails before speaking, the next
        model answers whole and its sentences follow at once.
        """
        t0 = time.perf_counter()
        self.tools_running = 0
        (spec, primary), *rest = self._line()
        before = copy.deepcopy(primary.messages)
        reply = ReplyStream(acting(self.body, on_sound), on_sentence, self.sounds)
        spoken = reply.spoken

        async def consume() -> None:
            async for event in primary.stream_async(heard):
                if "data" in event:
                    reply.feed(event["data"])
                elif "message" in event and event["message"].get("role") == "assistant":
                    reply.end_message()  # the rest of this message (before a tool call, or the end)

        task = asyncio.create_task(consume())
        deadline = t0 + BRAIN_TIMEOUT_S
        answered = primary
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=0.25)
                if done:
                    task.result()
                    break
                now = time.perf_counter()
                if self.tools_running:
                    deadline += 0.25
                if (not spoken and now > deadline) or now - t0 > 25.0:
                    task.cancel()
                    raise asyncio.TimeoutError()
        except Exception as e:
            if spoken or not rest:  # already talking (keep what was said rather than start over), or nobody else
                logger.warning(f"{spec.model} stopped mid-reply: {e!r}")
                if not spoken:
                    raise
            else:
                was, (spec, answered) = spec, rest[0]
                logger.warning(f"{was.model} {'timed out' if isinstance(e, asyncio.TimeoutError) else f'failed: {e!r}'}; "
                               f"using {spec.model}")
                answered.messages = before
                reply.whole(str(await answered.invoke_async(heard)))
        self._share(answered)
        self.last = {"say": " ".join(spoken), "actions": reply.actions, "model": spec.model,
                     "seconds": round(time.perf_counter() - t0, 2)}
        logger.info(f"BRAIN {spec.model} {self.last['seconds']}s (streamed) actions={reply.actions} say={self.last['say']!r}")
        return self.last

    async def _run_with_model_budget(self, agent: Agent, heard: str, t0: float, hard_cap_s: float = 25.0):
        """Run a turn, giving up after BRAIN_TIMEOUT_S of *model* time: the clock pauses while a tool runs."""
        task = asyncio.create_task(agent.invoke_async(heard))
        deadline = t0 + BRAIN_TIMEOUT_S
        while True:
            done, _ = await asyncio.wait({task}, timeout=0.25)
            if done:
                return task.result()
            now = time.perf_counter()
            if self.tools_running:
                deadline += 0.25
            if now > deadline or now - t0 > hard_cap_s:
                task.cancel()
                raise asyncio.TimeoutError()

    def _act(self, actions: list[tuple[str, str]]) -> None:
        act(self.body, actions)


def sounds_in(actions: list[tuple[str, str]]) -> list[str]:
    """The names of the sound clips a reply asked for, in order."""
    return [arg for kind, arg in actions if kind == "sound" and arg]


def clip_line(sentence: str, sounds: dict[str, str] | None) -> str | None:
    """The name of the sound clip whose words these are, word for word (case and punctuation aside)."""
    def plain(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", text.lower().replace("'", "").replace("’", "")).strip()

    said = plain(sentence)
    return next((name for name, words in (sounds or {}).items() if said and plain(words) == said), None)


def lead_with_clip(actions: list[tuple[str, str]], say: str, sounds: dict[str, str] | None) -> tuple[list[tuple[str, str]], str]:
    """A reply that opens with a recording's own words plays the recording instead of having a voice say them,
    as if the model had written the tag. A personality whose prompt has it say a catchphrase word for word
    gets the real recording that way, in any voice. If the tag is there too, the words are just dropped."""
    parts = sentences_of(say)
    while parts and (name := clip_line(parts[0], sounds)):
        if name not in sounds_in(actions):
            actions = [*actions, ("sound", name)]
        parts = parts[1:]
    return actions, "".join(parts).strip()


def end_with_clip(say: str, sounds: dict[str, str] | None) -> tuple[str, list[str]]:
    """A reply that ends with a recording's own words: (the words before them, the clips to play after those are
    said). ("Wait. Ta-da!" -> ("Wait.", ["ta-da"]).) For a voice that takes a whole reply at once, like Nova
    Sonic, which can't be handed a recording mid-sentence: it says the first part, and the recording follows."""
    parts, after = sentences_of(say), []
    while len(parts) > 1 and (name := clip_line(parts[-1], sounds)):
        after.insert(0, name)
        parts = parts[:-1]
    return ("".join(parts).strip(), after) if after else (say, [])


def acting(body, on_sound=None):
    """What a streamed reply's tags do as they arrive: gestures on the body, each [sound:name] to on_sound."""
    def run(actions: list[tuple[str, str]]) -> None:
        act(body, actions)
        if on_sound is not None:
            for name in sounds_in(actions):
                on_sound(name)

    return run


def act(body, actions: list[tuple[str, str]]) -> None:
    """Play a reply's gesture tags on the body, in a thread (moves take a while). Sound tags aren't the body's."""
    def run() -> None:
        for kind, arg in actions:
            try:
                if kind == "emotion" and (name := match(arg, body.emotions)):
                    body.play(EMOTIONS, name)
                elif kind == "dance":
                    name = None if arg in ("", "random") else match(arg, body.dances)
                    if name := name or (random.choice(body.dances) if body.dances else None):
                        body.play(DANCES, name, interrupt=True)
                elif kind == "look":
                    body.look(*LOOK.get(arg.lower(), LOOK["center"]))
                elif kind == "stop":
                    body.stop()
            except Exception as e:
                logger.warning(f"Action {kind}:{arg} failed: {e}")

    if actions:
        threading.Thread(target=run, name="brain-actions", daemon=True).start()


class ReplyStream:
    """A reply arriving as text: gestures from each message's leading tags as soon as they're complete, and
    each finished sentence to on_sentence(text) while the rest is still being written. Shared by the brain
    here and the one on AgentCore."""

    def __init__(self, act, on_sentence, sounds: dict[str, str] | None = None) -> None:
        self.act, self.on_sentence = act, on_sentence
        self.sounds = sounds or {}  # the personality's sound clips, {name: its words}
        self.spoken: list[str] = []
        self.messages: list[list[str]] = [[]]  # the sentences of each model message: the last one is the answer
        self.actions: list[tuple[str, str]] = []
        self._buf, self._tags_open = "", True

    def _emit(self, text: str) -> None:
        acts, say = split_actions(re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL))
        # A message that opens with a recording's own words plays the recording instead (see lead_with_clip).
        if say and not self.messages[-1] and (name := clip_line(say, self.sounds)):
            acts, say = acts + ([] if name in sounds_in(self.actions + acts) else [("sound", name)]), ""
        self.actions.extend(acts)
        self.act(acts)
        if say:
            self.spoken.append(say)
            self.messages[-1].append(say)
            self.on_sentence(say)

    @property
    def answer(self) -> str:
        """The last message's words: what a whole-reply voice says, without "let me check..." before a tool call."""
        return " ".join(next((m for m in reversed(self.messages) if m), []))

    def _take_tags(self) -> None:
        # Gesture tags lead a message; hold back until we know whether "[" is a whole tag.
        buf = self._buf.lstrip()
        while self._tags_open:
            if buf.startswith("[") and "]" not in buf:
                break
            m = TAG.match(buf)
            if not m:
                if buf:
                    self._tags_open = False
                break
            acts = [("emotion", m.group(3).strip())] if m.group(3) else [(m.group(1).lower(), (m.group(2) or "").strip())]
            self.actions.extend(acts)
            self.act(acts)
            buf = buf[m.end():].lstrip()
        self._buf = buf

    def feed(self, data: str) -> None:
        self._buf += data
        self._take_tags()
        if self._tags_open:
            return
        while m := SENTENCE_END.search(self._buf):
            self._emit(self._buf[: m.end()])
            self._buf = self._buf[m.end():]

    def end_message(self) -> None:
        """A model message ended (before a tool call, or the reply's end): say what's left of it."""
        if self._buf.strip():
            self._emit(self._buf)
        self._buf, self._tags_open = "", True
        if self.messages[-1]:
            self.messages.append([])

    def whole(self, text: str) -> None:
        """A whole reply at once (a fallback model's): its sentences follow one after another."""
        for sentence in sentences_of(re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)):
            self._emit(sentence)


VOICE_ONLY_PROMPT = """You are the voice of Gizmo, a small robot. You don't decide what to say: a separate brain does.

For EVERY thing the person says, immediately call think with exactly what they said, then speak the text it \
returns word for word, adding nothing before or after. Don't greet, comment or fill silence on your own. If think \
returns an empty string, say nothing."""


def render_prompt(prompt: str, emotions: str = "<emotion names>", dances: str = "<dance names>",
                  robot_tools: bool = True, has_mcp: bool = True, has_skills: bool = True,
                  sounds: dict[str, str] | None = None) -> str:
    """The brain's full prompt: the personality's own system prompt, then how the app works (also shown on the
    console). robot_tools: the camera and face tracking, which only the brain on the robot has. sounds: the
    personality's sound clips, {name: its words}; with none, the model isn't told about sound tags at all."""
    tools = [ROBOT_TOOLS] if robot_tools else []
    tools += [MCP_TOOLS] if has_mcp else []
    tools += [SKILL_TOOLS] if has_skills else []
    clips = SOUNDS.format(clips=", ".join(f'{name} ("{words}")' for name, words in sounds.items())) if sounds else ""
    return prompt.strip() + "\n\n" + MECHANICS.format(
        emotions=emotions, dances=dances, sounds=clips,
        tools="\n".join(tools) or "- None beyond answering from what you know.")


def voice_only_prompt(delivery: str = "") -> str:
    """Nova Sonic's prompt when the brain decides the words: just say them, in the personality's delivery."""
    return VOICE_ONLY_PROMPT + ("\n\n" + delivery.strip() if delivery.strip() else "")


def make_think_tool(brain, on_turn=None, speak=None, addressed=None, sound=None, sound_after=None, sounds=None):
    """The one tool Nova Sonic gets: hand what it heard to the brain and say what comes back.

    speak(text, started) -> bool: an async hook that says the reply in another voice (the personality's own,
    from a voice server); when it does, Nova gets an empty string and stays quiet. addressed(text) -> bool: in
    wake-word mode, False for speech that wasn't for Gizmo (Nova then says nothing). sound(names): play the
    reply's sound clips now, ahead of whichever voice says the words; `started` then tells speak that the
    reply's audio has already begun. sound_after(names): play clips once Nova has said its words, for a reply
    that ends with a recording's own words (`sounds`: the personality's clips, {name: its words})."""
    @tool
    async def think(heard: str) -> str:
        """Send what the person just said to Gizmo's brain. Returns the exact words to speak.

        Args:
            heard: What the person said, word for word.
        """
        if addressed is not None and not addressed(heard):
            return ""
        try:
            result = await brain.think(heard)
        except Exception as e:
            logger.exception("brain failed")
            return f"Sorry, my brain hiccupped. Could you say that again? ({type(e).__name__})"
        if on_turn is not None:
            on_turn(result)
        clips = sounds_in(result["actions"]) if sound is not None else []
        if clips:
            sound(clips)
        if speak is not None and await speak(result["say"], started=bool(clips)):
            return ""  # already being said in the personality's own voice
        if sound_after is not None:  # Nova says the words: a recording they end with follows its voice
            say, after = end_with_clip(result["say"], sounds)
            if after:
                sound_after(after)
                return say
        return result["say"]

    return think
