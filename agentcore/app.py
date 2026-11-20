"""Gizmo's brain on Amazon Bedrock AgentCore Runtime: the same Strands agent the robot can run itself.

The robot renders the prompt (the personality's system prompt, then how Gizmo works) and sends it with each
turn, with what the personality takes from each MCP server. This runtime keeps one Strands agent per AgentCore
session (one microVM each), so the conversation carries on between turns, and streams the reply back as it's
written, so the robot can start speaking before the end.

Skills served over MCP (SEP-2640): this runtime is a host of its own. It has its own connections to the MCP
servers, lists the skills they serve, and gives the agent the same plugin the robot's brain has (mcp_skills.py,
a copy of the robot's): the model sees names and descriptions, and a skill is fetched, checked against the
server's listing and labelled with its server only when the model loads it. The robot sends the personality's
picks, including its approvals of skills ticked one by one, and they're applied to this runtime's own listing.
Skills kept on the robot aren't served by anything, so those arrive as text and are ordinary Agent Skills here.

POST /invocations (runtimeSessionId set by the caller, 33+ characters):
    {"text": "...", "system_prompt": "...", "model": "haiku" | "sonnet",
     "servers": {"explorer": {"tools": null | ["flag", ...], "skills": null | ["skill://universe/SKILL.md", ...],
                              "approved": {"<skill uri>": "<fingerprint>"}}},
     "skills": [{"name": "house-notes", "content": "<SKILL.md>"}],      (the ones kept on the robot)
     "warmup": false}
null means all of them. "mcp": ["explorer", ...] is still read, as every server named taking everything.
Replies as Server-Sent Events, one JSON object per "data:" line:
    {"type": "text", "data": "Jupiter is"}         text as it's written
    {"type": "message_end"}                        an assistant message ended (before a tool call, or the end)
    {"type": "tool", "kind": "skill" | "mcp" | "tool", "text": "explorer · flag(country=\"Japan\") 0.4s",
     "seconds": 0.4, "detail": {...}}              detail: what the robot's console shows in its trace
    {"type": "done", "model": "...", "seconds": 2.1, "skills_loaded": [{"name": "universe", "server": "explorer"}]}
    {"type": "error", "error": "..."}              instead of done, when the turn failed
"warmup" builds the session's agent and runs a throwaway turn: only its done event comes back.

MCP servers: the demo server (a copy of the robot's gizmo/demo_server, served here on 127.0.0.1:8772 as
"explorer"), plus those in GIZMO_MCP_SERVERS, a JSON list like the robot's servers.json entries, each optionally
narrowed to some tools:
    [{"name": "countries", "url": "https://countries.dev/api/mcp", "tools": ["get_country", ...]}]
(deploy.py sets it from mcp.json). Each connects once when the runtime starts; one that fails is skipped.

Run locally like AgentCore does (port 8080), from this folder: PYTHONPATH=../robot/gizmo python app.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
for noisy in ("botocore", "urllib3", "httpx", "httpx2", "mcp", "strands"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger("gizmo.agentcore")

from bedrock_agentcore import BedrockAgentCoreApp  # noqa: E402
from strands import Agent  # noqa: E402
from strands.agent.conversation_manager import SlidingWindowConversationManager  # noqa: E402
from strands.hooks import AfterToolCallEvent  # noqa: E402
from strands.models import BedrockModel  # noqa: E402
from strands.tools.mcp import MCPClient  # noqa: E402
from strands.vended_plugins.skills import AgentSkills  # noqa: E402
from strands.vended_plugins.skills.skill import Skill  # noqa: E402

from demo_server import server as explorer  # noqa: E402  (after logging: building it calls logging.basicConfig)
from mcp_skills import McpSkills, SkillEntry, SkillPick, SkillsHost  # noqa: E402  (deploy.py copies it in beside this file)

REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
MODELS = {"haiku": "us.anthropic.claude-haiku-5-5", "sonnet": "us.anthropic.claude-sonnet-5-5"}
# Claude 5.5 on Bedrock thinks before it answers by default (~3.5 s a call): off, as on the robot. Each model
# spells it its own way (Sonnet refuses "disabled", Haiku refuses "between_tools").
THINKING_OFF = {"haiku": {"thinking": {"type": "disabled"}}, "sonnet": {"thinking": {"type": "between_tools"}}}
DEFAULT_PROMPT = ("You are Gizmo, a small Reachy Mini robot. A voice model speaks your words aloud: answer in one "
                  "to three short spoken sentences, with no markdown, lists or emoji. Load a skill when a question "
                  "is about something it covers, and use your tools for live facts.")
PREVIEW_CHARS = 1200  # how much of a tool's result goes back for the robot's trace
WARMUP = "Warm-up check, not a real visitor. Reply with just: OK"
EXPLORER_PORT = 8772

app = BedrockAgentCoreApp()


# --- MCP servers: connected once, shared by every session's agent ------------------------------------

def _connect(server: dict) -> MCPClient | None:
    name = server.get("name") or "kb"
    try:
        client = MCPClient(url=server["url"], headers=server.get("headers") or None, prefix=name,
                           tool_filters={"allowed": list(server["tools"])} if server.get("tools") else None)
        client.start()
        tools = client.list_tools_sync()
        host = SkillsHost(name, client)  # skills/list only: no skill file is fetched until a model loads one
        skills = host.list()
    except Exception as e:  # a server that's down shouldn't take the brain with it
        logger.warning(f"MCP server {name} unavailable: {e!r}")
        return None
    SERVER_TOOLS[name], HOSTS[name], SKILLS[name] = tools, host, skills
    logger.info(f"MCP server {name}: {[t.tool_name for t in tools]}; skills {[s.name for s in skills]}"
                f"{'' if host.supported else ' (no Skills extension)'}")
    for problem in host.problems:
        logger.warning(f"MCP server {name}: {problem}")
    return client


def _servers() -> list[dict]:
    servers = [{"name": "explorer", "url": f"http://127.0.0.1:{EXPLORER_PORT}/mcp"}]
    try:
        servers += json.loads(os.environ.get("GIZMO_MCP_SERVERS") or "[]")
    except ValueError as e:
        logger.warning(f"GIZMO_MCP_SERVERS isn't valid JSON: {e}")
    return servers


SERVER_TOOLS: dict[str, list] = {}  # server name -> its tools
HOSTS: dict[str, SkillsHost] = {}  # server name -> the host's side of skills over MCP for it
SKILLS: dict[str, list[SkillEntry]] = {}  # server name -> the skills it lists
explorer.start(port=EXPLORER_PORT)
CLIENTS = [c for s in _servers() if (c := _connect(s)) is not None]


def describe(name: str, tool_input: dict, result: dict | None = None, plugin: McpSkills | None = None) -> tuple[str, str, dict]:
    """How a tool call reads in the robot's log, and the detail its console's trace shows: (kind, text, detail).
    kind is "skill", "mcp" or "tool". The same shapes as the robot's own brain reports (toolbox.describe there)."""
    tool_input = tool_input or {}
    if name == "skills":  # the AgentSkills plugin's tool: a skill that came from the robot as text
        skill = tool_input.get("skill_name", "?")
        return "skill", f"loaded {skill} (kept on the robot)", {"skill": skill, "server": None}
    if name == "read_skill":
        server, uri = tool_input.get("server", "?"), tool_input.get("uri", "?")
        load = (plugin.loads.get((server, uri)) if plugin else None) or {}
        label = load.get("name") or uri
        return "skill", f"loaded {label} from {server}", {"skill": label, "server": server, "uri": uri, **load}
    if name == "read_resource":
        server, uri = tool_input.get("server", "?"), tool_input.get("uri", "?")
        return "skill", f"read {uri} from {server}", {"server": server, "uri": uri, "file": True}
    args = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in tool_input.items())
    args = args if len(args) <= 100 else args[:99] + "…"
    text = "\n".join(c["text"] for c in (result or {}).get("content", []) if isinstance(c, dict) and "text" in c)
    detail = {"args": tool_input, "result": text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + "…"}
    server = next((s for s, tools in SERVER_TOOLS.items() if any(t.tool_name == name for t in tools)), None)
    if server:
        tool = name.removeprefix(server + "_")
        return "mcp", f"{server} · {tool}({args})", {"server": server, "name": tool, **detail}
    return "tool", f"{name}({args})", {"name": name, **detail}


