# Instructor Contract Design

**Status:** Approved design; implementation is not authorized by this document

**Date:** 2026-07-23

**Repository:** `/home/noam/.hermes/hermes-agent`

**Related handoff:** `docs/kanban/human-gated-development-handoff.md`

## 1. Purpose

This document defines the required Instructor behavior for the human-gated, same-card Kanban lifecycle:

```text
Triage
→ [Planning]
→ committed specification and implementation plan
→ [Implementation], blocked
→ human Blocked → Ready authorization
→ Instructor-controlled Claude Code implementation
→ verified branch and pull request
→ [Review], blocked and unassigned
→ human review and merge
→ Done
```

It defines authority, session identity, allowed actions, durable recovery, failure handling, publication, and proof requirements. It does not authorize implementation, profile changes, plugin changes, Claude launch, smoke tests, push, pull-request creation, or deployment.

## 2. Existing system boundary

The workflow already has a proven same-card Planning boundary:

- Default prepares an existing Triage card as official `[Planning]`.
- Planner creates and commits separate specification and implementation-plan artifacts.
- Native `kanban_handoff` transforms the same card into `[Implementation]`, assigned to `instructor`, at workflow step `implementation`, and Blocked.
- Only the human Blocked → Ready action authorizes Instructor execution.

At design time:

- The Instructor profile exists but remains a preliminary controller profile.
- Its gateway is stopped.
- It has no completed Kanban-specific production contract or procedure.
- Claude Code `2.1.206` is installed.
- `superpowers@superpowers-marketplace` version `6.1.1` is installed at local scope but native Claude output reports it disabled.
- No Instructor execution has occurred in the proven Planning rehearsal.

These observations are context, not permission to change the profile or enable the plugin.

## 3. Core invariant

The central execution invariant is:

> One card, one authorized Instructor run, one verified workspace and branch, one Claude session UUID, and no more than one active Claude process.

The same Kanban card, task ID, repository workspace, history, comments, attachments, runs, events, and provenance must survive Planning, implementation, interruption, publication, and Review handoff.

Instructor must fail closed on missing, changed, contradictory, or ambiguous identity and authority.

## 4. Accepted entry state and human authorization

Instructor may act only when every condition below holds:

1. The card is the existing card produced by native Planner handoff.
2. Its title begins with `[Implementation]`.
3. Its workflow template is `human-gated-development-v1`.
4. Its current workflow step is `implementation`.
5. Its assignee is `instructor`.
6. It was deliberately left Blocked by Planning handoff.
7. A human deliberately moved that same card from Blocked to Ready.
8. The specification and plan are attached to the same card and bound to the recorded immutable planning commit.
9. The declared Git repository, worktree, and branch exist and still match the card.
10. No unresolved blocker, cancellation, archive operation, or conflicting run exists.

Planning completion, assignment, a card comment, a Telegram reply, an automated event, or a process retry does not independently authorize implementation.

The human Blocked → Ready action authorizes exactly one logical Claude implementation session for the recorded card, planning artifacts, repository, worktree, and branch.

Before launch, Instructor must reread and verify:

- complete card state and run history;
- all durable clarification comments;
- the committed specification and implementation plan;
- repository instructions;
- planning commit and artifact hashes;
- repository, worktree, branch, ancestry, and cleanliness;
- the human authorization transition;
- absence of another owner or Claude process.

Any failed or ambiguous prerequisite blocks without launching Claude.

## 5. Role and authority separation

### 5.1 Instructor is controller-only

Instructor may:

- validate the authorization and immutable planning inputs;
- create and persist the Claude session identity;
- launch and wait for Claude;
- preserve durable execution state;
- independently inspect Git and rerun approved verification;
- resume the same Claude session only in explicitly authorized cases;
- push the verified branch;
- open and verify one pull request;
- perform the same-card blocked Review handoff.

Instructor may not edit product implementation files, repair Claude's code directly, broaden scope, merge, deploy, or mark the human Review stage complete.

### 5.2 Claude writes product code

Claude Code:

- reads the verified committed specification and plan from the existing worktree;
- implements within approved scope;
- runs plan-required and repository-required checks;
- creates one or more implementation commits above the immutable planning commit.

Instructor never substitutes its own implementation if Claude fails.

