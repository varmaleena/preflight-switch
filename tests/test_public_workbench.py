from fastapi.testclient import TestClient

from app import public_workbench


def test_public_workbench_exposes_only_read_routes():
    client = TestClient(public_workbench.app)
    assert 'data-public-view="true"' in client.get("/").text
    assert client.get("/docs").status_code == 404
    for route in ("remediate", "probe", "discord-settings", "run-verification"):
        assert client.post(f"/api/control-plane/{route}", json={}).status_code == 404


def test_public_event_omits_human_identity_and_unrelated_payload():
    entry = {
        "action": "native_kickoff", "event_origin": "NATIVE_SWITCH", "run_id": "thread",
        "evidence": {"message_id": "message", "sender": "private identity", "body": "private message"},
    }
    event = public_workbench.public_event(entry, {"thread"}, [])
    assert event["evidence"] == {"message_id": "message", "sender": "Linked reviewer"}
    assert public_workbench.public_event(entry, {"another-thread"}, []) is None
    assert public_workbench.public_event({**entry, "event_origin": "LOCAL_ORCHESTRATION"}, {"thread"}, []) is None
