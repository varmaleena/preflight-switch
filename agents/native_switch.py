"""Drive the pinned AcmePay workflow through authenticated Switch room messages."""

import argparse
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

from agents.notifications import merge_reviewed_pr, publish_audit, require_open_pr
from app.agent_orchestrator import (
    NATIVE, approval_records, bind_proposal_message, collect_repro, commit_reviewed_patch, file_lock,
    git, inspect_state, ledger, ledger_path, patch_snapshot, pending_approval, propose_approval, record, review_patch,
    supersede_native_approval,
)
from app.demo_revision import PR_NUMBER, REPOSITORY, require_demo_revision
from app.evidence import ROOM_ID
from app.github_inspector import ROOT


ROOM = ROOM_ID
AGENTS = {
    "triager": ("bf8bd797-51bd-4f9f-bc05-964c93aeb049", "@switch-agent-8c6e3fa6:localhost"),
    "repro": ("3ecc4f39-86c1-42d8-8f4b-de403cad74f5", "@switch-agent-bfb3dc6f:localhost"),
    "runbook": ("69ee5c17-e053-492e-8584-f02a13be12d3", "@switch-agent-b2e8ed88:localhost"),
}
KICKOFF = "Run PreFlight verification for PR #402."
KICKOFFS = {
    f"{mention}{command.rstrip('.')}"
    for mention in ("", "@acmepay-triager ")
    for command in (KICKOFF, KICKOFF.replace("#402", "#1"))
}
DECISION = re.compile(r"(Approved|Rejected)(?: ([a-f0-9]{32}))?", re.IGNORECASE)
MERGE_DECISION = re.compile(r"Approve and merge ([a-f0-9]{32})", re.IGNORECASE)
TARGET = "https://github.com/varmaleena/acmepay-invoice-service.git"
BRANCH = "feat/security-tightening-pr402"


class SwitchError(RuntimeError):
    pass


def approver_identity():
    identity = os.environ.get("PREFLIGHT_SWITCH_APPROVER")
    if not identity:
        raise SwitchError("Configure PREFLIGHT_SWITCH_APPROVER with the linked Switch identity.")
    return identity


class SwitchRoom:
    """Use each provisioned agent's controller proxy, which authenticates to Core."""

    def __init__(self, client: httpx.Client):
        self.client = client
        self.bindings = {}
        root = Path(os.environ.get("APPDATA", "")) / "switchdash" / "agent-controller" / "servers"
        for role, (agent_id, _) in AGENTS.items():
            matches = list(root.glob(f"*/agents/{agent_id}/credentials.json"))
            if len(matches) != 1:
                raise SwitchError(f"Expected one provisioned Switch credential for {role}.")
            config = json.loads(matches[0].read_text(encoding="utf-8"))["env"]
            endpoint = httpx.URL(config["SWITCH_API_ENDPOINT"])
            if (config["SWITCH_AGENT_ID"] != agent_id or endpoint.scheme != "http"
                    or endpoint.host != "127.0.0.1" or endpoint.username or endpoint.password):
                raise SwitchError(f"Invalid loopback Switch binding for {role}.")
            self.bindings[role] = (endpoint, config["SWITCH_API_TOKEN"])

    def operation(self, role: str, operation: str, **arguments):
        endpoint, token = self.bindings[role]
        agent_id, _ = AGENTS[role]
        try:
            response = self.client.post(
                endpoint.join(f"/agents/{agent_id}/ops/{operation}"),
                headers={"Authorization": f"Bearer {token}", "X-Switch-Room-Id": ROOM},
                json=arguments,
            )
            response.raise_for_status()
            return response.json()["result"]
        except httpx.HTTPStatusError as exc:
            raise SwitchError(f"{role} {operation} failed (HTTP {exc.response.status_code}).") from exc
        except (httpx.RequestError, KeyError, ValueError) as exc:
            raise SwitchError(f"{role} {operation} failed ({type(exc).__name__}).") from exc

    def history(self, role="triager"):
        return self.operation(role, "read_context", room_id=ROOM, limit=150)["threads"]

    def post(self, role, body, thread_id):
        return self.operation(role, "post_message", body=body, thread_id=thread_id)["event_id"]

    def target(self, role, body, thread_id):
        name = f"acmepay-{role}"
        result = self.operation("triager", "send_targeted_message", body=body,
                                target_names=[name], thread_id=thread_id)
        status = result["target_statuses"].get(name)
        if status != "live":
            raise SwitchError(f"Switch target {name} is {status or 'unresolved'} (event {result['event_id']}).")
        return result["event_id"]


def thread_replies(history, thread_id):
    return next((thread["replies"] for thread in history if thread["root"]["id"] == thread_id), [])


