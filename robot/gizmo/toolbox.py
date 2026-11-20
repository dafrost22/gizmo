"""What a personality can use: MCP servers, the skills those servers serve, and skills kept on the robot.

MCP servers (~/.config/gizmo/servers.json, out of git: headers can hold tokens):
    {"servers": [{"name": "kb", "url": "https://.../mcp", "headers": {"Authorization": "Bearer ..."}}]}
Each server is connected once, through Strands' MCPClient. Its tools reach the model prefixed with its name
(explorer_space_station), and if it declares the Skills extension (SEP-2640, io.modelcontextprotocol/skills) the
skills it serves are listed too. Which of all that a personality gets is the personality's choice
(personalities.Selection), not the server's.

Skills served over MCP follow the SEP's host rules (mcp_skills.SkillsHost does the protocol and the checks):
- The registry is built from skills/list alone. A SKILL.md is fetched when the model asks for it, and not before.
- The model sees each skill's name and description with where it comes from, and loads one with
  read_skill(server, uri). What comes back is checked against the listing (size, SHA-256, frontmatter) and tagged
  with the server it came from: it is that server's text, not the developer's.
- read_resource(server, uri) reads a loaded skill's other files, from that same server only.
- A skill's allowed-tools is ignored, and nothing a skill says can run code here: Gizmo has no such tool.

Skills kept on the robot (~/.config/gizmo/skills/<name>/SKILL.md plus any resources) go through Strands' own
AgentSkills plugin, as on any Strands agent. Write one on the console, or upload a packaged skill: a .skill file
(a zip of <name>/SKILL.md and its resources), a .zip, or a bare SKILL.md.
"""

from __future__ import annotations

import io
import json
import logging
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from strands.tools.mcp import MCPClient
from strands.vended_plugins.skills import AgentSkills
from strands.vended_plugins.skills.skill import Skill

from gizmo.mcp_skills import McpSkills, SkillEntry, SkillsHost
from gizmo.personalities import Selection

logger = logging.getLogger("gizmo.toolbox")

CONFIG_DIR = Path.home() / ".config" / "gizmo"
LOCAL_SKILLS = CONFIG_DIR / "skills"
CONFIG_FILE = CONFIG_DIR / "servers.json"
SERVER_NAME = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MASK = "••••"
MAX_PACKAGE_BYTES = 10 * 1024 * 1024  # an uploaded .skill file
MAX_UNPACKED_BYTES = 25 * 1024 * 1024
MAX_PACKAGE_FILES = 200
PREVIEW_CHARS = 1200  # how much of a tool's result the console's trace keeps
# Until servers.json lists servers of its own: the demo server inside this app (gizmo/demo_server: everyday
# tools, live space data, and the skills it serves over MCP) and countries.dev, a public server with no key.
DEFAULT_SERVERS = [
    {"name": "explorer", "url": "http://127.0.0.1:8772/mcp"},
    {"name": "countries", "url": "https://countries.dev/api/mcp"},
]


def mask(value: str) -> str:
    """Show a secret as its last four characters, so the console never holds the token itself."""
    return MASK + value[-4:] if len(value) > 8 else MASK


def preview(result: dict | None) -> str:
    """The text of a Strands tool result, shortened for the console."""
    text = "\n".join(c["text"] for c in (result or {}).get("content", []) if isinstance(c, dict) and "text" in c)
    return text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + f"… ({len(text):,} characters in all)"


