"""Gizmo's brain on Amazon Bedrock AgentCore Runtime: the same Strands agent, hosted instead of on the robot.

Each turn the robot sends the rendered prompt (the personality's system prompt and how Gizmo works, minus the
camera), what the personality takes from each MCP server, the skills kept on the robot as their SKILL.md text,
and the model; the runtime (agentcore/app.py) keeps one Strands agent per session and streams back its words
and tool calls as server-sent events:
    {"type": "text", "data": "..."}          model text, as it's written
    {"type": "message_end"}                   a model message ended (before a tool call, or the reply's end)
    {"type": "tool", "kind": "skill"|"mcp"|"tool", "text": "explorer · flag(country=\"Japan\") 0.4s",
     "seconds": 0.4, "detail": {...}}        detail: what the console's trace shows, as the robot's brain reports it
    {"type": "done", "model": "...", "seconds": 2.1, "skills_loaded": [{"name": "universe", "server": "explorer"}]}
    {"type": "error", "error": "..."}       (the end of a failed turn; the SDK's own stream errors have no type)
Gestures ride in the text as tags, so the robot plays them as on its own brain. One runtime session per
brain (a new one after every session rebuild); the first call of a session boots a microVM (~4-5 s), which
warm_up() pays at session start. If the runtime fails or says nothing in time, the robot's own Strands brain
(Brain) takes the turn, without the runtime's conversation so far.

Skills served over MCP aren't sent. The runtime is a host of its own (SEP-2640): it has its own connections to
the MCP servers, lists their skills, and loads one when its model asks, checked and labelled with its server,
as the robot's brain does. The robot sends the personality's picks, including its approvals of skills ticked
one by one, and the runtime applies them to what its servers list. Only skills kept on the robot go as text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid

from gizmo.brain import ReplyStream, acting

logger = logging.getLogger("gizmo.agentcore")

MODELS = {"haiku": "us.anthropic.claude-haiku-5-5", "sonnet": "us.anthropic.claude-sonnet-5-5"}
FIRST_WORDS_S = 8.0  # time to the first sentence, tools excluded: the network and the runtime on top of the model
HARD_CAP_S = 30.0


class AgentCoreBrain:
    def __init__(self, session, body, runtime_arn: str, prompt: str, skills: list[dict], servers: dict[str, dict],
                 model: str = "haiku", fallback=None, on_tool=None, sounds: dict[str, str] | None = None) -> None:
        self.client = session.client("bedrock-agentcore")
        self.arn = runtime_arn
        self.prompt = prompt
        self.skills = skills  # [{"name", "content"}]: this personality's skills kept on the robot
        # What it takes from each MCP server: {name: {"tools", "skills", "approved"}} (None: all). The runtime
        # has its own connections to them.
        self.servers = servers
        self.model = model if model in MODELS else "haiku"
        self.sounds = sounds or {}  # the personality's sound clips, {name: its words}
        self.body = body
        self.fallback = fallback  # the robot's own brain (Brain), or None
        self.on_tool = on_tool  # (kind, text, detail) for each tool call: the console's trace
        self.session_id = f"gizmo-{uuid.uuid4().hex}"  # 38 characters; AgentCore wants 33 or more
        self.last: dict = {}
        self.tools_running = 0
        self._skills_loaded: list = []

    def loaded_skills(self) -> list[dict]:
        """As the runtime reported them. One that predates skills over MCP there sends bare names."""
        return [s if isinstance(s, dict) else {"name": s, "server": None} for s in self._skills_loaded]

    def _events(self, text: str, warmup: bool = False):
        """Invoke the runtime and yield its events as they arrive (blocking: run it in a thread)."""
        payload = {"text": text, "system_prompt": self.prompt, "model": self.model, "skills": self.skills,
                   "servers": self.servers, "mcp": list(self.servers),  # "mcp": for a runtime not yet redeployed
                   **({"warmup": True} if warmup else {})}
        resp = self.client.invoke_agent_runtime(
            agentRuntimeArn=self.arn, runtimeSessionId=self.session_id, qualifier="DEFAULT",
            payload=json.dumps(payload).encode(), contentType="application/json", accept="text/event-stream")
        body = resp["response"]
        if "text/event-stream" not in resp.get("contentType", "text/event-stream"):
            yield json.loads(body.read())  # a plain JSON answer (an error before streaming started)
            return
        for raw in body.iter_lines(chunk_size=64):  # the default 1 KB reads hold the words back until the end
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("data:"):
                try:
                    yield json.loads(line[5:].strip())
                except ValueError:
                    logger.warning(f"Unreadable event from the runtime: {line[:120]}")

    async def warm_up(self) -> float:
        """Boot this session's microVM and build its agent now, not on the first question."""
        t0 = time.perf_counter()
        await asyncio.to_thread(lambda: [None for _ in self._events("Warm-up check, not a real visitor.", warmup=True)])
        if self.fallback is not None:
            await self.fallback.warm_up()
        return round(time.perf_counter() - t0, 2)

    async def think(self, heard: str) -> dict:
        """One whole turn (the cloud pipeline, where Nova or the personality's own voice says it afterwards): only the
        last message is the answer, so "I'll check where the space station is" before a tool call isn't said."""
        return await self.think_streaming(heard, lambda sentence: None, whole=True)

    async def think_streaming(self, heard: str, on_sentence, on_sound=None, whole: bool = False) -> dict:
        t0 = time.perf_counter()
        reply = ReplyStream(acting(self.body, on_sound), on_sentence, self.sounds)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def pump() -> None:
            try:
                for event in self._events(heard):
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except Exception as e:  # the call itself failed (network, permissions, a bad ARN)
                loop.call_soon_threadsafe(queue.put_nowait, {"type": "error", "error": f"{type(e).__name__}: {e}"})
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        loop.run_in_executor(None, pump)
        model, deadline, error = MODELS[self.model], t0 + FIRST_WORDS_S, None
        while True:
            now = time.perf_counter()
            wait = (deadline if not reply.spoken else t0 + HARD_CAP_S) - now
            try:
                event = await asyncio.wait_for(queue.get(), timeout=max(0.05, wait))
            except asyncio.TimeoutError:
                error = "no words in time" if not reply.spoken else "the reply stalled"
                break
            if event is None:
                break
            kind = "error" if "error" in event else event.get("type")
            if kind == "text":
                reply.feed(event.get("data", ""))
            elif kind == "message_end":
                reply.end_message()
            elif kind == "tool":
                deadline += float(event.get("seconds", 0) or 0) + 1.0  # tool time doesn't count against the model
                if self.on_tool:
                    self.on_tool(event.get("kind", "tool"), f"{event.get('text', '')} (AgentCore)",
                                 {**(event.get("detail") or {}), "where": "agentcore",
                                  "ms": round(float(event.get("seconds", 0) or 0) * 1000)})
            elif kind == "done":
                reply.end_message()
                model = event.get("model") or model
                self._skills_loaded = list(event.get("skills_loaded") or self._skills_loaded)
            elif kind == "error":
                error = event.get("error") or "unknown error"
                break
        if error and not reply.spoken and self.fallback is not None:
            logger.warning(f"AgentCore brain: {error}; the robot's own brain takes the turn")
            result = await (self.fallback.think(heard) if whole
                            else self.fallback.think_streaming(heard, on_sentence, on_sound))
            self.last = {**result, "seconds": round(time.perf_counter() - t0, 2)}
            return self.last
        if error:
            logger.warning(f"AgentCore brain: {error}")
        self.last = {"say": reply.answer if whole else " ".join(reply.spoken), "actions": reply.actions,
                     "model": f"agentcore/{model}",
                     "seconds": round(time.perf_counter() - t0, 2)}
        logger.info(f"BRAIN agentcore {model} {self.last['seconds']}s actions={reply.actions} say={self.last['say']!r}")
        return self.last
