import asyncio
import json

from gizmo.agentcore_brain import AgentCoreBrain


class Body:
    emotions, dances = ["curious1"], []

    def play(self, *a, **k): pass

    def look(self, *a): pass

    def stop(self): pass


class Stream:
    def __init__(self, events):
        self.lines = [f"data: {json.dumps(e)}".encode() for e in events]

    def iter_lines(self, chunk_size=64):
        for line in self.lines:
            yield line
            yield b""


class Client:
    def __init__(self, events):
        self.events, self.calls = events, []

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(json.loads(kwargs["payload"]))
        return {"contentType": "text/event-stream; charset=utf-8", "response": Stream(self.events)}


class Session:
    def __init__(self, client):
        self._client = client

    def client(self, name):
        return self._client


class Fallback:
    async def think(self, heard):
        return {"say": "From the robot.", "actions": [], "model": "haiku", "seconds": 0.1}

    async def warm_up(self):
        return 0.0


def brain(events, fallback=None):
    client = Client(events)
    tools = []
    picks = {"explorer": {"tools": None, "skills": ["skill://universe/SKILL.md"],
                          "approved": {"skill://universe/SKILL.md": "sha256:abc"}}}
    b = AgentCoreBrain(Session(client), Body(), "arn:aws:bedrock-agentcore:us-east-1:1:runtime/gizmo_demo_brain-x",
                       "prompt", [{"name": "house-notes", "content": "---\nname: house-notes\n---\n"}], picks,
                       fallback=fallback, on_tool=lambda kind, text, detail: tools.append((kind, text, detail)))
    return b, client, tools


def test_a_turn_streams_text_tools_and_done():
    b, client, tools = brain([
        {"type": "text", "data": "Let me check. "}, {"type": "message_end"},
        {"type": "tool", "kind": "skill", "text": "loaded universe from explorer 0.1s", "seconds": 0.1,
         "detail": {"skill": "universe", "server": "explorer", "uri": "skill://universe/SKILL.md", "verified": True}},
        {"type": "tool", "kind": "mcp", "text": "explorer · space_station() 0.4s", "seconds": 0.4,
         "detail": {"server": "explorer", "name": "space_station", "args": {}, "result": "Over the Pacific."}},
        {"type": "text", "data": "[emotion:curious] It's over the Pacific. "}, {"type": "message_end"},
        {"type": "done", "model": "us.anthropic.claude-haiku-5-5", "seconds": 2.0,
         "skills_loaded": [{"name": "universe", "server": "explorer"}]},
    ])
    result = asyncio.run(b.think("Where is the space station?"))
    assert result["say"] == "It's over the Pacific."  # the narration before the tool isn't said
    assert result["actions"] == [("emotion", "curious")]
    # The runtime loaded the skill itself, from its own server, and says so: the console's trace gets the detail.
    assert [(kind, text) for kind, text, _ in tools] == [
        ("skill", "loaded universe from explorer 0.1s (AgentCore)"), ("mcp", "explorer · space_station() 0.4s (AgentCore)")]
    assert tools[0][2] == {"skill": "universe", "server": "explorer", "uri": "skill://universe/SKILL.md",
                           "verified": True, "where": "agentcore", "ms": 100}
    assert tools[1][2]["result"] == "Over the Pacific." and tools[1][2]["ms"] == 400
    assert b.loaded_skills() == [{"name": "universe", "server": "explorer"}]
    # What goes to the runtime: the personality's picks with its approvals, and only the robot's own skills as text.
    sent = client.calls[0]
    assert sent["servers"]["explorer"]["approved"] == {"skill://universe/SKILL.md": "sha256:abc"}
    assert [s["name"] for s in sent["skills"]] == ["house-notes"] and sent["mcp"] == ["explorer"] and sent["model"] == "haiku"


def test_a_runtime_from_before_skills_over_mcp_still_reports_its_skills():
    b, _, _ = brain([{"type": "text", "data": "Hi. "}, {"type": "done", "model": "m", "seconds": 1.0, "skills_loaded": ["universe"]}])
    asyncio.run(b.think("Hello?"))
    assert b.loaded_skills() == [{"name": "universe", "server": None}]


def test_an_error_before_any_words_falls_back_to_the_robot():
    b, _, _ = brain([{"error": "boom", "error_type": "RuntimeError"}], fallback=Fallback())
    assert asyncio.run(b.think("Hello?"))["say"] == "From the robot."
