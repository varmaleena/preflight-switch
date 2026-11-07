import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from fastapi.testclient import TestClient

from app.contract_verifier import MIB, pdf_payload
from app.main import app
from app.github_inspector import ROOT


@pytest.fixture
def client(monkeypatch):
    with TemporaryDirectory(dir=ROOT / "data") as directory:
        monkeypatch.setenv("PREFLIGHT_LEDGER_PATH", str(Path(directory) / "ledger.json"))
        with TestClient(app) as test_client:
            yield test_client


def test_state_uses_real_checkout_and_running_service(client):
    response = client.get("/api/control-plane/state")
    assert response.status_code == 200, response.text
    state = response.json()
    assert state["runtime_matches_checkout"]
    assert state["runtime"]["manifest"] == state["contracts"]
    assert state["runtime"]["health"]["git_commit_sha"] == state["repository"]["commit_sha"]
    assert "diff --git a/backend/config.py b/backend/config.py" in state["repository"]["unified_diff"]
    assert state["drift"]["payload_boundary_drift"]
    assert state["drift"]["boundary_delta_mb"] == 8
    assert state["drift"]["runbook_env_drift"]


def test_probe_observes_real_boundary_regression_and_writes_ledger(client):
    response = client.post("/api/control-plane/probe", json={"size_mb": 4.0})
    assert response.status_code == 200, response.text
    probe = response.json()
    assert probe["size_bytes"] == 4 * MIB
    assert probe["sha256"] == hashlib.sha256(pdf_payload(4 * MIB)).hexdigest()
    assert probe["http_status"] == 413
    assert probe["frontend_allows"] and probe["verified"]
    assert probe["classification"] == "CROSS_BOUNDARY_REGRESSION"
    event = client.get("/api/control-plane/state").json()["audit_ledger"][-1]
    assert event["id"] == probe["ledger_event_id"]
    assert event["evidence"]["sha256"] == probe["sha256"]


def test_multipart_probe_verifies_the_real_backend_receipt(client):
    content = pdf_payload(3 * MIB // 2)
    response = client.post("/api/control-plane/probe", files={"file": ("invoice.pdf", content, "application/pdf")})
    assert response.status_code == 200, response.text
    probe = response.json()
    digest = hashlib.sha256(content).hexdigest()
    assert probe["http_status"] == 200 and probe["verified"]
    assert probe["sha256"] == digest
    assert probe["response_json"]["receipt_id"] == digest[:24]


def test_remediation_requires_human_credential(client, monkeypatch):
    monkeypatch.setenv("PREFLIGHT_APPROVAL_TOKEN", "local-integration-test-approval")
    response = client.post("/api/control-plane/remediate", json={"approval_id": "untrusted"})
    assert response.status_code == 401


def test_handoff_records_real_probes_and_stops_before_remediation(client):
    response = client.post("/api/control-plane/stream-handoff")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "AWAITING_HUMAN_APPROVAL"
    assert [probe["http_status"] for probe in result["repro"]["probes"]] == [200, 413]
    assert "frontend_sdk/client_config.json" in result["prepare"]["proposed_diff"]
    state = client.get("/api/control-plane/state").json()
    handoffs = [event for event in state["audit_ledger"]
                if event["action"] == "handoff" and event["run_id"] == result["run_id"]]
    assert [event["step_status"] for event in handoffs] == [
        "REQUESTED", "AUDITING_DIFF", "ROUTED", "PROBING", "PROOF_RECORDED",
        "PREPARING_PATCH", "AWAITING_HUMAN_APPROVAL",
    ]
    assert state["release_gate"] == "BLOCKED"
