# Human-Gated Kanban Development Workflow: Current State and Handoff

**Last verified:** 2026-07-23
**Repository:** `/home/noam/.hermes/hermes-agent`
**User fork:** `https://github.com/noambav/hermes-agent`
**Workflow implementation baseline:** `9bb0402f579450fd02434467580036336d2e785f`
**Status:** Planning lifecycle implemented and verified; Instructor lifecycle deliberately not started

## Purpose

This document is the canonical handoff for future agents working on Noam's Hermes human-gated development workflow. Read it before searching old sessions or acting on the older orchestration design documents.

The verified lifecycle currently ends at a blocked Implementation card:

```text
Triage
  → Default explicitly processes the named board's Triage batch
  → same card becomes official [Planning]
  → Planner writes and commits specification + implementation plan
  → native handoff transforms the same card
  → [Implementation], assigned to instructor, blocked
  → human Blocked → Ready approval (future phase)
```

The intended later lifecycle is:

```text
human Blocked → Ready approval
  → Instructor implements in the existing workspace
  → tests, commit, push, and pull request
  → same card becomes [Review], blocked
  → human reviews and merges
  → Done
```

The Instructor and Review portions are future work. Do not begin them without explicit human authorization.

## Source-of-truth warning

Two older documents remain in this repository for provenance:

- `docs/superpowers/specs/2026-07-21-hermes-claude-kanban-orchestration-design.md`
- `docs/superpowers/plans/2026-07-21-hermes-claude-kanban-orchestration.md`

They describe an earlier standalone control-repository architecture, separate approval cards, and a `claude-controller` role. That architecture was not implemented and is partly superseded by the simpler native same-card lifecycle documented here. Do not execute those documents as the current plan.

## Non-negotiable workflow decisions

1. **One card, transformed in place.** Triage, Planning, Implementation, and Review use the same task ID so body, wording, comments, dependencies, attachments, run history, decisions, and event provenance remain together.
2. **Phase titles are workflow signals.** `[Planning]`, `[Implementation]`, and later `[Review]` are deterministic lifecycle states, not decorative prefixes.
3. **Default owns Triage.** It acts only after the human explicitly asks it to process Triage on a specific board.
4. **Planner is planning-only.** It may inspect the repository, ask questions durably, write and commit planning artifacts, and invoke native handoff. It may not implement product code or launch a coding agent.
5. **Planning completion is not implementation authorization.** Native handoff ends in blocked Implementation.
6. **Only a human Blocked → Ready action authorizes implementation.** Comments, planning completion, artifact creation, or handoff do not authorize Instructor.
7. **Human review and merge remain separate.** No worker may merge automatically. Native automated `review` dispatch is not used for the human-only review gate.
8. **Canonical Superpowers is required where specified.** Claude workflows must use the canonical `obra/superpowers` marketplace installation, not copied or adapted mirrors.
9. **No automatic Triage polling.** `kanban.auto_decompose` remains false.
10. **No automatic merge.** This remains prohibited throughout the workflow.

## Implemented native operations

### Planning → blocked Implementation: `kanban_handoff`

Published in commit:

```text
823fed3b7d1c577f4fdcf3815d0114314ff8903e
feat(kanban): add native planning handoff
```

The operation atomically transforms an official Planning card into blocked Implementation while preserving card identity and durable state. It validates:

- Planner ownership and run identity.
- Workflow template, phase, and title.
- Persistent Git workspace and repository identity.
- A clean planning-only commit.
- Separate specification and implementation-plan files.
- Canonical artifact paths under:
  - `docs/superpowers/specs/`
  - `docs/superpowers/plans/`
- Artifact bytes and hashes read from the declared immutable Git commit, not the mutable worktree.
- Attachment size and binary-safe persistence.
- Transactional card/attachment mutation.
- Retry and concurrent-winner idempotency.
- Board, notification, workspace, and task scope.

The result must be the same card with:

```text
title:    [Implementation] ...
assignee: instructor
status:   blocked
step:     implementation
workflow: human-gated-development-v1
```

### Triage → official Planning: `kanban_prepare_planning`

Published in commit:

```text
9bb0402f579450fd02434467580036336d2e785f
feat(kanban): add planning phase preparation
```

