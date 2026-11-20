"""The toolbox without a network: what a personality gets, how a skill served over MCP reaches the model, and the
skills kept on the robot. The protocol itself (SEP-2640) is tested against real servers in test_skills_over_mcp.py."""
import io
import zipfile

import pytest

from gizmo.mcp_skills import LoadedSkill, SkillEntry, SkillError, SkillFile
from gizmo.personalities import Selection, ServerPick
from gizmo.toolbox import Toolbox, mask

SKILL_MD = "---\nname: {name}\ndescription: {description}\n---\n\n# How\n\nUse the lookup tool, then read references/more.md.\n"


def entry(server: str, name: str, extra: bool = False) -> SkillEntry:
    root = f"skill://{name}"
    files = [SkillFile(f"{root}/SKILL.md", "sha256:" + "0" * 64, 120)]
    files += [SkillFile(f"{root}/references/more.md", "sha256:" + "1" * 64, 40)] if extra else []
    return SkillEntry(server=server, uri=f"{root}/SKILL.md", name=name, description=f"All about {name}",
                      frontmatter={"name": name, "description": f"All about {name}", "allowed-tools": "Bash(*)"},
                      files=tuple(files))


class Tool:
    def __init__(self, name: str) -> None:
        self.tool_name = name


class Host:
    """Stands in for mcp_skills.SkillsHost: serves what it was given, and remembers what was read."""
    supported, problems = True, []

    def __init__(self, entries: list[SkillEntry], unlisted: list[SkillEntry] = ()) -> None:
        self.entries, self.unlisted, self.read = entries, list(unlisted), []

    def list(self) -> list[SkillEntry]:
        return self.entries

    def get(self, uri: str) -> SkillEntry:
        for e in [*self.entries, *self.unlisted]:
            if e.uri == uri:
                return e
        raise SkillError(f"unknown skill {uri}")

    def load(self, e: SkillEntry) -> LoadedSkill:
        self.read.append(e.uri)
        text = SKILL_MD.format(name=e.name, description=e.description)
        return LoadedSkill(entry=e, text=text, body=text.split("---\n", 2)[2], verified=True)

    def read_file(self, e: SkillEntry, ref: str) -> str:
        self.read.append(ref)
        return "More detail."


@pytest.fixture
def box(tmp_path):
    box = Toolbox(config_file=tmp_path / "servers.json", local_skills=tmp_path / "skills")
    box.tools = {"kb": [Tool("kb_lookup"), Tool("kb_search")], "other": [Tool("other_ping")]}
    box.skills = {"kb": [entry("kb", "refunds", extra=True), entry("kb", "returns")], "other": [entry("other", "refunds")]}
    box.hosts = {"kb": Host(box.skills["kb"], unlisted=[entry("kb", "secret")]), "other": Host(box.skills["other"])}
    return box


def test_a_personality_gets_only_what_it_switched_on(box):
    everything = Selection(servers={"kb": ServerPick(), "other": ServerPick()})
    assert [t.tool_name for t in box.tools_for(everything)] == ["kb_lookup", "kb_search", "other_ping"]
    assert len(box.skills_for(everything)) == 3
    returns = box.skills["kb"][1]
    some = Selection(servers={"kb": ServerPick(tools=frozenset({"lookup"}), skills=frozenset({returns.uri}),
                                               approved=frozenset({(returns.uri, returns.fingerprint)}))})
    assert [t.tool_name for t in box.tools_for(some)] == ["kb_lookup"]  # picked by the server's own tool names
    assert [s.name for s in box.skills_for(some)] == ["returns"]
    assert box.tools_for(Selection()) == [] and box.mcp_skills(Selection()) is None


def test_the_catalog_names_each_skill_with_where_it_comes_from(box):
    plugin = box.mcp_skills(Selection(servers={"kb": ServerPick(), "other": ServerPick()}))
    catalog = plugin.catalog()
    assert "- refunds (server: kb, uri: skill://refunds/SKILL.md): All about refunds" in catalog
    assert "- refunds (server: other, uri: skill://refunds/SKILL.md)" in catalog  # same name, two origins, both kept
    assert box.hosts["kb"].read == []  # building the catalog fetches nothing


def test_loading_a_skill_tags_its_origin_and_never_passes_on_allowed_tools(box):
    plugin = box.mcp_skills(Selection(servers={"kb": ServerPick()}))
    text = plugin.read_skill(server="kb", uri="skill://refunds/SKILL.md")
    assert text.startswith('[Skill "refunds", served by the MCP server "kb" at skill://refunds/SKILL.md')
    assert "Use the lookup tool" in text and "skill://refunds/references/more.md" in text
    assert "allowed-tools" not in text and "Bash" not in text  # frontmatter stays out of the model's context
    assert plugin.loads[("kb", "skill://refunds/SKILL.md")]["verified"] is True
    kind, line, detail = box.describe("read_skill", {"server": "kb", "uri": "skill://refunds/SKILL.md"}, None, plugin)
    assert (kind, line) == ("skill", "loaded refunds from kb") and detail["server"] == "kb" and detail["files"] == 1


