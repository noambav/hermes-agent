# Instructor Contract Disposable Rehearsal

Date: 2026-07-23 UTC

## Tool versions and bootstrap

Commands:

```bash
claude plugin marketplace add obra/superpowers-marketplace
claude plugin install superpowers@superpowers-marketplace --scope local
claude plugin marketplace list
claude plugin list
```

Outcomes:

- Claude Code: `2.1.206`
- Python: `3.11.15`
- Git: `2.47.3`
- Marketplace source: `obra/superpowers-marketplace`
- Plugin: `superpowers@superpowers-marketplace` version `6.1.1`, local scope, enabled
- Copied or adapted Superpowers mirror: not used

## Controller and guard identity

- Instructor controller SHA-256: `eaedad4112cf5ca74cc6bdea9ff322baa2f4b09fd889d2513877da04d6194aeb`
- Guard SHA-256: `a38372d4772f50d9968f20a69a659fa1bb5804066978aa42a1b7a22efff5bbc9`
- Instructor execution skill SHA-256: `3d19ec43bd12dbf9a3d74d2f5633be7bc036a301ee5b22b46f43b7df55b0188e`
- Instructor SOUL SHA-256: `44e8f303b77c80ed3c366614f317548af19d87ea14478d82bf2d54a50e43cb0b`

## Live disposable Claude rehearsal

Command:

```bash
/home/noam/.hermes/hermes-agent/venv/bin/python /home/noam/.hermes/cache/instructor-task7-rehearsal-20260723T181845Z/run_e2e.py
```

Compact outcomes:

- Controller launch count: `1`
- Child return code: `0`
- Same durable Claude UUID from launch through completion: pass
- Final semantic state: `completed`
- `CLAUDE.md` referenced `@bible.md`: pass
- Exact discipline marker present in structured result: pass
- Exact marker absent from Instructor prompt: pass
- Exact marker absent from Claude-visible result schema: pass
- Claude debug evidence recorded `PreToolUse`: pass
- Controller explicitly requested canonical `superpowers:executing-plans`, as accepted by the human: pass
- Guard allowed the exact `Skill` request and denied other skill names/shapes: pass
- Claude `Skill` dispatch returned `superpowers:executing-plans` with `outcome=ok`: pass
- Expected external-guard denial recorded: pass
- Intentionally forbidden operation absent: pass
- Empty MCP configuration and no Agent/Web escape tools: pass
- Exact implementation result: pass
- Disposable worktree clean after local commit: pass

Compact evidence:

- `/home/noam/.hermes/cache/instructor-task7-rehearsal-20260723T181845Z/superpowers-compact.json`
- Raw stdout: `/home/noam/.hermes/cache/instructor-task7-rehearsal-20260723T181845Z/run4/stdout.log`
- Raw stderr: `/home/noam/.hermes/cache/instructor-task7-rehearsal-20260723T181845Z/run4/stderr.log`
- Claude debug log: `/home/noam/.hermes/cache/instructor-task7-rehearsal-20260723T181845Z/claude-superpowers-final-debug.log`

Raw Claude output was retained externally and was not copied or summarized here.

## Fake lifecycle and focused regressions

Commands:

```bash
/home/noam/.hermes/hermes-agent/venv/bin/python -m pytest \
  /home/noam/.hermes/profiles/instructor/skills/instructor-execution/scripts/test_instructor_session.py \
  /home/noam/.hermes/profiles/instructor/skills/instructor-execution/scripts/test_claude_guard.py -q

/home/noam/.hermes/hermes-agent/venv/bin/python -m pytest \
  tests/hermes_cli/test_kanban_phase_handoff.py \
  tests/tools/test_kanban_handoff.py -q
```

Outcomes:

- Controller and guard: `92 passed`
- Kanban handoff boundary: `105 passed`
- Covered with executable tests: quota state, UUID/process reconciliation, PID reuse refusal, stale claim refusal, one-repair cap, PR validation, atomic blocked/unassigned Review handoff, parent gating, and the exact canonical Skill allow boundary.
- Exact Telegram clarification transport and remote push retry were not exercised live. The human narrowed the remaining acceptance proof to actual canonical Superpowers skill invocation; that invocation is recorded above.
- Instructor Telegram live routing: deferred by human; fake transport only, live routing remains unverified. Delivery failure remains fail-closed.

## Full repository regression

Command:

```bash
/home/noam/.hermes/hermes-agent/.worktrees/instructor-contract/scripts/run_tests.sh
```

Outcome: **not green**.

- `2186` files
- `43860` tests passed
- `7` tests failed
- `2` files had no completed test collection/run
- Runtime: `5109.0s`, `8` workers

Full log:

- `/home/noam/.hermes/cache/instructor-task7-rehearsal-20260723T181845Z/full-regression.log`

Exact failed-file rerun:

```bash
scripts/run_tests.sh -j 2 \
  tests/cron/test_sessiondb_init_hang.py \
  tests/test_model_tools_async_bridge.py \
  tests/tools/test_web_tools_tavily.py \
  tests/tools/test_web_providers.py \
  tests/tui_gateway/test_slash_worker_mcp_discovery.py \
  tests/hermes_cli/test_web_server.py \
  tests/run_agent/test_run_agent.py
```

Outcome: `971 passed`, `5 failed`. The cron, web-server, run-agent, and slash-worker files passed on rerun. Five failures remained in three web/vision files, each reporting DNS resolution failure for `example.com`.

Rerun log:

- `/home/noam/.hermes/cache/instructor-task7-rehearsal-20260723T181845Z/failed-files-rerun.log`

Baseline comparison at approved commit `3c43c4eb408d7a3725f19e34ef61b1db891f12ac`:

```bash
scripts/run_tests.sh -j 2 \
  tests/test_model_tools_async_bridge.py \
  tests/tools/test_web_tools_tavily.py \
  tests/tools/test_web_providers.py
```

Outcome: the exact same five tests failed at the untouched approved base commit; `40` tests passed and `5` failed. The failure fingerprint was unchanged: DNS resolution failure for `example.com`. These are baseline/environment failures, not regressions introduced by the Instructor contract branch. No unrelated fix was attempted.

## Safety outcome

- Real Kanban card mutation: none
- Real push or PR: none
- Merge or deployment: none
- Review completion: none
- Credential values read or persisted: none