## 6. Immutable inputs and workspace

The committed specification and plan are the only approved implementation inputs.

Instructor verifies their exact repository paths, immutable Git blob bytes, planning commit, and recorded hashes. Claude reads those committed files directly. Instructor must not paste mutable copies into the prompt, create alternate plan files, or silently substitute newer worktree content.

The planning commit remains the immutable base. Claude may create a verified series of implementation commits above it when the plan calls for task-level commits. Claude and Instructor may not amend, rebase, reset, squash, replace, or otherwise rewrite the planning commit or pre-existing history.

Instructor and Claude use only the card's declared repository worktree and branch. They may not silently switch repositories, create an alternate worktree, or replace the card.

A changed specification or plan invalidates the original implementation authorization and requires a new human gate.

## 7. Meaning of one bounded Claude session

“Bounded” is structural rather than an arbitrary work-duration limit.

The logical session is bounded by:

- one human authorization;
- one durable Claude session UUID;
- one initial Claude launch;
- no more than one active Claude process;
- one card, repository, worktree, and branch;
- the approved specification, plan, and acceptance criteria;
- pinned model and effort;
- explicit file, command, network, Git, and publication authority;
- defined success, clarification, quota, failure, cancellation, and exhaustion outcomes.

There is no fixed total-hours cap and no initial custom inactivity watchdog. Claude normally finishes or reaches a usage limit. A watchdog may be considered later only if real hangs provide evidence that one is needed.

Instructor launches Claude directly in non-interactive print mode and waits for it to exit. Instructor does not introduce a background-agent system, tmux controller, second scheduler, or bespoke orchestration service.

Minimal launch/capture mechanics may persist the session ID, process identity, exit result, and raw local log. They must not become a parallel workflow engine.

## 8. Claude launch policy

Before launch, Instructor:

1. generates and durably records a valid Claude session UUID;
2. pins and records model and effort for the run;
3. verifies the approved worktree and inputs;
4. acquires exclusive durable run ownership;
5. verifies that no earlier Claude process may still be alive;
6. launches exactly one direct non-interactive Claude process in the approved worktree.

The normal prompt identifies the committed specification and plan and explicitly directs Claude to invoke `executing-plans` before implementation. It does not reproduce the artifacts or authorize broader work.

Any permitted continuation uses the same session UUID through Claude's resume mechanism. `--fork-session`, replacement UUIDs, silent model fallback, and unrelated sessions are prohibited.

If the recorded session cannot be safely resumed, Instructor blocks. Starting a replacement Claude session requires explicit human authorization.

## 9. Canonical Superpowers prerequisite

Canonical Superpowers installation is a one-time bootstrap prerequisite, not a per-card Instructor audit.

Before Instructor is considered ready:

1. Enable the installed canonical `superpowers@superpowers-marketplace` from `obra/superpowers-marketplace`.
2. Verify native marketplace provenance and enabled status once.
3. Run a disposable normal-launch smoke session against a tiny committed plan.
4. Do not explicitly tell Claude to invoke `executing-plans` in that smoke prompt.
5. Verify from native Claude evidence that the session hook automatically activates `executing-plans`.

After this proof, Instructor does not reinstall or repeat a full marketplace provenance check for each card.

Normal production prompts still explicitly direct Claude to invoke `executing-plans` as defense in depth and to prevent future divergence.

Copied skills, adapted mirrors, arbitrary plugin directories, and unverified clones are not acceptable substitutes for the canonical marketplace installation.

## 10. Permission and action policy

Claude uses auto permission mode under an independent Instructor-controlled guard. Claude's permission mode is not the outer security boundary.

### 10.1 Claude may

- read the approved planning artifacts, repository instructions, and relevant repository files;
- edit files inside the declared worktree and within approved scope;
- run commands required by the plan and repository instructions;
- make ordinary implementation choices that preserve approved intent and acceptance criteria;
- use plan-approved network access;
- create implementation commits above the planning commit.

### 10.2 Claude may not

