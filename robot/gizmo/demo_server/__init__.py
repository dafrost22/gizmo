"""The demo MCP server: everyday tools, live space data and flags (server.py), and five skills served over MCP
(skills_ext.py).

The folder imports nothing from the rest of gizmo, so it can be copied whole and run anywhere, as the AgentCore
runtime does with its own copy.
"""

from .server import PORT, mcp, start, warm_up

__all__ = ["PORT", "mcp", "start", "warm_up"]