# --- one agent per session ----------------------------------------------------------------------------

def picks_of(payload: dict) -> dict[str, dict]:
    """What the personality takes from each server, as the robot sent it: {name: {"tools", "skills", "approved"}}.
    An older robot sends only names ("mcp"): each of those takes everything."""
    servers = payload.get("servers")
    if not isinstance(servers, dict):
        servers = {str(name): {} for name in payload.get("mcp") or []}
    out = {}
    for name, pick in servers.items():
        pick = pick if isinstance(pick, dict) else {}
        out[str(name)] = {"tools": None if pick.get("tools") is None else sorted(str(t) for t in pick["tools"]),
                          "skills": None if pick.get("skills") is None else sorted(str(u) for u in pick["skills"]),
                          "approved": {str(u): str(m) for u, m in sorted((pick.get("approved") or {}).items())}}
    return out


class Session:
    """One robot conversation: its agent, rebuilt when the personality's prompt, model, skills or servers change."""

    def __init__(self, key: str, system_prompt: str, model: str, skills: list[dict], servers: dict[str, dict]) -> None:
        self.key = key
        self.model_id = MODELS[model]
        self.lock = asyncio.Lock()  # one turn at a time: an agent can't run two
        self.emit = lambda event: None  # where this turn's tool events go; set per turn
        tools = [t for name, pick in servers.items() for t in SERVER_TOOLS.get(name, [])
                 if pick["tools"] is None or t.tool_name.removeprefix(f"{name}_") in pick["tools"]]
        # Skills over MCP: this agent's own registry, from the listing alone. A server is asked again for its
        # listing now, so a conversation starts from what it serves today; nothing else is fetched until a load.
        picks = {name: SkillPick(skills=None if pick["skills"] is None else frozenset(pick["skills"]),
                                 approved=frozenset(pick["approved"].items())) for name, pick in servers.items()}
        hosts = {name: HOSTS[name] for name in picks if name in HOSTS and HOSTS[name].supported}
        for name, host in hosts.items():
            SKILLS[name] = host.list()
        entries = [s for name in hosts for s in SKILLS[name] if picks[name].approves(s.uri, s.fingerprint)]
        self.mcp_skills = McpSkills(hosts, picks, entries) if hosts else None
        local = [Skill.from_content(s["content"]) for s in skills if s.get("content")]
        plugins = ([AgentSkills(skills=local)] if local else []) + ([self.mcp_skills] if self.mcp_skills else [])
        self.agent = Agent(
            model=BedrockModel(model_id=self.model_id, region_name=REGION, max_tokens=500,
                               additional_request_fields=THINKING_OFF[model]),
            system_prompt=system_prompt or DEFAULT_PROMPT,
            tools=tools,
            plugins=plugins,
            conversation_manager=SlidingWindowConversationManager(window_size=24),
            callback_handler=None,
        )
        self.agent.add_hook(self._tool_finished, AfterToolCallEvent)
        logger.info(f"agent for {model}: MCP {list(servers)}, {len(tools)} tool(s), {len(entries)} skill(s) over MCP "
                    f"{[e.name for e in entries]}, {len(local)} from the robot")

    def _tool_finished(self, event: AfterToolCallEvent) -> None:
        use = event.tool_use
        if getattr(event, "cancel_message", None):  # asked for beside a skill, and held until the skill was read
            logger.info(f"TOOL held until the skill is read: {use['name']}")  # (McpSkills.skill_first)
            return
        result = event.result or {}
        kind, text, detail = describe(use["name"], use.get("input") or {}, result, self.mcp_skills)
        if event.duration is not None:
            text += f" {event.duration:.1f}s"
        if event.exception is not None or result.get("status") == "error" or detail.get("error"):
            why = event.exception or detail.get("error") or next(
                (c["text"] for c in result.get("content", []) if "text" in c), "")
            detail["error"] = str(why)[:300]
            text += f" failed: {str(why)[:120]}"
        logger.info(f"TOOL {kind}: {text}")
        self.emit({"type": "tool", "kind": kind, "text": text, "seconds": round(event.duration or 0.0, 3), "detail": detail})

    def loaded_skills(self) -> list[dict]:
        """Skills whose instructions are in the conversation now, as the robot's Brain reports them:
        {"name", "server"}, with server None for a skill the robot sent as text."""
        calls, ok = {}, set()
        for message in list(self.agent.messages):
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