def test_a_skill_is_its_server_and_uri_not_its_name(box):
    plugin = box.mcp_skills(Selection(servers={"kb": ServerPick(skills=frozenset({"skill://returns/SKILL.md"})), "other": ServerPick()}))
    assert plugin.read_skill(server="nobody", uri="skill://refunds/SKILL.md").startswith("Error:")
    # refunds is switched off on kb for this personality, though "other" serves a skill of the same name
    assert plugin.read_skill(server="kb", uri="skill://refunds/SKILL.md").startswith("Error:")
    assert 'the MCP server "other"' in plugin.read_skill(server="other", uri="skill://refunds/SKILL.md")
    assert box.hosts["kb"].read == []


def test_an_unlisted_skill_loads_by_uri_when_the_personality_takes_all(box):
    everything = box.mcp_skills(Selection(servers={"kb": ServerPick()}))
    assert 'Skill "secret"' in everything.read_skill(server="kb", uri="skill://secret/SKILL.md")
    assert everything.read_skill(server="kb", uri="skill://nope/SKILL.md").startswith("Error: couldn't load")
    assert everything.loads[("kb", "skill://nope/SKILL.md")]["error"]


def ticked(skill: SkillEntry, mark: str | None = None) -> Selection:
    """A personality with just this skill ticked on kb, approved with `mark` (its fingerprint as it is, by default)."""
    return Selection(servers={"kb": ServerPick(skills=frozenset({skill.uri}),
                                               approved=frozenset({(skill.uri, mark or skill.fingerprint)}))})


def test_a_skill_ticked_on_its_own_loads_only_as_it_was_approved(box):
    returns = box.skills["kb"][1]
    assert returns.fingerprint.startswith("sha256:") and returns.fingerprint != box.skills["kb"][0].fingerprint
    assert [s.name for s in box.skills_for(ticked(returns))] == ["returns"]
    assert 'Skill "returns"' in box.mcp_skills(ticked(returns)).read_skill(server="kb", uri=returns.uri)
    # Ticked, but approved when its files were different: not offered to the model, and not loadable by URI either.
    old = ticked(returns, "sha256:" + "f" * 64)
    stale = box.mcp_skills(old)
    assert box.skills_for(old) == [] and stale.catalog() == ""
    said = stale.read_skill(server="kb", uri=returns.uri)
    assert said.startswith("Error:") and "has changed since it was approved" in said and "console" in said
    assert box.hosts["kb"].read == [returns.uri]  # only the approved load fetched anything
    # A tick with no approval at all (saved before approvals existed) is the same.
    assert box.skills_for(Selection(servers={"kb": ServerPick(skills=frozenset({returns.uri}))})) == []


def test_a_skill_edited_after_it_was_approved_is_refused_when_it_loads(box):
    returns = box.skills["kb"][1]
    edited = SkillEntry(server="kb", uri=returns.uri, name="returns", description=returns.description,
                        frontmatter=returns.frontmatter, files=(SkillFile(returns.uri, "sha256:" + "9" * 64, 130),))
    # The listing is from before the edit: the load notices (digest mismatch), refreshes the entry, and it matches.
    box.hosts["kb"].load = lambda entry: LoadedSkill(entry=edited, text="new", body="new", verified=True)
    plugin = box.mcp_skills(ticked(returns))
    said = plugin.read_skill(server="kb", uri=returns.uri)
    assert said.startswith("Error:") and "has changed since it was approved" in said and "new" not in said
    assert plugin.loads[("kb", returns.uri)]["error"] and ("kb", returns.uri) not in plugin.loaded
    # The console is shown the skill as it is now, so it can be approved again.
    assert box.fingerprints("kb")[returns.uri] == edited.fingerprint != returns.fingerprint
    assert box.skills_for(ticked(returns)) == [] and [s.name for s in box.skills_for(ticked(edited))] == ["returns"]
    # With all of the server's skills on there is no approval to revoke: what the server serves now is what loads.
    assert box.mcp_skills(Selection(servers={"kb": ServerPick()})).read_skill(server="kb", uri=returns.uri).endswith("new")


def test_a_generated_skill_cant_be_approved_on_its_own(box):
    live = SkillEntry(server="kb", uri="skill://live/SKILL.md", name="live", description="Made on request",
                      frontmatter={"name": "live", "description": "Made on request"}, files=None)
    box.skills["kb"].append(live)
    assert live.fingerprint is None and live in box.skills_for(Selection(servers={"kb": ServerPick()}))
    only = Selection(servers={"kb": ServerPick(skills=frozenset({live.uri}), approved=frozenset({(live.uri, "anything")}))})
    assert box.skills_for(only) == []
    assert "generated on request" in box.mcp_skills(only).read_skill(server="kb", uri=live.uri)