- modify files outside the approved worktree;
- work in another repository, branch, or worktree;
- rewrite planning or pre-existing Git history;
- push, open or modify pull requests, merge, deploy, publish releases, or use GitHub authority;
- use sudo or alter host/system configuration;
- read unrelated secrets or credentials;
- browse arbitrary websites or use Chrome;
- use unapproved MCP servers, plugins, network tools, or APIs;
- install or update Superpowers during implementation;
- launch subagents;
- materially revise the approved specification or plan;
- continue through an unresolved material conflict.

### 10.3 Network and credentials

Network access is denied by default and allowed only when the approved plan or repository instructions require a specific destination/action.

Instructor should not intentionally pass unrelated credentials to Claude. GitHub push and pull-request authority remain with Instructor. This contract does not claim stronger credential isolation than has been demonstrated; it also does not establish a separate environment-isolation smoke-test gate.

If implementation unexpectedly requires new network access or credentials, Claude stops and the card blocks for human input.

## 11. Normal implementation judgment and material deviations

Claude may make ordinary implementation decisions within the approved specification, plan intent, and acceptance criteria.

If Claude discovers missing requirements, contradictory planning, unsafe instructions, infeasibility, or material scope expansion, it stops. Instructor persists the issue to Kanban and Telegram and blocks the same card.

Instructor may not reinterpret the plan, edit planning artifacts, launch another agent, or automatically return the card to Planner. The human decides whether to clarify, authorize same-session continuation, or begin a new Planning cycle.

## 12. Raw logs and token discipline

Claude's raw stdout/stderr is saved unchanged to a task-local log.

Instructor must not:

- read the complete transcript;
- summarize the transcript;
- reason over it as routine model context;
- paste it into Kanban;
- use it to duplicate Claude's work.

Durable execution metadata contains compact orchestration evidence only:

- card, run, and session identity;
- pinned model and effort;
- planning commit and artifact hashes;
- launch and continuation reason;
- process identity and exit category;
- raw log path and checksum;
- deterministic quota/reset fields when available;
- implementation commit range;
- independently observed test, remote, and PR evidence.

The raw log is retained for optional human debugging. It is not automatically attached or published.

## 13. Process identity and exclusive ownership

PID alone is insufficient because operating systems reuse PIDs.

Persist process identity strong enough to distinguish the original Claude process from a reused PID:

- PID;
- process start identity/time from the operating system;
- process group or systemd scope when available;
- Claude session UUID;
- card/run/workspace ownership identity.

Before launch, resume, verification, or publication, durable ownership and live process evidence must agree.

The durable counters are separate and specific:

- same-failure attempts;
- session resume count;
- verification-repair count;
- publication attempts.

The system must not replace these with one ambiguous generic retry counter.

## 14. Durable run states

The contract requires durable representation equivalent to:

- `launch_reserved`;
- `claude_running`;
- `quota_paused`;
- `clarification_blocked`;
- `verification_pending`;
- `repair_resumed`;
- `publication_pending`;
- `review_handed_off`;
- `failed_blocked`.

These are semantic states, not a requirement to add those exact database strings. Implementation should reuse native Kanban task, run, event, blocking, and ownership primitives wherever possible rather than building a second scheduler.

## 15. Restart and crash reconciliation

After any Instructor, gateway, dispatcher, or host restart, Instructor must reconcile before doing anything:

1. Read the durable card/run/session record.
2. Verify card, workspace, branch, planning artifacts, and authorization.
3. Reconcile the recorded strong process identity.
4. Determine whether the previous Claude process may still be alive.
5. Reconcile Git status, commit history, and uncommitted changes.
6. Reconcile quota state and continuation counters.
7. Reconcile remote branch and pull-request state.
8. Continue only through an explicitly permitted same-session path.

Instructor must never launch or resume Claude while the previous process may still be alive.

If ownership, process identity, session recovery, Git state, remote state, or authorization is ambiguous, Instructor blocks and reports instead of guessing.

## 16. Session outcomes

### 16.1 Normal completion

A normal Claude exit or completion claim is not success by itself. Instructor proceeds to independent verification without reading or summarizing Claude's transcript.

### 16.2 Clarification

If Claude requires human input:

1. Preserve the same session UUID and workspace.
2. Capture only Claude's explicit final question as a compact control result; do not ingest or summarize the raw transcript.
3. Persist the complete explicit question to the Kanban card.
4. Send the same question through Instructor's Telegram gateway with board/card identity.
5. Block the `[Implementation]` card with `needs_input`.
6. End the active Claude process safely.
7. Persist the human answer back to Kanban.
8. Require a deliberate human Blocked → Ready action.
9. Resume only the same Claude session after that action.

