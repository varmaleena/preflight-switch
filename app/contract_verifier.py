"""Send real PDF or user-supplied bytes to the invoice service."""

import os
from decimal import Decimal
from hashlib import sha256
from time import perf_counter

import httpx

from app.github_inspector import git, read_contracts


MIB = 1024 * 1024
MAX_PROBE_BYTES = 32 * MIB


class TargetUnavailable(RuntimeError):
    pass


def target_client() -> httpx.Client:
    return httpx.Client(base_url=os.environ.get("TARGET_API_URL", "http://localhost:8001"), timeout=15, trust_env=False)


def live_contracts(client: httpx.Client) -> dict:
    try:
        health = client.get("/health")
        manifest = client.get("/contract-manifest")
        health.raise_for_status()
        manifest.raise_for_status()
        runtime = {"health": health.json(), "manifest": manifest.json()}
        policy = runtime["manifest"]["backend"]
        if not isinstance(policy["MAX_UPLOAD_MB"], int) or policy["MAX_UPLOAD_MB"] <= 0:
            raise ValueError("Invalid live upload policy")
        if not isinstance(policy["RETRY_ENV_VAR"], str):
            raise ValueError("Invalid live retry setting")
        return runtime
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise TargetUnavailable(f"AcmePay health or contract manifest could not be verified: {exc}") from exc


def pdf_payload(size_bytes: int) -> bytes:
    # A complete one-page PDF with a cross-reference table and a padding comment.
    parts = [b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n"]
    offsets = [0]
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 72 72] >>",
    )
    for index, content in enumerate(objects, 1):
        offsets.append(sum(map(len, parts)))
        parts.append(f"{index} 0 obj\n".encode() + content + b"\nendobj\n")
    body = b"".join(parts)
    table = b"xref\n0 4\n0000000000 65535 f \n" + b"".join(
        f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:]
    )
    trailer = b"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n"
    ending = b"\n%%EOF\n"
    xref = size_bytes - len(table) - len(trailer) - len(ending) - 10
    padding = xref - len(body)
    if padding < 2:
        raise ValueError("Probe size is too small to encode a PDF.")
    return body + b"%" + b"x" * (padding - 2) + b"\n" + table + trailer + f"{xref:010d}".encode() + ending


def run_file_probe(content: bytes, filename: str, content_type: str = "application/pdf") -> dict:
    if len(content) > MAX_PROBE_BYTES:
        raise ValueError(f"Probe exceeds the {MAX_PROBE_BYTES}-byte control-plane limit.")
    contracts = read_contracts()
    frontend_limit = contracts["frontend_sdk"]["allowed_upload_mb"]
    digest = sha256(content).hexdigest()
    size_mb = len(content) / MIB
    frontend_allows = len(content) <= frontend_limit * MIB
    with target_client() as client:
        runtime = live_contracts(client)
        start = perf_counter()
        try:
            response = client.post(
                "/v1/invoices/upload",
                files={"file": (filename, content, content_type)},
                data={"merchant_id": "preflight-verifier"},
            )
        except httpx.HTTPError as exc:
            raise TargetUnavailable(f"AcmePay multipart request failed: {exc}") from exc
        latency = round((perf_counter() - start) * 1000, 2)
    try:
        body = response.json()
    except ValueError:
        body = {"non_json_body": response.text[:2000]}
    policy = runtime["manifest"]["backend"]
    health = runtime["health"]
    consistent = (
        policy == contracts["backend"]
        and runtime["manifest"].get("frontend_sdk") == contracts["frontend_sdk"]
        and health.get("enforced_max_upload_mb") == policy["MAX_UPLOAD_MB"]
        and health.get("active_retry_env_var") == policy["RETRY_ENV_VAR"]
        and health.get("git_commit_sha") == git("rev-parse", "HEAD").strip()
    )
    data = body if isinstance(body, dict) else {}
    detail = data.get("detail")
    expected_http = 200 if len(content) <= policy["MAX_UPLOAD_MB"] * MIB else 413
    accepted_proof = (
        response.status_code == 200 and data.get("status") == "ACCEPTED" and data.get("received_mb") == size_mb
        and data.get("receipt_id") == digest[:24]
    )
    rejected_proof = (
        response.status_code == 413 and isinstance(detail, dict)
        and detail.get("error_code") == "PAYLOAD_CONTRACT_VIOLATION"
        and detail.get("received_mb") == size_mb
        and detail.get("enforced_max_upload_mb") == policy["MAX_UPLOAD_MB"]
        and detail.get("active_retry_env_var") == policy["RETRY_ENV_VAR"]
    )
    verified = consistent and response.status_code == expected_http and (accepted_proof or rejected_proof)
    regression = bool(verified and frontend_allows and rejected_proof)
    return {
        "filename": filename, "content_type": content_type, "size_bytes": len(content), "size_mb": size_mb,
        "sha256": digest, "latency_ms": latency, "http_status": response.status_code,
        "response_json": body, "expected_backend_http_status": expected_http,
        "frontend_allowed_upload_mb": frontend_limit, "frontend_allows": frontend_allows,
        "runtime_policy_matches_checkout": consistent, "verified": verified,
        "cross_boundary_regression": regression,
        "classification": (
            "CROSS_BOUNDARY_REGRESSION" if regression else "VERIFIED" if verified else "UNVERIFIED"
        ),
        "target_commit_sha": health.get("git_commit_sha"),
    }


def run_boundary_probe(size_mb: float, filename: str = "preflight-invoice.pdf") -> dict:
    size = Decimal(str(size_mb)) * MIB
    if not size.is_finite() or size < 0 or size > MAX_PROBE_BYTES or size != int(size):
        raise ValueError("size_mb must resolve to a whole byte within the probe limit.")
    return run_file_probe(pdf_payload(int(size)), filename, "application/pdf")


def execute_live_probe(size_mb: float, filename: str | None = None, content_bytes: bytes | None = None,
                       *, content_type: str = "application/pdf", run_id: str | None = None) -> dict:
    from app.agent_orchestrator import record

    name = filename or "preflight-invoice.pdf"
    result = (run_boundary_probe(size_mb, name) if content_bytes is None
              else run_file_probe(content_bytes, name, content_type))
    event = record("probe", result["classification"], result, run_id=run_id)
    return {**result, "ledger_event_id": event["id"]}
