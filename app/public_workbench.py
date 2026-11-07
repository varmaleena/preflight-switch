"""Public, read-only view of the pinned AcmePay demonstration."""

import json
from difflib import unified_diff
from threading import Lock
from time import monotonic

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from app.agent_orchestrator import contract_files, proposed_files
from app.github_inspector import ROOT, contract_drift, git, parse_policy
from app.main import state


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
REPOSITORY = "https://github.com/varmaleena/acmepay-invoice-service"
SEED = "df746a739620f9a65b84c43f96f726b48c6b1942"
BRANCH = "feat/security-tightening-pr402"
PATHS = ("frontend_sdk/client_config.json", "docs/oncall-runbook.md")
AGENTS = {"@acmepay-triager", "@acmepay-repro", "@acmepay-runbook"}
PROBE_FIELDS = (
    "size_mb", "size_bytes", "sha256", "latency_ms", "http_status", "frontend_allows",
    "frontend_allowed_upload_mb", "verified", "classification", "target_commit_sha", "ledger_event_id",
)
NATIVE_ACTIONS = {
    "native_kickoff", "native_triage", "native_handoff", "native_before", "native_after",
    "native_proposal_diff", "native_proposal_review", "native_rejection", "native_result",
}
cache_lock = Lock()
cached: tuple[float, dict] | None = None


def pick(source: dict, *keys: str) -> dict:
    return {key: source[key] for key in keys if key in source}


def public_probe(probe: dict) -> dict:
    proof = pick(probe, *PROBE_FIELDS)
    body = probe.get("response_json")
    if isinstance(body, dict) and probe["http_status"] == 200:
        proof["response_json"] = pick(body, "status", "receipt_id", "received_mb")
    if isinstance(body, dict) and isinstance(body.get("detail"), dict) and probe["http_status"] == 413:
        proof["response_json"] = {"detail": pick(
            body["detail"], "error_code", "received_mb", "enforced_max_upload_mb", "active_retry_env_var",
        )}
    return proof


def public_event(event: dict, run_ids: set[str], drifts: list[dict]) -> dict | None:
    action, origin = event["action"], event["event_origin"]
    data = event["evidence"]
    if event.get("run_id") not in run_ids and data.get("thread_id") not in run_ids:
        return None
    proof = pick(data, "message_id", "thread_id")
    actor = event.get("agent_name")
    if origin == "NATIVE_SWITCH" and action in NATIVE_ACTIONS:
        if action == "native_kickoff":
            proof["sender"] = "Linked reviewer"
        if action == "native_handoff" and data.get("recipient") in {"repro", "runbook"}:
            proof["recipient"] = data["recipient"]
        if action in {"native_before", "native_after"}:
            report = data["result"]
            if report["drift"] not in drifts or any(not probe["verified"] for probe in report["probes"]):
                return None
            proof["result"] = {"drift": report["drift"], "probes": [public_probe(probe) for probe in report["probes"]]}
    elif action == "approval_delivery" and data.get("approval_source") == "NATIVE_SWITCH":
        proof.update(pick(data, "approval_id"))
    elif action == "approval_decision" and data.get("approval_source") == "NATIVE_SWITCH":
        proof.update(pick(data, "approval_id", "decision"))
    elif action == "github_merge" and event["status"] == "MERGED":
        proof.update(pick(data, "pr_number", "pr_url", "verified_head", "patch_head", "merge_sha"))
    elif action == "native_workflow_error":
        proof["error"] = "The workflow reported a failure. See the Discord thread for details."
    else:
        return None
    return {
        **pick(event, "id", "action", "status", "source", "event_origin", "timestamp", "run_id"),
        "agent_name": actor if actor in AGENTS else None, "evidence": proof,
    }


def public_state() -> dict:
    current = state()
    repo, contracts = current["repository"], current["contracts"]
    if repo["origin"] != REPOSITORY + ".git" or repo["branch"] != BRANCH:
        raise HTTPException(503, "The public AcmePay fixture is unavailable.")
    backend = parse_policy(git("show", f"{SEED}:backend/config.py"))
    pristine = contract_files(SEED)
    patched = proposed_files(*pristine, backend)
    fixtures = [{"backend": backend, "frontend_sdk": json.loads(sdk), "runbook": runbook}
                for sdk, runbook in (pristine, patched)]
    if not any(all(contracts.get(key) == value for key, value in fixture.items()) for fixture in fixtures):
        raise HTTPException(503, "The public AcmePay fixture is unavailable.")
    patch = "".join(line for path, before, after in zip(PATHS, pristine, patched) for line in unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}",
    ))
    events = current["audit_ledger"]
    run_ids = {event["run_id"] for event in events
               if event["action"] == "native_kickoff" and event["event_origin"] == "NATIVE_SWITCH"}
    drifts = [contract_drift(fixture) for fixture in fixtures]
    visible = [view for event in events if (view := public_event(event, run_ids, drifts)) is not None]
    approvals = [pick(proposal, "approval_id", "status", "approval_source", "diff_sha256", "full_unified_diff")
                 for proposal in current["approvals"] if proposal.get("thread_id") in run_ids
                 and proposal.get("approval_source") == "NATIVE_SWITCH"
                 and proposal.get("full_unified_diff") == patch]
    pr = next((item for item in repo["pull_requests"]["items"] or []
               if item["number"] == 1 and item["url"] == REPOSITORY + "/pull/1"), None)
    return {
        **pick(current, "drift", "runtime_matches_checkout", "release_gate", "payload_drift", "env_drift", "runbook_drift"),
        "public_view": True, "contracts": pick(contracts, "backend", "frontend_sdk", "runbook"),
        "audit_ledger": visible, "approvals": approvals,
        "repository": {
            **pick(repo, "origin", "branch", "short_sha", "commit_sha"),
            "head_diff": git("diff", "--no-ext-diff", "--no-textconv", f"{SEED}^", SEED, "--", "backend/config.py"),
            "working_tree_status": bool(repo["working_tree_status"]),
            "working_tree_diff": patch if contracts["runbook"] == patched[1] else "",
            "pull_requests": {"items": [pick(pr, "number", "url", "headRefName")] if pr else []},
        },
        "demo_revision": pick(current["demo_revision"], "demo_safe"),
        "switch_console": pick(current["switch_console"], "connected", "version"),
        "switch_handoff": pick(current["switch_handoff"], "room_id", "registered_member_agents",
                               "latest_native_message_id", "latest_native_handoff",
                               "latest_approval_source", "latest_approval_decision"),
    }


@app.get("/")
def home():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html.replace("<html ", '<html data-public-view="true" ', 1),
                        headers={"Cache-Control": "no-store"})


@app.get("/api/control-plane/state")
def live_state():
    global cached
    with cache_lock:
        now = monotonic()
        if cached is None or cached[0] <= now:
            try:
                result = public_state()
            except (KeyError, ValueError, RuntimeError, OSError, TypeError):
                raise HTTPException(503, "Live demo evidence is temporarily unavailable.") from None
            cached = monotonic() + 2, result
    return JSONResponse(cached[1], headers={"Cache-Control": "no-store"})