def confirmed_message(room, recipient, author, thread_id, message_id):
    """A target must see the exact server-issued message from the pinned sender."""
    for _ in range(6):
        replies = thread_replies(room.history(recipient), thread_id)
        for message in replies:
            if message["id"] == message_id:
                if message.get("kind") != "message" or message.get("sender") != AGENTS[author][1]:
                    raise SwitchError("Switch message sender does not match the pinned agent.")
                return message
        time.sleep(1)
    raise SwitchError(f"Switch message {message_id} is absent from the pinned thread.")


def room_event(role, action, thread_id, message_id, **evidence):
    record(action, "POSTED", {"room_id": ROOM, "thread_id": thread_id, "message_id": message_id, **evidence},
           source=NATIVE, actor=f"@acmepay-{role}", external_id=message_id, run_id=thread_id)


def post(room, role, action, thread_id, text, **evidence):
    message_id = room.post(role, text, thread_id)
    if confirmed_message(room, role, role, thread_id, message_id)["body"] != text:
        raise SwitchError(f"Switch did not preserve the body of message {message_id}.")
    room_event(role, action, thread_id, message_id, **evidence)
    return message_id


def target(room, role, thread_id, task):
    message_id = room.target(
        role, f"PreFlight workflow: {task}. The room worker executes this request; no separate CLI run is needed.",
        thread_id,
    )
    confirmed_message(room, role, "triager", thread_id, message_id)
    room_event("triager", "native_handoff", thread_id, message_id, recipient=role, task=task)
    return message_id


def check_loopback():
    url = httpx.URL(os.environ.get("TARGET_API_URL", "http://localhost:8001"))
    if (url.scheme != "http" or url.host not in {"127.0.0.1", "localhost", "::1"}
            or url.port != 8001 or url.path != "/" or url.query or url.fragment or url.username or url.password):
        raise SwitchError("Native verification requires the loopback AcmePay service on HTTP port 8001.")


def run_probes(room, thread_id, request_id, *, after=False):
    confirmed_message(room, "repro", "triager", thread_id, request_id)
    check_loopback()
    result = collect_repro(thread_id)
    probes = result["probes"]
    expected = "VERIFIED" if after else "CROSS_BOUNDARY_REGRESSION"
    if result["status"] != expected or [(p["size_mb"], p["http_status"]) for p in probes] != [(1.5, 200), (4, 413)]:
        raise SwitchError("The live probes did not match the expected invoice boundary.")
    return result


def probe_text(result):
    return "\n".join(
        f'{p["size_mb"]:g} MiB → HTTP {p["http_status"]}, SDK {"allows" if p["frontend_allows"] else "rejects"}, '
        f'SHA-256 `{p["sha256"]}`, evidence `data/audit_ledger.json#{p["ledger_event_id"]}`'
        for p in result["probes"]
    )


def start_run(room, kickoff):
    thread_id = kickoff["id"]
    record("native_kickoff", "RECEIVED", {"room_id": ROOM, "message_id": thread_id, "sender": kickoff["sender"]},
           source=NATIVE, external_id=thread_id, run_id=thread_id)
    check_loopback()
    require_demo_revision()
    state = inspect_state()
    repository, drift = state["repository"], state["drift"]
    if repository["origin"] != TARGET or repository["branch"] != BRANCH or state["overall_status"] != "DRIFT_BLOCKED":
        raise SwitchError("Expected the unpatched AcmePay PR checkout and matching live backend.")
    changed_files = git("diff", "--name-only", repository["base_ref"], "HEAD", "--").splitlines()
    post(room, "triager", "native_triage", thread_id,
         f'PR #402 (GitHub #1), `{repository["short_sha"]}`: BLOCKED. '
         f'Changed files: {", ".join(changed_files)}. '
         f'Upload limit: backend {drift["backend_max_upload_mb"]} MiB, SDK {drift["frontend_allowed_upload_mb"]} MiB. '
         f'Retry key: backend `{drift["runbook_audit"]["required_retry_env"]}`, SDK `{drift["frontend_retry_env"]}`. '
         f'Runbook drift: {drift["runbook_drift"]}.', changed_files=changed_files, drift=drift)
    request_id = target(room, "repro", thread_id, "record the before-patch multipart probes")
    before = run_probes(room, thread_id, request_id)
    evidence_id = post(room, "repro", "native_before", thread_id, "Before patch\n" + probe_text(before), result=before)
    confirmed_message(room, "triager", "repro", thread_id, evidence_id)
    request_id = target(room, "runbook", thread_id, "prepare the SDK and runbook diff for human review")
    confirmed_message(room, "runbook", "triager", thread_id, request_id)
    proposal = propose_approval(source=NATIVE, room_id=ROOM, initiating_message_id=thread_id,
                                thread_id=thread_id, requesting_agent_id=AGENTS["runbook"][0],
                                expected_commit=repository["commit_sha"])
    publish_proposal(room, proposal)