This is a constrained Default-orchestrator-only bridge. It is not a generic workflow-field editor. It:

- Accepts one existing Triage card.
- Preserves the same task ID, body, priority, comments, history, dependencies, workspace, and attachments.
- Sets workflow `human-gated-development-v1` and step `planning`.
- Sets the exact `[Planning]` title and assigns `planner`.
- Sets parent-free work to `ready`.
- Keeps dependent work in `todo` until every parent is specifically `done`.
- Does not accept an `archived` parent as satisfying this human-gated Planning dependency.
- Is atomic and retry-safe.
- Rejects invalid source states.
- Rejects task workers and every non-Default profile.
- Resolves Default identity authoritatively even when `HERMES_PROFILE` is unset.
- Rechecks the validated workspace kind/path inside the database transaction to close the preflight-to-write race.

The done-only Planning dependency rule is consistently enforced in preparation, readiness recomputation, claiming, and unblock paths.

## Relevant implementation files

Core source:

- `hermes_cli/kanban_db.py`
  - Planning preparation transition.
  - Planning → Implementation handoff.
  - workflow/phase validation.
  - done-only Planning dependency semantics.
  - workspace transaction checks.
  - attachment and idempotency behavior.
- `tools/kanban_tools.py`
  - `kanban_prepare_planning` and `kanban_handoff` schemas and handlers.
  - Default-only and worker/orchestrator authority gates.
- `toolsets.py`
  - native toolset exposure.
- `agent/transports/hermes_tools_mcp_server.py`
  - Hermes/Codex MCP exposure.

Tests:

- `tests/hermes_cli/test_kanban_phase_handoff.py`
- `tests/tools/test_kanban_handoff.py`
- `tests/tools/test_kanban_tools.py`
- `tests/test_toolsets.py`
- `tests/agent/transports/test_hermes_tools_mcp_server.py`

## Installed profile and skill wiring

Default Triage procedure:

```text
/home/noam/.hermes/skills/software-development/batch-triage-orchestrator/SKILL.md
```

It requires Default to:

- Verify the named board and exact repository root.
- Read the complete relevant Triage batch together.
- Preserve the human's wording and unresolved choices.
- Use proportional approval for meaningful decomposition or product decisions.
- Reuse atomic cards rather than creating duplicates.
- Serialize real dependencies with native parent links.
- Invoke `kanban_prepare_planning`; never emulate Planning with generic mutations.
- Stop before specification writing, Instructor work, implementation, review, or merge.

Planner contract:

```text
/home/noam/.hermes/profiles/planner/SOUL.md
```

Planner procedure:

```text
/home/noam/.hermes/profiles/planner/skills/software-development/planner-kanban-handoff/SKILL.md
```

Planner configuration remains planning-only and uses:

```text
provider: openai-codex
model:    gpt-5.6-sol
```

When meaningful decisions are unresolved, Planner must:

1. Add a durable card comment containing context and minimal numbered questions.
2. Call `kanban_block(..., kind="needs_input")`.
3. Stop without handoff or implementation.
4. On resumption, call `kanban_show` and reread the durable thread.

## Live Default configuration

The active Default config is `/home/noam/.hermes/config.yaml`. The relevant verified values are:

```yaml
toolsets:
  - hermes-cli
  - kanban

kanban:
  dispatch_in_gateway: true
  orchestrator_profile: default
  default_assignee: ''
  auto_decompose: false
```

The explicit `kanban` toolset is required for the Default orchestrator gate. After changing toolsets or code, start a new session or restart the gateway; Hermes intentionally does not mutate a live conversation's tool schema because prompt caching is preserved.

After the final gateway restart, a fresh-process smoke check verified:

```text
active profile:                    default
Default orchestrator gate:         true
kanban_prepare_planning visibility: true
```

The system service is:

```text
hermes-gateway.service
```

Use the Telegram `/restart` command for a safe gateway self-restart. A terminal command launched from inside the gateway is intentionally blocked from synchronously restarting its own service.

## Verification performed

### Automated verification

For the Planning bridge and native handoff, the recorded implementation evidence includes:

