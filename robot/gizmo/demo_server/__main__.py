"""Run the demo server on its own: python -m gizmo.demo_server [--port N] [--skills DIR].

For pointing any MCP client at it (http://127.0.0.1:8772/mcp), or for trying your own skills over MCP before they
have a server of their own: --skills serves a folder of <name>/SKILL.md skills in place of the bundled ones.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import requests

from . import server


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m gizmo.demo_server",
                                     description="The demo MCP server: everyday tools, live space data and skills over MCP.")
    parser.add_argument("--port", type=int, default=server.PORT, help=f"port on 127.0.0.1 (default {server.PORT})")
    parser.add_argument("--skills", type=Path, metavar="DIR", help="serve this folder of skills, not the bundled ones")
    args = parser.parse_args()
    if args.skills is not None and not args.skills.is_dir():
        parser.error(f"--skills: {args.skills} is not a folder")
    # force: importing the server built one, which already set logging up, at WARNING.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", force=True)
    mcp = server.build(args.skills.resolve()) if args.skills is not None else server.mcp
    try:
        lat, lon, place = server.home()
        server.log.info(f"home {place} ({lat:.2f}, {lon:.2f})")
    except (requests.RequestException, KeyError, ValueError) as e:  # offline: the skills can still be tried
        server.log.warning(f"couldn't find where home is, so sky_tonight and the weather at home won't work: {e!r}")
    server.log.info(f"explorer MCP server on http://127.0.0.1:{args.port}/mcp")
    server.serve(mcp, args.port)


if __name__ == "__main__":
    main()
