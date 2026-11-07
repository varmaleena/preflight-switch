# PreFlight demo

**A Switch-native, human-approved multi-agent release-verification room for API contract drift.**

> PreFlight is a Switch-native release-verification room for API contract drift. When an engineer initiates verification for a pull request, PreFlight compares the backend contract with the frontend SDK and on-call runbook, executes controlled HTTP probes against a local target service, publishes SHA-256-backed evidence in the Switch room, and requires explicit human approval before applying a narrowly scoped SDK or documentation remediation.
>
> The current demo verifies the real 2 MiB backend upload limit versus the 10 MiB SDK allowance, reproduces the oversized-upload rejection with real HTTP evidence, and applies a human-approved local SDK/runbook patch. It does not yet automatically ingest every GitHub PR, modify backend code, enforce a GitHub merge block, or test production systems.

## Set up the configured case

1. Start AcmePay and the workbench with `make demo` or `make.bat demo`. Verify that [PR #1](https://github.com/varmaleena/acmepay-invoice-service/pull/1) and the clean `feat/security-tightening-pr402` checkout both point to `df746a7`.
2. On the configured Windows workstation, use the existing [Discord-backed room](https://discord.com/channels/1568584928759783567/1568585020472561744) and the three registered agents. Confirm Switch can publish in the channel and the operator is linked. Set `PREFLIGHT_SWITCH_APPROVER` in the local `.env` to the linked identity from authenticated Switch room history. Start `python -m agents.native_switch` from the project environment if the listener is down. The native listener uses the workstation's Switch-managed agent bindings.
3. Run `python -m agents.cli_tools verify-switch-handoff` to check live room history and membership. Before the first workflow, later steps will report `NOT_OBSERVED`.

## Show the evidence

1. Send `Run PreFlight verification for PR #402.` in the room. Show the triage report, the targeted repro handoff, and the room evidence. A 1.5 MiB PDF receives HTTP 200. A 4 MiB PDF receives HTTP 413 even though the SDK allows it. Both results include SHA-256 evidence.
2. Open the workbench after the kickoff. The triage result unlocks the Contract Matrix. Sandbox shows the repro agent’s requests automatically. Review the exact SDK/runbook diff and proposal ID in the Discord thread.
3. Reply `Approved` for a local patch and audit comment, `Rejected` to decline, or `Approve and merge <proposal-id>` to authorize a commit, push, and merge after verification. Use the linked approver account. The after-check still returns HTTP 200 for 1.5 MiB and HTTP 413 for 4 MiB. Sandbox shows both runs and the SDK’s changed decision for the 4 MiB file. Overview links to GitHub after GitHub confirms the merge. A real merge closes the seeded PR, so use the merge command during the final take.
4. Run the handoff diagnostic again to confirm which steps have evidence. Use **Run Local Orchestration** for the explicitly labeled local path if Discord is unavailable.
