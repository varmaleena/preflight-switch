"""Inspect the target checkout without executing its source files."""

import ast
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from threading import Lock
from time import monotonic

import httpx


ROOT = Path(__file__).resolve().parents[1]
BASE = "main"
PR_BRANCH = "feat/security-tightening-pr402"
CONFIG = "backend/config.py"
SDK = "frontend_sdk/client_config.json"
RUNBOOK = "docs/oncall-runbook.md"
POLICY_FIELDS = {"SERVICE_NAME", "VERSION", "MAX_UPLOAD_MB", "RETRY_ENV_VAR", "DEFAULT_RETRIES"}
LIMIT = re.compile(r"(?im)(\bmaximum(?: allowed)? (?:invoice )?(?:upload |invoice )?size is\s+\*{0,2})(\d+(?:\.\d+)?)[ \t]*(?:MiB|MB)\b")
_pr_lock = Lock()
_pr_cache: dict[str, tuple[float, dict]] = {}


class InspectionError(RuntimeError):
    pass


def target_repo() -> Path:
    return Path(os.environ.get("TARGET_REPO_PATH") or ROOT.parent / "acmepay-invoice-service").expanduser().resolve()


def git(*args: str, timeout: int = 10) -> str:
    repo = target_repo()
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "--no-optional-locks", *args],
            capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InspectionError(f"Git could not inspect {repo}: {exc}") from exc
    if result.returncode:
        raise InspectionError(result.stderr.strip() or f"git {args[0]} exited with {result.returncode}.")
    return result.stdout