def test_other_files_are_read_only_from_a_loaded_skill_on_its_own_server(box):
    plugin = box.mcp_skills(Selection(servers={"kb": ServerPick(), "other": ServerPick()}))
    more = "skill://refunds/references/more.md"
    assert plugin.read_resource(server="kb", uri=more).startswith("Error:")  # not loaded yet
    plugin.read_skill(server="kb", uri="skill://refunds/SKILL.md")
    assert plugin.read_resource(server="kb", uri=more) == "More detail."
    assert plugin.read_resource(server="other", uri=more).startswith("Error:")  # another server's skill of that name
    assert plugin.read_resource(server="kb", uri="skill://returns/SKILL.md").startswith("Error:")
    assert box.hosts["other"].read == []


def test_tool_calls_are_described_for_the_trace(box):
    kind, line, detail = box.describe("kb_lookup", {"q": "refund window"}, {"content": [{"text": "30 days."}]})
    assert (kind, line) == ("mcp", 'kb · lookup(q="refund window")')
    assert detail == {"server": "kb", "name": "lookup", "args": {"q": "refund window"}, "result": "30 days."}
    assert box.describe("see", {"question": "who"})[0] == "tool"
    assert box.describe("skills", {"skill_name": "notes"})[:2] == ("skill", "loaded notes (on this robot)")
    long = box.describe("kb_lookup", {}, {"content": [{"text": "x" * 5000}]})[2]["result"]
    assert len(long) < 1300 and long.endswith("characters in all)")


def test_servers_are_saved_with_their_secrets_masked(box):
    assert [s["name"] for s in box.servers()] == ["explorer", "countries"]  # the defaults, until one is saved
    with pytest.raises(ValueError):
        box.save_server({"name": "Bad Name", "url": "http://x/mcp"})
    with pytest.raises(ValueError):
        box.save_server({"name": "kb", "url": "ftp://x"})
    box.save_server({"name": "kb", "url": "http://kb.test/mcp", "headers": {"Authorization": "Bearer abcdefgh1234"}})
    shown = next(s for s in box.servers() if s["name"] == "kb")
    assert shown["headers"] == {"Authorization": "••••1234"} and mask("short") == "••••"
    # the console sends the masked value back: the saved one is kept, and a rename carries it over
    box.save_server({"name": "docs", "url": "http://kb.test/mcp", "headers": shown["headers"]}, original_name="kb")
    saved = next(s for s in box.config["servers"] if s["name"] == "docs")
    assert saved["headers"]["Authorization"] == "Bearer abcdefgh1234" and "kb" not in box.server_names()
    assert oct(box.config_file.stat().st_mode & 0o777) == "0o600"
    box.delete_server("docs")
    assert "docs" not in box.server_names()


def test_skills_kept_on_the_robot(box):
    assert box.local_skills() == [] and box.local_plugin(Selection(skills=frozenset({"notes"}))) is None
    box.write_local_skill("notes", SKILL_MD.format(name="notes", description="House notes"))
    assert box.local_skills() == [{"name": "notes", "description": "House notes", "files": []}]
    assert box.local_plugin(Selection(skills=frozenset({"notes"}))) is not None and box.local_plugin(Selection()) is None
    with pytest.raises(ValueError):
        box.write_local_skill("other", SKILL_MD.format(name="notes", description="x"))  # the name must match
    texts = box.local_skill_texts(Selection(servers={"kb": ServerPick()}, skills=frozenset({"notes"})))
    assert [t["name"] for t in texts] == ["notes"]  # for a brain elsewhere: only these. It loads served ones itself
    assert box.hosts["kb"].read == []  # so nothing is fetched from a server ahead of need
    box.delete_local_skill("notes")
    with pytest.raises(KeyError):
        box.read_local_skill("notes")


def test_an_uploaded_package_cant_write_outside_its_folder(box):
    def package(files: dict[str, str]) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, content in files.items():
                z.writestr(name, content)
        return buf.getvalue()

    md = SKILL_MD.format(name="packed", description="A packaged skill")
    assert box.install("packed.skill", package({"packed/SKILL.md": md, "packed/references/more.md": "More."})) == "packed"
    assert box.local_skills()[0]["files"] == ["references/more.md"]
    with pytest.raises(ValueError):
        box.install("evil.skill", package({"SKILL.md": md, "../outside.txt": "x"}))
    with pytest.raises(ValueError):
        box.install("empty.zip", package({"readme.txt": "no skill here"}))
    assert box.install("SKILL.md", md.replace("packed", "bare").encode()) == "bare"