- Full Kanban-named suite: **1,106 tests passed**.
- Focused bridge/handoff/tool slice: **199 tests passed** in the final review context.
- Ruff: `All checks passed!`.
- Python compilation: passed.
- `git diff --check`: passed.
- Independent native-handoff review: approved with no High or Medium finding.
- Final bridge review found three High issues; all were fixed and followed by green regressions:
  1. Default authority bypass when `HERMES_PROFILE` was unset.
  2. Archived-parent bypass of the done-only Planning dependency rule.
  3. Workspace change between preflight and database mutation.

### Disposable end-to-end rehearsal

Board:

```text
testing-orchestration
```

Board repository:

```text
/mnt/hermes-data/dev-orchestrator/repos/testing-hermes-workflow
```

Disposable card:

```text
t_60c2c904
```

Observed lifecycle:

```text
Triage
→ [Planning], planner, ready
→ Planner run
→ one expected planning clarification block
→ durable clarification comment
→ resumed Planner run
→ [Implementation], instructor, blocked
→ archived
```

Final pre-archive state:

```text
title:       [Implementation] [DISPOSABLE] Plan static rehearsal footer
assignee:    instructor
status:      blocked
step:        implementation
run outcome: handed_off
run error:   none
```

Planning-only commit created by Planner:

```text
7e4055937c0764fc7364f1560dabb1970b901f23
```

The commit contained exactly the required specification and plan Markdown files and no product implementation. Git-blob hashes matched the handoff metadata. Exactly two attachments and one phase-handoff event existed. Repeating the native handoff returned `idempotent: true` and did not create duplicate events or attachments. The implementation branch was not pushed, no pull request was opened, and no Instructor process launched.

After verification:

- The disposable card was archived.
- Its isolated worktree and local disposable branch were removed.
- The historical real card `t_1479e43b` was not used or modified.

## Current repository and publication state

Immediately before this handoff document was committed, the workflow implementation baseline was:

```text
branch:      main
local HEAD:  9bb0402f579450fd02434467580036336d2e785f
origin/main: 9bb0402f579450fd02434467580036336d2e785f
origin:      https://github.com/noambav/hermes-agent.git
upstream:    https://github.com/NousResearch/hermes-agent.git
```

The user fork is the active publication target. Official upstream synchronization is deferred. Do not push these custom workflow changes to official upstream without a separate human decision.

## Important operational lessons

1. `kanban_create --workspace worktree:<path>` may record a declared path before the worktree is materialized. Create and verify the exact clean Git worktree before `kanban_prepare_planning`.
2. A visible `[Planning]` title, Planner assignment, and Ready status are insufficient. The internal workflow template and Planning-step markers must be established through `kanban_prepare_planning`.
3. Only `done` parents satisfy dependencies for official human-gated Planning cards; `archived` parents do not.
4. Tool visibility and direct handler authorization are separate boundaries. Both are enforced.
5. The active-profile resolver is authoritative; do not infer Default from an absent `HERMES_PROFILE` variable.
6. External Git validation must be tied to the database mutation with transaction-time workspace comparison.
7. Artifact bytes must come from the declared commit, not mutable files.
8. Do not infer end-to-end success from a process exit code alone. Re-read the task, runs, events, attachments, workspace, and Git history.
9. Do not use native automated Review while review and merge must remain human-only.
10. Never expose credentials in cards, comments, logs, documentation, or chat.

## Required future work — not authorized yet

### Required capability: Planner Telegram clarification bridge

This is a required part of the intended workflow, not an optional enhancement. It has not yet been implemented or proven. When Planner needs a meaningful human decision:

1. Planner writes the complete question, context, and minimal numbered choices as a durable Kanban comment.
2. Planner sends the same complete question through its configured Telegram gateway, including the board and task identity and a dashboard link when available.
3. Planner blocks the Planning card with `kind="needs_input"` and ends the current run.
4. A Telegram reply is explicitly correlated with that exact board and card; an ordinary reply must never be assumed to identify a task.
5. The human answer is persisted to the Kanban thread before any unblock or resumed dispatch.
6. Planner may evaluate the attached answer and self-unblock only its own Planning-clarification card when the answer is sufficient.
7. A resumed Planner run begins with `kanban_show` and rereads the complete durable thread. If the answer is insufficient, Planner remains blocked and asks a follow-up.

