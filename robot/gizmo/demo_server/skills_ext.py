"""Skills over MCP, the server side: serve a folder of Agent Skills from an MCPServer (SEP-2640).

    mcp = MCPServer("my-server")
    serve_skills(mcp, Path(__file__).parent / "skills")     # skills/<name>/SKILL.md, plus any other files

This file needs only the `mcp` SDK and PyYAML, and nothing else from this repo: copy it next to your own server.

What a host then sees (the extension is `io.modelcontextprotocol/skills`; the skill format itself is
agentskills.io's):
- The capability, declared under `capabilities.extensions`. `directoryRead` is not declared, because
  `resources/directory/read` is not implemented here: every entry lists all of its files anyway.
- `skills/list`: one entry for each skill, all in one page. An entry is the SKILL.md's URI, its frontmatter
  verbatim as JSON, and every file of the skill with its SHA-256 digest and size.
- `skills/get`: the same entry for one skill, by its SKILL.md URI. A URI that isn't a skill here is -32602.
- Both results are marked as not to be cached (`ttlMs` 0, `cacheScope` "private"), for clients on protocol
  2026-07-28 or later. The extension's spec page requires the two fields on both; the SEP left `skills/get` open.
- Each file as an ordinary resource, `skill://<name>/<path inside the skill>`, read with `resources/read`.

Design:
- The disk is read on every request, so an edited SKILL.md is served, and its digest recomputed, without a
  restart. A host that listed the skill before the edit sees a digest mismatch when it loads it, asks `skills/get`
  again and gets the new entry: the SEP's staleness path, which is the point of a server you develop against.
- A file added while the server runs becomes a readable resource the next time `skills/list` or `skills/get` sees
  it, which is also how a host learns of it. A deleted file stays in `resources/list` until a restart (the SDK has
  no way to take a resource back) and reads as an unknown resource.
- Text is served as text only when the bytes are valid UTF-8, decoded as they are (no newline or byte-order-mark
  changes), so the text a host gets back encodes to the very bytes the digest covers. Anything else is a blob.
- A skill that breaks the rules is left out, with a warning in the log: no SKILL.md, no frontmatter, a `name`
  that isn't the folder's name or isn't lowercase letters, digits and hyphens, or no `description`. A host would
  refuse it anyway, and here the log says why.
- Hidden files, `__pycache__` and symbolic links are not served: a link could point outside the skill.
- A SKILL.md deeper inside a skill is served as one of that skill's files, not listed as a skill of its own.

Once the `mcp` SDK releases its own `mcp.server.skills.Skills` (written, not yet in a release), that replaces the
extension class and the wire handling in this file: pass it to `MCPServer(extensions=[...])` with two handlers
that return what `_Skills.scan` builds here.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import anyio.to_thread
import yaml
from mcp.server.context import ServerRequestContext
from mcp.server.extension import Extension, MethodBinding
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError
from mcp.server.mcpserver.resources import Resource
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, PaginatedRequestParams, RequestParams

__all__ = ["EXTENSION_ID", "serve_skills", "skill_uris"]

log = logging.getLogger(__name__)

EXTENSION_ID = "io.modelcontextprotocol/skills"
SKILL_MD = "SKILL.md"
# The SEP's per-skill limits. A skill over them is still served, with a warning: a host may refuse it.
MAX_FILES = 512
MAX_BYTES = 16 * 1024 * 1024
# agentskills.io: a name is 1 to 64 lowercase letters, digits and single hyphens; a description 1 to 1024 characters.
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
MAX_NAME, MAX_DESCRIPTION = 64, 1024
SKILL_URI = re.compile(rf"skill://({NAME.pattern})/{re.escape(SKILL_MD)}")
URI_SAFE = "/!$&'()*+,;=:@"
CACHING_SINCE = "2026-07-28"  # the protocol version that gave results ttlMs and cacheScope
FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)^---[ \t]*\r?$", re.DOTALL | re.MULTILINE)
# What skills usually hold. Python's own table differs between platforms, so the common ones are fixed here.
MIME = {".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain", ".py": "text/x-python",
        ".sh": "text/x-shellscript", ".js": "text/javascript", ".ts": "text/typescript", ".json": "application/json",
        ".yaml": "application/yaml", ".yml": "application/yaml", ".toml": "application/toml", ".csv": "text/csv",
        ".html": "text/html", ".css": "text/css", ".xml": "application/xml", ".svg": "image/svg+xml"}


class _Invalid(Exception):
    """Why a folder isn't served as a skill."""


@dataclass(frozen=True)
class _Found:
    """One file of a skill as it is on disk now."""

    relative: str  # its path inside the skill, with forward slashes
    path: Path
    digest: str
    size: int
    mime_type: str