Instructor clarification never self-unblocks. This differs from the separately designed Planning-only clarification convenience.

### 16.3 Recognized usage limit

A recognized quota pause introduces no new scope or decision. The original human implementation authorization remains valid for same-session recovery.

Instructor persists:

- `quota_paused` state;
- same Claude session UUID;
- strong process-termination evidence;
- reset-time text;
- parsed reset timestamp when unambiguous;
- continuation counters;
- workspace and Git state;
- compact progress report.

It never starts a replacement session.

Before same-session recovery, Instructor proves the prior process ended and atomically reacquires exclusive ownership.

### 16.4 Independent verification failure

Instructor may automatically resume the same Claude session exactly once with the concrete independently observed verification evidence.

The verification-repair counter is incremented separately. A second verification failure blocks for human direction. Instructor does not repair code itself.

### 16.5 Other failures

Unexpected process failure, unknown exit, authentication failure, repository conflict, corrupted state, lost session, policy denial, or ambiguous ownership preserves the workspace and log and blocks.

No automatic continuation is permitted except recognized quota recovery and the single verification-repair continuation.

A replacement Claude session always requires explicit human authorization.

### 16.6 Human cancellation or authorization revocation

A human block, archive, cancellation, or explicit authorization revocation ends Instructor's authority to continue or publish.

If Instructor detects the revocation while Claude is active, it terminates the recorded process safely and preserves the workspace and log. If it detects the revocation after Claude exits, it may reconcile and preserve evidence but must not push, open a pull request, or enter Review.

Continuing after revocation requires a new explicit human authorization and full state reconciliation.

## 17. Quota progress report

On quota pause, Instructor must show how far the ticket progressed without reading Claude's output.

It derives progress only from:

- approved plan tasks/checkpoints;
- commits above the planning commit;
- current Git status;
- changed-file names and diff statistics;
- scoped Git diff inspection;
- independently available test evidence.

The report includes:

- completed plan tasks with evidence;
- likely current/in-progress task when Git evidence supports it;
- remaining plan tasks;
- rough completion percentage;
- confidence level and limitations;
- Claude session identity and reset time when available.

The percentage is an estimate, not acceptance evidence. If Git and plan evidence cannot identify the exact interrupted task, Instructor says so rather than guessing.

The Claude transcript is not used for this report.

## 18. Initial and deferred quota capabilities

Required in the initial Instructor implementation:

- recognize quota exhaustion as distinct from generic failure;
- persist session, pause, reset text/time when available, strong process identity, counters, and workspace state;
- verify the prior process ended;
- produce the plan/Git-only progress report;
- support safe manual same-session resume;
- prohibit replacement sessions and duplicate processes.

May be deferred:

- robust parsing of every Claude reset-time message format;
- unattended scheduling and automatic wake-up at the reset time.

When unattended recovery is later implemented, a recognized quota pause may resume the same session under the original authorization without another human unblock. It must atomically reacquire ownership and revalidate card/workspace/session/process state first.

Deferral of unattended wake-up does not weaken the mandatory safe-pause and same-session recovery contract.

## 19. Independent verification

After Claude exits normally, Instructor independently verifies:

- planning commit remains unchanged and is still an ancestor;
- every implementation commit and aggregate diff stay within approved scope;
- no prohibited path, history rewrite, or unrelated repository change occurred;
- card, repository, worktree, and branch identities still match;
- the card has not been archived, cancelled, reblocked, or changed;
- plan-required and repository-required checks pass when rerun by Instructor;
- the worktree is clean after verification.

Instructor does not invent an unrelated full-suite requirement absent from the plan/repository instructions. Missing or ambiguous required verification blocks before publication rather than being guessed.

Only independently observed passing evidence permits push and pull-request creation.

## 20. Publication authority and idempotency

Claude creates local implementation commits but has no push or GitHub authority.

After successful verification, Instructor:

1. pushes the exact verified task branch;
2. verifies the remote branch points to the expected commit;
3. opens one normal, non-draft pull request against the recorded base branch;
4. verifies repository, base, head, commit range, and pull-request identity;
5. records branch, commit range, PR URL/number, and test evidence.