def publish_proposal(room, proposal):
    thread_id = proposal["thread_id"]
    approval_id = proposal["approval_id"]
    diff = proposal["full_unified_diff"]
    # Discord message limits apply even when Switch accepts the room operation.
    # Preserve literal runbook fences by posting those diffs as unfenced text.
    fenced = "```" not in diff
    for offset in range(0, len(diff), 1300):
        part = diff[offset:offset + 1300]
        display = f"```diff\n{part}```" if fenced else f"{part}\nEnd of diff part."
        post(room, "runbook", "native_proposal_diff", thread_id,
             f"Proposal `{approval_id}` · diff part {offset // 1300 + 1}\n{display}")
    publish_review_request(room, proposal)


def publish_review_request(room, proposal):
    approval_id = proposal["approval_id"]
    proposal_message = post(
        room, "runbook", "native_proposal_review", proposal["thread_id"],
        f'Proposal `{approval_id}` · SHA-256 `{proposal["diff_sha256"]}`\n'
        "Reply Approved to apply the reviewed two-file patch locally, or Rejected to decline. "
        f"Reply `Approve and merge {approval_id}` to authorize the local fix, verification, "
        "a commit and push of those two files, an audit comment, and merging GitHub PR #1 if the checks pass.",
    )
    bind_proposal_message(approval_id, proposal_message, allow_merge=True,
                          previous_message_id=proposal["proposal_message_id"])


def find_decision(proposal, replies):
    for index, message in enumerate(replies):
        if message["id"] == proposal["proposal_message_id"] and message["sender"] == AGENTS["runbook"][1]:
            break
    else:
        return None
    for message in replies[index + 1:]:
        if message.get("kind") != "message" or message.get("sender") != approver_identity():
            continue
        text = message["body"].strip()
        merge = MERGE_DECISION.fullmatch(text)
        if proposal.get("allow_merge") and merge and merge[1].lower() == proposal["approval_id"]:
            return message, "APPROVE_AND_MERGE"
        match = DECISION.fullmatch(text)
        if match and match[2] in {None, proposal["approval_id"]}:
            return message, match[1].upper()
    return None


def finish_run(room, proposal, message, decision):
    thread_id = proposal["thread_id"]
    check_loopback()
    merge = decision == "APPROVE_AND_MERGE"
    result = review_patch(
        proposal["approval_id"], proposal["diff_sha256"], "APPROVED" if merge else decision, approver_identity=message["sender"],
        source=NATIVE, room_id=ROOM, approval_message_id=message["id"],
        timestamp=datetime.fromtimestamp(message["timestamp"] / 1000, timezone.utc).isoformat(),
        merge=merge,
    )
    if decision == "REJECTED":
        post(room, "runbook", "native_rejection", thread_id, f'Proposal `{proposal["approval_id"]}` rejected.')
        return
    request_id = target(room, "repro", thread_id, "record the after-patch multipart probes")
    after = run_probes(room, thread_id, request_id, after=True)
    before = next(event["evidence"]["result"] for event in reversed(ledger())
                  if event["action"] == "native_before" and event["run_id"] == thread_id)
    post(room, "repro", "native_after", thread_id,
         "Before patch\n" + probe_text(before) + "\n\nAfter patch\n" + probe_text(after), result=after)
    if git("rev-parse", "HEAD").strip() != proposal["base_commit_sha"]:
        raise SwitchError("Target HEAD changed before publication.")
    verified_head = proposal["base_commit_sha"]
    head = verified_head
    if merge:
        require_open_pr(REPOSITORY, PR_NUMBER, BRANCH, verified_head)
        head = commit_reviewed_patch(proposal["approval_id"])
        ref = f"refs/heads/{BRANCH}"
        git("push", f"--force-with-lease={ref}:{verified_head}", "origin", f"{head}:{ref}", timeout=45)
        result.update(pushed=True, commit_sha=head)
    audit = publish_audit({**result, "verification": after, "before_verification": before,
                           "expected_pr_number": PR_NUMBER, "expected_pr_head": head,
                           "approval": proposal})
    record("github_comment", audit["status"], audit, source=NATIVE, run_id=thread_id)
    if audit["status"] != "POSTED":
        raise SwitchError(f"GitHub audit publication failed: {audit['error']}")
    if merge:
        try:
            merged = merge_reviewed_pr(REPOSITORY, PR_NUMBER, BRANCH, head)
        except (RuntimeError, OSError, ValueError) as exc:
            record("github_merge", "FAILED", {"error": str(exc), "patch_head": head}, source=NATIVE, run_id=thread_id)
            raise
        record("github_merge", "MERGED", {**merged, "verified_head": verified_head}, source=NATIVE, run_id=thread_id)
        post(room, "triager", "native_result", thread_id,
             f'GitHub confirmed PR #1 merged at `{merged["merge_sha"][:7]}`. Audit: {audit["comment_url"]}')
        return
    post(room, "triager", "native_result", thread_id, f'Verified local patch. GitHub audit: {audit["comment_url"]}')