def _jsonable(value: Any) -> Any:
    """YAML's values as JSON has them: a date becomes its ISO text, a mapping's keys become strings."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _frontmatter(data: bytes, name: str) -> dict[str, Any]:
    """A SKILL.md's frontmatter as the JSON object the listing carries, checked against agentskills.io's rules."""
    try:
        text = data.decode("utf-8").removeprefix("﻿")
    except UnicodeDecodeError:
        raise _Invalid(f"{SKILL_MD} isn't UTF-8 text") from None
    match = FRONTMATTER.match(text)
    if match is None:
        raise _Invalid(f"{SKILL_MD} must start with YAML frontmatter between two --- lines")
    try:
        parsed = yaml.safe_load(match.group(1))
    except yaml.YAMLError as e:
        raise _Invalid(f"the frontmatter isn't valid YAML: {' '.join(str(e).split())}") from None
    if not isinstance(parsed, dict):
        raise _Invalid("the frontmatter must be a YAML mapping with name and description")
    frontmatter = _jsonable(parsed)
    found, description = frontmatter.get("name"), frontmatter.get("description")
    if found != name:
        raise _Invalid(f"the frontmatter's name is {found!r}, and it must be the folder's name, {name!r}")
    if not NAME.fullmatch(name) or len(name) > MAX_NAME:
        raise _Invalid(f"the name {name!r} must be 1 to {MAX_NAME} lowercase letters, digits and single hyphens")
    if not isinstance(description, str) or not 1 <= len(description.strip()) <= MAX_DESCRIPTION:
        raise _Invalid(f"the frontmatter needs a description of 1 to {MAX_DESCRIPTION} characters")
    return frontmatter


def _hidden(name: str) -> bool:
    return name.startswith(".") or name == "__pycache__"


def _mime_type(relative: str, data: bytes) -> str:
    suffix = Path(relative).suffix.lower()
    if suffix in MIME:
        return MIME[suffix]
    if guessed := mimetypes.guess_type(relative)[0]:
        return guessed
    try:
        data.decode("utf-8")
        return "text/plain"
    except UnicodeDecodeError:
        return "application/octet-stream"


def _uri(name: str, relative: str) -> str:
    # Only what a URI path can't hold as it is gets percent-encoded: a space, non-ASCII, "?", "#", "%" and so on.
    return f"skill://{name}/{quote(relative, safe=URI_SAFE)}"


def _skill_folders(folder: Path) -> list[Path]:
    return sorted(d for d in folder.iterdir() if d.is_dir() and not _hidden(d.name)) if folder.is_dir() else []


def _read_skill(directory: Path) -> tuple[dict[str, Any], list[_Found]]:
    """A skill folder as it is now: its frontmatter and every file, SKILL.md first. Raises _Invalid or OSError."""
    skill_md = directory / SKILL_MD
    if not skill_md.is_file() or skill_md.is_symlink():
        raise _Invalid(f"it has no {SKILL_MD}")
    paths = []
    for root, folders, names in os.walk(directory):  # os.walk doesn't go into linked folders
        folders[:] = [f for f in folders if not _hidden(f)]
        paths += [p for n in names if not _hidden(n) and (p := Path(root, n)).is_file() and not p.is_symlink()]
    frontmatter: dict[str, Any] = {}
    files = []
    for path in sorted(paths, key=lambda p: (p != skill_md, p.relative_to(directory).as_posix())):
        relative, data = path.relative_to(directory).as_posix(), path.read_bytes()
        if path == skill_md:
            frontmatter = _frontmatter(data, directory.name)
        # The size and the digest are of one read, so they always describe the same bytes.
        files.append(_Found(relative, path, f"sha256:{hashlib.sha256(data).hexdigest()}", len(data),
                            _mime_type(relative, data)))
    return frontmatter, files


class _SkillFile(Resource):
    """One file of a skill, read from disk each time it is asked for."""

    path: Path

    async def read(self) -> str | bytes:
        try:
            data = await anyio.to_thread.run_sync(self.path.read_bytes)
        except OSError:  # deleted since it was registered: the same answer as a URI that never existed
            raise ResourceNotFoundError(f"Unknown resource: {self.uri}") from None
        try:
            # Not read_text(): universal newlines or a dropped byte-order mark would change the bytes the host
            # hashes, and the digest in the listing is of the file as it is on disk.
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data


class _GetParams(RequestParams):
    uri: str


def _caching(ctx: ServerRequestContext[Any, Any]) -> dict[str, Any]:
    """`ttlMs` and `cacheScope`, which the extension's spec requires on both results from protocol 2026-07-28.
    Not to be cached at all: the next edit changes the answer. (Protocol versions are dates, so they compare as
    text.) An older client is not sent fields it doesn't know."""
    return {"ttlMs": 0, "cacheScope": "private"} if ctx.protocol_version >= CACHING_SINCE else {}


