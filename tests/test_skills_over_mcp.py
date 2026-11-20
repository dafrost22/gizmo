"""Skills over MCP (SEP-2640), both ends: demo_server/skills_ext.py serves a folder of skills, and
mcp_skills.SkillsHost lists, checks and reads them. Each test talks real streamable HTTP to a server on a free
localhost port, through Strands' MCPClient as the robot does. No robot, AWS or internet."""
import asyncio
import hashlib
import json
import logging
import socket
import threading
import time
from collections import Counter
from concurrent.futures import Future
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn
import yaml
from mcp.server.extension import Extension, MethodBinding
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.resources import FunctionResource
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, PaginatedRequestParams, RequestParams
from strands.tools.mcp import MCPClient

from gizmo import mcp_skills
from gizmo.demo_server.skills_ext import serve_skills, skill_uris
from gizmo.mcp_skills import EXTENSION_ID, SkillEntry, SkillError, SkillsHost

MIB = 1024 * 1024
ALPHA = """---
name: alpha
description: Answers questions about alpha. Load it for anything alpha.
license: Apache-2.0
metadata:
  version: "1.2"
  tags: [first, second]
  stable: true
  released: 2026-01-02
---

# Alpha

Read references/guide.md first, then My Notes.md.
"""


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def skill_md(name: str, description: str = "Does one thing.") -> bytes:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\nThe body of {name}.\n".encode()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def connected(mcp: MCPServer, label: str = "test"):
    """Serve `mcp` on a free port in a background thread, and connect to it the way the robot does."""
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(mcp.streamable_http_app(stateless_http=True), host="127.0.0.1", port=port,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    client = MCPClient(url=f"http://127.0.0.1:{port}/mcp")
    client.start()
    try:
        yield SkillsHost(label, client), client
    finally:
        client.stop(None, None, None)
        server.should_exit = True


# --- a real folder of skills, served by skills_ext ----------------------------------------------------

@pytest.fixture
def folder(tmp_path: Path) -> Path:
    alpha = tmp_path / "alpha"
    (alpha / "references").mkdir(parents=True)
    (alpha / "assets").mkdir()
    (alpha / "__pycache__").mkdir()
    (alpha / "SKILL.md").write_text(ALPHA)
    (alpha / "references" / "guide.md").write_text("# Guide\n\nAlpha comes first.\r\n")  # the \r\n must survive
    (alpha / "My Notes.md").write_text("Notes with a space in the file's name, and an é.\n", encoding="utf-8")
    (alpha / "assets" / "logo.bin").write_bytes(bytes(range(256)))
    (alpha / ".secret").write_text("hidden files are not served")
    (alpha / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    (tmp_path / "beta").mkdir()
    (tmp_path / "beta" / "SKILL.md").write_bytes(skill_md("beta"))
    (tmp_path / "beta" / "data.txt").write_text("beta's own file\n")
    (tmp_path / "wrong-name").mkdir()
    (tmp_path / "wrong-name" / "SKILL.md").write_bytes(skill_md("right-name"))
    (tmp_path / "no-frontmatter").mkdir()
    (tmp_path / "no-frontmatter" / "SKILL.md").write_text("# Just markdown\n")
    return tmp_path


@pytest.fixture
def host(folder: Path):
    mcp = MCPServer("skills-test", log_level="WARNING")
    serve_skills(mcp, folder)
    with connected(mcp) as (host, _):
        yield host


def count_reads(host: SkillsHost) -> list[str]:
    """Every resources/read the host sends from now on, by URI."""
    reads, real = [], host._read
    host._read = lambda uri: (reads.append(uri), real(uri))[1]
    return reads


def test_the_capability_is_declared_and_found(host):
    assert host.supported and not host.directory_read  # resources/directory/read isn't served, so it isn't claimed


def test_listing_gives_frontmatter_verbatim_and_every_file_with_its_digest(host, folder):
    reads = count_reads(host)
    alpha, beta = host.list()
    assert host.problems == [] and reads == []  # the two bad folders never reach the wire; no file was fetched
    assert (alpha.server, alpha.uri, alpha.name) == ("test", "skill://alpha/SKILL.md", "alpha")
    assert alpha.root == "skill://alpha"
    # Every field the author wrote, not only name and description. YAML's date is JSON's text.
    assert alpha.frontmatter == yaml.safe_load(ALPHA.split("---")[1].replace("2026-01-02", '"2026-01-02"'))
    assert alpha.frontmatter["license"] == "Apache-2.0" and alpha.frontmatter["metadata"] == {
        "version": "1.2", "tags": ["first", "second"], "stable": True, "released": "2026-01-02"}
    assert alpha.description == "Answers questions about alpha. Load it for anything alpha."
    on_disk = {"skill://alpha/SKILL.md": "SKILL.md", "skill://alpha/My%20Notes.md": "My Notes.md",
               "skill://alpha/assets/logo.bin": "assets/logo.bin",
               "skill://alpha/references/guide.md": "references/guide.md"}
    assert {f.uri for f in alpha.files} == set(on_disk) and alpha.files[0].uri == alpha.uri
    for f in alpha.files:
        data = (folder / "alpha" / on_disk[f.uri]).read_bytes()
        assert (f.digest, f.size) == (digest(data), len(data)), f.uri
    assert alpha.total_bytes == sum(f.size for f in alpha.files) and not alpha.dynamic
    assert [f.uri for f in beta.files] == ["skill://beta/SKILL.md", "skill://beta/data.txt"]
    shown = json.loads(json.dumps(alpha.as_dict()))  # what the console gets is plain JSON
    assert shown["server"] == "test" and shown["files"][0] == {"uri": alpha.uri, "digest": alpha.files[0].digest,
                                                               "size": alpha.files[0].size}
    assert len({alpha, beta}) == 2  # entries can be kept in sets and as dictionary keys


def test_the_server_leaves_out_a_skill_that_breaks_the_format_and_says_why(folder, caplog):
    with caplog.at_level(logging.WARNING):
        serve_skills(MCPServer("skills-test", log_level="WARNING"), folder)
    assert "'wrong-name' is not served: the frontmatter's name is 'right-name'" in caplog.text
    assert "'no-frontmatter' is not served: SKILL.md must start with YAML frontmatter" in caplog.text
    assert skill_uris(folder) == ["skill://alpha/SKILL.md", "skill://beta/SKILL.md"]


def test_get_answers_for_a_skill_and_refuses_anything_else(host):
    alpha = host.list()[0]
    assert host.get("skill://alpha/SKILL.md") == alpha
    for uri in ["skill://nobody/SKILL.md", "skill://wrong-name/SKILL.md", "skill://alpha/references/SKILL.md"]:
        with pytest.raises(SkillError, match=r"Unknown skill: .* \(error -32602\)"):
            host.get(uri)
    with pytest.raises(SkillError, match=r"must end in /SKILL\.md"):
        host.get("skill://alpha/references/guide.md")
    with pytest.raises(SkillError, match=r"Unknown cursor: later \(error -32602\)"):  # one page, so no cursors
        host._request(mcp_skills._ListRequest(params=PaginatedRequestParams(cursor="later")), "skills/list")
    page = host._request(mcp_skills._ListRequest(), "skills/list")  # the result as it is on the wire
    assert (page["resultType"], page["ttlMs"], page["cacheScope"]) == ("complete", 0, "private")
    assert "nextCursor" not in page
    assert [sorted(skill) for skill in page["skills"]] == [["frontmatter", "resources", "uri"]] * 2
    assert sorted(page["skills"][0]["resources"][0]) == ["digest", "size", "uri"]
    # skills/get is cacheable too: the SEP left that open, and the extension's spec page requires both fields.
    one = host._request(mcp_skills._GetRequest(params=mcp_skills._GetParams(uri=alpha.uri)), "skills/get")
    assert (one["resultType"], one["ttlMs"], one["cacheScope"]) == ("complete", 0, "private")
    assert "nextCursor" not in one and one["skill"] == page["skills"][0]


def test_load_checks_the_skill_and_returns_its_body(host):
    alpha = host.list()[0]
    reads = count_reads(host)
    loaded = host.load(alpha)
    assert loaded.verified and loaded.entry is alpha and loaded.text == ALPHA
    assert loaded.body.startswith("# Alpha\n") and "description:" not in loaded.body
    assert host.load(alpha).text == ALPHA and reads == [alpha.uri]  # the second load came from memory


def test_an_edit_after_listing_is_picked_up_through_skills_get(host, folder):
    alpha = host.list()[0]
    edited = ALPHA.replace("Load it for anything alpha.", "Now with more.").replace("# Alpha", "# Alpha, edited")
    (folder / "alpha" / "SKILL.md").write_text(edited)
    (folder / "alpha" / "added.md").write_text("written after the listing\n")
    loaded = host.load(alpha)  # the old digest fails, the entry is refreshed, and the new one matches
    assert loaded.verified and loaded.text == edited and loaded.body.startswith("# Alpha, edited")
    assert loaded.entry != alpha and loaded.entry.description.endswith("Now with more.")
    assert loaded.entry.files[0].digest == digest(edited.encode()) != alpha.files[0].digest
    with pytest.raises(SkillError, match=r"skill://alpha/added\.md is not one of the files alpha lists"):
        host.read_file(alpha, "added.md")  # the entry held from before doesn't have it
    assert host.read_file(loaded.entry, "added.md") == "written after the listing\n"


def test_a_file_edited_while_its_skill_is_in_use_is_refused(host, folder):
    alpha = host.load(host.list()[0]).entry
    assert host.read_file(alpha, "references/guide.md").startswith("# Guide")
    (folder / "alpha" / "references" / "guide.md").write_text("# Guide\n\nAlpha comes last.\r\n")
    assert host.read_file(alpha, "references/guide.md").endswith("first.\r\n")  # what was checked is what is kept
    (folder / "alpha" / "My Notes.md").write_text("Something else entirely.\n")
    with pytest.raises(SkillError, match="has changed on test since it was loaded: load it again"):
        host.read_file(alpha, "My Notes.md")


def test_read_file_resolves_paths_inside_the_skill_only(host):
    alpha, beta = host.list()
    guide = "# Guide\n\nAlpha comes first.\r\n"
    for ref in ["references/guide.md", "./references/guide.md", "assets/../references/guide.md",
                "references/guide.md#alpha", "skill://alpha/references/guide.md"]:
        assert host.read_file(alpha, ref) == guide, ref
    assert host.read_file(alpha, "My Notes.md") == host.read_file(alpha, "My%20Notes.md") != ""
    assert host.read_file(alpha, "SKILL.md") == ALPHA
    refused = {"../beta/data.txt": "leads out of the skill", "references/../../beta/data.txt": "leads out of the skill",
               "skill://beta/data.txt": "is outside the skill", "skill://alpha/../beta/data.txt": "is outside",
               "skill://alpha/%2e%2e/beta/data.txt": "is outside the skill",
               "https://example.com/guide.md": "is outside the skill", "file:///etc/passwd": "is outside the skill",
               "/etc/passwd": "is not a path inside the skill", "": "is not a path inside the skill",
               "references/missing.md": "is not one of the files alpha lists", "assets/logo.bin": "is not text"}
    for ref, why in refused.items():
        with pytest.raises(SkillError, match=why):
            host.read_file(alpha, ref)
    assert host.read_file(beta, "data.txt") == "beta's own file\n"


def test_a_skill_is_only_read_from_the_server_that_listed_it(host):
    alpha = host.list()[0]
    elsewhere = SkillEntry(server="another", uri=alpha.uri, name="alpha", description=alpha.description,
                           frontmatter=alpha.frontmatter, files=alpha.files)
    for read in (lambda: host.load(elsewhere), lambda: host.read_file(elsewhere, "references/guide.md")):
        with pytest.raises(SkillError, match="only ever read from the server that serves it"):
            read()


def test_the_files_are_ordinary_resources_too(folder):
    mcp = MCPServer("skills-test", log_level="WARNING")
    serve_skills(mcp, folder)
    with connected(mcp) as (_, client):
        listed = {str(r.uri): r for r in client.list_resources_sync().resources}
        skill = listed["skill://alpha/SKILL.md"]
        assert (skill.name, skill.mime_type) == ("alpha", "text/markdown") and skill.description.startswith("Answers")
        assert listed["skill://alpha/references/guide.md"].name == "alpha/references/guide.md"
        assert not any(".secret" in uri or "pycache" in uri or "wrong-name" in uri for uri in listed)
        assert client.read_resource_sync("skill://alpha/SKILL.md").contents[0].text == ALPHA
        assert client.read_resource_sync("skill://alpha/assets/logo.bin").contents[0].blob  # not UTF-8: a blob


# --- a server that doesn't keep the rules -----------------------------------------------------------

class GetParams(RequestParams):
    uri: str


class Handwritten(Extension):
    """The Skills extension with entries written out by hand, so a test can make a server say anything."""

    identifier = EXTENSION_ID

    def __init__(self, listed: list, unlisted: list, page: int) -> None:
        self.listed, self.unlisted, self.page = listed, unlisted, page

    def methods(self):
        return [MethodBinding("skills/list", PaginatedRequestParams, self.list_skills),
                MethodBinding("skills/get", GetParams, self.get_skill)]

    async def list_skills(self, ctx, params):
        start = int(params.cursor or 0)
        more = {"nextCursor": str(start + self.page)} if start + self.page < len(self.listed) else {}
        return {"skills": self.listed[start:start + self.page], **more}

    async def get_skill(self, ctx, params):
        for entry in self.listed + self.unlisted:
            if isinstance(entry, dict) and entry.get("uri") == params.uri:
                return {"skill": entry}
        raise MCPError(code=INVALID_PARAMS, message=f"Unknown skill: {params.uri}")


def honest(name: str, path: str = "", **more: bytes) -> tuple[dict, dict[str, bytes]]:
    """A correct entry for a skill at skill://<path><name>, and the files behind it."""
    root = f"skill://{path}{name}"
    files = {f"{root}/SKILL.md": skill_md(name), **{f"{root}/{relative}": data for relative, data in more.items()}}
    resources = [{"uri": uri, "digest": digest(data), "size": len(data)} for uri, data in files.items()]
    frontmatter = {"name": name, "description": "Does one thing."}
    return {"uri": f"{root}/SKILL.md", "frontmatter": frontmatter, "resources": resources}, files


@contextmanager
def handwritten(listed: list, files: dict[str, bytes], unlisted: list = (), page: int = 100):
    """Connect to a server that lists exactly `listed` and serves `files`. Yields (host, reads by URI)."""
    mcp = MCPServer("handwritten", extensions=[Handwritten(list(listed), list(unlisted), page)], log_level="WARNING")
    reads: Counter = Counter()

    def reader(uri: str, data: bytes):
        def read() -> str | bytes:
            reads[uri] += 1
            try:
                return data.decode()
            except UnicodeDecodeError:
                return data
        return read

    for uri, data in files.items():
        mcp.add_resource(FunctionResource(uri=uri, name=uri, fn=reader(uri, data)))
    with connected(mcp, "handwritten") as (host, _):
        yield host, reads


def test_entries_that_break_the_rules_are_dropped_and_the_rest_listed_across_pages():
    files: dict[str, bytes] = {}
    good = []
    for name, path in [("first", ""), ("refunds", "acme/billing/"), ("refunds", "support/"), ("last", "")]:
        entry, its_files = honest(name, path, **{"notes.md": b"notes\n"})
        good.append(entry)
        files |= its_files
    misnamed = honest("misnamed")[0] | {"frontmatter": {"name": "something-else", "description": "Named wrongly."}}
    no_resources = {k: v for k, v in honest("no-resources")[0].items() if k != "resources"}
    odd_resources = honest("odd-resources")[0] | {"resources": "static"}
    big = honest("too-big")[0]
    big["resources"].append({"uri": "skill://too-big/video.bin", "digest": digest(b""), "size": 17 * MIB})
    many = honest("too-many")[0]
    many["resources"] += [{"uri": f"skill://too-many/{i}.md", "digest": digest(b""), "size": 0} for i in range(512)]
    headless = honest("headless", **{"notes.md": b"notes\n"})[0]
    headless["resources"] = headless["resources"][1:]
    stray = honest("stray")[0]
    stray["resources"].append({"uri": "skill://elsewhere/secret.md", "digest": digest(b""), "size": 0})
    climbing = honest("climbing")[0]
    climbing["resources"].append({"uri": "skill://climbing/../first/notes.md", "digest": digest(b""), "size": 0})
    readme = honest("readme")[0] | {"uri": "skill://readme/README.md"}
    weak = honest("weak-digest")[0]
    weak["resources"][0]["digest"] = "md5:0123"
    bad = [misnamed, no_resources, odd_resources, big, many, headless, stray, climbing, readme, weak, "not an entry"]
    listed = [good[0], *bad[:5], good[1], good[0], *bad[5:], good[2], good[3]]  # good[0] is there twice
    with handwritten(listed, files, page=4) as (host, reads):
        entries = host.list()
        assert [e.uri for e in entries] == [e["uri"] for e in good] and not reads
        assert [e.name for e in entries] == ["first", "refunds", "refunds", "last"]  # same name, two skills: both kept
        problems = "\n".join(host.problems)
        for words in ["Not listed: skill://misnamed/SKILL.md: the frontmatter's name is 'something-else', and it must "
                      "be the last part of the skill's path, 'misnamed'",
                      "Not listed: skill://no-resources/SKILL.md: the entry has no resources",
                      "skill://odd-resources/SKILL.md: resources must be the list of the skill's files or \"dynamic\", "
                      "not 'static'",
                      "skill://too-big/SKILL.md: the skill's files add up to 17,825,", "16,777,216 (16 MiB)",
                      "skill://too-many/SKILL.md: the skill lists 513 files, and the most a skill may have here is 512",
                      "skill://headless/SKILL.md: resources must include the SKILL.md itself",
                      "skill://stray/SKILL.md: resources lists skill://elsewhere/secret.md, which is not a file inside",
                      "resources lists skill://climbing/../first/notes.md, which is not a file inside",
                      "skill://readme/README.md: a skill's uri must be its SKILL.md",
                      "skill://weak-digest/SKILL.md: its digest must be \"sha256:\" and 64 lowercase hex",
                      "an entry is 'not an entry', not an object",
                      "skill://first/SKILL.md is listed more than once",
                      "2 skills are called 'refunds': skill://acme/billing/refunds/SKILL.md, "
                      "skill://support/refunds/SKILL.md"]:
            assert words in problems, words
        assert len(host.problems) == len(bad) + 2
        for uri, why in [("skill://too-big/SKILL.md", "16 MiB"), ("skill://no-resources/SKILL.md", "has no resources")]:
            with pytest.raises(SkillError, match=why):  # asked for by URI, they are refused for the same reason
                host.get(uri)
        refunds = host.load(entries[1])
        assert refunds.verified and refunds.entry.root == "skill://acme/billing/refunds"
        assert host.read_file(refunds.entry, "notes.md") == "notes\n"
        with pytest.raises(SkillError, match="is outside the skill"):  # the other skill of the same name
            host.read_file(refunds.entry, "skill://support/refunds/notes.md")


@pytest.mark.parametrize("lie, why, reads_sent", [
    ("digest", "skill://liar/SKILL.md is not the content that was listed", 2),
    ("size", r"skill://liar/SKILL.md is \d+ bytes, and the listing says \d+", 2),
    ("description", "its frontmatter is not what the listing said .'description' is 'Much nicer than the file says.' "
                    "in the listing and 'Does one thing.' in the file", 1),
    ("extra", "'allowed-tools' is in the listing and not in the file", 1),
    ("missing", "'license' is in the file and not in the listing", 1),
    ("truth", "'metadata.safe' is True in the listing and 1 in the file", 1),
], ids=["digest", "size", "description", "extra-field", "missing-field", "true-is-not-1"])
def test_a_skill_that_is_not_what_its_listing_said_never_loads(lie, why, reads_sent):
    entry, files = honest("liar")
    if lie == "digest":
        entry["resources"][0]["digest"] = digest(b"some other content")
    elif lie == "size":
        entry["resources"][0]["size"] += 1
    elif lie == "description":
        entry["frontmatter"]["description"] = "Much nicer than the file says."
    elif lie == "extra":
        entry["frontmatter"]["allowed-tools"] = "Bash(rm:*)"
    else:
        extra = "license: MIT\n" if lie == "missing" else "metadata:\n  safe: 1\n"
        data = skill_md("liar").replace(b"---\n\n", extra.encode() + b"---\n\n")
        files[entry["uri"]] = data
        entry["resources"][0] |= {"digest": digest(data), "size": len(data)}
        entry["frontmatter"] |= {} if lie == "missing" else {"metadata": {"safe": True}}
    with handwritten([entry], files) as (host, reads):
        (listed,) = host.list()  # the listing itself is well formed, so nothing looks wrong yet
        assert host.problems == []
        with pytest.raises(SkillError, match=why) as refused:
            host.load(listed)
        assert "even against the entry handwritten gave just now" in str(refused.value)
        # Read, the entry refreshed with skills/get, read once more, then given up. Bytes that did match their
        # digest are not fetched twice: it is the frontmatter that is wrong, and asking again can't change that.
        assert reads[listed.uri] == reads_sent


def test_a_dynamic_skill_loads_unverified_with_its_frontmatter_still_checked(monkeypatch):
    entry, files = honest("live", **{"notes.md": b"n" * 40, "more.md": b"m" * 40})
    entry["resources"] = "dynamic"
    lying, lying_files = honest("live-lie")
    lying |= {"resources": "dynamic", "frontmatter": {"name": "live-lie", "description": "Not the file's words."}}
    with handwritten([entry, lying], files | lying_files) as (host, reads):
        live, live_lie = host.list()
        assert host.problems == [] and live.dynamic and live.files is None and live.total_bytes == 0
        assert live.as_dict()["files"] is None and not reads
        loaded = host.load(live)
        assert not loaded.verified and loaded.body == "# live\n\nThe body of live.\n"
        with pytest.raises(SkillError, match="its frontmatter is not what the listing said"):
            host.load(live_lie)
        with pytest.raises(SkillError, match="leads out of the skill"):
            host.read_file(live, "../live-lie/SKILL.md")
        # Nothing was listed to add up, so the 16 MiB limit is on what is read (made small here).
        monkeypatch.setattr(mcp_skills, "MAX_BYTES", len(files[live.uri]) + 60)
        assert host.read_file(live, "notes.md") == host.read_file(live, "notes.md") == "n" * 40  # counted once
        with pytest.raises(SkillError, match=r"would be over .* the most that is read of one skill"):
            host.read_file(live, "more.md")


def test_a_skill_the_server_serves_without_listing_it_is_found_by_uri():
    quiet, files = honest("quiet")
    decoy = honest("swapped")[0] | {"uri": "skill://decoy/SKILL.md"}  # another skill's entry under this URI
    with handwritten([], files, unlisted=[quiet, decoy]) as (host, _):
        assert host.list() == [] and host.problems == []  # an empty listing doesn't mean there are no skills
        entry = host.get("skill://quiet/SKILL.md")
        assert entry.name == "quiet" and host.load(entry).verified
        with pytest.raises(SkillError, match="the frontmatter's name is 'swapped'"):
            host.get("skill://decoy/SKILL.md")


# --- no extension, no session -----------------------------------------------------------------------

def test_a_server_without_the_extension_has_no_skills():
    mcp = MCPServer("plain", log_level="WARNING")
    mcp.add_tool(lambda: "pong", name="ping", description="Ping.")
    with connected(mcp) as (host, client):
        assert [t.tool_name for t in client.list_tools_sync()] == ["ping"]
        assert not host.supported and not host.directory_read and host.list() == [] and host.problems == []
        with pytest.raises(SkillError, match="does not declare the Skills extension"):
            host.get("skill://alpha/SKILL.md")


class Session:
    """What SkillsHost needs of a Strands MCPClient, with whatever capabilities a test gives it."""

    def __init__(self, capabilities) -> None:
        self._background_thread_session = SimpleNamespace(server_capabilities=capabilities)

    def _invoke_on_background_thread(self, coro) -> Future:
        future: Future = Future()
        future.set_result(asyncio.run(coro))
        return future


def test_capabilities_from_older_servers_and_clients_that_cannot_be_asked():
    for old in (None, SimpleNamespace(tools={}), SimpleNamespace(extensions=None), SimpleNamespace(extensions={})):
        host = SkillsHost("old", Session(old))  # before 2026-07-28 there was no `extensions` at all
        assert not host.supported and host.list() == [] and host.problems == []
    both = SkillsHost("new", Session(SimpleNamespace(extensions={EXTENSION_ID: {"directoryRead": True}})))
    assert both.supported and both.directory_read
    for client in (object(), MCPClient(url="http://127.0.0.1:9/mcp")):  # not a Strands client; one never started
        host = SkillsHost("gone", client)
        assert not host.supported and host.list() == []
        assert "no open session" in host.problems[0] and "_background_thread_session" in host.problems[0]
        with pytest.raises(SkillError, match="no open session"):
            host.get("skill://alpha/SKILL.md")


# --- the demo server itself -------------------------------------------------------------------------

def test_the_demo_server_serves_its_skills_and_they_load_verified(folder):
    from gizmo import demo_server
    from gizmo.demo_server import server

    port = free_port()
    assert demo_server.start(port=port, wait_s=10)
    client = MCPClient(url=f"http://127.0.0.1:{port}/mcp", prefix="explorer")
    client.start()
    try:
        host = SkillsHost("explorer", client)
        skills = {s.name: s for s in host.list()}
        assert host.supported and host.problems == []
        assert list(skills) == ["about-this-robot", "quiz-night", "trip-briefing", "universe", "world-countries"]
        assert all(s.uri == f"skill://{name}/SKILL.md" for name, s in skills.items())
        assert all(s.uri in client.server_instructions for s in skills.values())
        tools = {t.tool_name.removeprefix("explorer_") for t in client.list_tools_sync()}
        assert tools == {t.__name__ for t in server.TOOLS} and len(tools) == 12
        bodies = {}
        for name, skill in skills.items():
            loaded = host.load(skill)
            on_disk = (server.SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
            assert loaded.verified and loaded.text == on_disk
            assert skill.frontmatter["license"] == "Apache-2.0" and len(skill.description) > 80
            # The skills name the server's tools as the server does: the host's prefix is the host's business.
            assert "explorer_" not in loaded.text and "countries_" not in loaded.text
            bodies[name] = loaded.body
        # Each skill only names tools the server has.
        space = {"space_station", "people_in_space", "rocket_launches", "close_asteroids", "moon", "sky_tonight"}
        assert all(f"- {tool}:" in bodies["universe"] for tool in space | {"encyclopedia"})
        assert all(f"{tool}(" in bodies["trip-briefing"] for tool in ("local_time", "weather", "convert"))
        assert all(f"{tool}(" in bodies["quiz-night"] for tool in ("encyclopedia", "flag", "calculate"))
        # A skill with more than its SKILL.md: every file is listed with a digest, and reads back checked.
        quiz, about = skills["quiz-night"], skills["about-this-robot"]
        rounds = [f"skill://quiz-night/questions/{name}.md" for name in ("space", "tech", "world")]
        assert sorted(f.uri for f in quiz.files) == ["skill://quiz-night/SKILL.md", *rounds]
        assert sorted(f.uri for f in about.files) == [
            "skill://about-this-robot/SKILL.md", "skill://about-this-robot/references/a-turn.md",
            "skill://about-this-robot/references/skills-over-mcp.md"]
        for uri in rounds:
            text = host.read_file(quiz, uri)
            assert text == (server.SKILLS / uri.removeprefix("skill://")).read_text(encoding="utf-8")
            assert text.count("- Answer:") == 10 == text.count("- After:")  # ten questions, each with both
            assert uri.removeprefix("skill://quiz-night/") in bodies["quiz-night"]  # the SKILL.md points at it
        assert all(f.uri.removeprefix("skill://about-this-robot/") in bodies["about-this-robot"]
                   for f in about.files if f.uri != about.uri)
        with pytest.raises(SkillError):
            host.read_file(quiz, "skill://universe/SKILL.md")  # another skill's file isn't this skill's
    finally:
        client.stop(None, None, None)
    # python -m gizmo.demo_server --skills DIR: the same server over somebody else's folder of skills.
    other = server.build(folder)
    assert "skill://alpha/SKILL.md, skill://beta/SKILL.md" in other.instructions
    assert "universe" not in other.instructions
    with connected(other) as (host, _):
        assert [s.name for s in host.list()] == ["alpha", "beta"]


def test_a_step_that_loads_a_skill_only_loads_skills():
    """A model often asks for a skill and for the tools it guesses it needs in one step. The tools wait."""
    plugin = mcp_skills.McpSkills({}, {}, [])

    def use(name, tool_use_id):
        return {"toolUse": {"toolUseId": tool_use_id, "name": name, "input": {}}}

    def held(agent, block):
        event = SimpleNamespace(agent=agent, tool_use=block["toolUse"], cancel_tool=False)
        plugin.skill_first(event)
        return event.cancel_tool

    step = {"role": "assistant", "content": [{"text": "Let me look."}, use("explorer_sky_tonight", "t1"), use("read_skill", "t2"),
                                             use("explorer_moon", "t3"), use("read_resource", "t4")]}
    agent = SimpleNamespace(messages=[{"role": "user", "content": [{"text": "What's in the sky tonight?"}]}, step])
    assert held(agent, step["content"][1]) == mcp_skills.SKILL_FIRST  # asked for before the skill, in the same step
    assert held(agent, step["content"][3]) == mcp_skills.SKILL_FIRST  # and after it
    assert held(agent, step["content"][2]) is False and held(agent, step["content"][4]) is False  # the skill, and a file of it

    later = {"role": "assistant", "content": [use("explorer_sky_tonight", "t5"), use("explorer_moon", "t6")]}
    agent.messages += [{"role": "user", "content": [{"toolResult": {"toolUseId": "t2"}}]}, later]
    assert held(agent, later["content"][0]) is False and held(agent, later["content"][1]) is False  # the next step runs
    assert held(agent, step["content"][1]) is False  # a call that isn't one of the last step's is left alone

    local = {"role": "assistant", "content": [use("skills", "t7"), use("explorer_flag", "t8")]}  # a skill kept on disk
    agent.messages.append(local)
    assert held(agent, local["content"][1]) == mcp_skills.SKILL_FIRST and held(agent, local["content"][0]) is False
    assert held(SimpleNamespace(messages=[]), later["content"][0]) is False  # nothing to go by

    hooks = []
    plugin.init_agent(SimpleNamespace(system_prompt="You are a robot.", add_hook=lambda fn, kind: hooks.append(kind)))
    assert hooks == []  # no skills served to this agent: nothing to hold tools for