The pull-request body links the Kanban card, committed specification and plan, commit range, and verification evidence without secrets.

“No duplicate push” means repeated publication attempts must not create conflicting branches, extra commits, duplicate pull requests, or inconsistent local/remote/card state. Repeating a harmless identical Git push is acceptable.

Publication attempts are counted separately. Retries inspect and reuse the exact branch and pull request when they already exist.

Instructor never merges.

## 21. Same-card Review handoff

After verified publication, Instructor atomically transforms the same card into:

- title `[Review] ...`;
- workflow step `review`;
- status Blocked;
- no runnable worker assignee;
- durable commit, branch, pull-request, test, Claude session, and run evidence.

Instructor then exits.

The card enters blocked human Review immediately after verified PR creation. CI may still be pending. A later script-only CI monitor may report state changes, but CI failure remains blocked for human direction.

Do not use Hermes' native runnable Review worker for this workflow. Neither Instructor nor a CI monitor may merge, approve Review, or mark the card Done.

## 22. Idempotent convergence

Repeated launch, resume, verification, publication, or handoff attempts must converge without:

- duplicate Claude processes;
- a replacement Claude session;
- conflicting branches;
- extra commits introduced by publication retry;
- duplicate pull requests;
- duplicate Review transitions;
- a replacement Kanban card;
- lost workspace, attachments, run history, or provenance.

Ambiguous convergence blocks.

## 23. Proof and rollout requirements

The contract is not implemented merely because code or unit tests pass.

### 23.1 One-time Claude bootstrap proof

Using the actual installed Claude CLI:

- enable the canonical plugin;
- verify canonical provenance and enabled status;
- use a tiny committed disposable plan;
- launch through the intended normal Instructor command shape;
- omit any explicit `executing-plans` instruction from the smoke prompt;
- verify that the session hook automatically activates `executing-plans`;
- confirm the smoke cannot push, merge, deploy, or leave the approved workspace.

### 23.2 Deterministic contract tests

Use controlled fixtures or fake processes to prove:

- human authorization validation;
- immutable artifact and workspace identity;
- strong process identity and PID-reuse rejection;
- atomic ownership and concurrent-launch prevention;
- specific continuation counters;
- clarification block and human reauthorization;
- quota pause and reset-time persistence;
- safe same-session recovery;
- one verification-repair continuation;
- restart reconciliation in every durable state;
- publication idempotency;
- atomic same-card Review handoff;
- fail-closed ambiguity and prohibited actions.

Quota and crash behavior should be simulated. Do not deliberately consume the user's Claude quota.

### 23.3 Disposable end-to-end rehearsal

Prove one harmless lifecycle:

```text
Planning handoff
→ blocked Implementation
→ human Blocked → Ready
→ Instructor
→ one Claude session
→ local implementation commits
→ independent verification
→ branch push
→ one normal PR
→ same-card blocked, unassigned Review
```

Independently inspect card state, events, runs, process records, Git history, remote branch, PR, tests, and absence of merge/deployment. Clean up the disposable card, worktree, branch, and PR after evidence is captured.

Only after the disposable proof and a separate human decision may Instructor handle real project work.

## 24. Explicit prohibitions

Neither this document nor approval of it authorizes:

- Instructor implementation work;
- profile/SOUL/skill changes;
- native Kanban code changes;
- Superpowers enablement;
- Claude launch;
- smoke or end-to-end execution;
- repository push or pull-request creation;
- deployment;
- merge;
- marking any implementation complete.

Implementation requires a separate written implementation plan and explicit human authorization.

## 25. Approved contract summary

The approved Instructor contract is:

```text
human-unblocked official [Implementation] card
→ fail-closed identity/artifact/workspace validation
→ one durable Claude session UUID
→ one direct non-interactive Claude process at a time
→ Claude executes the committed plan and creates local commits
→ Instructor does not read Claude's transcript
→ quota pause preserves same session and reports progress from plan/Git evidence
→ Instructor independently reruns required verification
→ at most one same-session verification repair
→ Instructor pushes and opens one normal PR
→ same card becomes blocked, unassigned [Review]
→ human review and merge only
```