class _Skills(Extension):
    """The extension: the capability, `skills/list` and `skills/get`, answered from the folder as it is now."""

    identifier = EXTENSION_ID

    def __init__(self, folder: Path, server: MCPServer) -> None:
        self.folder = folder.resolve()  # so a relative path keeps meaning the same folder
        self._server = server
        self._resources: dict[str, _SkillFile] = {}  # by URI: the files registered with the server so far
        self._warned: dict[str, str] = {}  # by folder name: the last warning, so each is logged once
        self._lock = threading.Lock()  # scans run on worker threads, one for each request

    def methods(self) -> Sequence[MethodBinding]:
        return [MethodBinding("skills/list", PaginatedRequestParams, self._list),
                MethodBinding("skills/get", _GetParams, self._get)]

    async def _list(self, ctx: ServerRequestContext[Any, Any], params: PaginatedRequestParams) -> dict[str, Any]:
        if params.cursor is not None:  # everything fits in one page, so no cursor was ever handed out
            raise MCPError(code=INVALID_PARAMS, message=f"Unknown cursor: {params.cursor}")
        skills = await anyio.to_thread.run_sync(self.scan)
        return {"skills": skills, **_caching(ctx)}

    async def _get(self, ctx: ServerRequestContext[Any, Any], params: _GetParams) -> dict[str, Any]:
        # The name is in the URI, so only that skill's files are hashed: this is the call a host makes to
        # refresh one skill after a digest mismatch. The pattern also keeps the name from being a path.
        match = SKILL_URI.fullmatch(params.uri)
        entries = await anyio.to_thread.run_sync(self.scan, match.group(1)) if match else []
        if not entries:
            raise MCPError(code=INVALID_PARAMS, message=f"Unknown skill: {params.uri}", data={"uri": params.uri})
        return {"skill": entries[0], **_caching(ctx)}

    def scan(self, only: str | None = None) -> list[dict[str, Any]]:
        """The entries of the valid skills on disk now (or of the one called `only`), their files registered."""
        with self._lock:
            if only is not None:  # a name nobody made a folder for is just unknown: nothing to warn about
                folders = [self.folder / only] if (self.folder / only).is_dir() else []
            elif self.folder.is_dir():
                folders = _skill_folders(self.folder)
            else:
                self._warn("", f"the skills folder {self.folder} doesn't exist: no skills are served")
                return []
            return [entry for d in folders if (entry := self._entry(d)) is not None]

    def _entry(self, directory: Path) -> dict[str, Any] | None:
        name = directory.name
        try:
            frontmatter, files = _read_skill(directory)
        except (_Invalid, OSError) as e:
            self._warn(name, f"skill folder {name!r} is not served: {e}")
            return None
        total = sum(f.size for f in files)
        if len(files) > MAX_FILES or total > MAX_BYTES:
            self._warn(name, f"skill {name!r} has {len(files)} files and {total:,} bytes, over the limit of "
                             f"{MAX_FILES} files and {MAX_BYTES:,} bytes: hosts may refuse it")
        else:
            self._warned.pop(name, None)
        for f in files:
            self._register(name, f, frontmatter)
        return {"uri": _uri(name, SKILL_MD), "frontmatter": frontmatter,
                "resources": [{"uri": _uri(name, f.relative), "digest": f.digest, "size": f.size} for f in files]}

    def _register(self, name: str, found: _Found, frontmatter: dict[str, Any]) -> None:
        """Make a file readable with resources/read, a SKILL.md described the way the SEP asks."""
        uri, is_skill_md = _uri(name, found.relative), found.relative == SKILL_MD
        description = frontmatter["description"] if is_skill_md else None
        if (known := self._resources.get(uri)) is not None:
            known.description = description  # an edited description shows in resources/list too
            return
        resource = _SkillFile(uri=uri, name=name if is_skill_md else f"{name}/{found.relative}",
                              description=description, mime_type=found.mime_type, path=found.path)
        self._resources[uri] = resource
        self._server.add_resource(resource)

    def _warn(self, name: str, message: str) -> None:
        if self._warned.get(name) != message:
            self._warned[name] = message
            log.warning(message)


def _add_extension(server: MCPServer, extension: Extension) -> None:
    """Add an extension to a server that is already built.

    The SDK's public way is `MCPServer(extensions=[...])`, at construction. A server whose tools hang off a
    module-level object is built before anything else can be added to it, so this calls the method the
    constructor itself uses for each extension. It is private, hence the one place and the plain error if it goes.
    """
    apply = getattr(server, "_apply_extension", None)
    if apply is None:
        raise RuntimeError("this version of the mcp SDK has no MCPServer._apply_extension, which skills_ext uses "
                           "to add the Skills extension to a built server; if it now ships mcp.server.skills, "
                           "use that instead")
    apply(extension)


def serve_skills(mcp: MCPServer, folder: Path) -> None:
    """Serve every skill in `folder` (one sub-folder each, holding a SKILL.md) from `mcp`, per SEP-2640."""
    skills = _Skills(Path(folder), mcp)
    _add_extension(mcp, skills)
    served = skills.scan()  # now, so the files are in resources/list from the start and a bad skill is logged early
    log.info(f"skills served over MCP from {folder}: {[s['frontmatter']['name'] for s in served]}")


def skill_uris(folder: Path) -> list[str]:
    """The SKILL.md URIs `serve_skills` lists for `folder` as it is now. A server's instructions can name them,
    and those have to be written before the server exists."""
    uris = []
    for directory in _skill_folders(Path(folder)):
        try:
            _read_skill(directory)
        except (_Invalid, OSError):
            continue  # serve_skills logs why
        uris.append(_uri(directory.name, SKILL_MD))
    return uris
