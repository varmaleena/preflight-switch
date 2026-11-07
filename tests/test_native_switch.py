from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agents import native_switch as native
from app import agent_orchestrator as review
from app import demo_revision, evidence

APPROVER = "@test-operator:localhost"


@pytest.fixture
def tmp_path():
    with TemporaryDirectory(dir=review.ROOT / "data") as directory:
        yield Path(directory)


@pytest.fixture
def target(tmp_path, monkeypatch):
    monkeypatch.setenv("PREFLIGHT_LEDGER_PATH", str(tmp_path / "ledger.json"))
    monkeypatch.delenv("TARGET_API_URL", raising=False)
    monkeypatch.setenv("PREFLIGHT_SWITCH_APPROVER", APPROVER)
    monkeypatch.setattr(review, "require_demo_revision", lambda: None)
    monkeypatch.setattr(native, "require_demo_revision", lambda: None)
    paths = tuple(tmp_path / filename for filename in ("sdk.json", "runbook.md"))
    for path in paths:
        path.write_text("before", encoding="utf-8")
    diff = "--- a/sdk.json\n+++ b/sdk.json\n@@ -1 +1 @@\n-before\n+after\n"
    snapshot = {"target_repository": native.TARGET, "target_branch": native.BRANCH,
                "base_commit_sha": "a" * 40, "proposed_file_paths": [review.SDK, review.RUNBOOK],
                "full_unified_diff": diff, "diff_sha256": sha256(diff.encode()).hexdigest()}
    monkeypatch.setattr(review, "patch_snapshot", lambda: snapshot.copy())
    monkeypatch.setattr(review, "repo_edit_context", lambda: ("a" * 40, paths, ("before",) * 2, ("after",) * 2))
    monkeypatch.setattr(review, "inspect_state", lambda: {"overall_status": "RELEASE_READY",
                                                        "release_gate": "VERIFIED", "drift": {}})
    return paths, snapshot


def decision(proposal, status="APPROVED", **changes):
    options = dict(approver_identity=APPROVER, source=review.NATIVE, room_id=native.ROOM,
                   approval_message_id="approval-message", timestamp="2026-11-07T12:00:00Z")
    return review.review_patch(proposal["approval_id"], proposal["diff_sha256"], status, **{**options, **changes})


def pending(*, bind=True):
    proposal = review.propose_approval(source=review.NATIVE, room_id=native.ROOM, initiating_message_id="thread",
                                       thread_id="thread", requesting_agent_id=native.AGENTS["runbook"][0])
    if bind:
        review.bind_proposal_message(proposal["approval_id"], "proposal-message")
    return proposal


def message(identifier, sender, body):
    return {"id": identifier, "sender": sender, "kind": "message", "body": body, "timestamp": 2000}


@pytest.mark.parametrize("body", sorted(native.KICKOFFS))
def test_kickoff_accepts_plain_and_addressed_authenticated_messages(target, monkeypatch, body):
    root = message("thread", APPROVER, body)
    room = SimpleNamespace(history=lambda: [{"root": root, "replies": []}])
    run = Mock()
    monkeypatch.setattr(native, "start_run", run)
    native.tick(room, 1000)
    run.assert_called_once_with(room, root)


def test_unauthorized_sender_and_spoofed_sender_cannot_invoke_repro(target):
    forged = {**message("request", "unrelated-human", "@acmepay-repro run probes"),
              "sender_name": "acmepay-triager"}
    room = SimpleNamespace(history=lambda role: [{"root": {"id": "thread"}, "replies": [forged]}])
    with pytest.raises(native.SwitchError, match="pinned agent"):
        native.run_probes(room, "thread", "request")
    assert review.ledger() == []


def test_wrong_room_and_wrong_digest_cannot_approve(target):
    proposal = pending()
    with pytest.raises(review.MutationConflict, match="does not match"):
        decision(proposal, room_id="another-room")
    with pytest.raises(review.MutationConflict, match="does not match"):
        decision({**proposal, "diff_sha256": "0" * 64})
    assert all(path.read_text() == "before" for path in target[0])


