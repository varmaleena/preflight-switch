"""Publish verification summaries to explicitly configured external channels."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile

import httpx

from app.github_inspector import ROOT, git, github_slug, target_repo


DISCORD_URL = re.compile(r"https://discord\.com/api(?:/v[0-9]+)?/webhooks/[0-9]{16,22}/[A-Za-z0-9._-]{30,160}\Z")
DISCORD_PATH = ROOT / "data" / ".discord_webhook"
AVATARS = {"@preflight-triager": 0, "@preflight-repro": 2, "@preflight-runbook": 3}


def discord_url() -> str | None:
    try:
        value = DISCORD_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        value = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    return value if DISCORD_URL.fullmatch(value) else None


def configure_discord(value: str) -> bool:
    value = value.strip()
    if value and not DISCORD_URL.fullmatch(value):
        raise ValueError("Use an HTTPS discord.com webhook URL.")
    # An empty local override explicitly disables notifications, including an environment default.
    DISCORD_PATH.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=DISCORD_PATH.parent, delete=False) as stream:
        path = Path(stream.name)
        stream.write(value.encode("utf-8"))
    try:
        os.replace(path, DISCORD_PATH)
    finally:
        path.unlink(missing_ok=True)
    return bool(value)


def discord_notify(event: dict) -> str | None:
    try:
        url = discord_url()
        if not url:
            return None
        actor = event["from_agent"]
        avatar = AVATARS.get(actor, 1)
        message = f"**{event['step_status']}** · {event['summary']}"
        if event.get("to_agent"):
            message += f"\nHandoff to {event['to_agent']}"
        response = httpx.post(
            url, json={"username": actor.removeprefix("@"),
                       "avatar_url": f"https://cdn.discordapp.com/embed/avatars/{avatar}.png",
                       "content": message[:1900], "allowed_mentions": {"parse": []}},
            timeout=5, follow_redirects=False,
        )
        return "delivered" if response.is_success else f"http_{response.status_code}"
    except (OSError, httpx.HTTPError):
        return "delivery_failed"


def github_cli() -> str:
    configured = os.environ.get("PREFLIGHT_GH_PATH")
    found = configured or shutil.which("gh")
    if found:
        return found
    local = next((ROOT / ".demo" / "github-cli").rglob("gh.exe"), None)
    if local:
        return str(local)
    raise RuntimeError("GitHub CLI is unavailable.")


def github_environment() -> dict[str, str]:
    env = {**os.environ, "GH_HOST": "github.com", "GH_PROMPT_DISABLED": "1", "GIT_TERMINAL_PROMPT": "0"}
    if env.get("GH_TOKEN") or env.get("GITHUB_TOKEN") or os.name != "nt":
        return env
    try:
        result = subprocess.run(
            ["git", "-c", "credential.interactive=never", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n", text=True, capture_output=True,
            timeout=5, env={**env, "GCM_INTERACTIVE": "never"},
        )
        if result.returncode == 0:
            token = next((line.removeprefix("password=") for line in result.stdout.splitlines()
                          if line.startswith("password=")), "")
            if token:
                env["GH_TOKEN"] = token
    except (OSError, subprocess.SubprocessError):
        pass
    return env


def markdown_audit(result: dict) -> str:
    triage = result.get("triage", result)
    verification = result.get("verification") or result.get("repro") or result
    runbook = result.get("runbook_audit") or result.get("prepare") or {}
    drift = triage.get("drift") or result.get("drift") or {}
    runbook_status = runbook.get("runbook_status") or runbook.get("status") or (
        "STALE" if drift.get("runbook_drift") else "ALIGNED" if drift else "NOT CHECKED"
    )
    commit = triage.get("commit_sha") or git("rev-parse", "HEAD").strip()
    gate = result.get("release_gate") or triage.get("overall_status") or verification.get("status") or "OBSERVED"
    lines = [
        "## PreFlight Switch Room verification", "",
        f"Revision `{commit}` · Result **{gate}**", "",
        "| Agent | Addressing | SHA-256 payload digest | HTTP status | Runbook status |",
        "| --- | --- | --- | --- | --- |",
        f"| `@preflight-triager` | anyone | — | — | {runbook_status} |",
    ]
    for probe in verification.get("probes", []):
        lines.append(
            f"| `@preflight-repro` | owner-agents | `{probe['sha256']}` | "
            f"{probe['http_status']} ({probe['size_mb']:g} MB) | — |"
        )
    if not verification.get("probes"):
        lines.append("| `@preflight-repro` | owner-agents | — | Not run in this step | — |")
    lines.append(f"| `@preflight-runbook` | owner-agents | — | — | {runbook_status} |")
    if any(probe.get("cross_boundary_regression") for probe in verification.get("probes", [])):
        lines.extend(["", "The SDK permits the 4 MB PDF while the live backend returns "
                      "`413 PAYLOAD_CONTRACT_VIOLATION`. The release remains blocked pending human approval."])
    if result.get("before_verification"):
        before = result["before_verification"]["probes"]
        lines.extend(["", "### Before and after the approved local patch", "",
                      "| Size | Before | After | SDK after |",
                      "| --- | --- | --- | --- |"])
        for old, new in zip(before, verification["probes"]):
            lines.append(f"| {new['size_mb']:g} MiB | HTTP {old['http_status']} | HTTP {new['http_status']} | "
                         f"{'Allows' if new['frontend_allows'] else 'Rejects'} |")
        lines.extend(["", f"Approval `{result['approval']['approval_id']}` · "
                      f"reviewed diff SHA-256 `{result['approval']['diff_sha256']}`. "
                      "The verified patch is local."])
    lines.extend(["", "_Evidence collected against the running AcmePay service by PreFlight._", ""])
    return "\n".join(lines)


def comment_audit(result: dict) -> dict:
    slug = github_slug(git("remote", "get-url", "origin").strip())
    if not slug:
        raise RuntimeError("Target origin is not a supported GitHub repository.")
    branch = os.environ.get("TARGET_PR_BRANCH", "feat/security-tightening-pr402")
    executable = github_cli()
    env = github_environment()

    def gh(*args: str) -> str:
        response = subprocess.run([executable, *args], cwd=target_repo(), env=env, capture_output=True,
                                  text=True, encoding="utf-8", timeout=25)
        if response.returncode:
            raise RuntimeError(f"GitHub CLI {args[0]} {args[1]} returned exit code {response.returncode}.")
        return response.stdout.strip()

    pr = json.loads(gh("pr", "view", branch, "--repo", slug, "--json", "number,url,state,headRefName,headRefOid"))
    if pr["state"] != "OPEN" or pr["headRefName"] != branch:
        raise RuntimeError("Could not confirm an open PR on the target branch.")
    if "expected_pr_number" in result and (pr["number"] != result["expected_pr_number"]
                                         or pr["headRefOid"] != result["expected_pr_head"]):
        raise RuntimeError("The hosted PR or its head differs from the reviewed revision.")
    with NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".md", delete=False) as body:
        path = Path(body.name)
        body.write(markdown_audit(result))
    try:
        url = gh("pr", "comment", str(pr["number"]), "--repo", slug, "--body-file", str(path))
    finally:
        path.unlink(missing_ok=True)
    return {"status": "POSTED", "pr_number": pr["number"], "pr_url": pr["url"], "comment_url": url}


def publish_audit(result: dict) -> dict:
    try:
        return comment_audit(result)
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        return {"status": "FAILED", "error": str(exc)}
