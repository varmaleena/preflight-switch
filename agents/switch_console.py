"""Wire contract for the local Switch Console integration."""

import json
import os
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field


CONSOLE_VERSION = "0.40.1"
CONSOLE_SOURCE = f"Switch Console v{CONSOLE_VERSION} · Codex Provider"
ROOM = "#acmepay-preflight"
AGENT_ADDRESSING = {
    "@acmepay-triager": "anyone",
    "@acmepay-repro": "owner-agents",
    "@acmepay-runbook": "owner-agents",
}


class ConsoleEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    event_id: str = Field(pattern=r"^[a-f0-9]{32}:(started|finished|failed)$")
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    agent_id: Literal["@acmepay-triager", "@acmepay-repro", "@acmepay-runbook"]
    addressing: Literal["anyone", "owner-agents"]
    room: Literal["#acmepay-preflight"]
    source: Literal[CONSOLE_SOURCE]
    command: str = Field(min_length=1, max_length=2048)
    phase: Literal["started", "finished", "failed"]
    status: str = Field(min_length=1, max_length=80)
    stdout_proof: str = Field(max_length=65536)
    probes: list[dict] = Field(default_factory=list, max_length=8)


def local_switch_status() -> dict:
    url = os.environ.get("PREFLIGHT_SWITCH_SERVER_URL")
    if not url and (appdata := os.environ.get("APPDATA")):
        try:
            ports = Path(appdata) / "switchdash" / "local-switch-server" / "ports.json"
            port = json.loads(ports.read_text(encoding="utf-8"))["api"]
            if type(port) is int and 0 < port < 65536:
                url = f"http://127.0.0.1:{port}"
        except (OSError, ValueError, KeyError, TypeError):
            pass
    connected = False
    if url:
        try:
            endpoint = httpx.URL(url)
            if endpoint.scheme in {"http", "https"} and endpoint.host in {"localhost", "127.0.0.1", "::1"}:
                response = httpx.get(f"{url.rstrip('/')}/health", timeout=1, trust_env=False, follow_redirects=False)
                connected = response.is_success and response.json().get("status") in {"ok", "healthy"}
        except (httpx.HTTPError, ValueError, AttributeError):
            pass
    return {"version": CONSOLE_VERSION, "configured": bool(url), "connected": connected}
