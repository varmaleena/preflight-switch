import asyncio
import json
import os
from hmac import compare_digest
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from agents.switch_protocol_worker import switch_room
from agents.switch_console import AGENT_ADDRESSING, ConsoleEvent, local_switch_status
from agents.notifications import configure_discord, discord_url, publish_audit
from app.agent_orchestrator import (
    MutationConflict, approval_records, checkout_pr_state, handoff, inspect_state, ledger, prepare_handoff,
    record, repo_edit_context, repro, review_patch, run_pipeline, triage,
)
from app.contract_verifier import MAX_PROBE_BYTES, TargetUnavailable, execute_live_probe
from app.demo_revision import REVISION_MISMATCH, require_demo_revision, verify_demo_state
from app.evidence import handoff_diagnostics
from app.github_inspector import ROOT, InspectionError


app = FastAPI(title="PreFlight Switch Control Plane", version="2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[item.strip() for item in os.environ.get(
        "PREFLIGHT_CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
    ).split(",") if item.strip()],
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
)


def require_token(variable: str, authorization: str | None, *, optional: bool = False) -> None:
    token = os.environ.get(variable)
    if not token:
        if optional:
            return
        raise HTTPException(503, detail=f"{variable} is not configured.")
    if not compare_digest((authorization or "").encode(), f"Bearer {token}".encode()):
        raise HTTPException(401, detail="Invalid bearer credential.", headers={"WWW-Authenticate": "Bearer"})


def require_api_access(authorization: Annotated[str | None, Header()] = None) -> None:
    require_token("PREFLIGHT_API_TOKEN", authorization, optional=True)


access = [Depends(require_api_access)]


class ProbeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    size_mb: float = Field(gt=0, le=MAX_PROBE_BYTES / 1048576, allow_inf_nan=False)
    filename: str | None = Field(default=None, min_length=1, max_length=255)


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    approval_id: str = Field(min_length=1, max_length=200)


class Remediation(Approval):
    diff_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["APPROVED", "REJECTED"] = "APPROVED"
    push: bool = False
    comment_pr: bool = False


class DiscordSettings(Approval):
    webhook_url: str = Field(max_length=400)


stream_lock = asyncio.Lock()


async def inspection_failed(_request: Request, exc: Exception):
    status = 409 if isinstance(exc, MutationConflict) else 502 if isinstance(exc, TargetUnavailable) else 503
    return JSONResponse(status_code=status, content={"detail": str(exc)})


for failure in (InspectionError, TargetUnavailable, MutationConflict):
    app.add_exception_handler(failure, inspection_failed)


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(ROOT / "static" / "index.html", media_type="text/html", headers={"Cache-Control": "no-cache"})


@app.get("/api/control-plane/state", dependencies=access)
def state():
    entries = ledger()
    inspection = inspect_state()
    revision = verify_demo_state(fresh=False)
    if REVISION_MISMATCH in revision["problems"] or revision["pr_head_sha"] is None:
        inspection.update(release_gate="UNVERIFIED", overall_status="UNVERIFIED")
    return {**inspection, "demo_revision": revision, "switch_handoff": handoff_diagnostics(entries),
            "audit_ledger": entries, "approvals": approval_records(entries),
            "discord_configured": bool(discord_url()),
            "switch_console": local_switch_status()}


@app.post(
    "/api/control-plane/probe", dependencies=access,
    openapi_extra={"requestBody": {"content": {"application/json": {"schema": ProbeSpec.model_json_schema()}}}},
)
async def probe(request: Request, file: UploadFile | None = File(default=None), *, local_only: bool = False):
    await run_in_threadpool(require_demo_revision, local_only=local_only)
    try:
        if file is not None:
            data = await file.read(MAX_PROBE_BYTES + 1)
            if len(data) > MAX_PROBE_BYTES:
                raise HTTPException(413, detail="Uploaded probe exceeds 32 MB.")
            result = await run_in_threadpool(
                execute_live_probe, 0, file.filename, data, content_type=file.content_type or "application/octet-stream",
            )
        else:
            media = request.headers.get("content-type", "").split(";", 1)[0].lower()
            if media != "application/json":
                raise HTTPException(415, detail="Send application/json or a multipart file.")
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > 1024:
                    raise HTTPException(413, detail="Probe specification is too large.")
                body.extend(chunk)
            spec = ProbeSpec.model_validate_json(body)
            result = await run_in_threadpool(execute_live_probe, spec.size_mb, spec.filename)
    except (ValidationError, ValueError) as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    except (InspectionError, TargetUnavailable) as exc:
        await run_in_threadpool(record, "probe", "ERROR", {"error": str(exc)})
        raise
    return result


@app.post("/api/control-plane/run-pipeline", dependencies=access)
def pipeline(local_only: bool = False):
    require_demo_revision(local_only=local_only)
    return run_pipeline()


@app.post("/api/control-plane/run-verification", dependencies=access)
def run_verification(local_only: bool = False):
    require_demo_revision(local_only=local_only)
    result = run_pipeline(include_runbook=False)
    return {**result, "audit_ledger": ledger()}


