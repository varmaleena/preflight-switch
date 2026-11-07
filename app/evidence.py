"""Classify ledger evidence without trusting client-supplied labels."""


NATIVE = "NATIVE_SWITCH"
LOCAL = "LOCAL_ORCHESTRATION"
ROOM_ID = "18294b6d-9b98-4bfa-89bd-12f5e673d887"
ROOM_AGENTS = ("@acmepay-triager", "@acmepay-repro", "@acmepay-runbook")
NATIVE_MESSAGES = {
    "native_kickoff", "native_triage", "native_handoff", "native_before",
    "native_proposal_diff", "native_proposal_review", "native_rejection", "native_after", "native_result",
    "native_room_message",
}


def event_origin(event: dict) -> str:
    action, source = event.get("action"), event.get("source")
    proof = event.get("evidence") or {}
    if source == "room_rehearsal":
        return "REHEARSAL"
    if action == "github_comment":
        return "GITHUB"
    if action in {"probe", "repro"}:
        return "SANDBOX_PROBE"
    if source == NATIVE and proof.get("room_id") == ROOM_ID:
        if (action in NATIVE_MESSAGES and proof.get("message_id")
                and proof["message_id"] == event.get("external_id")):
            return NATIVE
        if (action == "approval_decision" and proof.get("approval_message_id")
                and proof.get("decision") in {"APPROVED", "REJECTED"}
                and proof.get("approval_source") == NATIVE):
            return NATIVE
    return LOCAL


def handoff_diagnostics(entries: list[dict]) -> dict:
    native = [event for event in entries if event_origin(event) == NATIVE]
    latest = native[-1] if native else None
    handoff = next((event for event in reversed(native) if event["action"] == "native_handoff"), None)
    decision = next((event for event in reversed(entries)
                     if event["action"] == "approval_decision" and event["status"] in {"APPROVED", "REJECTED"}), None)
    kickoff = next((event for event in reversed(native) if event["action"] == "native_kickoff"), None)
    thread = kickoff["run_id"] if kickoff else None
    run = [event for event in native if thread and (event.get("run_id") == thread
                                                   or event["evidence"].get("thread_id") == thread)]

    def observed(action, **match):
        return any(event["action"] == action and all(event["evidence"].get(k) == v for k, v in match.items())
                   for event in run)

    def status(passed):
        return "PASS" if passed else "NOT_OBSERVED"

    publication = next((event for event in reversed(entries) if thread and event.get("run_id") == thread
                        and event_origin(event) == "GITHUB"), None)
    outcome = next((event["status"] for event in reversed(run) if event["action"] == "approval_decision"), None)
    steps = {
        "human_kickoff": status(kickoff), "triage_report": status(observed("native_triage")),
        "repro_handoff": status(observed("native_handoff", recipient="repro")),
        "before_probes": status(observed("native_before")),
        "runbook_handoff": status(observed("native_handoff", recipient="runbook")),
        "diff_and_review_request": status(observed("native_proposal_diff") and observed("native_proposal_review")),
        "human_approval": "REJECTED" if outcome == "REJECTED" else status(outcome == "APPROVED"),
        "local_patch": status(any(event["action"] == "patch-and-verify" and event["status"] == "APPLIED"
                                  and event["evidence"].get("approval_id") == approval["evidence"]["approval_id"]
                                  for event in entries for approval in run if approval["action"] == "approval_decision")),
        "after_probes": status(observed("native_after")),
        "github_publication": ("PASS" if publication["status"] == "POSTED" else "FAIL")
        if publication else "NOT_OBSERVED",
    }
    failure = next((event["evidence"]["error"] for event in reversed(entries)
                    if thread and event.get("run_id") == thread and event["action"] == "native_workflow_error"), None)
    return {
        "room_id": ROOM_ID, "registered_member_agents": list(ROOM_AGENTS),
        "latest_native_message_id": (latest["evidence"].get("message_id")
                                     or latest["evidence"].get("approval_message_id")) if latest else None,
        "latest_native_handoff": ({"status": handoff["status"], "recipient": handoff["evidence"]["recipient"],
                                   "message_id": handoff["evidence"]["message_id"]} if handoff else None),
        "latest_approval_source": event_origin(decision) if decision else None,
        "latest_approval_decision": decision["status"] if decision else None,
        "steps": steps, "latest_workflow_error": failure,
    }
