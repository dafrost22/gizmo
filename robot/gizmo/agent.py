"""Gizmo's voice agent: a Strands BidiAgent on Nova Sonic (Amazon Bedrock).

Nova Sonic hears and, unless the personality has a voice of its own on a voice server, speaks. It doesn't decide
what to say: its one tool, think, hands what it heard to the Strands brain (on the robot or on AgentCore) and it
says what comes back. Credentials: the aws_profile in config.py.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence

import boto3
from strands.bidi import BidiAgent
from strands.bidi.models.bedrock import BedrockNovaSonicModel

from gizmo.config import CONFIG
from gizmo.personalities import DEFAULT_NOVA_VOICE

logger = logging.getLogger("gizmo.voice")

REGION = CONFIG["aws_region"]
PROFILE = CONFIG["aws_profile"] or None  # None: boto3's default credential chain
# Nova 2.5 starts speaking ~0.4 s sooner on small talk and ~1 s sooner on harder questions than Nova 2.
VOICE_MODEL = os.environ.get("GIZMO_VOICE_MODEL", "amazon.nova-2-5-sonic")
# How long a pause ends your turn: HIGH ~1.5 s, MEDIUM ~1.75 s (Nova's default), LOW ~2 s.
ENDPOINTING = os.environ.get("GIZMO_ENDPOINTING", "HIGH")


def build_agent(system_prompt: str, session: boto3.Session | None = None, voice_model: str = VOICE_MODEL,
                voice: str = DEFAULT_NOVA_VOICE, tools: Sequence = (), output_rate: int | None = None) -> BidiAgent:
    """The voice agent. `tools` is the think tool; `output_rate` matches the robot's speaker."""
    session = session or boto3.Session(profile_name=PROFILE, region_name=REGION)
    audio = {"input": {"sample_rate": 16000}}
    if output_rate in (8000, 16000, 24000):
        audio["output"] = {"sample_rate": output_rate}
    model = BedrockNovaSonicModel(
        model_id=voice_model, boto_session=session, voice=voice, audio=audio,
        params={"turnDetectionConfiguration": {"endpointingSensitivity": ENDPOINTING}},  # sent in sessionStart
    )
    return BidiAgent(model=model, tools=list(tools), system_prompt=system_prompt, name="gizmo")