def tick(room, since):
    threads = room.history()
    entries = ledger()
    senders = {agent[1]: role for role, agent in AGENTS.items()}
    messages = [(message, thread["root"]["id"]) for thread in threads
                for message in (thread["root"], *thread["replies"])
                if message["kind"] == "message" and message["sender"] in senders]
    if latest := max(messages, key=lambda item: item[0]["timestamp"], default=None):
        message, thread_id = latest
        if not any(event.get("external_id") == message["id"] and event["source"] == NATIVE for event in entries):
            record("native_room_message", "OBSERVED",
                   {"room_id": ROOM, "thread_id": thread_id, "message_id": message["id"]},
                   source=NATIVE, actor=f"@acmepay-{senders[message['sender']]}", external_id=message["id"])
    approvals = approval_records(entries)
    claimed = {event["run_id"] for event in entries if event["action"] == "native_kickoff"}
    human = approver_identity()
    kickoff = max((thread["root"] for thread in threads
                   if thread["root"].get("kind") == "message" and thread["root"].get("sender") == human
                   and thread["root"]["timestamp"] >= since and thread["root"]["body"].strip().removesuffix(".") in KICKOFFS
                   and thread["root"]["id"] not in claimed), key=lambda message: message["timestamp"], default=None)
    for proposal in approvals:
        if proposal["status"] != "PENDING" or proposal["approval_source"] != NATIVE or proposal["room_id"] != ROOM:
            continue
        match = find_decision(proposal, thread_replies(threads, proposal["thread_id"]))
        if match:
            try:
                finish_run(room, proposal, *match)
            except (RuntimeError, OSError, ValueError) as exc:
                record("native_workflow_error", "FAILED", {"error": str(exc)}, run_id=proposal["thread_id"],
                       external_id=match[0]["id"])
                raise
            return
        if proposal["proposal_message_id"]:
            created = datetime.fromisoformat(proposal["created_at"]).timestamp() * 1000
            if not kickoff or kickoff["timestamp"] <= created:
                return
            require_demo_revision()
            supersede_native_approval(proposal["approval_id"], ROOM, kickoff["id"])
    if kickoff:
        try:
            start_run(room, kickoff)
        except (RuntimeError, OSError, ValueError) as exc:
            record("native_workflow_error", "FAILED", {"error": str(exc)}, run_id=kickoff["id"])
            raise


def main():
    load_dotenv(ROOT / ".env", override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-thread", help="Post harmless delivery and reachability checks in this room thread.")
    parser.add_argument("--offer-merge", metavar="PROPOSAL_ID", help="Post a new merge authorization request for a pending native proposal.")
    args = parser.parse_args()
    with httpx.Client(timeout=15, trust_env=False) as client:
        room = SwitchRoom(client)
        if args.offer_merge:
            with file_lock(ledger_path().with_name(".native_switch.lock")):
                proposal = pending_approval(args.offer_merge)
                if proposal["approval_source"] != NATIVE or proposal["room_id"] != ROOM or not proposal["proposal_message_id"]:
                    raise SwitchError("A previously displayed native proposal is required.")
                require_demo_revision()
                if any(proposal[key] != value for key, value in patch_snapshot().items()):
                    raise SwitchError("The target changed; start a fresh review.")
                confirmed_message(room, "runbook", "runbook", proposal["thread_id"], proposal["proposal_message_id"])
                publish_review_request(room, proposal)
            print(f"Explicit merge review posted for proposal {args.offer_merge}.", flush=True)
            return
        if args.check_thread:
            event = room.post("triager", "PreFlight threaded delivery check. No changes requested.", args.check_thread)
            confirmed_message(room, "triager", "triager", args.check_thread, event)
            addressed = room.target("repro", "Delivery check only. Do not run probes or make changes.", args.check_thread)
            confirmed_message(room, "repro", "triager", args.check_thread, addressed)
            print(f"Switch room confirmed: post={event}; targeted={addressed}; acmepay-repro=live.", flush=True)
            return
        with file_lock(ledger_path().with_name(".native_switch.lock")):
            since = int(time.time() * 1000)
            approver_identity()
            check_loopback()
            room.history()
            print(f"Listening for PreFlight kickoff messages in Switch room {ROOM}.", flush=True)
            last_error = None
            while True:
                try:
                    tick(room, since)
                    last_error = None
                except (RuntimeError, OSError, ValueError) as exc:
                    if str(exc) != last_error:
                        print(f"Native Switch workflow paused: {exc}", flush=True)
                        last_error = str(exc)
                time.sleep(2)


if __name__ == "__main__":
    main()
