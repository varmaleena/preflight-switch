# PreFlight

**A Switch-native, human-approved multi-agent release-verification room for API contract drift.**

> PreFlight is a Switch-native release-verification room for API contract drift. When an engineer initiates verification for a pull request, PreFlight compares the backend contract with the frontend SDK and on-call runbook, executes controlled HTTP probes against a local target service, publishes SHA-256-backed evidence in the Switch room, and requires explicit human approval before applying a narrowly scoped SDK or documentation remediation.
>
> The current demo verifies the real 2 MiB backend upload limit versus the 10 MiB SDK allowance, reproduces the oversized-upload rejection with real HTTP evidence, and applies a human-approved local SDK/runbook patch. It does not yet automatically ingest every GitHub PR, modify backend code, enforce a GitHub merge block, or test production systems.

The configured case is [AcmePay GitHub PR #1, titled PR #402](https://github.com/varmaleena/acmepay-invoice-service/pull/1). Demo verification checks the clean local checkout against the hosted PR and its pinned revision. See [DEMO.md](DEMO.md) for the room setup and walkthrough.

## Run locally

Use Git, Python 3.11+, Bash, and curl. On Windows, use Git Bash via `make.bat`. The launcher creates local virtual environments, starts AcmePay on `:8001` and the workbench on `:8000`, and uses loopback by default. [.env.example](.env.example) lists settings without credentials.

| Task | Unix | Windows |
|---|---|---|
| Set up both environments | `make setup` | `make.bat setup` |
| Start the local services | `make services` | `make.bat services` |
| Start services and check the seeded revision | `make demo` | `make.bat demo` |
| Run tests and both diagnostics | `make verify` | `make.bat verify` |
| Clear generated Python and pytest caches | `make clean` | `make.bat clean` |

Open the [local workbench](http://localhost:8000/). **Run Local Orchestration** produces real probe evidence and a reviewable diff. **Review Local Patch** records the authenticated decision against that exact proposal. The launcher leaves the target files in place. Use the workbench’s authorized reset to restore the seeded files before a second demo.

The read-only CLI diagnostics are `python -m agents.cli_tools verify-demo-state` and `python -m agents.cli_tools verify-switch-handoff`. The latter exits nonzero if a required step has failed or has no recorded proof. For explicitly local-only evidence, use `--local-only` with `verify-demo-state`, `triage`, or `repro`. Local-only mode skips the hosted comparison and still requires the correct repository, branch, clean tree, and seeded files. It grants no mutation or publication authority.

## Frameworks and evidence

Switch Rooms provides native messaging and addressing for the three Codex-backed agent identities. The pinned Python listener uses Switch’s authenticated operations for room history and handoffs. Python enforces the revision and diff-bound approval checks. LangGraph is available for the separate local orchestration pipeline. Google ADK is integrated only for optional Gemini runbook advice and is outside the native approval path.

The workbench distinguishes native room messages, local orchestration, rehearsal transcripts, GitHub publications, and sandbox probes. GitHub publication requires a working authenticated `gh` client. The resulting comment describes the verified local patch.
