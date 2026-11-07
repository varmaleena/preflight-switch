#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
root=$(pwd -P)
branch=feat/security-tightening-pr402
target=${TARGET_REPO_PATH:-../acmepay-invoice-service}
target_url=https://github.com/varmaleena/acmepay-invoice-service.git
case "${1:-}" in
  ""|--demo) mode=demo ;;
  --setup|--services|--verify|--clean) mode=${1#--} ;;
  --docker) mode=docker ;;
  *) printf 'Usage: %s [--setup|--services|--demo|--verify|--clean|--docker]\n' "$0" >&2; exit 2 ;;
esac

base_python=${PYTHON:-python3}
venv_bin=bin/python
if command -v cygpath >/dev/null 2>&1; then
  base_python=${PYTHON:-python}
  venv_bin=Scripts/python.exe
fi
if [[ "$mode" == clean ]]; then
  "$base_python" - <<'PY'
import shutil
from pathlib import Path
root = Path.cwd().resolve()
for folder in map(Path, ("app", "agents", "tests")):
    for path in folder.rglob("__pycache__"):
        if path.is_dir() and not path.is_symlink() and path.resolve().is_relative_to(root):
            shutil.rmtree(path)
cache = Path(".pytest_cache")
if cache.is_dir() and not cache.is_symlink() and cache.resolve().is_relative_to(root):
    shutil.rmtree(cache)
PY
  exit
fi
if [[ "$mode" == verify ]]; then
  result=0
  "$root/.venv/$venv_bin" -m pytest -q || result=1
  "$root/.venv/$venv_bin" -m agents.cli_tools verify-demo-state || result=1
  "$root/.venv/$venv_bin" -m agents.cli_tools verify-switch-handoff || result=1
  exit "$result"
fi

if [[ ! -e "$target" ]]; then
  git clone --branch "$branch" "$target_url" "$target"
fi
target=$(cd -- "$target" && pwd -P)
git -C "$target" rev-parse --verify "$branch^{commit}" >/dev/null ||
  git -C "$target" branch "$branch" "origin/$branch"
git -C "$target" rev-parse --verify 'main^{commit}' >/dev/null ||
  git -C "$target" branch main origin/main

mkdir -p .demo data
if [[ ! -e .env ]]; then
  make_token() { od -An -N32 -tx1 /dev/urandom | tr -d ' \n'; }
  (umask 077; set -o noclobber; printf 'PREFLIGHT_APPROVAL_TOKEN=%s\nPREFLIGHT_SWITCH_WEBHOOK_TOKEN=%s\n' \
    "$(make_token)" "$(make_token)" > .env)
fi

ready() { curl --noproxy '*' -fsS --max-time 10 "$1" >/dev/null 2>&1; }
occupied() { curl --noproxy '*' -s --max-time 2 "$1" >/dev/null 2>&1; }
wait_ready() {
  local url=$1
  for ((attempt=0; attempt<30; attempt++)); do
    if ready "$url"; then return; fi
    sleep 1
  done
  printf 'Service failed its readiness check: %s\n' "$url" >&2
  return 1
}

if [[ "$mode" == docker ]]; then
  if occupied http://127.0.0.1:8001/health || occupied http://127.0.0.1:8000/; then
    docker compose ps --services --filter status=running | grep -qx preflight-control-plane ||
      { printf 'A local service already owns port 8000 or 8001.\n' >&2; exit 1; }
  fi
  export ACMEPAY_REPO_PATH="$target"
  export PREFLIGHT_CONTAINER_UID="$(id -u)" PREFLIGHT_CONTAINER_GID="$(id -g)"
  if command -v cygpath >/dev/null 2>&1; then
    export ACMEPAY_REPO_PATH="$(cygpath -m "$target")" MSYS_NO_PATHCONV=1
  fi
  docker compose up -d --build
  wait_ready http://127.0.0.1:8001/health
  wait_ready http://127.0.0.1:8000/api/control-plane/state
else
  if command -v cygpath >/dev/null 2>&1; then
    export TARGET_REPO_PATH="$(cygpath -w "$target")"
  else
    export TARGET_REPO_PATH="$target"
  fi
  prepare_python() {
    local project=$1 module=$2 executable="$1/.venv/$venv_bin"
    if [[ ! -f "$executable" ]]; then "$base_python" -m venv "$project/.venv" >&2; fi
    if ! "$executable" -c "import $module" 2>/dev/null; then
      "$executable" -m pip --version >/dev/null 2>&1 || "$executable" -m ensurepip >&2
      "$executable" -m pip install -r "$project/requirements.txt" >&2
    fi
    printf '%s\n' "$executable"
  }
  preflight_python=$(prepare_python "$root" 'fastapi,uvicorn,httpx,dotenv,langgraph,google.adk,yaml')
  if [[ "$mode" == setup ]]; then
    prepare_python "$target" 'fastapi,uvicorn,httpx,python_multipart' >/dev/null
    printf 'Both virtual environments are ready.\n'
    exit
  fi
  if ! ready http://127.0.0.1:8001/health; then
    if occupied http://127.0.0.1:8001/; then printf 'Port 8001 is already in use.\n' >&2; exit 1; fi
    acmepay_python=$(prepare_python "$target" 'fastapi,uvicorn,httpx,python_multipart')
    (cd -- "$target"; nohup "$acmepay_python" -m uvicorn backend.app:app --host 127.0.0.1 --port 8001 \
      >"$root/.demo/acmepay.log" 2>&1 </dev/null &)
  fi
  wait_ready http://127.0.0.1:8001/health
  if ! ready http://127.0.0.1:8000/api/control-plane/state; then
    if occupied http://127.0.0.1:8000/; then printf 'Port 8000 is already in use.\n' >&2; exit 1; fi
    nohup "$preflight_python" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --env-file .env \
      >.demo/preflight.log 2>&1 </dev/null &
  fi
  wait_ready http://127.0.0.1:8000/api/control-plane/state
  "$preflight_python" -c 'from pathlib import Path; from agents.switch_protocol_bridge import read_state; from app.github_inspector import target_repo; state = read_state(); assert Path(state["repository"]["path"]).samefile(target_repo()), "Running control plane targets a different checkout"'
  if [[ "$mode" == demo ]]; then
    "$preflight_python" -m agents.cli_tools verify-demo-state
  fi
fi

printf '\nPreFlight workbench    http://localhost:8000/\n'
printf 'Live state API         http://localhost:8000/api/control-plane/state\n'
printf 'AcmePay health         http://localhost:8001/health\n'
printf 'AcmePay manifest       http://localhost:8001/contract-manifest\n'
printf 'Switch template        %s/preflight.template.yaml\n' "$root"
printf '\nFor the pinned Discord room, see DEMO.md. Starting local services does not send a room message.\n'