def test_approval_without_a_pending_proposal_or_delivery_fails(target):
    with pytest.raises(review.MutationConflict, match="pending approval"):
        decision({"approval_id": "missing", "diff_sha256": "0" * 64})
    with pytest.raises(review.MutationConflict, match="verified Switch reply"):
        decision(pending(bind=False))
    assert all(path.read_text() == "before" for path in target[0])


@pytest.mark.parametrize("field", ["diff_sha256", "base_commit_sha", "target_branch"],
                         ids=["stale_diff", "changed_head", "changed_branch"])
def test_approval_invalidates_changed_snapshot(target, field):
    proposal = pending()
    target[1][field] = "changed"
    with pytest.raises(review.MutationConflict, match="changed"):
        decision(proposal)
    assert review.approval_records()[0]["status"] == "INVALIDATED"
    assert all(path.read_text() == "before" for path in target[0])


def test_approval_invalidates_dirty_worktree(target, monkeypatch):
    proposal = pending()

    def dirty():
        raise review.MutationConflict("The target must be clean.")

    monkeypatch.setattr(review, "patch_snapshot", dirty)
    with pytest.raises(review.MutationConflict, match="clean"):
        decision(proposal)
    assert review.approval_records()[0]["status"] == "INVALIDATED"


def test_approval_applies_once_and_replay_fails(target):
    proposal = pending()
    assert decision(proposal)["status"] == "APPLIED"
    assert all(path.read_text() == "after" for path in target[0])
    event = review.approval_records()[0]
    assert (event["status"], event["approval_message_id"], event["reviewed_diff_digest"]) == (
        "APPROVED", "approval-message", proposal["diff_sha256"])
    with pytest.raises(review.MutationConflict, match="pending"):
        decision(proposal)


def test_approval_rejection_persists_and_never_edits(target):
    proposal = pending()
    result = decision(proposal, "REJECTED")
    assert result["decision"] == "REJECTED"
    assert result == review.approval_records()[0]
    assert all(path.read_text() == "before" for path in target[0])


def test_approval_requires_linked_human_after_the_bound_proposal(target):
    proposal = {**pending(), "proposal_message_id": "proposal-message"}
    valid = message("human", APPROVER, f'Approved {proposal["approval_id"]}')
    displayed = message("proposal-message", native.AGENTS["runbook"][1], "Review this diff.")
    forged = message("bot", native.AGENTS["triager"][1], "Approved")
    assert native.find_decision(proposal, [valid, displayed, forged]) is None
    assert native.find_decision(proposal, [displayed, forged, valid]) == (valid, "APPROVED")
    assert native.find_decision(proposal, [displayed, {**valid, "body": "Approved " + "0" * 32}]) is None


def test_merge_requires_explicit_command_and_new_scope(target):
    proposal = {**pending(), "proposal_message_id": "proposal-message"}
    displayed = message("proposal-message", native.AGENTS["runbook"][1], "Review this diff.")
    valid = message("human", APPROVER, f'Approve and merge {proposal["approval_id"]}')
    assert native.find_decision(proposal, [displayed, valid]) is None
    with pytest.raises(review.MutationConflict, match="scope"):
        decision(proposal, merge=True)
    proposal["allow_merge"] = True
    assert native.find_decision(proposal, [displayed, valid]) == (valid, "APPROVE_AND_MERGE")
    assert native.find_decision(proposal, [displayed, {**valid, "sender": "another-human"}]) is None
    ordinary = {**valid, "body": "Approved"}
    assert native.find_decision(proposal, [displayed, ordinary]) == (ordinary, "APPROVED")