class Toolbox:
    def __init__(self, config_file: Path = CONFIG_FILE, local_skills: Path = LOCAL_SKILLS) -> None:
        self.config_file = config_file
        self.local_dir = local_skills
        self.config = self._read_config()
        self.clients: dict[str, MCPClient] = {}
        self.hosts: dict[str, SkillsHost] = {}
        self.tools: dict[str, list] = {}  # server -> its Strands tools
        self.skills: dict[str, list[SkillEntry]] = {}  # server -> the skills it lists
        self.status: dict[str, dict] = {}

    def _read_config(self) -> dict:
        try:
            config = json.loads(self.config_file.read_text())
        except (OSError, ValueError):
            config = {}
        config.setdefault("servers", [dict(s) for s in DEFAULT_SERVERS])  # saved once the console changes one
        return config

    def _save_config(self) -> None:
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        self.config_file.write_text(json.dumps(self.config, indent=2))
        self.config_file.chmod(0o600)

    # --- MCP servers -------------------------------------------------------------------------

    def start(self) -> None:
        """Connect to the configured MCP servers. A server that fails is skipped (and reported)."""
        for server in self.config.get("servers", []):
            name = server.get("name") or "server"
            try:
                self._adopt(name, *self._connect(server))
            except Exception as e:
                logger.warning(f"MCP server {name} unavailable: {e!r}")
                self.status[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}

    def _adopt(self, name: str, client: MCPClient, tools: list, host: SkillsHost, skills: list[SkillEntry],
               seconds: float) -> None:
        self.clients[name], self.hosts[name], self.tools[name], self.skills[name] = client, host, tools, skills
        self.status[name] = self._describe_server(name, tools, host, skills, seconds)
        logger.info(f"MCP server {name}: {len(tools)} tools, skills {[s.name for s in skills]}"
                    f"{'' if host.supported else ' (no Skills extension)'}")

    @staticmethod
    def _connect(server: dict) -> tuple[MCPClient, list, SkillsHost, list[SkillEntry], float]:
        t0 = time.perf_counter()
        name = server.get("name") or "server"
        client = MCPClient(url=server["url"], headers=server.get("headers") or None, prefix=name)
        client.start()
        try:
            tools = client.list_tools_sync()
            host = SkillsHost(name, client)
            skills = host.list()  # the listing only: no SKILL.md is fetched until the model asks for it
            return client, tools, host, skills, time.perf_counter() - t0
        except Exception:
            client.stop(None, None, None)
            raise

    @staticmethod
    def _describe_server(name: str, tools: list, host: SkillsHost, skills: list[SkillEntry], seconds: float) -> dict:
        """What the console's inspector shows for a connected server."""
        def spec(t) -> dict:
            s = t.tool_spec
            return {"name": t.tool_name.removeprefix(f"{name}_"), "description": (s.get("description") or "").strip(),
                    "schema": (s.get("inputSchema") or {}).get("json") or {}}

        return {"ok": True, "connect_ms": round(seconds * 1000), "tools": [spec(t) for t in tools],
                "skills_extension": host.supported, "skills": [s.as_dict() for s in skills],
                "problems": list(host.problems)}

    def stop(self) -> None:
        for client in self.clients.values():
            try:
                client.stop(None, None, None)
            except Exception:
                pass
        self.clients, self.hosts, self.tools, self.skills, self.status = {}, {}, {}, {}, {}

    def reload(self) -> None:
        """After a change from the console: re-read the config and reconnect."""
        self.stop()
        self.config = self._read_config()
        self.start()

    def refresh(self, name: str) -> None:
        """Ask one connected server again what it offers (you changed a tool or a skill on it)."""
        client, host = self.clients[name], self.hosts[name]
        t0 = time.perf_counter()
        tools = client.list_tools_sync()
        skills = host.list()
        self._adopt(name, client, tools, host, skills, time.perf_counter() - t0)

    def test_server(self, server: dict) -> dict:
        """Connect once to see what a server offers, without adopting it."""
        try:
            client, tools, host, skills, seconds = self._connect(self._with_saved_secrets(server))
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
        client.stop(None, None, None)
        return self._describe_server(server.get("name") or "server", tools, host, skills, seconds)

    def servers(self) -> list[dict]:
        """For the console: headers masked."""
        out = []
        for s in self.config.get("servers", []):
            name = s.get("name") or "server"
            out.append({"name": name, "url": s.get("url", ""),
                        "headers": {k: mask(str(v)) for k, v in (s.get("headers") or {}).items()},
                        "status": self.status.get(name, {"ok": False, "error": "not connected"})})
        return out

    def server_names(self) -> list[str]:
        return [s.get("name") or "server" for s in self.config.get("servers", [])]

    def _with_saved_secrets(self, server: dict) -> dict:
        """The console sends masked header values back unchanged: put the real ones back in."""
        old = next((s for s in self.config.get("servers", []) if s.get("name") == server.get("name")), {})
        old_headers = old.get("headers") or {}
        headers = {k: (old_headers.get(k, "") if str(v).startswith(MASK) else v)
                   for k, v in (server.get("headers") or {}).items() if k}
        return {"name": server.get("name") or "server", "url": str(server.get("url", "")).strip(), "headers": headers}

    def save_server(self, server: dict, original_name: str | None = None) -> str:
        clean = self._with_saved_secrets({**server, "name": original_name or server.get("name")})
        clean["name"] = server.get("name") or clean["name"]
        if not SERVER_NAME.match(clean["name"]):
            raise ValueError("name: lowercase letters, digits and _ (it prefixes the server's tool names)")
        if not clean["url"].startswith(("http://", "https://")):
            raise ValueError("the URL starts with http:// or https://")
        keep = [s for s in self.config.get("servers", []) if s.get("name") not in (original_name, clean["name"])]
        self.config["servers"] = keep + [clean]
        self._save_config()
        return clean["name"]

    def delete_server(self, name: str) -> None:
        self.config["servers"] = [s for s in self.config.get("servers", []) if s.get("name") != name]
        self._save_config()

    def call_tool(self, server: str, tool_name: str, arguments: dict) -> dict:
        """Run one of a server's tools by hand, from the console's inspector."""
        if server not in self.clients:
            raise KeyError(server)
        t0 = time.perf_counter()
        result = self.clients[server].call_tool_sync(f"console-{uuid.uuid4().hex[:8]}", tool_name, arguments or {})
        return {"ok": result.get("status") != "error", "ms": round((time.perf_counter() - t0) * 1000),
                "text": preview(result)}

    # --- what one personality gets -----------------------------------------------------------

    def tools_for(self, selection: Selection) -> list:
        """The MCP tools this personality's brain gets."""
        return [t for name, pick in selection.servers.items() for t in self.tools.get(name, [])
                if pick.has_tool(t.tool_name.removeprefix(f"{name}_"))]

    def skills_for(self, selection: Selection) -> list[SkillEntry]:
        """The skills served over MCP that this personality may load as they are now. One it ticked on its own
        and whose files have changed since isn't among them until it's approved again."""
        return [s for name, pick in selection.servers.items() for s in self.skills.get(name, [])
                if pick.approves(s.uri, s.fingerprint)]

    def fingerprints(self, server: str) -> dict[str, str | None]:
        """Each listed skill's fingerprint, by URI: what ticking it approves."""
        return {s.uri: s.fingerprint for s in self.skills.get(server, [])}

    def mcp_skills(self, selection: Selection) -> McpSkills | None:
        """A plugin for one brain agent with this personality's MCP-served skills, or None when it has none."""
        hosts = {name: self.hosts[name] for name in selection.servers if self.hosts.get(name) and self.hosts[name].supported}
        return McpSkills(hosts, selection.servers, self.skills_for(selection), on_refreshed=self._refreshed) if hosts else None

    def _refreshed(self, entry: SkillEntry) -> None:
        """A load found that a skill had changed on its server since it was listed: keep the current entry, so
        the console shows the skill as it is now (and that an approval of the old one no longer covers it)."""
        listed = self.skills.get(entry.server)
        if listed is None:
            return
        self.skills[entry.server] = [entry if s.uri == entry.uri else s for s in listed]
        if isinstance(self.status.get(entry.server, {}).get("skills"), list):
            self.status[entry.server]["skills"] = [s.as_dict() for s in self.skills[entry.server]]

    def local_plugin(self, selection: Selection) -> AgentSkills | None:
        """Strands' AgentSkills for this personality's skills kept on the robot (each agent gets its own: the
        plugin keeps per-agent state), or None when it has none."""
        local = [skill for name, skill in self._local_skills().items() if name in selection.skills]
        return AgentSkills(skills=local) if local else None

    def local_skill_texts(self, selection: Selection) -> list[dict]:
        """This personality's skills kept on the robot, as SKILL.md text, for a brain running somewhere else
        (AgentCore). Skills served over MCP aren't sent: that brain is a host of its own, with its own
        connections to the servers, and loads them itself when the model asks."""
        return [{"name": name, "content": (self.local_dir / name / "SKILL.md").read_text()}
                for name in self._local_skills() if name in selection.skills]

    def describe(self, name: str, tool_input: dict, result: dict | None = None,
                 plugin: McpSkills | None = None) -> tuple[str, str, dict]:
        """How the console logs a tool call: (kind, one line, detail). kind is skill, mcp or tool."""
        tool_input = tool_input or {}
        if name == "skills":  # Strands' AgentSkills: a skill kept on the robot
            skill = tool_input.get("skill_name", "?")
            return "skill", f"loaded {skill} (on this robot)", {"skill": skill, "server": None}
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
        detail = {"args": tool_input, "result": preview(result)}
        owner = next((s for s, tools in self.tools.items() if any(t.tool_name == name for t in tools)), None)
        if owner:
            bare = name.removeprefix(f"{owner}_")
            return "mcp", f"{owner} · {bare}({args})", {"server": owner, "name": bare, **detail}
        return "tool", f"{name}({args})", {"name": name, **detail}

    # --- skills kept on the robot ------------------------------------------------------------

    def _local_skills(self) -> dict[str, Skill]:
        found: dict[str, Skill] = {}
        for md in sorted(self.local_dir.glob("*/SKILL.md")):
            if md.parent.name.startswith("."):
                continue  # an upload still being unpacked
            try:
                found[md.parent.name] = Skill.from_file(md.parent)
            except Exception as e:
                logger.warning(f"Skipping the skill in {md.parent}: {e!r}")
        return found

    def local_skills(self) -> list[dict]:
        return [{"name": n, "description": s.description,
                 "files": sorted(str(p.relative_to(self.local_dir / n)) for p in (self.local_dir / n).rglob("*")
                                 if p.is_file() and p.name != "SKILL.md")}
                for n, s in self._local_skills().items()]

    def read_local_skill(self, name: str) -> str:
        md = self.local_dir / name / "SKILL.md"
        if not SKILL_NAME.match(name) or not md.exists():
            raise KeyError(name)
        return md.read_text()

    def write_local_skill(self, name: str, content: str) -> None:
        """Create or replace a skill on the robot. The SKILL.md must parse, and its name match."""
        if not SKILL_NAME.match(name):
            raise ValueError("skill names are lowercase letters, digits and hyphens")
        skill = Skill.from_content(content)  # raises on a bad SKILL.md
        if skill.name != name:
            raise ValueError(f"the frontmatter says name: {skill.name}, but this skill is {name}")
        folder = self.local_dir / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text(content)

    def delete_local_skill(self, name: str) -> None:
        folder = self.local_dir / name
        if not SKILL_NAME.match(name) or not (folder / "SKILL.md").exists():
            raise KeyError(name)
        shutil.rmtree(folder)

    def install(self, filename: str, data: bytes) -> str:
        """An uploaded skill: a package (.skill or .zip: <name>/SKILL.md plus its resources) or a bare SKILL.md.
        It replaces any skill on the robot with the same name."""
        if not data or len(data) > MAX_PACKAGE_BYTES:
            raise ValueError("the file is empty or bigger than 10 MB")
        if zipfile.is_zipfile(io.BytesIO(data)):
            files = self._unpack(data)
        elif filename.lower().endswith(".md") or data.lstrip().startswith(b"---"):
            files = {"SKILL.md": data}
        else:
            raise ValueError("upload a .skill package (or a .zip of a skill folder), or a SKILL.md")
        try:
            skill = Skill.from_content(files["SKILL.md"].decode("utf-8"))
        except Exception as e:
            raise ValueError(f"its SKILL.md doesn't parse: {e}") from e
        if not SKILL_NAME.match(skill.name):
            raise ValueError(f"the skill's name ({skill.name}) must be lowercase letters, digits and hyphens")
        staging = self.local_dir / f".{skill.name}.upload"
        shutil.rmtree(staging, ignore_errors=True)
        try:
            for rel, content in files.items():
                path = staging / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            shutil.rmtree(self.local_dir / skill.name, ignore_errors=True)
            staging.rename(self.local_dir / skill.name)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        logger.info(f"installed skill {skill.name} from {filename}: {sorted(files)}")
        return skill.name

    @staticmethod
    def _unpack(data: bytes) -> dict[str, bytes]:
        """The skill's files from a package, relative to its SKILL.md, refusing anything that could land outside
        the skill's folder."""
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = [i for i in z.infolist() if not i.is_dir() and not i.filename.startswith("__MACOSX/")
                     and not PurePosixPath(i.filename).name.startswith(".")]
            mds = [PurePosixPath(i.filename) for i in infos if PurePosixPath(i.filename).name == "SKILL.md"]
            if not mds:
                raise ValueError("there's no SKILL.md in that package")
            root = min((p.parent for p in mds), key=lambda p: len(p.parts))
            if len(root.parts) > 1:
                raise ValueError("SKILL.md should be at the top of the package, or in its one folder")
            files, total = {}, 0
            for info in infos:
                path = PurePosixPath(info.filename)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError(f"unsafe path in the package: {info.filename}")
                if root.parts and path.parts[: len(root.parts)] != root.parts:
                    continue  # outside the skill's folder
                total += info.file_size
                if total > MAX_UNPACKED_BYTES or len(files) >= MAX_PACKAGE_FILES:
                    raise ValueError("that package is too big (25 MB or 200 files at most)")
                files[str(PurePosixPath(*path.parts[len(root.parts):]))] = z.read(info)
        return files

    def summary(self) -> dict:
        return {"servers": {name: bool(self.status.get(name, {}).get("ok")) for name in self.server_names()},
                "local_skills": sorted(self._local_skills())}