def parse_policy(source: str) -> dict:
    values = {}
    for statement in ast.parse(source).body:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name) and target.id in POLICY_FIELDS:
                    values[target.id] = ast.literal_eval(statement.value)
    if missing := POLICY_FIELDS - values.keys():
        raise ValueError(f"Backend policy is missing: {', '.join(sorted(missing))}.")
    if type(values["MAX_UPLOAD_MB"]) is not int or values["MAX_UPLOAD_MB"] <= 0:
        raise ValueError("MAX_UPLOAD_MB must be a positive integer.")
    if not isinstance(values["RETRY_ENV_VAR"], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", values["RETRY_ENV_VAR"]):
        raise ValueError("RETRY_ENV_VAR must be an environment variable name.")
    return values


def read_contracts() -> dict:
    repo = target_repo()
    try:
        backend = parse_policy((repo / CONFIG).read_text(encoding="utf-8"))
        sdk = json.loads((repo / SDK).read_text(encoding="utf-8"))
        runbook = (repo / RUNBOOK).read_text(encoding="utf-8")
        if type(sdk["allowed_upload_mb"]) is not int or sdk["allowed_upload_mb"] <= 0:
            raise ValueError("allowed_upload_mb must be a positive integer.")
        if not isinstance(sdk["expected_retry_env"], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", sdk["expected_retry_env"]):
            raise ValueError("expected_retry_env must be an environment variable name.")
    except (OSError, SyntaxError, ValueError, TypeError, KeyError) as exc:
        raise InspectionError(f"Could not read target contracts: {exc}") from exc
    return {"backend": backend, "frontend_sdk": sdk, "runbook": runbook}


def contract_drift(contracts: dict) -> dict:
    backend, sdk, runbook = (contracts[key] for key in ("backend", "frontend_sdk", "runbook"))
    runtime_limit = backend["MAX_UPLOAD_MB"]
    frontend_limit = sdk["allowed_upload_mb"]
    retry = backend["RETRY_ENV_VAR"]
    deprecated = {"LEGACY_RETRY_LIMIT", sdk["expected_retry_env"]} - {retry}
    stale = sorted(key for key in deprecated if re.search(rf"\b{re.escape(key)}\b", runbook))
    limits = [float(match[2]) for match in LIMIT.finditer(runbook)]
    audit = {
        "required_retry_env": retry,
        "required_retry_env_documented": bool(re.search(rf"\b{re.escape(retry)}\b", runbook)),
        "deprecated_env_vars": stale,
        "documented_limits_mb": limits,
        "stale_upload_limit": not limits or any(limit != runtime_limit for limit in limits),
    }
    return {
        "backend_max_upload_mb": runtime_limit,
        "frontend_allowed_upload_mb": frontend_limit,
        "boundary_delta_mb": frontend_limit - runtime_limit,
        "payload_boundary_drift": frontend_limit != runtime_limit,
        "payload_drift": frontend_limit != runtime_limit,
        "env_drift": sdk["expected_retry_env"] != retry,
        "runbook_drift": bool(stale) or not audit["required_retry_env_documented"] or audit["stale_upload_limit"],
        "frontend_retry_env": sdk["expected_retry_env"],
        "runbook_env_drift": (
            sdk["expected_retry_env"] != retry or bool(stale)
            or not audit["required_retry_env_documented"] or audit["stale_upload_limit"]
        ),
        "runbook_audit": audit,
    }


def github_slug(remote: str) -> str | None:
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([\w.-]+/[\w.-]+?)(?:\.git)?/?", remote)
    return match[1] if match else None


def fetch_pull_requests(remote: str) -> dict:
    slug = github_slug(remote)
    if not slug:
        return {"source": "local_git", "items": None, "error": "Origin is not a recognized github.com remote."}
    if shutil.which("gh"):
        try:
            result = subprocess.run(
                ["gh", "pr", "list", "--repo", slug, "--state", "open",
                 "--json", "number,title,headRefName,baseRefName,url,author,state"],
                capture_output=True, encoding="utf-8", check=True, timeout=6,
            )
            return {"source": "gh", "items": json.loads(result.stdout)}
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = httpx.get(
            f"https://api.github.com/repos/{slug}/pulls", params={"state": "open", "per_page": 100},
            headers=headers, timeout=6,
        )
        response.raise_for_status()
        return {"source": "github_rest", "items": [
            {"number": pr["number"], "title": pr["title"], "headRefName": pr["head"]["ref"],
             "baseRefName": pr["base"]["ref"], "url": pr["html_url"], "state": pr["state"],
             "author": {"login": pr["user"]["login"]}}
            for pr in response.json()
        ]}
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        return {"source": "local_git", "items": None, "error": f"GitHub PR lookup unavailable: {type(exc).__name__}."}


def open_pull_requests(remote: str) -> dict:
    # The UI polls local Git and HTTP evidence every two seconds. Cache only hosted metadata.
    with _pr_lock:
        now = monotonic()
        cached = _pr_cache.get(remote)
        if cached and now < cached[0]:
            return cached[1]
        result = fetch_pull_requests(remote)
        _pr_cache[remote] = (monotonic() + 75, result)
        return result


def inspect_repository() -> dict:
    remote = git("remote", "get-url", "origin").strip()
    branch = git("rev-parse", "--abbrev-ref", "HEAD").strip()
    commit = git("rev-parse", "HEAD").strip()
    base = os.environ.get("TARGET_BASE_BRANCH", BASE)
    pr_branch = os.environ.get("TARGET_PR_BRANCH", PR_BRANCH)
    try:
        # Validate refs before using them to construct a revision range.
        base_sha = git("rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}").strip()
        pr_sha = git("rev-parse", "--verify", "--end-of-options", f"{pr_branch}^{{commit}}").strip()
        comparison = f"{base}..{pr_branch}"
        revisions = (base_sha, pr_sha)
    except InspectionError:
        base_sha = git("rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}").strip()
        comparison = f"{base}...HEAD"
        revisions = (f"{base_sha}...{commit}",)
    flags = ("--no-ext-diff", "--no-textconv", "--no-color")
    return {
        "path": str(target_repo()), "origin": remote, "branch": branch, "commit_sha": commit,
        "short_sha": git("rev-parse", "--short", "HEAD").strip(),
        "history": [
            {"sha": sha, "subject": subject}
            for line in git("log", "-10", "--format=%H%x09%s").splitlines()
            for sha, _, subject in [line.partition("\t")]
        ],
        "comparison": comparison, "base_ref": base, "unified_diff": git("diff", *flags, *revisions, "--"),
        "head_diff": git("diff", *flags, base_sha, commit, "--"),
        "working_tree_status": git("status", "--short"),
        "working_tree_diff": git("diff", *flags, "HEAD", "--"),
        "pull_requests": open_pull_requests(remote),
    }
