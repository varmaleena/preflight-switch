"""Read live verification evidence or submit a human-approved control-plane operation."""

import argparse
import json
import os
import sys

import httpx

from app.agent_orchestrator import record, repro, triage
from app.contract_verifier import TargetUnavailable
from app.github_inspector import InspectionError


def approved_operation(command: str, approval_id: str | None, push: bool, comment_pr: bool = False) -> dict:
    token = os.environ.get("PREFLIGHT_APPROVAL_TOKEN")
    if not token or not approval_id:
        raise ValueError("Human approval requires PREFLIGHT_APPROVAL_TOKEN and an approval ID.")
    route = "remediate" if command in {"remediate", "patch-and-verify"} else "checkout-unpatched"
    body = {"approval_id": approval_id}
    if route == "remediate":
        body["push"] = push
        body["comment_pr"] = comment_pr
    with httpx.Client(base_url=os.environ.get("PREFLIGHT_API_URL", "http://localhost:8000"),
                      timeout=90, trust_env=False) as client:
        response = client.post(f"/api/control-plane/{route}", headers={"Authorization": f"Bearer {token}"}, json=body)
        if response.is_error:
            raise ValueError(f"Control plane rejected {command} (HTTP {response.status_code}): {response.text[:1000]}")
        return response.json()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("triage", "repro"):
        commands.add_parser(name).add_argument("--comment-pr", action="store_true")
    for name in ("remediate", "checkout-unpatched", "patch-and-verify", "checkout-pr-state"):
        operation = commands.add_parser(name)
        operation.add_argument("--approval-id", default=os.environ.get("PREFLIGHT_APPROVAL_ID"))
        if name in {"remediate", "patch-and-verify"}:
            operation.add_argument("--push", action="store_true")
            operation.add_argument("--comment-pr", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "triage":
            result = triage(comment_pr=args.comment_pr)
            status = result["overall_status"]
        elif args.command == "repro":
            result = repro(comment_pr=args.comment_pr)
            status = result["status"]
        else:
            result = approved_operation(args.command, args.approval_id, getattr(args, "push", False),
                                        getattr(args, "comment_pr", False))
            status = result["status"]
        if args.command == "triage":
            drift = result["drift"]
            print(f"## PreFlight triage · {status}\n"
                  f"- Revision: {result['branch']} ({result['commit_sha'][:7]})\n"
                  f"- Upload limits: backend {drift['backend_max_upload_mb']} MB, SDK {drift['frontend_allowed_upload_mb']} MB\n"
                  f"- Retry env: backend {drift['runbook_audit']['required_retry_env']}, SDK {drift['frontend_retry_env']}\n"
                  f"- Runbook drift: {drift['runbook_drift']}\n\n"
                  f"```diff\n{result['unified_diff']}```")
            if "github_comment" in result:
                print(json.dumps(result["github_comment"], ensure_ascii=True))
        else:
            print(json.dumps(result, ensure_ascii=True, indent=2))
        if status in {"DRIFT_BLOCKED", "CROSS_BOUNDARY_REGRESSION"}:
            return 1
        return 2 if status in {"UNVERIFIED", "PUSH_FAILED"} else 0
    except (InspectionError, TargetUnavailable, httpx.HTTPError, ValueError) as exc:
        if args.command in {"triage", "repro"}:
            record(args.command, "ERROR", {"error": str(exc)})
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