This bridge must never self-unblock an `[Implementation]` or `[Review]` card, authorize Instructor, approve implementation, or approve merge. Those transitions remain human-only.

The bridge is required before the overall workflow is considered complete. Its implementation order relative to Instructor design remains a human sequencing decision; do not silently treat this document as implementation authorization.

### Required Phase 1: design the Instructor contract

The approved design is recorded at:

- `docs/superpowers/specs/2026-07-23-instructor-contract-design.md`

That design is documentation only and does not authorize implementation. Before implementation, the contract requires:

- The exact meaning of human Blocked → Ready authorization.
- Instructor authority and prohibitions.
- How Instructor reads and verifies the immutable planning artifacts.
- Canonical `obra/superpowers` Claude Code installation and invocation.
- Worktree, branch, repository, and plan-hash checks.
- Test-driven implementation and verification requirements.
- Commit, push, and pull-request behavior.
- Failure, interruption, usage-limit, conflict, and retry semantics.
- Idempotency across worker restarts and repeated dispatch.
- The deterministic same-card transition to `[Review]`, blocked.
- A strict prohibition on automatic merge.

Do not implement Instructor while designing this contract. Proceed slowly, one decision at a time, and obtain explicit human approval before implementation.

### Required Phase 2: implement and prove Instructor on a disposable card

Only after Phase 1 approval:

1. Add the minimum required Instructor profile/SOUL/skill wiring.
2. Reuse native Kanban persistence and the existing card/workspace; do not build a second scheduler or workflow engine.
3. Use canonical Claude Code Superpowers workflows.
4. Implement one harmless disposable task in an isolated workspace.
5. Verify tests, commits, branch, push, and PR independently rather than trusting agent prose.
6. Transform the same card to `[Review]`, blocked.
7. Confirm no automatic merge and no premature Done transition.
8. Archive/clean up the disposable proof after evidence is captured.

### Required Phase 3: deterministic CI monitoring

After Instructor proof:

- Poll CI with a script-only/no-agent job to minimize orchestrator model use.
- Report only state changes.
- On CI failure, block and ask the human what to do.
- Do not automatically resume Claude after failed CI.
- Keep implementation credentials separate from any read-only monitor identity where practical.

### Required Phase 4: human review, merge, and completion reconciliation

Design a human-only final gate:

- `[Review]` remains blocked and non-dispatchable.
- Human reviews and merges or closes the PR.
- A deterministic reconciler may verify the exact PR/merge state and mark the same card Done.
- No agent or native automated Review worker merges.

### Later optional work

These are explicitly deferred:

- Optional separate LLM/code-review worker.
- Broader updater or profile-selection hardening.
- Official upstream synchronization.
- Non-urgent cleanup of old restart scripts and diagnostics.

## Fresh-session checklist

A future agent should begin with these read-only checks:

1. Read this document fully.
2. Verify `/home/noam/.hermes/hermes-agent` is the intended repository.
3. Run `git status --short`, `git branch --show-current`, and compare `HEAD` with `origin/main`.
4. Read the current Default, Planner, and later Instructor profile files rather than assuming this document is still current.
5. Verify `/home/noam/.hermes/config.yaml` contains the intended toolsets and Kanban settings without printing credentials.
6. Verify gateway health and active profile.
7. Re-run the focused tests before changing native workflow code.
8. Inspect the named board and exact repository root before any Triage mutation.
9. Ask for explicit authorization at the current human gate.
10. Do not resume the old July 21 standalone-orchestrator plan by inertia.

## Current stopping point

The completed and verified boundary is:

```text
Triage → official Planning → committed planning artifacts
→ same-card blocked Implementation
```

Two required work items remain at the next boundary. The human chooses their implementation order:

```text
Planner Telegram clarification bridge
  → durable Kanban question
  → Telegram notification/reply correlation
  → durable Kanban answer
  → safe Planning-only resumption
```

and:

```text
human implementation approval
→ Instructor implementation/PR
→ same-card blocked Review
```

Nothing beyond design discussion for that next boundary is authorized by this document.