@app.post("/api/control-plane/stream-handoff", dependencies=access)
async def stream_handoff(approval: Approval | None = None,
                         authorization: Annotated[str | None, Header()] = None, *, local_only: bool = False):
    if stream_lock.locked():
        raise HTTPException(409, detail="A room handoff is already running.")
    async with stream_lock:
        await run_in_threadpool(require_demo_revision, local_only=local_only)
        _, paths, pristine, _ = await run_in_threadpool(repo_edit_context)
        current = await run_in_threadpool(lambda: tuple(path.read_text(encoding="utf-8") for path in paths))
        if current != pristine:
            require_token("PREFLIGHT_APPROVAL_TOKEN", authorization)
            if approval is None:
                raise HTTPException(422, detail="Provide an approval reference to reset the patched working tree.")
            await run_in_threadpool(checkout_pr_state, approval.approval_id)
        run_id = uuid4().hex
        await run_in_threadpool(handoff, "@developer", "@preflight-triager", "anyone", "REQUESTED",
                                "The workbench requested live verification of the unpatched PR.", run_id)
        finding = await run_in_threadpool(triage, run_id)
        await asyncio.sleep(1.5)
        reproduction = await run_in_threadpool(repro, run_id)
        await asyncio.sleep(1.5)
        proposal = await run_in_threadpool(prepare_handoff, run_id)
        return {"run_id": run_id, "triage": finding, "repro": reproduction, "prepare": proposal,
                "status": proposal["status"]}


@app.post("/api/control-plane/remediate")
def remediate(body: Remediation, authorization: Annotated[str | None, Header()] = None):
    require_token("PREFLIGHT_APPROVAL_TOKEN", authorization)
    if body.push:
        raise HTTPException(422, detail="Remediation is local only.")
    result = review_patch(body.approval_id, body.diff_sha256, body.decision,
                          approver_identity=os.environ.get("PREFLIGHT_LOCAL_OPERATOR", "authenticated-local-operator"))
    if body.comment_pr and result["status"] == "VERIFIED":
        result["github_comment"] = publish_audit(result)
        record("github_comment", result["github_comment"]["status"], result["github_comment"])
    return result


@app.post("/api/control-plane/discord-settings")
def discord_settings(body: DiscordSettings, authorization: Annotated[str | None, Header()] = None):
    require_token("PREFLIGHT_APPROVAL_TOKEN", authorization)
    try:
        return {"configured": configure_discord(body.webhook_url)}
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc


@app.post("/api/control-plane/reset-pr-head")
@app.post("/api/control-plane/checkout-unpatched")
def reset(body: Approval, authorization: Annotated[str | None, Header()] = None):
    require_token("PREFLIGHT_APPROVAL_TOKEN", authorization)
    return checkout_pr_state(body.approval_id)


@app.post("/api/switch/webhook", status_code=202)
def switch_webhook(body: dict, authorization: Annotated[str | None, Header()] = None):
    require_token("PREFLIGHT_SWITCH_WEBHOOK_TOKEN", authorization)
    if "agent_id" in body and "stdout_proof" in body:
        try:
            console_event = ConsoleEvent.model_validate(body)
        except ValidationError as exc:
            raise HTTPException(422, detail="Invalid Switch Console command event.") from exc
        if (AGENT_ADDRESSING[console_event.agent_id] != console_event.addressing
                or console_event.event_id != f"{console_event.run_id}:{console_event.phase}"):
            raise HTTPException(422, detail="Invalid Switch Console event identity or addressing.")
        event = record("switch_command", console_event.status, console_event.model_dump(),
                       source="switch_console", external_id=console_event.event_id,
                       actor=console_event.agent_id, addressing_mode=console_event.addressing,
                       command=console_event.command, run_id=console_event.run_id)
        return {"ledger_event_id": event["id"]}
    payload = body.get("payload")
    envelope = payload if isinstance(payload, dict) else body
    configured_room = os.environ.get("SWITCH_ROOM_ID")
    room = switch_room(body)
    if configured_room and room != configured_room:
        raise HTTPException(403, detail="Switch event is outside the configured room.")
    external_id = body.get("event_id") or body.get("id") or envelope.get("message_id")
    event = record("switch_event", "RECEIVED", body, source="switch", external_id=(
        f"{room}:{external_id}" if isinstance(external_id, (int, str)) else None
    ))
    return {"ledger_event_id": event["id"]}


@app.get("/api/control-plane/events", dependencies=access)
async def events(request: Request, after: int = 0, last_event_id: Annotated[str | None, Header()] = None):
    try:
        cursor = max(after, int(last_event_id or 0), 0)
    except ValueError as exc:
        raise HTTPException(400, detail="Last-Event-ID must be an integer.") from exc

    async def stream():
        nonlocal cursor
        quiet = 0
        yield "retry: 2000\n\n"
        while not await request.is_disconnected():
            entries = await run_in_threadpool(ledger, cursor)
            for event in entries:
                cursor = event["id"]
                yield f"id: {cursor}\nevent: audit\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            quiet = 0 if entries else quiet + 1
            if quiet == 15:
                yield ": heartbeat\n\n"
                quiet = 0
            await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