def test_fresh_linked_kickoff_supersedes_a_pending_proposal(target, monkeypatch):
    proposal = pending()
    kickoff = {**message("new-thread", APPROVER, native.KICKOFF), "timestamp": 10**15}
    room = SimpleNamespace(history=lambda: [{"root": kickoff, "replies": []}])
    start = Mock()
    monkeypatch.setattr(native, "start_run", start)
    native.tick(room, 1000)
    start.assert_called_once_with(room, kickoff)
    invalidated = next(p for p in review.approval_records() if p["approval_id"] == proposal["approval_id"])
    assert invalidated["status"] == "INVALIDATED"
    assert invalidated["superseded_by_message_id"] == kickoff["id"]
    assert all(path.read_text() == "before" for path in target[0])


def test_loopback_guard_rejects_non_local_targets(monkeypatch):
    native.check_loopback()
    monkeypatch.setenv("TARGET_API_URL", "https://api.example.com")
    with pytest.raises(native.SwitchError, match="loopback"):
        native.check_loopback()


def test_local_orchestration_cannot_consume_native_proposal(target):
    proposal = pending()
    with pytest.raises(review.MutationConflict, match="does not match"):
        review.review_patch(proposal["approval_id"], proposal["diff_sha256"], "APPROVED",
                            approver_identity="operator")
    assert all(path.read_text() == "before" for path in target[0])


def test_native_switch_origin_requires_authenticated_room_message_evidence(target):
    proof = {"room_id": native.ROOM, "message_id": "sw_verified", "recipient": "repro"}
    native.room_event("triager", "native_handoff", "thread", "sw_verified", recipient="repro")
    review.record("handoff", "POSTED", proof, source=review.NATIVE, external_id="sw_synthetic")
    review.record("switch_event", "RECEIVED", {**proof, "event_origin": review.NATIVE},
                  source="switch", external_id="sw_verified")
    review.record("switch_command", "FINISHED", proof, source="switch_console")
    review.record("native_handoff", "POSTED", {**proof, "room_id": "elsewhere", "message_id": "sw_other"},
                  source=review.NATIVE, external_id="sw_other")
    review.record("room_message", "APPROVED", proof, source="room_rehearsal")
    review.record("github_comment", "POSTED", {}, source=review.NATIVE)
    review.record("probe", "VERIFIED", {}, source=review.NATIVE)
    events = review.ledger()
    assert [event["event_origin"] for event in events] == [
        review.NATIVE, review.LOCAL, review.LOCAL, review.LOCAL, review.LOCAL,
        "REHEARSAL", "GITHUB", "SANDBOX_PROBE",
    ]
    assert evidence.handoff_diagnostics(events)["latest_native_message_id"] == "sw_verified"


def test_revision_mismatch_blocks_unless_explicit_local_only(monkeypatch):
    values = {
        "remote": "https://github.com/varmaleena/acmepay-invoice-service.git",
        "rev-parse": "1" * 40, "status": "", "diff": "",
    }
    monkeypatch.setattr(demo_revision, "git", lambda cmd, *args: (
        demo_revision.PR_BRANCH if "--abbrev-ref" in args else values[cmd]))
    monkeypatch.setattr(demo_revision, "read_contracts", lambda: {})
    monkeypatch.setattr(demo_revision, "contract_drift", lambda _: {
        "backend_max_upload_mb": 2, "frontend_allowed_upload_mb": 10, "payload_drift": True})
    monkeypatch.setattr(demo_revision, "hosted_head", lambda **_: (demo_revision.DEMO_HEAD, None))
    with pytest.raises(demo_revision.InspectionError, match="Inspection revision does not match hosted PR head"):
        demo_revision.require_demo_revision()
    assert demo_revision.verify_demo_state(local_only=True)["demo_safe"]
    values["status"] = " M docs/oncall-runbook.md"
    assert not demo_revision.verify_demo_state(local_only=True)["demo_safe"]