SESSIONS: dict[str, Session] = {}


def session_for(session_id: str, payload: dict) -> Session:
    model = payload.get("model") if payload.get("model") in MODELS else "haiku"
    skills = [s for s in payload.get("skills") or [] if isinstance(s, dict)]
    servers = picks_of(payload)
    prompt = str(payload.get("system_prompt") or "")
    key = hashlib.sha1(json.dumps([prompt, model, [[s.get("name"), s.get("content")] for s in skills], servers],
                                  sort_keys=True).encode()).hexdigest()
    current = SESSIONS.get(session_id)
    if current is None or current.key != key:  # a new conversation, or the personality changed
        SESSIONS[session_id] = current = Session(key, prompt, model, skills, servers)
    return current


# --- the entrypoint -----------------------------------------------------------------------------------

@app.entrypoint
async def invoke(payload: dict, context):
    t0 = time.perf_counter()
    payload = payload or {}
    session_id = getattr(context, "session_id", None) or str(payload.get("session") or "local")
    text = str(payload.get("text") or "").strip()
    if not text and not payload.get("warmup"):
        yield {"type": "error", "error": 'Send {"text": "<what the person said>"}'}
        return
    try:
        s = session_for(session_id, payload)
    except Exception as e:  # e.g. a SKILL.md that doesn't parse
        yield {"type": "error", "error": f"{type(e).__name__}: {e}"[:300]}
        return

    async with s.lock:
        if payload.get("warmup"):
            try:
                await s.agent.invoke_async(WARMUP)
            except Exception as e:
                yield {"type": "error", "error": f"{type(e).__name__}: {e}"[:300]}
                return
            s.agent.messages.clear()
            yield {"type": "done", "model": s.model_id, "seconds": round(time.perf_counter() - t0, 2), "skills_loaded": []}
            return

        # Tool hooks and the text stream feed one queue, in the order they happen.
        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        s.emit = lambda event: loop.call_soon_threadsafe(queue.put_nowait, event)
        failed: list[str] = []

        async def consume() -> None:
            try:
                async for event in s.agent.stream_async(text):
                    if "data" in event:
                        s.emit({"type": "text", "data": event["data"]})
                    elif "message" in event and event["message"].get("role") == "assistant":
                        s.emit({"type": "message_end"})
            except Exception as e:
                logger.exception("turn failed")
                failed.append(f"{type(e).__name__}: {e}"[:300])
            finally:
                s.emit(None)

        task = asyncio.create_task(consume())
        try:
            while (event := await queue.get()) is not None:
                yield event
            await task
        finally:
            if not task.done():  # the caller went away mid-reply
                task.cancel()
            s.emit = lambda event: None
        if failed:
            yield {"type": "error", "error": failed[0]}
            return
        seconds = round(time.perf_counter() - t0, 2)
        logger.info(f"turn {s.model_id} {seconds}s: {text[:80]!r}")
        yield {"type": "done", "model": s.model_id, "seconds": seconds, "skills_loaded": s.loaded_skills()}


if __name__ == "__main__":
    app.run()
