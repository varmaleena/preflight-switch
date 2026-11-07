"""Identify the seeded checkout before running demo verification."""

import os
from threading import Lock
from time import monotonic

import httpx

from app.github_inspector import CONFIG, PR_BRANCH, RUNBOOK, SDK, InspectionError, contract_drift, git, read_contracts


REPOSITORY = "varmaleena/acmepay-invoice-service"
PR_NUMBER = 1
DEMO_HEAD = "df746a739620f9a65b84c43f96f726b48c6b1942"
REVISION_MISMATCH = "Inspection revision does not match hosted PR head."
_lock = Lock()
_cached_head = (0.0, None, None)


def hosted_pr():
    headers = {"Accept": "application/vnd.github+json"}
    if token := os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    response = httpx.get(f"https://api.github.com/repos/{REPOSITORY}/pulls/{PR_NUMBER}", headers=headers, timeout=6)
    response.raise_for_status()
    pr = response.json()
    if (pr["number"] != PR_NUMBER or pr["state"] != "open" or pr["head"]["ref"] != PR_BRANCH
            or pr["head"]["repo"]["full_name"] != REPOSITORY):
        raise ValueError("PR #1 is not open on the expected repository and branch.")
    return pr["head"]["sha"]


def hosted_head(*, fresh: bool):
    global _cached_head
    with _lock:
        deadline, head, error = _cached_head
        if fresh or monotonic() >= deadline:
            try:
                head, error = hosted_pr(), None
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                head, error = None, f"Hosted PR head could not be verified ({type(exc).__name__})."
            _cached_head = (monotonic() + 30, head, error)
        return head, error


def verify_demo_state(*, local_only: bool = False, fresh: bool = True) -> dict:
    remote = git("remote", "get-url", "origin").strip()
    head = git("rev-parse", "HEAD").strip()
    branch = git("rev-parse", "--abbrev-ref", "HEAD").strip()
    dirty = git("status", "--porcelain", "--untracked-files=all").splitlines()
    origins = {f"https://github.com/{REPOSITORY}", f"git@github.com:{REPOSITORY}", f"ssh://git@github.com/{REPOSITORY}"}
    problems = []
    if remote.removesuffix(".git") not in origins or branch != PR_BRANCH:
        problems.append("The checkout must be AcmePay on feat/security-tightening-pr402.")
    if dirty:
        problems.append("The demo requires a clean working tree.")
    try:
        seeded = not git("diff", "--no-ext-diff", "--no-textconv", "--name-only",
                         DEMO_HEAD, "--", CONFIG, SDK, RUNBOOK).strip()
        drift = contract_drift(read_contracts())
        drift_status = ("BLOCKED" if any(drift[key] for key in ("payload_drift", "env_drift", "runbook_drift"))
                        else "ALIGNED")
        seeded = seeded and drift["backend_max_upload_mb"] == 2 and drift["frontend_allowed_upload_mb"] == 10
    except InspectionError:
        seeded, drift_status = False, "UNVERIFIED"
    if not seeded:
        problems.append("The expected seeded policy, SDK, and runbook files are missing or changed.")
    hosted, hosted_error = (None, None) if local_only else hosted_head(fresh=fresh)
    if not local_only:
        if head != DEMO_HEAD or (hosted is not None and (hosted != DEMO_HEAD or head != hosted)):
            problems.append(REVISION_MISMATCH)
        if hosted_error:
            problems.append(hosted_error)
    return {"repository": REPOSITORY, "origin": remote, "pr_number": PR_NUMBER,
            "pr_head_sha": hosted, "pinned_head_sha": DEMO_HEAD, "local_head_sha": head, "branch": branch,
            "dirty_files": dirty, "seeded_files_match": seeded, "contract_drift_status": drift_status,
            "local_only": local_only, "demo_safe": not problems, "problems": problems}


def require_demo_revision(*, local_only: bool = False) -> None:
    result = verify_demo_state(local_only=local_only)
    if not result["demo_safe"]:
        raise InspectionError(" ".join(result["problems"]))
