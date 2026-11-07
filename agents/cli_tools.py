"""Read live verification evidence or submit a human-approved control-plane operation."""

import argparse
import json
import os
import shlex
import subprocess
import sys
from uuid import uuid4

import httpx
from dotenv import dotenv_values, load_dotenv

from agents.switch_console import AGENT_ADDRESSING, CONSOLE_SOURCE, ROOM
from app.agent_orchestrator import ledger, record, repro, triage
from app.contract_verifier import TargetUnavailable
from app.demo_revision import require_demo_revision, verify_demo_state
from app.evidence import handoff_diagnostics
from app.github_inspector import ROOT, InspectionError


CONSOLE_AGENTS = {"triage": "@acmepay-triager", "repro": "@acmepay-repro",
                  "remediate": "@acmepay-runbook", "patch-and-verify": "@acmepay-runbook"}


def console_reporter(command: str):
    actor = CONSOLE_AGENTS.get(command)
    if not actor or os.environ.get("PREFLIGHT_SWITCH_CONSOLE") != "1":
        return None
    token = os.environ.get("PREFLIGHT_SWITCH_WEBHOOK_TOKEN") or dotenv_values(
        ROOT / ".env", interpolate=False
    ).get("PREFLIGHT_SWITCH_WEBHOOK_TOKEN")
    endpoint = httpx.URL(os.environ.get("PREFLIGHT_API_URL", "http://localhost:8000"))
    if not token or endpoint.host not in {"localhost", "127.0.0.1", "::1"}:
        print("Switch relay unavailable. A loopback API URL and webhook credential are required.", file=sys.stderr)
        return None
    invocation = [sys.executable, "-m", "agents.cli_tools", *sys.argv[1:]]
    command_line = subprocess.list2cmdline(invocation) if os.name == "nt" else shlex.join(invocation)
    run_id = uuid4().hex

    def send(phase: str, status: str, output: str = "", probes: list | None = None) -> None:
        try:
            response = httpx.post(
                f"{str(endpoint).rstrip('/')}/api/switch/webhook",
                headers={"Authorization": f"Bearer {token}"},
                json={"event_id": f"{run_id}:{phase}", "run_id": run_id, "agent_id": actor,
                      "addressing": AGENT_ADDRESSING[actor], "room": ROOM, "source": CONSOLE_SOURCE,
                      "command": command_line, "phase": phase, "status": status,
                      "stdout_proof": output[:65536], "probes": probes or []},
                timeout=2, trust_env=False, follow_redirects=False,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            print(f"Switch relay {phase} event could not be delivered ({type(exc).__name__}).", file=sys.stderr)

    return send


def approved_operation(command: str, approval_id: str | None, push: bool, comment_pr: bool = False,
                       diff_sha256: str | None = None, decision: str = "APPROVED") -> dict:
    token = os.environ.get("PREFLIGHT_APPROVAL_TOKEN")
    if not token or not approval_id:
        raise ValueError("Human approval requires PREFLIGHT_APPROVAL_TOKEN and an approval ID.")
    route = "remediate" if command in {"remediate", "patch-and-verify"} else "checkout-unpatched"
    body = {"approval_id": approval_id}
    if route == "remediate":
        if not diff_sha256:
            raise ValueError("Review the pending proposal and provide --diff-sha256.")
        body["diff_sha256"] = diff_sha256
        body["decision"] = decision
        body["push"] = push
        body["comment_pr"] = comment_pr
    with httpx.Client(base_url=os.environ.get("PREFLIGHT_API_URL", "http://localhost:8000"),
                      timeout=90, trust_env=False) as client:
        response = client.post(f"/api/control-plane/{route}", headers={"Authorization": f"Bearer {token}"}, json=body)
        if response.is_error:
            raise ValueError(f"Control plane rejected {command} (HTTP {response.status_code}): {response.text[:1000]}")
        return response.json()


def main() -> int:
    load_dotenv(ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("triage", "repro", "verify-demo-state"):
        commands.add_parser(name).add_argument("--local-only", action="store_true",
                                               help="Skip hosted comparison for local-only evidence.")
    commands.add_parser("verify-switch-handoff")
    for name in ("remediate", "checkout-unpatched", "patch-and-verify", "checkout-pr-state"):
        operation = commands.add_parser(name)
        operation.add_argument("--approval-id", default=os.environ.get("PREFLIGHT_APPROVAL_ID"))
        if name in {"remediate", "patch-and-verify"}:
            operation.add_argument("--diff-sha256", required=True)
            operation.add_argument("--decision", choices=("APPROVED", "REJECTED"), default="APPROVED")
            operation.add_argument("--push", action="store_true")
            operation.add_argument("--comment-pr", action="store_true")
    args = parser.parse_args()
    report = console_reporter(args.command)
    if report:
        report("started", "RUNNING")
    try:
        if args.command == "verify-demo-state":
            result = verify_demo_state(local_only=args.local_only)
            print(json.dumps(result, indent=2))
            return 0 if result["demo_safe"] else 2
        if args.command == "verify-switch-handoff":
            from agents.native_switch import SwitchError, SwitchRoom

            result = handoff_diagnostics(ledger())
            try:
                with httpx.Client(timeout=12, trust_env=False) as client:
                    room = SwitchRoom(client)
                    history = room.history()
                    participants = room.operation("triager", "list_participants")
                messages = [message for thread in history for message in (thread["root"], *thread["replies"])
                            if message["kind"] == "message"]
                latest = max(messages, key=lambda message: message["timestamp"], default=None)
                result["latest_live_room_message_id"] = latest["id"] if latest else None
                names = {f"@{item['name']}" for item in participants}
                result["registered_member_agents"] = [name for name in result["registered_member_agents"] if name in names]
                result["steps"]["room_history"] = "PASS"
                result["steps"]["registered_members"] = "PASS" if len(result["registered_member_agents"]) == 3 else "FAIL"
            except (SwitchError, OSError, ValueError, KeyError) as exc:
                result["steps"]["room_history"] = "FAIL"
                result["steps"]["registered_members"] = "NOT_OBSERVED"
                result["live_error"] = str(exc)
            print(json.dumps(result, indent=2))
            return 0 if all(status == "PASS" for status in result["steps"].values()) else 2
        if args.command in {"triage", "repro"}:
            require_demo_revision(local_only=args.local_only)
        if args.command == "triage":
            result = triage()
            status = result["overall_status"]
        elif args.command == "repro":
            result = repro()
            status = result["status"]
        else:
            result = approved_operation(args.command, args.approval_id, getattr(args, "push", False),
                                        getattr(args, "comment_pr", False), getattr(args, "diff_sha256", None),
                                        getattr(args, "decision", "APPROVED"))
            status = result["status"]
        if args.command == "triage":
            drift = result["drift"]
            output = (f"## PreFlight triage · {status}\n"
                  f"- Revision: {result['branch']} ({result['commit_sha'][:7]})\n"
                  f"- Upload limits: backend {drift['backend_max_upload_mb']} MB, SDK {drift['frontend_allowed_upload_mb']} MB\n"
                  f"- Retry env: backend {drift['runbook_audit']['required_retry_env']}, SDK {drift['frontend_retry_env']}\n"
                  f"- Runbook drift: {drift['runbook_drift']}\n\n"
                      f"```diff\n{result['unified_diff']}```")
        else:
            output = json.dumps(result, ensure_ascii=True, indent=2)
        print(output, flush=True)
        if report:
            probes = result.get("probes") or result.get("verification", {}).get("probes")
            report("finished", status, output, probes)
        if status in {"DRIFT_BLOCKED", "CROSS_BOUNDARY_REGRESSION"}:
            return 1
        return 2 if status in {"UNVERIFIED", "PUSH_FAILED"} else 0
    except (InspectionError, TargetUnavailable, httpx.HTTPError, ValueError) as exc:
        if args.command in {"triage", "repro"}:
            record(args.command, "ERROR", {"error": str(exc)})
        print(str(exc), file=sys.stderr)
        if report:
            report("failed", "ERROR", str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
