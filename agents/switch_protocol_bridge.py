"""Switch room relay and explicitly authorized local rehearsal."""

import argparse
import asyncio
import json
import os
import sys
from difflib import unified_diff
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import httpx

from agents.cli_tools import approved_operation
from agents.switch_protocol_worker import worker
from app.agent_orchestrator import proposed_files, record, repro, triage
from app.github_inspector import RUNBOOK, SDK, git, target_repo
from app.demo_revision import require_demo_revision


def post_room_message(message: str) -> None:
    server = os.environ.get("SWITCH_SERVER_URL") or os.environ.get("SWITCH_BASE_URL")
    token = os.environ.get("SWITCH_AGENT_TOKEN") or os.environ.get("SWITCH_AGENT_API_KEY")
    room = os.environ.get("SWITCH_ROOM_ID")
    agent = os.environ.get("SWITCH_AGENT_ID")
    if not all((server, token, room, agent)):
        raise ValueError("Configure SWITCH_SERVER_URL, SWITCH_AGENT_TOKEN, SWITCH_ROOM_ID and SWITCH_AGENT_ID.")
    with httpx.Client(base_url=server, timeout=15, headers={"Authorization": f"Bearer {token}"}) as client:
        response = client.post(f"/agents/{quote(agent, safe='')}/message", json={"room_id": room, "content": message})
        response.raise_for_status()
    print(f"Switch accepted the room message (HTTP {response.status_code}).", flush=True)


def read_state() -> dict:
    token = os.environ.get("PREFLIGHT_API_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(base_url=os.environ.get("PREFLIGHT_API_URL", "http://localhost:8000"),
                      timeout=20, trust_env=False, headers=headers) as client:
        response = client.get("/api/control-plane/state")
        response.raise_for_status()
        return response.json()


def simulate_room_flow(approval_id: str | None, diff_sha256: str | None = None) -> dict:
    if not approval_id or not diff_sha256 or not os.environ.get("PREFLIGHT_APPROVAL_TOKEN"):
        raise ValueError("A local rehearsal needs a reviewed proposal ID, diff digest, and operator token.")
    require_demo_revision()
    state = read_state()
    if (state["release_gate"] != "BLOCKED" or not state["runtime_matches_checkout"]
            or state["repository"]["commit_sha"] != git("rev-parse", "HEAD").strip()
            or not Path(state["repository"]["path"]).samefile(target_repo())):
        raise ValueError("Start the rehearsal against the aligned, unpatched checkout.")
    run_id = uuid4().hex

    def say(actor: str, message: str, *, status: str, command: str | None = None,
            addressing: str | None = None, **evidence) -> None:
        record("room_message", status, {"room": "#acmepay-preflight", "message": message, **evidence},
               source="room_rehearsal", command=command, run_id=run_id, actor=actor, addressing_mode=addressing)
        print(f"[local rehearsal] {actor} {message}", flush=True)

    finding = triage(run_id)
    drift = finding["drift"]
    say("@preflight-triager",
        f"Backend {drift['backend_max_upload_mb']} MB versus SDK {drift['frontend_allowed_upload_mb']} MB. "
        f"Backend {drift['runbook_audit']['required_retry_env']} versus SDK {drift['frontend_retry_env']}. "
        "@preflight-repro, verify the binary boundary. @preflight-runbook, prepare the patch.",
        status=finding["overall_status"], addressing="anyone", command="python -m agents.cli_tools triage")

    reproduction = repro(run_id)
    if reproduction["status"] != "CROSS_BOUNDARY_REGRESSION":
        raise ValueError("The unpatched release did not reproduce a verified boundary regression.")
    probes = reproduction["probes"]
    proof = " | ".join(f"{item['size_mb']:g} MB: HTTP {item['http_status']}, SHA-256 {item['sha256']}, "
                       f"{item['latency_ms']} ms" for item in probes)
    say("@preflight-repro", proof, status=reproduction["status"], addressing="owner-agents",
        command="python -m agents.cli_tools repro", probes=probes)

    paths = (SDK, RUNBOOK)
    repo = target_repo()
    before = tuple((repo / path).read_text(encoding="utf-8") for path in paths)
    after = proposed_files(*before, state["contracts"]["backend"])
    diff = "".join("".join(unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                       fromfile=f"a/{path}", tofile=f"b/{path}"))
                   for path, old, new in zip(paths, before, after))
    say("@preflight-runbook", "I prepared the SDK and runbook diff. Requesting local operator approval.",
        status="AWAITING_APPROVAL", addressing="owner-agents", proposed_diff=diff)
    result = approved_operation("remediate", approval_id, push=False, diff_sha256=diff_sha256)
    say("Local operator", "The authenticated review approved the local rehearsal patch.",
        status="APPROVED", approval_id=approval_id, approval_source="explicit_local_operator_invocation")
    if result["release_gate"] != "VERIFIED" or read_state()["release_gate"] != "VERIFIED":
        raise ValueError("Live verification did not confirm the local patch.")
    say("@preflight-runbook",
        "Verified. The 1.5 MB PDF returned HTTP 200. The 4 MB PDF returned HTTP 413 and the SDK now rejects it too.",
        status="VERIFIED", addressing="owner-agents", command="python -m agents.cli_tools remediate",
        verification=result["verification"])
    return {"run_id": run_id, "release_gate": "VERIFIED", "transcript_source": "room_rehearsal"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--simulate-room-flow", action="store_true")
    modes.add_argument("--send-message")
    parser.add_argument("--approval-id", default=os.environ.get("PREFLIGHT_APPROVAL_ID"))
    parser.add_argument("--diff-sha256")
    args = parser.parse_args()
    try:
        if args.simulate_room_flow:
            print(json.dumps(simulate_room_flow(args.approval_id, args.diff_sha256), indent=2))
        elif args.send_message is not None:
            post_room_message(args.send_message)
        else:
            asyncio.run(worker())
    except KeyboardInterrupt:
        return 0
    except (ValueError, RuntimeError, OSError, httpx.HTTPError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
