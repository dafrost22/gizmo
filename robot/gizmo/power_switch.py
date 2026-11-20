"""The console's Shut down / Start up, beyond the robot itself.

The cloud part needs nothing here: while the app is off, its session loop opens no session, so
nothing calls Bedrock. A laptop voice server is asked to quit (it only takes that from Gizmo); it
can't be started from here. The home box stays manual: its services cost nothing per minute, Gizmo
has no access to the box, and addons/home-box/gizmo-ctl.sh down|up on the box switches them. Here we only say
whether its voice is running.
"""

from __future__ import annotations

import logging

import requests


logger = logging.getLogger(__name__)


def box_voice_up(urls: list[str]) -> bool:
    """Is a configured voice server answering, at any of its addresses?"""
    return bool(answering(urls))


def answering(urls: list[str]) -> list[str]:
    """The voice servers among these that answer their health check."""
    up = []
    for url in urls:
        try:
            if requests.get(f"{url}/health", timeout=1.5).ok:
                up.append(url)
        except requests.RequestException:
            pass
    return up


def quit_macs(urls: list[str]) -> tuple[bool | None, str]:
    """Ask each announced Mac voice server to exit: (ok, what happened); None when none is running."""
    if not urls:
        return None, "not running"
    stopped = []
    for url in urls:
        try:
            requests.post(f"{url}/quit", timeout=3).raise_for_status()
            stopped.append(url)
        except requests.RequestException as e:
            logger.warning(f"Mac voice at {url} didn't quit: {e}")
    if len(stopped) == len(urls):
        return True, "stopped"
    return False, f"{len(urls) - len(stopped)} of {len(urls)} didn't answer"