class Room:
    def __init__(self):
        self.replies = []
        self.operations = []
        self.kickoff = message("thread", APPROVER, native.KICKOFF)

    def history(self, role="triager"):
        return [{"root": self.kickoff, "replies": self.replies}]

    def post(self, role, text, thread):
        if "Reply Approved" in text:
            assert review.approval_records()[0]["status"] == "PENDING"
        return self.add(role, text, thread)

    def target(self, role, text, thread):
        self.operations.append(("target", role))
        return self.add("triager", text, thread)

    def add(self, role, text, thread):
        assert thread == "thread"
        identifier = str(len(self.replies))
        self.replies.append(message(identifier, native.AGENTS[role][1], text))
        return identifier


@pytest.mark.parametrize("fail_after, merge", [(False, False), (True, False), (False, True), (True, True)])
@pytest.mark.parametrize("runbook_fence", ["", "```sh\n export LEGACY_RETRY_LIMIT=5\n ```\n"])
def test_native_handoff_and_publication_order(target, monkeypatch, fail_after, merge, runbook_fence):
    room = Room()
    target[1]["full_unified_diff"] += runbook_fence
    target[1]["diff_sha256"] = sha256(target[1]["full_unified_diff"].encode()).hexdigest()
    monkeypatch.setattr(native, "inspect_state", lambda: {
        "overall_status": "DRIFT_BLOCKED",
        "repository": {"origin": native.TARGET, "branch": native.BRANCH, "short_sha": "aaaaaaa",
                       "commit_sha": target[1]["base_commit_sha"], "base_ref": "main"},
        "drift": {"backend_max_upload_mb": 2, "frontend_allowed_upload_mb": 10,
                  "runbook_audit": {"required_retry_env": "NEW"}, "frontend_retry_env": "OLD", "runbook_drift": True},
    })
    operations = []
    def target_git(*args, **options):
        if args[0] == "push":
            operations.append("push")
        return target[1]["base_commit_sha"]
    monkeypatch.setattr(native, "git", target_git)
    monkeypatch.setattr(native, "require_open_pr", lambda *args: operations.append("open_pr"))
    monkeypatch.setattr(native, "commit_reviewed_patch", lambda *args: operations.append("commit") or "b" * 40)
    monkeypatch.setattr(native, "merge_reviewed_pr", lambda *args: operations.append("merge") or {
        "merge_sha": "c" * 40, "patch_head": "b" * 40, "pr_url": "https://github.com/example", "pr_number": 1,
    })
    publisher = Mock(return_value={"status": "POSTED", "comment_url": "https://github.com/example"})
    monkeypatch.setattr(native, "publish_audit", publisher)

    def probe(thread):
        after = target[0][0].read_text() == "after"
        if after and fail_after:
            raise native.SwitchError("after-check failed")
        if after:
            operations.append("after-check")
        return {"status": "VERIFIED" if after else "CROSS_BOUNDARY_REGRESSION", "probes": [
            {"size_mb": size, "http_status": status, "frontend_allows": size < 2 or not after,
             "sha256": "0" * 64, "ledger_event_id": index}
            for index, (size, status) in enumerate([(1.5, 200), (4, 413)], 1)
        ]}

    monkeypatch.setattr(native, "collect_repro", probe)
    native.tick(room, 1000)
    assert room.operations == [("target", "repro"), ("target", "runbook")]
    assert publisher.call_count == 0
    assert all(path.read_text() == "before" for path in target[0])
    assert review.approval_records()[0]["full_unified_diff"] in "".join(m["body"] for m in room.replies)
    approval = review.approval_records()[0]
    room.replies.append(message("human-approval", APPROVER,
                                f'Approve and merge {approval["approval_id"]}' if merge else "Approved"))
    if fail_after:
        with pytest.raises(native.SwitchError, match="after-check failed"):
            native.tick(room, 1000)
        publisher.assert_not_called()
        assert operations == []
    else:
        native.tick(room, 1000)
        publisher.assert_called_once()
        assert room.operations[-1] == ("target", "repro")
        assert any("After patch" in reply["body"] for reply in room.replies)
        assert operations == (["after-check", "open_pr", "commit", "push", "merge"] if merge else ["after-check"])
        if merge:
            assert any(event["action"] == "github_merge" and event["status"] == "MERGED" for event in review.ledger())
