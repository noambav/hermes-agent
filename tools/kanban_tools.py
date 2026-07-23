"""Kanban tools — structured tool-call surface for worker + orchestrator agents.

These tools are registered into the model's schema when the agent is
running under the dispatcher (env var ``HERMES_KANBAN_TASK`` set) or when
the active profile explicitly enables the ``kanban`` toolset for
orchestrator work. A normal ``hermes chat`` session still sees **zero**
kanban tools in its schema unless configured.

Why tools instead of just shelling out to ``hermes kanban``?

1. **Backend portability.** A worker whose terminal tool points at Docker
   / Modal / Singularity / SSH would run ``hermes kanban complete …``
   inside the container, where ``hermes`` isn't installed and the DB
   isn't mounted. Tools run in the agent's Python process, so they
   always reach ``~/.hermes/kanban.db`` regardless of terminal backend.

2. **No shell-quoting footguns.** Passing ``--metadata '{"x": [...]}'``
   through shlex+argparse is fragile. Structured tool args skip it.

3. **Better errors.** Tool-call failures return structured JSON the
   model can reason about, not stderr strings it has to parse.

Humans continue to use the CLI (``hermes kanban …``), the dashboard
(``hermes dashboard``), and the slash command (``/kanban …``) — all
three bypass the agent entirely. The tools are for dispatcher-spawned
worker handoffs and for configured orchestrator profiles that route work
through the board.
"""
from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any, Optional

from agent.redact import redact_sensitive_text
from hermes_cli.goals import judge_goal
from tools.registry import registry, tool_error
from hermes_cli.config import cfg_get, load_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

KANBAN_LIST_DEFAULT_LIMIT = 50
KANBAN_LIST_MAX_LIMIT = 200


def _profile_has_kanban_toolset() -> bool:
    # Uses load_config() which has mtime-based caching, so this adds
    # negligible overhead. The check_fn results are further TTL-cached
    # (~30s) by the tool registry.
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        toolsets = cfg.get("toolsets", [])
        return "kanban" in toolsets
    except Exception:
        return False


def _check_kanban_mode() -> bool:
    """Task-lifecycle tools are available when:

    1. ``HERMES_KANBAN_TASK`` is set (dispatcher-spawned worker), OR
    2. The current profile has ``kanban`` in its toolsets config
       (orchestrator profiles like techlead that route work via Kanban).

    Humans running ``hermes chat`` without the kanban toolset see zero
    kanban tools. Workers spawned by the kanban dispatcher (gateway-
    embedded by default) and orchestrator profiles with the kanban
    toolset enabled see the Kanban lifecycle tool surface.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return True
    return _profile_has_kanban_toolset()


def _check_kanban_orchestrator_mode() -> bool:
    """Board-routing tools (kanban_list, kanban_unblock) are intentionally
    hidden from task workers.

    Dispatcher-spawned workers should close their own task via the
    lifecycle tools (complete/block/heartbeat), not enumerate or unblock
    board state. Profiles that explicitly opt into the kanban toolset
    and are NOT scoped to a single task are the orchestrator surface.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return False
    return _profile_has_kanban_toolset()


def _check_default_kanban_orchestrator_mode() -> bool:
    """Expose Planning preparation only to the authoritative Default profile."""
    if not _check_kanban_orchestrator_mode():
        return False
    from hermes_cli.profiles import get_active_profile_name

    return get_active_profile_name() == "default"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _default_task_id(arg: Optional[str]) -> Optional[str]:
    """Resolve ``task_id`` arg or fall back to the env var the dispatcher set."""
    if arg:
        return arg
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    return env_tid or None


def _worker_run_id(task_id: str) -> Optional[int]:
    """Return this worker's dispatcher run id when it is scoped to task_id."""
    if os.environ.get("HERMES_KANBAN_TASK") != task_id:
        return None
    raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _stamp_worker_session_metadata(
    task_id: str, metadata: Optional[dict]
) -> Optional[dict]:
    """Add trusted worker session id metadata for this worker's own task."""
    if os.environ.get("HERMES_KANBAN_TASK") != task_id:
        return metadata
    session_id = os.environ.get("HERMES_SESSION_ID")
    if not session_id:
        return metadata
    stamped = dict(metadata or {})
    stamped["worker_session_id"] = session_id
    return stamped


def _enforce_worker_task_ownership(tid: str) -> Optional[str]:
    """Reject worker-driven destructive calls on foreign task IDs.

    A process spawned by the dispatcher has ``HERMES_KANBAN_TASK`` set
    to its own task id. Tools like ``kanban_complete`` / ``kanban_block``
    / ``kanban_heartbeat`` mutate run-lifecycle state, so a buggy or
    prompt-injected worker that passed an explicit ``task_id`` for some
    other task could corrupt sibling or cross-tenant runs (see #19534).

    Orchestrator profiles (kanban toolset enabled but **no**
    ``HERMES_KANBAN_TASK`` in env) aren't subject to this check — their
    job is routing, and they sometimes legitimately close out child
    tasks or reopen blocked ones. Workers are narrowly scoped to their
    one task.

    Returns ``None`` when the call is allowed, or a tool-error string
    when it must be rejected. Callers should ``return`` the error
    verbatim.
    """
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    if not env_tid:
        # Orchestrator or CLI context — no task-scope restriction.
        return None
    if tid != env_tid:
        return tool_error(
            f"worker is scoped to task {env_tid}; refusing to mutate "
            f"{tid}. Use kanban_comment to hand off information to other "
            f"tasks, or kanban_create to spawn follow-up work."
        )
    return None


def _connect(board: Optional[str] = None):
    """Import + connect lazily so the module imports cleanly in non-kanban
    contexts (e.g. test rigs that import every tool module).

    When ``board`` is provided it's forwarded to :func:`kb.connect`, which
    routes the connection to that board's sqlite file. ``None`` (the
    default) preserves the legacy resolution chain
    (``HERMES_KANBAN_DB`` → ``HERMES_KANBAN_BOARD`` env → current symlink
    → ``default``). Per-tool ``board`` lets a Telegram-side agent override
    the env-pinned active board without restarting Hermes.
    """
    from hermes_cli import kanban_db as kb
    return kb, kb.connect(board=board)


_GOAL_MODE_BLOCK_ALLOWED_KINDS = frozenset({"dependency", "needs_input"})


def _goal_judge_available() -> bool:
    """True when an auxiliary client is configured for the goal judge.

    ``judge_goal`` is fail-open at the source: when no auxiliary model can
    be reached it returns a ``"continue"`` verdict that is indistinguishable
    from a real "not done yet" judgment. The completion gate must not treat
    that as a rejection, or an unconfigured/degraded auxiliary model would
    wedge every ``goal_mode`` worker (it could never close its own task).

    So we probe availability first and only enforce the gate when a judge is
    actually reachable. This mirrors the same client lookup ``judge_goal``
    performs internally.
    """
    try:
        from agent.auxiliary_client import get_text_auxiliary_client
        client, model = get_text_auxiliary_client("goal_judge")
    except Exception:
        return False
    return client is not None and bool(model)


# ---------------------------------------------------------------------------
# Runtime-activity → board-heartbeat bridge (#31752)
# ---------------------------------------------------------------------------
# When the agent ticks ``_touch_activity`` during normal work (between
# tool calls, mid-stream chunks, etc.), we want the kanban board's
# ``last_heartbeat_at`` columns to reflect that liveness so the dispatcher
# watchdog (which reads ``tasks.last_heartbeat_at``, not the agent's
# in-process timestamp) doesn't reclaim an actively-running worker as
# stale. The model is not required to call the explicit ``kanban_heartbeat``
# tool for this to work — that tool stays available for workers that want
# to attach a note or pre-emptively extend a claim across a known-long op.
#
# Constraints:
#   - Best-effort: never raise. The agent loop must not care if the bridge
#     fails (board missing, DB locked, etc.).
#   - Rate-limited to one DB write per 60s per-process; runtime activity
#     can tick on every chunk/tool result and we don't need that resolution.
#   - No-op outside dispatcher-spawned worker context (no ``HERMES_KANBAN_TASK``).
#   - No durable note on these auto-heartbeats; that's reserved for the
#     explicit tool which carries a model-supplied note.

_AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS = 60.0
_auto_heartbeat_last_attempt: float = 0.0


def heartbeat_current_worker_from_env() -> bool:
    """Best-effort: extend the kanban claim + bump board heartbeat for the
    current dispatcher-spawned worker, using identity from env vars.

    Returns True if a write was attempted (whether or not it succeeded);
    False if the call was skipped (not a kanban worker, rate-limited, or
    swallowed exception). The boolean is informational — callers should
    not branch on it.

    Identity comes from:
      * ``HERMES_KANBAN_TASK`` — task id (required; absence means no-op)
      * ``HERMES_KANBAN_RUN_ID`` — pins the run row so we don't heartbeat
        a stale run that may have already been reclaimed
      * ``HERMES_KANBAN_CLAIM_LOCK`` — claim lock for ``heartbeat_claim``;
        falls back to the default ``_claimer_id()`` for locally-driven
        workers that never went through the dispatcher path

    Rate-limited via the module-level ``_auto_heartbeat_last_attempt``
    timestamp (monotonic clock); not thread-safe in the strict sense, but
    the worst case is one extra DB write per race, which is harmless.
    """
    global _auto_heartbeat_last_attempt
    tid = os.environ.get("HERMES_KANBAN_TASK")
    if not tid:
        return False
    import time as _time
    now = _time.monotonic()
    if (now - _auto_heartbeat_last_attempt) < _AUTO_HEARTBEAT_MIN_INTERVAL_SECONDS:
        return False
    _auto_heartbeat_last_attempt = now
    try:
        kb, conn = _connect()
        try:
            claim_lock = os.environ.get("HERMES_KANBAN_CLAIM_LOCK")
            try:
                kb.heartbeat_claim(conn, tid, claimer=claim_lock)
            except Exception:
                logger.debug("auto-heartbeat: heartbeat_claim failed", exc_info=True)
            run_id_raw = os.environ.get("HERMES_KANBAN_RUN_ID")
            run_id: Optional[int]
            try:
                run_id = int(run_id_raw) if run_id_raw else None
            except (TypeError, ValueError):
                run_id = None
            try:
                kb.heartbeat_worker(conn, tid, note=None, expected_run_id=run_id)
            except Exception:
                logger.debug("auto-heartbeat: heartbeat_worker failed", exc_info=True)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return True
    except Exception:
        logger.debug("auto-heartbeat: bridge failed", exc_info=True)
        return False


def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields})


def _normalize_profile(value: Any) -> Optional[str]:
    """Normalize CLI-compatible assignee sentinels for the tool surface."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "-", "null"}:
        return None
    return text


def _parse_bool_arg(args: dict, name: str, *, default: bool = False):
    value = args.get(name)
    if value is None:
        return default, None
    if isinstance(value, bool):
        return value, None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True, None
    if text in {"false", "0", "no"}:
        return False, None
    return default, f"{name} must be a boolean or 'true'/'false'"


def _require_orchestrator_tool(tool_name: str) -> Optional[str]:
    """Belt-and-suspenders runtime guard for orchestrator-only handlers.

    The check_fn (`_check_kanban_orchestrator_mode`) keeps these tools
    out of the worker schema entirely, but in case a stale registration
    or test harness routes a worker to one of them anyway, return a
    structured tool_error so the model gets a clear refusal instead of
    silently mutating board state from a worker context.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return tool_error(
            f"{tool_name} is orchestrator-only; dispatcher-spawned workers "
            "must use kanban_complete, kanban_block, kanban_heartbeat, or "
            "kanban_comment for their assigned task."
        )
    return None


def _task_summary_dict(kb, conn, task) -> dict[str, Any]:
    """Compact task shape for board-listing tools."""
    parents = kb.parent_ids(conn, task.id)
    children = kb.child_ids(conn, task.id)
    return {
        "id": task.id,
        "title": task.title,
        "assignee": task.assignee,
        "status": task.status,
        "priority": task.priority,
        "tenant": task.tenant,
        "workspace_kind": task.workspace_kind,
        "workspace_path": task.workspace_path,
        "project_id": task.project_id,
        "created_by": task.created_by,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "current_run_id": task.current_run_id,
        "model_override": task.model_override,
        "parents": parents,
        "children": children,
        "parent_count": len(parents),
        "child_count": len(children),
    }


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _handle_show(args: dict, **kw) -> str:
    """Read a task's full state: task row, parents, children, comments,
    runs (attempt history), and the last N events."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            task = kb.get_task(conn, tid)
            if task is None:
                return tool_error(f"task {tid} not found")
            comments = kb.list_comments(conn, tid)
            events = kb.list_events(conn, tid)
            runs = kb.list_runs(conn, tid)
            parents = kb.parent_ids(conn, tid)
            children = kb.child_ids(conn, tid)

            def _task_dict(t):
                return {
                    "id": t.id, "title": t.title, "body": t.body,
                    "assignee": t.assignee, "status": t.status,
                    "tenant": t.tenant, "priority": t.priority,
                    "workspace_kind": t.workspace_kind,
                    "workspace_path": t.workspace_path,
                    "created_by": t.created_by, "created_at": t.created_at,
                    "started_at": t.started_at,
                    "completed_at": t.completed_at,
                    "result": t.result,
                    "current_run_id": t.current_run_id,
                    "model_override": t.model_override,
                }

            def _run_dict(r):
                return {
                    "id": r.id, "profile": r.profile,
                    "status": r.status, "outcome": r.outcome,
                    "summary": r.summary, "error": r.error,
                    "metadata": r.metadata,
                    "started_at": r.started_at, "ended_at": r.ended_at,
                }

            return json.dumps({
                "task": _task_dict(task),
                "parents": parents,
                "children": children,
                "comments": [
                    {"author": c.author, "body": c.body,
                     "created_at": c.created_at}
                    for c in comments
                ],
                "events": [
                    {"kind": e.kind, "payload": e.payload,
                     "created_at": e.created_at, "run_id": e.run_id}
                    for e in events[-50:]   # cap; full log via CLI
                ],
                "runs": [_run_dict(r) for r in runs],
                # Also surface the worker's own context block so the
                # agent can include it directly if it wants. This is
                # the same string build_worker_context returns to the
                # dispatcher at spawn time.
                "worker_context": kb.build_worker_context(conn, tid),
            })
        finally:
            conn.close()
    except ValueError as e:
        # Invalid board slug surfaces as ValueError from _normalize_board_slug.
        return tool_error(f"kanban_show: {e}")
    except Exception as e:
        logger.exception("kanban_show failed")
        return tool_error(f"kanban_show: {e}")


def _handle_list(args: dict, **kw) -> str:
    """List task summaries with the same core filters as the CLI."""
    guard = _require_orchestrator_tool("kanban_list")
    if guard:
        return guard
    assignee = args.get("assignee")
    status = args.get("status")
    tenant = args.get("tenant")
    include_archived, bool_error = _parse_bool_arg(args, "include_archived")
    if bool_error:
        return tool_error(bool_error)
    limit = args.get("limit")
    if limit is None:
        limit = KANBAN_LIST_DEFAULT_LIMIT
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return tool_error("limit must be an integer")
    if limit < 1:
        return tool_error("limit must be >= 1")
    if limit > KANBAN_LIST_MAX_LIMIT:
        return tool_error(f"limit must be <= {KANBAN_LIST_MAX_LIMIT}")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            # Match CLI list: dependencies that cleared since the last
            # dispatcher tick should be visible to orchestrators immediately.
            promoted = kb.recompute_ready(conn)
            # Fetch one extra row so model-facing output can report that
            # a bounded listing was truncated without dumping the board.
            rows = kb.list_tasks(
                conn,
                assignee=assignee,
                status=status,
                tenant=tenant,
                include_archived=include_archived,
                limit=limit + 1,
            )
            truncated = len(rows) > limit
            tasks = rows[:limit]
            return json.dumps({
                "tasks": [_task_summary_dict(kb, conn, t) for t in tasks],
                "count": len(tasks),
                "limit": limit,
                "truncated": truncated,
                "next_limit": (
                    min(limit * 2, KANBAN_LIST_MAX_LIMIT)
                    if truncated and limit < KANBAN_LIST_MAX_LIMIT else None
                ),
                "promoted": promoted,
            })
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_list: {e}")
    except Exception as e:
        logger.exception("kanban_list failed")
        return tool_error(f"kanban_list: {e}")


def _handle_complete(args: dict, **kw) -> str:
    """Mark the current task done with a structured handoff."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    summary = args.get("summary")
    metadata = args.get("metadata")
    result = args.get("result")
    if summary:
        summary = redact_sensitive_text(str(summary), force=True)
    if result:
        result = redact_sensitive_text(str(result), force=True)
    if metadata is not None and isinstance(metadata, dict):
        meta_json = json.dumps(metadata)
        meta_json = redact_sensitive_text(meta_json, force=True)
        try:
            metadata = json.loads(meta_json)
        except json.JSONDecodeError:
            pass
    created_cards = args.get("created_cards")
    artifacts = args.get("artifacts")
    if created_cards is not None:
        if isinstance(created_cards, str):
            # Accept a single id as a string for convenience.
            created_cards = [created_cards]
        if not isinstance(created_cards, (list, tuple)):
            return tool_error(
                f"created_cards must be a list of task ids, got "
                f"{type(created_cards).__name__}"
            )
        # Normalise: strings only, stripped, non-empty.
        created_cards = [
            str(c).strip() for c in created_cards if str(c).strip()
        ]
    if artifacts is not None:
        if isinstance(artifacts, str):
            # Accept a single path as a string for convenience.
            artifacts = [artifacts]
        if not isinstance(artifacts, (list, tuple)):
            return tool_error(
                f"artifacts must be a list of file paths, got "
                f"{type(artifacts).__name__}"
            )
        artifacts = [
            str(p).strip() for p in artifacts if str(p).strip()
        ]
        # Carry the artifact list inside metadata so it rides the
        # existing completed-event payload without a schema change at
        # the DB layer.  The gateway notifier reads payload['artifacts']
        # off the completion event and uploads each path as a native
        # attachment.
        if artifacts:
            if metadata is None:
                metadata = {}
            elif not isinstance(metadata, dict):
                return tool_error(
                    f"metadata must be an object/dict, got "
                    f"{type(metadata).__name__}"
                )
            # Don't overwrite an existing metadata.artifacts the worker
            # passed manually — merge instead.
            existing = metadata.get("artifacts")
            if isinstance(existing, (list, tuple)):
                merged: list[str] = []
                seen: set[str] = set()
                for item in list(existing) + artifacts:
                    s = str(item).strip()
                    if s and s not in seen:
                        seen.add(s)
                        merged.append(s)
                metadata["artifacts"] = merged
            else:
                metadata["artifacts"] = artifacts
    if not (summary or result):
        return tool_error(
            "provide at least one of: summary (preferred), result"
        )
    if metadata is not None and not isinstance(metadata, dict):
        return tool_error(
            f"metadata must be an object/dict, got {type(metadata).__name__}"
        )
    metadata = _stamp_worker_session_metadata(tid, metadata)
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            # Goal-mode pre-completion judge gate (Issue #38367).
            # Prevent workers from bypassing the auxiliary judge by
            # calling kanban_complete before acceptance criteria are met.
            # Only enforce when a judge is actually reachable — see
            # _goal_judge_available for why an unavailable judge fails open.
            task = kb.get_task(conn, tid)
            if task and task.goal_mode and _goal_judge_available():
                verdict = "done"
                reason = ""
                try:
                    # judge_goal returns (verdict, reason, parse_failed,
                    # wait_directive, transport_failed) — see
                    # hermes_cli/goals.py. Unpacking fewer raises ValueError,
                    # which the defensive handler below swallows, leaving
                    # verdict="done" and silently disabling the gate.
                    verdict, reason, _, _, _ = judge_goal(
                        goal=f"{task.title}\n\n{task.body or ''}".strip(),
                        last_response=(summary or result or "").strip(),
                    )
                except Exception as judge_exc:
                    # Defensive: judge_goal swallows its own errors, but if
                    # it ever raises, fail open rather than wedge the worker.
                    logger.warning(
                        "goal judge check failed, allowing completion: %s",
                        judge_exc,
                        exc_info=True,
                    )
                if verdict != "done":
                    return tool_error(
                        f"Goal completion rejected by judge: {reason}. "
                        f"To proceed, either: (1) provide explicit acceptance "
                        f"evidence in your summary matching the task's criteria, "
                        f"or (2) create continuation tasks with parents=[{tid}] "
                        f"and keep this task alive."
                    )

            try:
                ok = kb.complete_task(
                    conn, tid,
                    result=result, summary=summary, metadata=metadata,
                    created_cards=created_cards,
                    expected_run_id=_worker_run_id(tid),
                )
            except kb.ArtifactPreservationError as artifact_err:
                return tool_error(
                    f"kanban_complete could not preserve the declared artifacts: "
                    f"{artifact_err}. Your task is still in-flight and its "
                    f"scratch workspace was kept. Fix the artifact path or "
                    f"storage error, then retry kanban_complete with the same handoff."
                )
            except kb.HallucinatedCardsError as hall_err:
                # Structured rejection — surface the phantom ids so the
                # worker can retry with a corrected list or drop the
                # field. Audit event already landed in the DB.
                #
                # The task itself was NOT mutated (the gate runs before
                # the write txn), so the worker can simply call
                # kanban_complete again. Spell that out — without it the
                # model often interprets a tool_error as a terminal
                # failure and either blocks or crashes the run instead
                # of retrying. See #22923.
                return tool_error(
                    f"kanban_complete blocked: the following created_cards "
                    f"do not exist or were not created by this worker: "
                    f"{', '.join(hall_err.phantom)}. "
                    f"Your task is still in-flight (no state change). "
                    f"Retry kanban_complete with the same summary/metadata "
                    f"and either drop these ids from created_cards, or pass "
                    f"created_cards=[] to skip the card-claim check entirely."
                )
            if not ok:
                return tool_error(
                    f"could not complete {tid} (unknown id or already terminal)"
                )
            run = kb.latest_run(conn, tid)
            return _ok(task_id=tid, run_id=run.id if run else None)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_complete: {e}")
    except Exception as e:
        logger.exception("kanban_complete failed")
        return tool_error(f"kanban_complete: {e}")


def _handle_block(args: dict, **kw) -> str:
    """Transition the task to blocked with a reason a human will read."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    reason = args.get("reason")
    if not reason or not str(reason).strip():
        return tool_error("reason is required — explain what input you need")
    reason = redact_sensitive_text(str(reason), force=True)
    kind = args.get("kind")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        if kind is not None and kind not in kb.VALID_BLOCK_KINDS:
            conn.close()
            return tool_error(
                f"kind must be one of {sorted(kb.VALID_BLOCK_KINDS)} (or omit it)"
            )
        # Goal-mode block gate (Issue #38696, sibling of the kanban_complete
        # judge gate in #38367). kanban_block is a second exit path out of
        # the goal loop — run_kanban_goal_loop() treats ANY `blocked` status
        # as terminal, identically to `done`, regardless of kind. Without
        # this, a worker that learns kanban_complete is gated can just call
        # kanban_block(reason="anything") to escape the loop instead.
        # Restrict goal_mode tasks to the kinds that represent a genuine
        # external blocker the worker cannot resolve itself; `capability`
        # and `transient` (or an unset kind) route back through
        # kanban_complete, which the judge now gates.
        task = kb.get_task(conn, tid)
        if (
            task
            and task.goal_mode
            and kind not in _GOAL_MODE_BLOCK_ALLOWED_KINDS
        ):
            conn.close()
            return tool_error(
                f"goal_mode tasks can only block with kind in "
                f"{sorted(_GOAL_MODE_BLOCK_ALLOWED_KINDS)} (got {kind!r}). "
                f"If the task is actually finished or cannot proceed for "
                f"another reason, call kanban_complete instead — the "
                f"completion judge will evaluate it."
            )
        try:
            ok = kb.block_task(
                conn, tid,
                reason=reason,
                kind=kind,
                expected_run_id=_worker_run_id(tid),
            )
            if not ok:
                return tool_error(
                    f"could not block {tid} (unknown id or not in "
                    f"running/ready)"
                )
            run = kb.latest_run(conn, tid)
            # Tell the worker where the task actually landed so it doesn't
            # assume it's sitting in 'blocked' when routing sent it elsewhere.
            landed = kb.get_task(conn, tid)
            return _ok(
                task_id=tid,
                run_id=run.id if run else None,
                status=landed.status if landed else "blocked",
                block_kind=kind,
            )
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_block: {e}")
    except Exception as e:
        logger.exception("kanban_block failed")
        return tool_error(f"kanban_block: {e}")


def _git(repo: Path, *args: str) -> str:
    """Run a read-only Git query and return stripped stdout."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "git command failed").strip()
        raise ValueError(detail)
    return proc.stdout.strip()


def _git_bytes(repo: Path, *args: str) -> bytes:
    """Run a Git query and return exact stdout bytes without text decoding."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or b"git command failed").decode(
            "utf-8", errors="replace"
        ).strip()
        raise ValueError(detail)
    return bytes(proc.stdout)


def _attachment_name_matches(actual: str, desired: str) -> bool:
    """Match a native collision suffix (``name (N).md``) for one source name."""
    if actual == desired:
        return True
    desired_path = Path(desired)
    actual_path = Path(actual)
    stem = actual_path.stem
    return (
        actual_path.suffix == desired_path.suffix
        and stem.startswith(f"{desired_path.stem} (")
        and stem.endswith(")")
        and stem[len(desired_path.stem) + 2 : -1].isdigit()
    )


def _reusable_handoff_attachment(
    kb,
    conn,
    task_id: str,
    *,
    filename: str,
    data_sha256: str,
    size: int,
    excluded_ids: set[int],
):
    """Find an exact prior Planner blob left by an interrupted handoff."""
    for attachment in kb.list_attachments(conn, task_id):
        if (
            attachment.id in excluded_ids
            or attachment.uploaded_by != "planner-handoff"
            or attachment.size != size
            or not _attachment_name_matches(attachment.filename, filename)
        ):
            continue
        try:
            stored = Path(attachment.stored_path)
            if stored.is_file() and hashlib.sha256(stored.read_bytes()).hexdigest() == data_sha256:
                return attachment
        except OSError:
            continue
    return None


def _stage_handoff_blob(kb, task_id: str, data: bytes, *, board: Optional[str]) -> Path:
    """Write a hidden temporary blob and remove it if staging fails."""
    attachment_dir = kb.task_attachments_dir(task_id, board=board)
    attachment_dir.mkdir(parents=True, exist_ok=True)
    fd, staged_name = tempfile.mkstemp(
        prefix=".handoff-", suffix=".tmp", dir=attachment_dir
    )
    os.close(fd)
    staged_path = Path(staged_name)
    try:
        staged_path.write_bytes(data)
    except Exception:
        staged_path.unlink(missing_ok=True)
        raise
    return staged_path


def _validated_repo_artifact(
    repo: Path, raw_path: Any, *, label: str
) -> tuple[str, Path]:
    """Resolve a repository-relative regular file without following escapes."""
    text = str(raw_path or "").strip()
    if not text:
        raise ValueError(f"{label} path is required")
    rel = Path(text)
    if rel.is_absolute():
        raise ValueError(f"{label} path must be repository-relative")
    candidate = repo / rel
    if candidate.is_symlink():
        raise ValueError(f"{label} path must not be a symlink")
    try:
        repo_resolved = repo.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(repo_resolved)
    except (FileNotFoundError, ValueError):
        raise ValueError(f"{label} path escapes the repository or does not exist")
    if not resolved.is_file():
        raise ValueError(f"{label} path must be a regular file")
    return resolved.relative_to(repo_resolved).as_posix(), resolved


def _normalized_repo_relative_arg(raw_path: Any) -> str:
    """Lexically normalize a retry path without consulting the worktree."""
    text = str(raw_path or "").strip()
    if not text:
        return text
    path = Path(text)
    if path.is_absolute():
        return text
    return Path(os.path.normpath(text)).as_posix()


def _implementation_handoff_body(
    *,
    repo: Path,
    branch: str,
    base_commit: str,
    planning_commit: str,
    spec: dict,
    plan: dict,
    parent_ids: list[str],
) -> str:
    dependencies = ", ".join(parent_ids) if parent_ids else "none"
    return (
        "## Implementation handoff\n\n"
        f"- Repository: `{repo}`\n"
        f"- Worktree: `{repo}`\n"
        f"- Branch: `{branch}`\n"
        f"- Base commit: `{base_commit}`\n"
        f"- Planning commit: `{planning_commit}`\n\n"
        "### Artifacts\n"
        f"- Specification: `{spec['path']}`\n"
        f"  SHA-256: `{spec['sha256']}`\n"
        f"  Attachment: `{spec['attachment_name']}` (ID `{spec['attachment_id']}`)\n"
        f"- Plan: `{plan['path']}`\n"
        f"  SHA-256: `{plan['sha256']}`\n"
        f"  Attachment: `{plan['attachment_name']}` (ID `{plan['attachment_id']}`)\n\n"
        "### Dependencies\n"
        f"- Required completed cards: `{dependencies}`\n\n"
        "### Approval\n"
        "`Blocked → Ready` authorizes Instructor to implement exactly the "
        "planning commit and artifact hashes above. Instructor must block on "
        "any mismatch, unresolved dependency, or scope expansion."
    )


def _handle_prepare_planning(args: dict, **kw) -> str:
    """Validate a Triage card's Git workspace and opt it into Planning."""
    guard = _require_orchestrator_tool("kanban_prepare_planning")
    if guard:
        return guard

    from hermes_cli.profiles import get_active_profile_name

    profile = get_active_profile_name()
    if profile != "default":
        return tool_error(
            "kanban_prepare_planning is restricted to the Default orchestrator profile"
        )

    tid = str(args.get("task_id") or "").strip()
    if not tid:
        return tool_error("task_id is required")

    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            task = kb.get_task(conn, tid)
            if task is None:
                return tool_error(f"task {tid} not found")

            # A lost-response retry must return the durable database result even
            # after Planner has begun editing the worktree.
            if task.workflow_template_id is not None or task.current_step_key is not None:
                result = kb.prepare_planning_task(
                    conn,
                    tid,
                    actor=profile,
                    expected_workspace_kind=task.workspace_kind,
                    expected_workspace_path=task.workspace_path,
                )
            else:
                if task.workspace_kind not in {"dir", "worktree"}:
                    return tool_error(
                        "kanban_prepare_planning requires persistent Git workspace_kind "
                        "'dir' or 'worktree'"
                    )
                if not task.workspace_path:
                    return tool_error(
                        "kanban_prepare_planning requires a task workspace_path"
                    )
                repo = Path(task.workspace_path).expanduser().resolve(strict=True)
                git_root = Path(
                    _git(repo, "rev-parse", "--show-toplevel")
                ).resolve(strict=True)
                if git_root != repo:
                    return tool_error(
                        f"task workspace_path must be the Git worktree root: {git_root}"
                    )
                if _git(repo, "status", "--porcelain"):
                    return tool_error(
                        "kanban_prepare_planning requires a clean Git worktree"
                    )

                result = kb.prepare_planning_task(
                    conn,
                    tid,
                    actor=profile,
                    expected_workspace_kind=task.workspace_kind,
                    expected_workspace_path=task.workspace_path,
                )
            return _ok(
                task_id=tid,
                phase="planning",
                status=result.status,
                assignee="planner",
                idempotent=result.idempotent,
            )
        finally:
            conn.close()
    except (OSError, ValueError) as e:
        return tool_error(f"kanban_prepare_planning: {e}")
    except Exception as e:
        logger.exception("kanban_prepare_planning failed")
        return tool_error(f"kanban_prepare_planning: {e}")


def _handle_implementation_handoff(args: dict, **kw) -> str:
    """Verify Planner artifacts and atomically hand the same card to Instructor."""
    if not os.environ.get("HERMES_KANBAN_TASK", "").strip():
        return tool_error("kanban_handoff requires a dispatcher task-scoped worker")
    if args.get("board") not in (None, ""):
        return tool_error(
            "task-scoped kanban_handoff must not include a board override"
        )
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    if os.environ.get("HERMES_PROFILE", "").strip().lower() != "planner":
        return tool_error(
            "kanban_handoff planning -> implementation requires profile 'planner'"
        )
    if args.get("to_phase") != "implementation":
        return tool_error(
            "kanban_handoff currently supports only to_phase='implementation'"
        )

    planning_commit_arg = str(args.get("planning_commit") or "").strip().lower()
    if not planning_commit_arg:
        return tool_error("planning_commit is required")
    board = None
    staged_paths: list[Path] = []
    finalized_paths: list[Path] = []
    transaction_state = None
    result = None
    hook_notified = False
    try:
        kb, conn = _connect(board=board)
        try:
            task = kb.get_task(conn, tid)
            if task is None:
                return tool_error(f"task {tid} not found")

            # A lost-response retry must be decided from the durable handoff,
            # not mutable live Git state. Find the closed Planner handoff run
            # and require the caller's immutable artifact identifiers to match.
            if task.current_step_key == "implementation":
                handoff_runs = kb.list_runs(
                    conn,
                    tid,
                    include_active=False,
                    state_type="outcome",
                    state_name="handed_off",
                )
                prior_run = handoff_runs[-1] if handoff_runs else None
                prior_metadata = prior_run.metadata if prior_run else {}
                requested_spec = _normalized_repo_relative_arg(
                    args.get("specification")
                )
                requested_plan = _normalized_repo_relative_arg(args.get("plan"))
                if (
                    prior_metadata.get("planning_commit") != planning_commit_arg
                    or prior_metadata.get("specification", {}).get("path")
                    != requested_spec
                    or prior_metadata.get("plan", {}).get("path")
                    != requested_plan
                ):
                    return tool_error(
                        "task is already in implementation with a different handoff"
                    )
                transition_key = prior_metadata.get("transition_key")
                if not transition_key:
                    raise RuntimeError(
                        "idempotent handoff is missing prior transition metadata"
                    )
                result = kb.handoff_task(
                    conn,
                    tid,
                    to_step="implementation",
                    body=task.body,
                    metadata=prior_metadata,
                    transition_key=transition_key,
                    expected_run_id=_worker_run_id(tid),
                )
                attachment_ids = [
                    prior_metadata.get("specification", {}).get("attachment_id"),
                    prior_metadata.get("plan", {}).get("attachment_id"),
                ]
                if any(value is None for value in attachment_ids):
                    raise RuntimeError(
                        "idempotent handoff is missing prior attachment metadata"
                    )
                return _ok(
                    task_id=tid,
                    phase=result.to_step,
                    run_id=result.run_id,
                    idempotent=True,
                    planning_commit=planning_commit_arg,
                    attachment_ids=attachment_ids,
                )

            if task.workspace_kind not in {"dir", "worktree"}:
                return tool_error(
                    "kanban_handoff requires persistent Git workspace_kind "
                    "'dir' or 'worktree'"
                )
            if not task.workspace_path:
                return tool_error("kanban_handoff requires a task workspace_path")
            repo = Path(task.workspace_path).expanduser().resolve(strict=True)
            git_root = Path(
                _git(repo, "rev-parse", "--show-toplevel")
            ).resolve(strict=True)
            if git_root != repo:
                return tool_error(
                    f"task workspace_path must be the Git worktree root: {git_root}"
                )
            if _git(repo, "status", "--porcelain"):
                return tool_error("kanban_handoff requires a clean worktree")

            planning_commit = _git(
                repo, "rev-parse", "--verify", f"{planning_commit_arg}^{{commit}}"
            )
            if planning_commit.lower() != planning_commit_arg:
                return tool_error("planning_commit must be the full commit SHA")
            head = _git(repo, "rev-parse", "HEAD")
            if head != planning_commit:
                return tool_error(
                    f"planning_commit must equal worktree HEAD ({head})"
                )
            base_commit = _git(repo, "rev-parse", f"{planning_commit}^")
            branch = _git(repo, "symbolic-ref", "--short", "HEAD")

            spec_rel, spec_path = _validated_repo_artifact(
                repo, args.get("specification"), label="specification"
            )
            plan_rel, plan_path = _validated_repo_artifact(
                repo, args.get("plan"), label="plan"
            )
            if not spec_rel.startswith("docs/superpowers/specs/") or not spec_rel.endswith(
                ".md"
            ):
                return tool_error(
                    "specification must be under docs/superpowers/specs/ and be Markdown"
                )
            if not plan_rel.startswith("docs/superpowers/plans/") or not plan_rel.endswith(
                ".md"
            ):
                return tool_error(
                    "plan must be under docs/superpowers/plans/ and be Markdown"
                )
            if spec_rel == plan_rel:
                return tool_error("specification and plan must be different files")
            changed = {
                line.strip()
                for line in _git(
                    repo,
                    "diff-tree",
                    "--no-commit-id",
                    "--name-only",
                    "-r",
                    planning_commit,
                ).splitlines()
                if line.strip()
            }
            expected_changed = {spec_rel, plan_rel}
            if changed != expected_changed:
                return tool_error(
                    "planning commit must change exactly the specification and plan; "
                    f"got {sorted(changed)}"
                )
            for label, rel in (("specification", spec_rel), ("plan", plan_rel)):
                blob_size = int(
                    _git(repo, "cat-file", "-s", f"{planning_commit}:{rel}")
                )
                if blob_size > kb.KANBAN_ATTACHMENT_MAX_BYTES:
                    raise kb.AttachmentTooLarge(
                        f"{label} exceeds the attachment size limit "
                        f"({blob_size} > {kb.KANBAN_ATTACHMENT_MAX_BYTES} bytes)"
                    )

            spec_bytes = _git_bytes(
                repo, "show", f"{planning_commit}:{spec_rel}"
            )
            plan_bytes = _git_bytes(repo, "show", f"{planning_commit}:{plan_rel}")
            spec_hash = hashlib.sha256(spec_bytes).hexdigest()
            plan_hash = hashlib.sha256(plan_bytes).hexdigest()
            manifest = {
                "planning_commit": planning_commit,
                "specification": {"path": spec_rel, "sha256": spec_hash},
                "plan": {"path": plan_rel, "sha256": plan_hash},
            }
            transition_key = hashlib.sha256(
                json.dumps(
                    manifest, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()

            staged_spec = _stage_handoff_blob(kb, tid, spec_bytes, board=board)
            staged_paths.append(staged_spec)
            staged_plan = _stage_handoff_blob(kb, tid, plan_bytes, board=board)
            staged_paths.append(staged_plan)
            attachment_ids: list[int] = []

            transaction_state = kb.WriteTxnState()
            with kb.write_txn(conn, state=transaction_state):
                locked_task = kb.get_task(conn, tid)
                if locked_task is None:
                    raise ValueError(f"task {tid} not found")
                try:
                    locked_repo = Path(locked_task.workspace_path or "").expanduser().resolve(
                        strict=True
                    )
                except (OSError, ValueError):
                    raise ValueError("task workspace changed during handoff")
                if (
                    locked_task.workspace_kind != task.workspace_kind
                    or locked_repo != repo
                ):
                    raise ValueError("task workspace changed during handoff")

                # Another identical caller may have committed while this call
                # was staging blobs. BEGIN IMMEDIATE serializes this check with
                # all attachment rows and the phase CAS below.
                if locked_task.current_step_key == "implementation":
                    handoff_runs = kb.list_runs(
                        conn,
                        tid,
                        include_active=False,
                        state_type="outcome",
                        state_name="handed_off",
                    )
                    prior_run = handoff_runs[-1] if handoff_runs else None
                    prior_metadata = prior_run.metadata if prior_run else {}
                    result = kb.handoff_task(
                        conn,
                        tid,
                        to_step="implementation",
                        body=locked_task.body,
                        metadata=prior_metadata,
                        transition_key=transition_key,
                        expected_run_id=_worker_run_id(tid),
                        _within_transaction=True,
                    )
                    attachment_ids = [
                        prior_metadata.get("specification", {}).get("attachment_id"),
                        prior_metadata.get("plan", {}).get("attachment_id"),
                    ]
                    if any(value is None for value in attachment_ids):
                        raise RuntimeError(
                            "concurrent handoff winner is missing attachment metadata"
                        )
                else:
                    used_attachment_ids: set[int] = set()
                    artifact_inputs = (
                        (
                            "specification",
                            Path(spec_rel).name,
                            spec_bytes,
                            spec_hash,
                            staged_spec,
                        ),
                        (
                            "plan",
                            Path(plan_rel).name,
                            plan_bytes,
                            plan_hash,
                            staged_plan,
                        ),
                    )
                    artifact_metadata: dict[str, dict] = {}
                    for kind, filename, data, data_hash, staged in artifact_inputs:
                        attachment = _reusable_handoff_attachment(
                            kb,
                            conn,
                            tid,
                            filename=filename,
                            data_sha256=data_hash,
                            size=len(data),
                            excluded_ids=used_attachment_ids,
                        )
                        if attachment is None:
                            attachment_id, final_path = kb.add_staged_attachment_in_txn(
                                conn,
                                tid,
                                filename=filename,
                                staged_path=staged,
                                content_type=(
                                    mimetypes.guess_type(filename)[0]
                                    or "text/markdown"
                                ),
                                size=len(data),
                                uploaded_by="planner-handoff",
                                board=board,
                            )
                            finalized_paths.append(final_path)
                            attachment = kb.get_attachment(conn, attachment_id)
                            if attachment is None:
                                raise RuntimeError(
                                    "failed to read back staged handoff attachment"
                                )
                        else:
                            staged.unlink(missing_ok=True)
                        used_attachment_ids.add(attachment.id)
                        attachment_ids.append(attachment.id)
                        artifact_metadata[kind] = {
                            **manifest[kind],
                            "attachment_id": attachment.id,
                            "attachment_name": attachment.filename,
                        }

                    parent_ids = kb.parent_ids(conn, tid)
                    body = _implementation_handoff_body(
                        repo=repo,
                        branch=branch,
                        base_commit=base_commit,
                        planning_commit=planning_commit,
                        spec=artifact_metadata["specification"],
                        plan=artifact_metadata["plan"],
                        parent_ids=parent_ids,
                    )
                    metadata = {
                        "repository": str(repo),
                        "worktree": str(repo),
                        "branch": branch,
                        "base_commit": base_commit,
                        "planning_commit": planning_commit,
                        "specification": artifact_metadata["specification"],
                        "plan": artifact_metadata["plan"],
                        "parent_ids": parent_ids,
                    }
                    result = kb.handoff_task(
                        conn,
                        tid,
                        to_step="implementation",
                        body=redact_sensitive_text(body, force=True),
                        metadata=metadata,
                        transition_key=transition_key,
                        expected_run_id=_worker_run_id(tid),
                        _within_transaction=True,
                    )

            kb.notify_phase_handoff(result, board=board)
            hook_notified = True
            return _ok(
                task_id=tid,
                phase=result.to_step,
                run_id=result.run_id,
                idempotent=result.idempotent,
                planning_commit=planning_commit,
                attachment_ids=attachment_ids,
            )
        finally:
            if (
                transaction_state is not None
                and transaction_state.committed
                and result is not None
                and not hook_notified
            ):
                kb.notify_phase_handoff(result, board=board)
            for staged_path in staged_paths:
                staged_path.unlink(missing_ok=True)
            if not (transaction_state is not None and transaction_state.committed):
                for final_path in finalized_paths:
                    final_path.unlink(missing_ok=True)
            conn.close()
    except (OSError, ValueError, RuntimeError) as e:
        return tool_error(f"kanban_handoff: {e}")
    except Exception as e:
        logger.exception("kanban_handoff failed")
        return tool_error(f"kanban_handoff: {e}")


def _validate_review_worker(args: dict) -> tuple[str, str, str, list[dict], str]:
    """Validate and canonicalize Instructor-supplied Review evidence."""
    if os.environ.get("HERMES_PROFILE", "").strip().lower() != "instructor":
        raise ValueError("kanban_handoff implementation -> review requires profile 'instructor'")

    pull_request = str(args.get("pull_request") or "").strip()
    if not pull_request:
        raise ValueError("pull_request is required (GitHub PR URL or number)")
    implementation_head = str(args.get("implementation_head") or "").strip().lower()
    if not implementation_head:
        raise ValueError("implementation_head is required")
    if len(implementation_head) != 40 or any(
        char not in "0123456789abcdef" for char in implementation_head
    ):
        raise ValueError("implementation_head must be a full lowercase commit SHA")
    base_branch = str(args.get("base_branch") or "").strip()
    if not base_branch:
        raise ValueError("base_branch is required")

    commands = args.get("verification_commands")
    if not isinstance(commands, list) or not commands:
        raise ValueError("verification_commands are required")
    canonical_commands = []
    for result in commands:
        command = result.get("command") if isinstance(result, dict) else None
        exit_code = result.get("exit_code") if isinstance(result, dict) else None
        if (
            not isinstance(command, str)
            or not command.strip()
            or command.strip().splitlines() != [command.strip()]
            or isinstance(exit_code, bool)
            or not isinstance(exit_code, int)
        ):
            raise ValueError(
                "verification_commands must contain command and integer exit_code results"
            )
        if exit_code != 0:
            raise ValueError("verification_commands must all have exit_code 0")
        try:
            safe_command = redact_sensitive_text(
                command.strip(),
                force=True,
                file_read=True,
                redact_url_credentials=True,
            )
        except Exception:
            raise ValueError(
                "verification command evidence could not be safely redacted"
            ) from None
        if not isinstance(safe_command, str) or not safe_command.strip():
            raise ValueError(
                "verification command evidence could not be safely redacted"
            )
        safe_command = re.sub(
            r"((?:[A-Za-z][A-Za-z0-9+.-]*:)?//)[^/\s?#@]+@",
            r"\1***@",
            safe_command,
        )
        canonical_commands.append({"command": safe_command.strip(), "exit_code": 0})
    verification_digest = hashlib.sha256(
        json.dumps(
            canonical_commands, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    return (
        pull_request,
        implementation_head,
        base_branch,
        canonical_commands,
        verification_digest,
    )


def _load_planning_authorization(kb, conn, task_id: str, repo: Path) -> dict:
    """Recover and verify the immutable Planning handoff artifacts."""
    runs = kb.list_runs(
        conn,
        task_id,
        include_active=False,
        state_type="outcome",
        state_name="handed_off",
    )
    planning_run = next(
        (
            run
            for run in reversed(runs)
            if (run.metadata or {}).get("from_step") == "planning"
        ),
        None,
    )
    authorization = planning_run.metadata if planning_run else None
    if not isinstance(authorization, dict):
        raise ValueError("prior Planning handoff metadata is required")
    planning_commit = str(authorization.get("planning_commit") or "")
    if not planning_commit:
        raise ValueError("prior Planning handoff is missing planning_commit")

    for location in ("repository", "worktree"):
        try:
            authorized_repo = Path(authorization[location]).expanduser().resolve(strict=True)
        except (KeyError, OSError, TypeError, ValueError):
            raise ValueError(f"prior Planning handoff has invalid {location}")
        if authorized_repo != repo:
            raise ValueError(f"task workspace differs from Planning {location}")

    for label in ("specification", "plan"):
        artifact = authorization.get(label)
        if not isinstance(artifact, dict):
            raise ValueError(f"prior Planning handoff is missing {label} metadata")
        rel = str(artifact.get("path") or "")
        expected_hash = str(artifact.get("sha256") or "")
        attachment = kb.get_attachment(conn, artifact.get("attachment_id"))
        if attachment is None:
            raise ValueError(f"Planning {label} attachment is missing")
        blob = _git_bytes(repo, "show", f"{planning_commit}:{rel}")
        if hashlib.sha256(blob).hexdigest() != expected_hash:
            raise ValueError(f"Planning {label} Git blob hash does not match authorization")
        try:
            attachment_hash = hashlib.sha256(
                Path(attachment.stored_path).read_bytes()
            ).hexdigest()
        except OSError as exc:
            raise ValueError(f"Planning {label} attachment cannot be read: {exc}")
        if attachment_hash != expected_hash:
            raise ValueError(f"Planning {label} attachment hash does not match authorization")
    return authorization


def _github_repository(remote_url: str) -> str:
    """Return owner/repository for a credential-free GitHub remote."""
    from urllib.parse import urlparse

    remote_url = remote_url.strip()
    if remote_url.startswith("git@github.com:"):
        path = remote_url.removeprefix("git@github.com:")
    else:
        parsed = urlparse(remote_url)
        if parsed.hostname != "github.com" or parsed.username not in (None, "git"):
            raise ValueError("origin remote must be a credential-free GitHub URL")
        if parsed.password or (parsed.username and parsed.scheme in {"http", "https"}):
            raise ValueError("origin remote URL must not contain credentials")
        path = parsed.path.lstrip("/")
    repository = path.removesuffix(".git").strip("/")
    if repository.count("/") != 1 or not all(repository.split("/")):
        raise ValueError("origin remote must identify one GitHub repository")
    return repository


def _verify_review_git_state(
    kb,
    conn,
    task,
    *,
    implementation_head: str,
) -> dict:
    """Verify workspace identity, Git state, and Planning ancestry."""
    if task.workspace_kind not in {"dir", "worktree"} or not task.workspace_path:
        raise ValueError("kanban_handoff requires a persistent Git worktree")
    repo = Path(task.workspace_path).expanduser().resolve(strict=True)
    git_root = Path(_git(repo, "rev-parse", "--show-toplevel")).resolve(strict=True)
    if git_root != repo:
        raise ValueError(f"task workspace_path must be the Git worktree root: {git_root}")
    if _git(repo, "status", "--porcelain"):
        raise ValueError("kanban_handoff review requires a clean worktree")
    branch = _git(repo, "symbolic-ref", "--short", "HEAD")
    if branch != task.branch_name:
        raise ValueError("current branch does not match the task branch")
    head = _git(repo, "rev-parse", "HEAD")
    if head != implementation_head:
        raise ValueError(f"implementation_head must equal worktree HEAD ({head})")

    authorization = _load_planning_authorization(kb, conn, task.id, repo)
    planning_commit = authorization["planning_commit"]
    try:
        _git(repo, "merge-base", "--is-ancestor", planning_commit, implementation_head)
    except ValueError:
        raise ValueError("planning commit must be an ancestor of implementation_head")
    if int(_git(repo, "rev-list", "--count", f"{planning_commit}..{implementation_head}")) < 1:
        raise ValueError("implementation range must be non-empty")
    artifact_paths = [
        authorization["specification"]["path"],
        authorization["plan"]["path"],
    ]
    changed_artifacts = _git(
        repo,
        "diff",
        "--name-only",
        f"{planning_commit}..{implementation_head}",
        "--",
        *artifact_paths,
    )
    if changed_artifacts:
        raise ValueError("a planning artifact changed after the planning commit")

    changed_files = [
        line
        for line in _git(
            repo, "diff", "--name-only", f"{planning_commit}..{implementation_head}"
        ).splitlines()
        if line
    ]
    diff_stat = _git(
        repo, "diff", "--shortstat", f"{planning_commit}..{implementation_head}"
    )
    repository = _github_repository(_git(repo, "config", "--get", "remote.origin.url"))
    return {
        "repo": repo,
        "repository": repository,
        "branch": branch,
        "planning": authorization,
        "changed_files": changed_files,
        "diff_stat": diff_stat,
    }


def _verify_open_pull_request(
    repo: Path,
    pull_request: str,
    *,
    repository: str,
    branch: str,
    implementation_head: str,
    base_branch: str,
) -> dict:
    """Resolve one PR with gh and prove its complete local identity."""
    fields = (
        "number,url,state,isDraft,headRefName,headRefOid,baseRefName,"
        "headRepository,headRepositoryOwner"
    )
    proc = subprocess.run(
        ["gh", "pr", "view", pull_request, "--json", fields],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = redact_sensitive_text(
            (proc.stderr or proc.stdout or "no pull request found").strip(), force=True
        )
        raise ValueError(f"gh pr view failed: {detail}")
    try:
        pr = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise ValueError("gh pr view returned invalid JSON")
    if not isinstance(pr, dict):
        raise ValueError("gh pr view must resolve exactly one pull request")
    if pr.get("state") != "OPEN":
        raise ValueError("pull request must be one open pull request")
    if pr.get("isDraft") is not False:
        raise ValueError("pull request must not be a draft")

    number = pr.get("number")
    url = str(pr.get("url") or "")
    expected_url = f"https://github.com/{repository}/pull/{number}"
    if not isinstance(number, int) or number < 1 or url != expected_url:
        raise ValueError("pull request URL/number does not match the repository")
    if pull_request.isdigit():
        if int(pull_request) != number:
            raise ValueError("pull request number does not match gh")
    elif pull_request != url:
        raise ValueError("pull request URL does not match gh")

    head_repository = pr.get("headRepository")
    head_name = (
        head_repository.get("nameWithOwner")
        if isinstance(head_repository, dict)
        else None
    )
    if head_name != repository:
        raise ValueError("pull request head repository is ambiguous or does not match")
    if pr.get("headRefName") != branch:
        raise ValueError("pull request head branch does not match the task")
    if pr.get("headRefOid") != implementation_head:
        raise ValueError("pull request head SHA does not match implementation_head")
    if pr.get("baseRefName") != base_branch:
        raise ValueError("pull request base branch does not match base_branch")
    return {"number": number, "url": url}


def _build_review_handoff(
    git_state: dict,
    pr: dict,
    *,
    implementation_head: str,
    base_branch: str,
    commands: list[dict],
) -> str:
    """Render the single durable human Review record."""
    planning_commit = git_state["planning"]["planning_commit"]
    files = ", ".join(f"`{path}`" for path in git_state["changed_files"])
    command_lines = "\n".join(
        f"- `{item['command']}` — exit `{item['exit_code']}`" for item in commands
    )
    return (
        "## Review handoff\n\n"
        f"- Repository: `{git_state['repository']}`\n"
        f"- Worktree: `{git_state['repo']}`\n"
        f"- Branch: `{git_state['branch']}`\n"
        f"- Base branch: `{base_branch}`\n"
        f"- Planning commit: `{planning_commit}`\n"
        f"- Implementation head: `{implementation_head}`\n"
        f"- PR: `#{pr['number']}` — {pr['url']}\n\n"
        "### Implementation diff\n"
        f"- Changed files: `{len(git_state['changed_files'])}` — {files}\n"
        f"- Summary: {git_state['diff_stat']}\n\n"
        "### Verification\n"
        f"{command_lines}\n\n"
        "Human review and merge required; Instructor cannot complete this card."
    )


def _review_retry(kb, conn, task, args: dict, commands: list[dict], digest: str):
    """Use only durable evidence to decide a lost-response Review retry."""
    runs = kb.list_runs(
        conn,
        task.id,
        include_active=False,
        state_type="outcome",
        state_name="handed_off",
    )
    review_run = next(
        (
            run
            for run in reversed(runs)
            if (run.metadata or {}).get("to_step") == "review"
        ),
        None,
    )
    metadata = review_run.metadata if review_run else None
    if not isinstance(metadata, dict):
        raise ValueError("idempotent review handoff is missing prior metadata")
    requested_pr = str(args["pull_request"]).strip()
    prior_pr = metadata["pull_request"]
    pr_matches = requested_pr == prior_pr["url"] or (
        requested_pr.isdigit() and int(requested_pr) == prior_pr["number"]
    )
    if (
        not pr_matches
        or args["implementation_head"].strip().lower()
        != metadata["implementation_head"]
        or args["base_branch"].strip() != metadata["base_branch"]
        or commands != metadata["verification_commands"]
        or digest != metadata["verification_digest"]
    ):
        raise ValueError("task is already in review with a different review handoff")
    result = kb.handoff_task_to_review(
        conn,
        task.id,
        body=task.body,
        metadata=metadata,
        expected_run_id=_worker_run_id(task.id),
        expected_profile="instructor",
    )
    return result, prior_pr


def _handle_review_handoff(args: dict, **kw) -> str:
    """Verify local implementation + PR evidence and hand the card to Review."""
    try:
        pull_request, head, base, commands, digest = _validate_review_worker(args)
        tid = _default_task_id(args.get("task_id"))
        if not tid:
            raise ValueError("task_id is required (or set HERMES_KANBAN_TASK in the env)")
        kb, conn = _connect()
        try:
            task = kb.get_task(conn, tid)
            if task is None:
                raise ValueError(f"task {tid} not found")
            if task.current_step_key == "review":
                result, pr = _review_retry(kb, conn, task, args, commands, digest)
            else:
                git_state = _verify_review_git_state(
                    kb, conn, task, implementation_head=head
                )
                pr = _verify_open_pull_request(
                    git_state["repo"],
                    pull_request,
                    repository=git_state["repository"],
                    branch=git_state["branch"],
                    implementation_head=head,
                    base_branch=base,
                )
                body = _build_review_handoff(
                    git_state,
                    pr,
                    implementation_head=head,
                    base_branch=base,
                    commands=commands,
                )
                metadata = {
                    "planning_commit": git_state["planning"]["planning_commit"],
                    "implementation_head": head,
                    "branch": git_state["branch"],
                    "base_branch": base,
                    "pull_request": pr,
                    "verification_commands": commands,
                    "verification_digest": digest,
                    "publication_attempt_count": 1,
                }
                result = kb.handoff_task_to_review(
                    conn,
                    tid,
                    body=redact_sensitive_text(body, force=True),
                    metadata=metadata,
                    expected_run_id=_worker_run_id(tid),
                    expected_profile="instructor",
                )
            return _ok(
                task_id=tid,
                phase="review",
                run_id=result.run_id,
                idempotent=result.idempotent,
                pull_request=pr["url"],
            )
        finally:
            conn.close()
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as e:
        try:
            detail = redact_sensitive_text(
                str(e), force=True, redact_url_credentials=True
            )
        except Exception:
            detail = "review evidence could not be safely redacted"
        if not isinstance(detail, str):
            detail = "verification command evidence could not be safely redacted"
        return tool_error(f"kanban_handoff: {detail}")


def _handle_handoff(args: dict, **kw) -> str:
    """Dispatch one same-card human-gated phase handoff."""
    if not os.environ.get("HERMES_KANBAN_TASK", "").strip():
        return tool_error("kanban_handoff requires a dispatcher task-scoped worker")
    if args.get("board") not in (None, ""):
        return tool_error("task-scoped kanban_handoff must not include a board override")
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error("task_id is required (or set HERMES_KANBAN_TASK in the env)")
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    if args.get("to_phase") == "implementation":
        return _handle_implementation_handoff(args, **kw)
    if args.get("to_phase") == "review":
        return _handle_review_handoff(args, **kw)
    return tool_error("to_phase must be 'implementation' or 'review'")


def _handle_heartbeat(args: dict, **kw) -> str:
    """Signal that the worker is still alive during a long operation.

    Extends the claim TTL via ``heartbeat_claim`` AND records a heartbeat
    event via ``heartbeat_worker``. Without the ``heartbeat_claim`` half,
    a diligent worker that loops this tool while a single tool call
    blocks the agent for >DEFAULT_CLAIM_TTL_SECONDS still gets reclaimed
    by ``release_stale_claims`` — which is exactly the trap that
    ``heartbeat_claim``'s docstring warns against.
    """
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    note = args.get("note")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            # Extend the claim TTL first. The dispatcher pins
            # HERMES_KANBAN_CLAIM_LOCK in the worker env at spawn time
            # (see _default_spawn in kanban_db.py); falling back to the
            # default _claimer_id() covers locally-driven workers that
            # never went through the dispatcher path.
            claim_lock = os.environ.get("HERMES_KANBAN_CLAIM_LOCK")
            kb.heartbeat_claim(conn, tid, claimer=claim_lock)

            ok = kb.heartbeat_worker(
                conn,
                tid,
                note=note,
                expected_run_id=_worker_run_id(tid),
            )
            if not ok:
                return tool_error(
                    f"could not heartbeat {tid} (unknown id or not running)"
                )
            return _ok(task_id=tid)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_heartbeat: {e}")
    except Exception as e:
        logger.exception("kanban_heartbeat failed")
        return tool_error(f"kanban_heartbeat: {e}")


def _handle_comment(args: dict, **kw) -> str:
    """Append a comment to a task's thread."""
    tid = args.get("task_id")
    if not tid:
        return tool_error(
            "task_id is required (use the current task id if that's what "
            "you mean — pulls from env but kept explicit here)"
        )
    body = args.get("body")
    if not body or not str(body).strip():
        return tool_error("body is required")
    body = redact_sensitive_text(str(body), force=True)
    # Author is intentionally derived from the worker's own runtime
    # identity, NOT from caller-supplied args. Comments are injected
    # into the next worker's system prompt by ``build_worker_context``
    # as ``**{author}** (timestamp): {body}`` — accepting an
    # ``args["author"]`` override let a worker forge a comment from
    # an authoritative-looking name like ``hermes-system`` and poison
    # the future-worker context with what reads as a system directive.
    # Cross-task commenting itself remains unrestricted (see #19713) —
    # comments are the deliberate handoff channel between tasks.
    author = os.environ.get("HERMES_PROFILE") or "worker"
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            cid = kb.add_comment(conn, tid, author=author, body=str(body))
            return _ok(task_id=tid, comment_id=cid)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_comment: {e}")
    except Exception as e:
        logger.exception("kanban_comment failed")
        return tool_error(f"kanban_comment: {e}")


def _handle_attach(args: dict, **kw) -> str:
    """Attach an inline (base64) file to a task.

    Mirrors the dashboard's upload endpoint for the agent surface: decode
    the payload, enforce the shared size cap, write it under the per-task
    attachments dir, and record the metadata row — all via
    ``kanban_db.store_attachment_bytes`` so the three surfaces stay in lockstep.
    """
    from hermes_cli import kanban_db as kb

    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    filename = args.get("filename")
    if not filename or not str(filename).strip():
        return tool_error("filename is required")
    content_b64 = args.get("content_base64")
    if not content_b64 or not str(content_b64).strip():
        return tool_error("content_base64 is required")
    import base64
    import binascii
    try:
        data = base64.b64decode(str(content_b64), validate=True)
    except (binascii.Error, ValueError) as e:
        return tool_error(f"content_base64 is not valid base64: {e}")
    content_type = args.get("content_type")
    board = args.get("board")
    try:
        _, conn = _connect(board=board)
        try:
            att_id = kb.store_attachment_bytes(
                conn,
                tid,
                str(filename),
                data,
                content_type=content_type,
                uploaded_by="agent",
                board=board,
            )
            return _ok(task_id=tid, attachment_id=att_id, size=len(data))
        finally:
            conn.close()
    except kb.AttachmentTooLarge as e:
        return tool_error(f"kanban_attach: {e}")
    except ValueError as e:
        return tool_error(f"kanban_attach: {e}")
    except Exception as e:
        logger.exception("kanban_attach failed")
        return tool_error(f"kanban_attach: {e}")


_MAX_ATTACH_URL_REDIRECTS = 5


def _download_url_with_cap(url: str, max_bytes: int) -> tuple[bytes, Optional[str]]:
    """Fetch ``url`` over http(s) with SSRF guarding, capped at ``max_bytes``.

    Every hop — the initial URL and each redirect target — is validated with
    ``tools.url_safety.is_safe_url`` before it is fetched, so a
    model-controlled URL (or a public host 302ing to one) cannot reach
    loopback, private/CGNAT ranges, or cloud metadata endpoints. Redirects
    are followed manually (``follow_redirects=False``) so each Location is
    re-checked, mirroring ``tools.skills_hub._guarded_http_get``.

    Returns ``(data, content_type)``. Raises ``ValueError`` for a non-http(s)
    scheme, an SSRF-blocked target, too many redirects, or a body that
    overruns the cap (the caller maps it to a clean tool error). Reads in
    chunks so an oversize response is rejected without buffering the whole
    thing.
    """
    from urllib.parse import urljoin, urlparse

    import httpx

    from tools.url_safety import is_safe_url

    current_url = url
    for _ in range(_MAX_ATTACH_URL_REDIRECTS + 1):
        scheme = (urlparse(current_url).scheme or "").lower()
        if scheme not in ("http", "https"):
            raise ValueError(
                f"unsupported URL scheme {scheme!r}; only http/https are allowed"
            )
        if not is_safe_url(current_url):
            raise ValueError(
                f"URL blocked by SSRF protection (private/internal address): {current_url}"
            )
        chunks: list[bytes] = []
        total = 0
        with httpx.stream(
            "GET",
            current_url,
            headers={"User-Agent": "hermes-kanban/attach"},
            timeout=30,
            follow_redirects=False,
        ) as resp:
            if resp.is_redirect:
                location = resp.headers.get("location")
                if not location:
                    raise ValueError(f"redirect without Location header from {current_url}")
                current_url = urljoin(current_url, location)
                continue
            resp.raise_for_status()
            content_type = (resp.headers.get("content-type") or "").split(";")[0].strip() or None
            for chunk in resp.iter_bytes(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(
                        f"attachment exceeds {max_bytes // (1024 * 1024)} MB limit"
                    )
                chunks.append(chunk)
        return b"".join(chunks), content_type
    raise ValueError(f"too many redirects fetching {url}")


def _handle_attach_url(args: dict, **kw) -> str:
    """Attach a file fetched server-side from a URL.

    The agent passes a URL; Hermes downloads it (with the shared size cap)
    and stores it as a real attachment. Useful when the agent has a link
    rather than the bytes. Only http/https URLs are accepted.
    """
    from hermes_cli import kanban_db as kb

    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    url = args.get("url")
    if not url or not str(url).strip():
        return tool_error("url is required")
    url = str(url).strip()
    filename = args.get("filename") or args.get("title")
    if not filename or not str(filename).strip():
        # Derive a name from the URL path's leaf component.
        from urllib.parse import unquote, urlparse
        leaf = unquote(urlparse(url).path.rsplit("/", 1)[-1]).strip()
        filename = leaf or "download"
    content_type = args.get("content_type")
    board = args.get("board")
    try:
        data, fetched_ct = _download_url_with_cap(url, kb.KANBAN_ATTACHMENT_MAX_BYTES)
    except ValueError as e:
        return tool_error(f"kanban_attach_url: {e}")
    except Exception as e:
        logger.exception("kanban_attach_url download failed")
        return tool_error(f"kanban_attach_url: failed to fetch {url}: {e}")
    try:
        _, conn = _connect(board=board)
        try:
            att_id = kb.store_attachment_bytes(
                conn,
                tid,
                str(filename),
                data,
                content_type=content_type or fetched_ct,
                uploaded_by="agent",
                board=board,
            )
            return _ok(task_id=tid, attachment_id=att_id, size=len(data))
        finally:
            conn.close()
    except kb.AttachmentTooLarge as e:
        return tool_error(f"kanban_attach_url: {e}")
    except ValueError as e:
        return tool_error(f"kanban_attach_url: {e}")
    except Exception as e:
        logger.exception("kanban_attach_url failed")
        return tool_error(f"kanban_attach_url: {e}")


def _handle_attachments(args: dict, **kw) -> str:
    """List a task's attachments (read-only; no ownership restriction)."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            if kb.get_task(conn, tid) is None:
                return tool_error(f"task {tid} not found")
            atts = kb.list_attachments(conn, tid)
            return json.dumps({
                "ok": True,
                "task_id": tid,
                "attachments": [
                    {
                        "id": a.id,
                        "filename": a.filename,
                        "content_type": a.content_type,
                        "size": a.size,
                        "uploaded_by": a.uploaded_by,
                        "stored_path": a.stored_path,
                        "created_at": a.created_at,
                    }
                    for a in atts
                ],
            })
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_attachments: {e}")
    except Exception as e:
        logger.exception("kanban_attachments failed")
        return tool_error(f"kanban_attachments: {e}")


def _handle_create(args: dict, **kw) -> str:
    """Create a child task. Orchestrator workers use this to fan out.

    ``parents`` can be a list of task ids; dependency-gated promotion
    works as usual.
    """
    title = args.get("title")
    if not title or not str(title).strip():
        return tool_error("title is required")
    assignee = args.get("assignee")
    if not assignee:
        return tool_error(
            "assignee is required — name the profile that should execute this "
            "task (the dispatcher will only spawn tasks with an assignee)"
        )
    body = args.get("body")
    parents = args.get("parents") or []
    tenant = args.get("tenant") or os.environ.get("HERMES_TENANT")
    # Stamp the originating session id when the agent loop runs under
    # ACP (which sets HERMES_SESSION_ID before invoking tools). NULL on
    # CLI / dashboard paths and on legacy hosts that don't set the env.
    session_id = args.get("session_id") or os.environ.get("HERMES_SESSION_ID")
    priority = args.get("priority")
    # Resolve workspace. If the caller passed one explicitly, honor it.
    # Otherwise, a dispatcher-spawned worker (HERMES_KANBAN_TASK set)
    # inherits its own running task's workspace, so a worker editing a
    # dir:/worktree project that spawns a follow-up child keeps the child
    # in that project instead of a throwaway scratch dir. Orchestrators
    # (kanban toolset, no HERMES_KANBAN_TASK) and CLI/dashboard callers
    # fall back to scratch as before. Explicit None path stays None.
    workspace_kind = args.get("workspace_kind")
    workspace_path = args.get("workspace_path")
    project_id = args.get("project") or args.get("project_id")
    _inherit_workspace = workspace_kind is None and workspace_path is None
    if workspace_kind is None:
        workspace_kind = "scratch"
    triage, bool_error = _parse_bool_arg(args, "triage")
    if bool_error:
        return tool_error(bool_error)
    idempotency_key = args.get("idempotency_key")
    max_runtime_seconds = args.get("max_runtime_seconds")
    initial_status = args.get("initial_status") or "running"
    skills = args.get("skills")
    if isinstance(skills, str):
        # Accept a single skill name as a string for convenience.
        skills = [skills]
    if skills is not None and not isinstance(skills, (list, tuple)):
        return tool_error(
            f"skills must be a list of skill names, got {type(skills).__name__}"
        )
    goal_mode, goal_bool_error = _parse_bool_arg(args, "goal_mode")
    if goal_bool_error:
        return tool_error(goal_bool_error)
    goal_max_turns = args.get("goal_max_turns")
    if isinstance(parents, str):
        parents = [parents]
    if not isinstance(parents, (list, tuple)):
        return tool_error(
            f"parents must be a list of task ids, got {type(parents).__name__}"
        )
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            # Inherit the spawning worker's own task workspace when the
            # caller didn't specify one (see resolution note above).
            if _inherit_workspace:
                _self_tid = os.environ.get("HERMES_KANBAN_TASK")
                if _self_tid:
                    _self_task = kb.get_task(conn, _self_tid)
                    if _self_task is not None and _self_task.workspace_kind:
                        workspace_kind = _self_task.workspace_kind
                        workspace_path = _self_task.workspace_path
                        # Keep follow-up children inside the same project so the
                        # whole subtree shares one repo + branch convention.
                        if project_id is None and _self_task.project_id:
                            project_id = _self_task.project_id
            new_tid = kb.create_task(
                conn,
                title=str(title).strip(),
                body=body,
                assignee=str(assignee),
                parents=tuple(parents),
                tenant=tenant,
                priority=int(priority) if priority is not None else 0,
                workspace_kind=str(workspace_kind),
                workspace_path=workspace_path,
                project_id=project_id,
                triage=triage,
                idempotency_key=idempotency_key,
                max_runtime_seconds=(
                    int(max_runtime_seconds)
                    if max_runtime_seconds is not None else None
                ),
                skills=skills,
                goal_mode=goal_mode,
                goal_max_turns=(
                    int(goal_max_turns) if goal_max_turns is not None else None
                ),
                initial_status=str(initial_status),
                created_by=os.environ.get("HERMES_PROFILE") or "worker",
                session_id=session_id,
            )
            new_task = kb.get_task(conn, new_tid)
            subscribed = _maybe_auto_subscribe(conn, new_tid)
            return _ok(
                task_id=new_tid,
                status=new_task.status if new_task else None,
                subscribed=subscribed,
            )
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_create: {e}")
    except Exception as e:
        logger.exception("kanban_create failed")
        return tool_error(f"kanban_create: {e}")


def _maybe_auto_subscribe(conn: Any, task_id: str) -> bool:
    """Auto-subscribe the calling session to task completion / block events.

    Returns True if a subscription row was written, False otherwise (no
    session context, config gate disabled, or best-effort failure). The
    caller surfaces this in the ``subscribed`` field of the kanban_create
    response so an orchestrator can decide whether to fall back to an
    explicit ``kanban_notify-subscribe`` or to polling.

    Gated by ``kanban.auto_subscribe_on_create`` in config.yaml (default
    True). Disable to mirror pre-feature behaviour, e.g. when the
    originating user/chat opted out via the per-platform notification
    toggle (see ``hermes dashboard``).

    Subscription paths:

    - **Gateway** (telegram/discord/slack/etc): ``HERMES_SESSION_PLATFORM``
      and ``HERMES_SESSION_CHAT_ID`` are set in ContextVars by the
      messaging gateway before agent dispatch. The notification poller
      already keys off these, so we just register a row.

    - **TUI** (herm desktop / herm TUI): the platform/chat_id ContextVars
      are intentionally cleared (TUI is a single-channel local UI, not
      a multi-tenant chat surface), but the agent subprocess inherits
      ``HERMES_SESSION_KEY`` from the parent session. We subscribe with
      ``platform="tui"`` and ``chat_id=<key>``; the TUI notification
      poller (``tui_gateway/server.py``) reads ``kanban_notify_subs``
      for these rows and posts the completion message into the running
      session.

    - **CLI / cron / test / unattached**: no persistent delivery channel,
      no-op.

    Failure mode: any exception inside the function is logged at WARNING
    with the offending exception + diagnostic env vars and swallowed.
    We never want a notification bookkeeping failure to fail the
    kanban_create that the agent is mid-conversation about.
    """
    try:
        cfg = load_config()
        if not cfg_get(cfg, "kanban", "auto_subscribe_on_create", default=True):
            return False
    except Exception:
        # If config can't load we still default to True — this is the
        # user-friendly behaviour that mirrors the pre-gate implementation.
        pass

    platform = ""
    chat_id = ""
    try:
        from gateway.session_context import get_session_env
        platform = get_session_env("HERMES_SESSION_PLATFORM", "")
        chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
        if not platform or not chat_id:
            # TUI / desktop fallback: platform/chat_id ContextVars are
            # cleared for TUI sessions, but the parent process exports
            # HERMES_SESSION_KEY into the subprocess env. Treat that
            # as a "tui" subscription so the TUI notification poller
            # (tui_gateway/server.py) can pick it up.
            #
            # HERMES_SESSION_ID is intentionally NOT a fallback here:
            # it is set by ACP / the agent subprocess for telemetry
            # regardless of whether the parent is a TUI or a CLI, so
            # treating it as a notification target would auto-subscribe
            # every CLI invocation, which is exactly the over-eager
            # behaviour that got #19718 reverted upstream. The TUI
            # poller keys on HERMES_SESSION_KEY.
            session_key = (
                get_session_env("HERMES_SESSION_KEY", "")
                or os.environ.get("HERMES_SESSION_KEY", "")
            )
            if not session_key:
                return False  # CLI / cron / test — no persistent channel
            platform = "tui"
            chat_id = session_key
        thread_id = get_session_env("HERMES_SESSION_THREAD_ID", "") or None
        user_id = get_session_env("HERMES_SESSION_USER_ID", "") or None
        notifier_profile = (
            get_session_env("HERMES_SESSION_PROFILE", "")
            or os.environ.get("HERMES_PROFILE")
        )

        # Lazy-import to keep the module-level dependency light
        from hermes_cli import kanban_db as _kb
        _kb.add_notify_sub(
            conn, task_id=task_id,
            platform=platform, chat_id=chat_id,
            thread_id=thread_id, user_id=user_id,
            notifier_profile=notifier_profile,
        )
        return True
    except Exception as _exc:
        logger.warning(
            "_maybe_auto_subscribe failed: %r (platform=%r key_set=%r)",
            _exc, platform, bool(chat_id),
        )
        return False


def _handle_unblock(args: dict, **kw) -> str:
    """Transition a blocked task to ready, or todo while parents remain open."""
    guard = _require_orchestrator_tool("kanban_unblock")
    if guard:
        return guard
    tid = args.get("task_id")
    if not tid:
        return tool_error("task_id is required")
    ownership_err = _enforce_worker_task_ownership(str(tid))
    if ownership_err:
        return ownership_err
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            ok = kb.unblock_task(conn, str(tid))
            if not ok:
                return tool_error(f"could not unblock {tid} (not blocked or unknown)")
            task = kb.get_task(conn, str(tid))
            return _ok(task_id=str(tid), status=task.status if task else None)
        finally:
            conn.close()
    except ValueError as e:
        return tool_error(f"kanban_unblock: {e}")
    except Exception as e:
        logger.exception("kanban_unblock failed")
        return tool_error(f"kanban_unblock: {e}")


def _handle_link(args: dict, **kw) -> str:
    """Add a parent→child dependency edge after the fact."""
    parent_id = args.get("parent_id")
    child_id = args.get("child_id")
    if not parent_id or not child_id:
        return tool_error("both parent_id and child_id are required")
    board = args.get("board")
    try:
        kb, conn = _connect(board=board)
        try:
            kb.link_tasks(conn, parent_id=parent_id, child_id=child_id)
            return _ok(parent_id=parent_id, child_id=child_id)
        finally:
            conn.close()
    except ValueError as e:
        # Covers cycle + self-parent rejections
        return tool_error(f"kanban_link: {e}")
    except Exception as e:
        logger.exception("kanban_link failed")
        return tool_error(f"kanban_link: {e}")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

_DESC_TASK_ID_DEFAULT = (
    "Task id. If omitted, defaults to HERMES_KANBAN_TASK from the env "
    "(the task the dispatcher spawned you to work on)."
)

_DESC_BOARD = (
    "Kanban board slug to target. When omitted, the call resolves the "
    "active board the usual way: HERMES_KANBAN_DB env → "
    "HERMES_KANBAN_BOARD env → the 'current' symlink under the kanban "
    "home → 'default'. Pass an explicit slug only when the caller (e.g. "
    "a Telegram routing layer) needs to override the env-pinned active "
    "board for this one call."
)


def _board_schema_prop() -> dict[str, str]:
    """Schema fragment for the optional ``board`` parameter.

    Centralised so a future tweak to the description / validation hint
    only has to land in one place.
    """
    return {"type": "string", "description": _DESC_BOARD}

KANBAN_SHOW_SCHEMA = {
    "name": "kanban_show",
    "description": (
        "Read a task's full state — title, body, assignee, parent task "
        "handoffs, your prior attempts on this task if any, comments, "
        "and recent events. Use this to (re)orient yourself before "
        "starting work, especially on retries. The response includes a "
        "pre-formatted ``worker_context`` string suitable for inclusion "
        "verbatim in your reasoning."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_LIST_SCHEMA = {
    "name": "kanban_list",
    "description": (
        "List Kanban task summaries so an orchestrator profile can discover "
        "work to route. Supports the same core filters as the CLI: assignee, "
        "status, tenant, include_archived, and limit. Returns compact rows "
        "with ids, title, status, assignee, priority, parent/child ids, and "
        "counts. Bounded to 50 rows by default, 200 max, with truncation "
        "metadata. Also recomputes ready tasks before listing, matching the "
        "CLI. Orchestrator-only — dispatcher-spawned task workers never see "
        "this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "assignee": {
                "type": "string",
                "description": "Optional assignee/profile filter.",
            },
            "status": {
                "type": "string",
                "enum": [
                    "triage", "todo", "ready", "running",
                    "blocked", "done", "archived",
                ],
                "description": "Optional task status filter.",
            },
            "tenant": {
                "type": "string",
                "description": "Optional tenant/project namespace filter.",
            },
            "include_archived": {
                "type": "boolean",
                "description": "Include archived tasks. Defaults to false.",
            },
            "limit": {
                "type": "integer",
                "description": "Optional maximum rows to return (default 50, max 200).",
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_COMPLETE_SCHEMA = {
    "name": "kanban_complete",
    "description": (
        "Mark your current task done with a structured handoff for "
        "downstream workers and humans. Prefer ``summary`` for a "
        "human-readable 1-3 sentence description of what you did; put "
        "machine-readable facts in ``metadata`` (changed_files, "
        "tests_run, decisions, findings, etc). At least one of "
        "``summary`` or ``result`` is required. If you created new "
        "tasks via ``kanban_create`` during this run, list their ids "
        "in ``created_cards`` — the kernel verifies them so phantom "
        "references are caught before they leak into downstream "
        "automation. If you produced deliverable files (charts, PDFs, "
        "spreadsheets, generated images), list their absolute paths "
        "in ``artifacts`` — the gateway notifier will upload them as "
        "native attachments to the human who subscribed to the task, "
        "so the deliverable lands in their chat alongside the summary "
        "instead of being a path they have to fetch by hand."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "summary": {
                "type": "string",
                "description": (
                    "Human-readable handoff, 1-3 sentences. Appears in "
                    "Run History on the dashboard and in downstream "
                    "workers' context."
                ),
            },
            "metadata": {
                "type": "object",
                "description": (
                    "Free-form dict of structured facts about this "
                    "attempt — {\"changed_files\": [...], \"tests_run\": 12, "
                    "\"findings\": [...]}. Surfaced to downstream "
                    "workers alongside ``summary``."
                ),
            },
            "result": {
                "type": "string",
                "description": (
                    "Short result log line (legacy field, maps to "
                    "task.result). Use ``summary`` instead when "
                    "possible; this exists for compatibility with "
                    "callers that still set --result on the CLI."
                ),
            },
            "created_cards": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional structured manifest of task ids you "
                    "created via ``kanban_create`` during this run. "
                    "The kernel verifies each id exists and was "
                    "created by this worker's profile; any phantom "
                    "id blocks the completion with an error listing "
                    "what went wrong (auditable in the task's events). "
                    "Only list ids you got back from a successful "
                    "``kanban_create`` call — do not invent or "
                    "remember ids from prose. Omit the field if you "
                    "did not create any cards."
                ),
            },
            "artifacts": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of absolute paths to deliverable "
                    "files you produced during this run — generated "
                    "charts, PDFs, spreadsheets, images, archives. "
                    "Examples: [\"/tmp/q3-revenue.png\", "
                    "\"/tmp/report.pdf\"]. The gateway notifier "
                    "uploads each path as a native attachment to the "
                    "subscribed chat (images embed inline, everything "
                    "else uploads as a file) so the deliverable "
                    "lands with the completion notification. Skip "
                    "intermediate scratch files and references that "
                    "are not the deliverable. The path must exist "
                    "on disk at completion. Files inside a managed scratch "
                    "workspace are copied to durable task attachments before "
                    "cleanup; a missing declared scratch artifact keeps the "
                    "task in-flight so you can fix the path and retry."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_BLOCK_SCHEMA = {
    "name": "kanban_block",
    "description": (
        "Stop work on this task and route it according to WHY you're stuck. "
        "Set ``kind`` to say which: 'dependency' (waiting on another task — "
        "goes to todo and auto-resumes when that task finishes, no human "
        "needed), 'needs_input' (you need a human decision/answer), "
        "'capability' (a hard wall: no access, missing credentials, an action "
        "no agent can do), or 'transient' (a flaky failure that may clear). "
        "``reason`` is shown to the human on the board. If a task keeps "
        "getting unblocked and re-blocked for the same reason, it is "
        "auto-escalated to triage. Use for genuine blockers only — don't "
        "block on things you can resolve yourself."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "reason": {
                "type": "string",
                "description": (
                    "What you need answered or what stopped you, in one or "
                    "two sentences. Don't paste the whole conversation; the "
                    "human has the board and can ask follow-ups via comments."
                ),
            },
            "kind": {
                "type": "string",
                "enum": ["dependency", "needs_input", "capability", "transient"],
                "description": (
                    "Why you're blocked. 'dependency' waits in todo and "
                    "resumes automatically; the others surface to a human. "
                    "Omit only if none apply."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": ["reason"],
    },
}

KANBAN_PREPARE_PLANNING_SCHEMA = {
    "name": "kanban_prepare_planning",
    "description": (
        "Default-orchestrator-only. Atomically opt one existing Triage card into "
        "the fixed human-gated Planning phase. Preserves the same card, body, "
        "priority, comments, dependencies, and persistent Git workspace; applies "
        "the [Planning] title, assigns Planner, and chooses Ready or Todo from "
        "parent completion. Accepts no workflow, phase, assignee, title, body, or "
        "status overrides."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Existing Triage card to prepare for Planner.",
            },
            "board": _board_schema_prop(),
        },
        "required": ["task_id"],
    },
}


KANBAN_HANDOFF_SCHEMA = {
    "name": "kanban_handoff",
    "description": (
        "Atomically advance the current human-gated workflow card. Planner calls "
        "to_phase='implementation' with the planning commit and artifacts. "
        "Instructor calls to_phase='review' with the PR identity, implementation "
        "head, base branch, and structured verification results. Each transition "
        "is verified fail-closed and leaves the same card blocked for a human."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "to_phase": {
                "type": "string",
                "enum": ["implementation", "review"],
            },
            "planning_commit": {
                "type": "string",
                "description": "Full SHA of the clean worktree HEAD planning-only commit.",
            },
            "specification": {
                "type": "string",
                "description": "Repository-relative specification Markdown path.",
            },
            "plan": {
                "type": "string",
                "description": "Repository-relative implementation-plan Markdown path.",
            },
            "pull_request": {
                "type": "string",
                "description": "GitHub pull request URL or positive PR number.",
            },
            "implementation_head": {
                "type": "string",
                "description": "Full SHA of the clean implementation worktree HEAD.",
            },
            "base_branch": {
                "type": "string",
                "description": "Expected pull request base branch.",
            },
            "verification_commands": {
                "type": "array",
                "description": "Verification command summaries and exit codes; raw output is not accepted or persisted.",
                "items": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "exit_code": {"type": "integer"},
                    },
                    "required": ["command", "exit_code"],
                },
            },
        },
        "required": ["to_phase"],
    },
}

KANBAN_HEARTBEAT_SCHEMA = {
    "name": "kanban_heartbeat",
    "description": (
        "Signal that you're still alive during a long operation "
        "(training, encoding, large crawls). Call every few minutes so "
        "humans see liveness separately from PID checks. Pure side "
        "effect — no work changes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "note": {
                "type": "string",
                "description": (
                    "Optional short note describing current progress. "
                    "Shown in the event log."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_COMMENT_SCHEMA = {
    "name": "kanban_comment",
    "description": (
        "Append a comment to a task's thread. Use for durable notes "
        "that should outlive this run (questions for the next worker, "
        "partial findings, rationale). Ephemeral reasoning doesn't "
        "belong here — use your normal response instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": (
                    "Task id. Required (may be your own task or "
                    "another's — comment threads are per-task)."
                ),
            },
            "body": {
                "type": "string",
                "description": "Markdown-supported comment body.",
            },
            "board": _board_schema_prop(),
        },
        "required": ["task_id", "body"],
    },
}

KANBAN_ATTACH_SCHEMA = {
    "name": "kanban_attach",
    "description": (
        "Attach a file to a task by passing its bytes inline (base64). "
        "Use for genuine file artifacts the next worker or a human should "
        "be able to download — generated reports, images, exports. The "
        "file is stored as a real attachment (not a comment link) under "
        "the task's attachments dir, capped at 25 MB. Prefer "
        "kanban_attach_url when you only have a URL."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "filename": {
                "type": "string",
                "description": (
                    "File name to store it under (e.g. 'report.pdf'). "
                    "Directory components are stripped; only the leaf is kept."
                ),
            },
            "content_base64": {
                "type": "string",
                "description": "The file contents, base64-encoded. Max 25 MB decoded.",
            },
            "content_type": {
                "type": "string",
                "description": "Optional MIME type (e.g. 'application/pdf').",
            },
            "board": _board_schema_prop(),
        },
        "required": ["filename", "content_base64"],
    },
}

KANBAN_ATTACH_URL_SCHEMA = {
    "name": "kanban_attach_url",
    "description": (
        "Attach a file to a task by URL — Hermes downloads it server-side "
        "and stores it as a real attachment (capped at 25 MB). Use when "
        "you have a link rather than the bytes. Only http/https URLs are "
        "accepted."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "url": {
                "type": "string",
                "description": "http(s) URL to fetch and store.",
            },
            "filename": {
                "type": "string",
                "description": (
                    "Optional name to store it under. Defaults to the URL "
                    "path's leaf component."
                ),
            },
            "content_type": {
                "type": "string",
                "description": (
                    "Optional MIME type override. Defaults to the "
                    "Content-Type the server returns."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": ["url"],
    },
}

KANBAN_ATTACHMENTS_SCHEMA = {
    "name": "kanban_attachments",
    "description": (
        "List the files attached to a task: id, filename, content_type, "
        "size, who uploaded it, and the absolute on-disk path you can read."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "board": _board_schema_prop(),
        },
        "required": [],
    },
}

KANBAN_CREATE_SCHEMA = {
    "name": "kanban_create",
    "description": (
        "Create a new kanban task, optionally as a child of the current "
        "one (pass the current task id in ``parents``). Used by "
        "orchestrator workers to fan out — decompose work into child "
        "tasks with specific assignees, link them into a pipeline, "
        "then complete your own task. The dispatcher picks up the new "
        "tasks on its next tick and spawns the assigned profiles."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short task title (required).",
            },
            "assignee": {
                "type": "string",
                "description": (
                    "Profile name that should execute this task "
                    "(e.g. 'researcher-a', 'reviewer', 'writer'). "
                    "Required — tasks without an assignee are never "
                    "dispatched."
                ),
            },
            "body": {
                "type": "string",
                "description": (
                    "Opening post: full spec, acceptance criteria, "
                    "links. The assigned worker reads this as part of "
                    "its context."
                ),
            },
            "parents": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Parent task ids. The new task stays in 'todo' "
                    "until every parent reaches 'done'; then it "
                    "auto-promotes to 'ready'. Typical fan-in: list "
                    "all the researcher task ids when creating a "
                    "synthesizer task."
                ),
            },
            "tenant": {
                "type": "string",
                "description": (
                    "Optional namespace for multi-project isolation. "
                    "Defaults to HERMES_TENANT env if set."
                ),
            },
            "priority": {
                "type": "integer",
                "description": (
                    "Dispatcher tiebreaker. Higher = picked sooner "
                    "when multiple ready tasks share an assignee."
                ),
            },
            "workspace_kind": {
                "type": "string",
                "enum": ["scratch", "dir", "worktree"],
                "description": (
                    "Workspace flavor: 'scratch' (fresh tmp dir, "
                    "default), 'dir' (shared directory, requires "
                    "absolute workspace_path), 'worktree' (git worktree)."
                ),
            },
            "workspace_path": {
                "type": "string",
                "description": (
                    "Absolute path for 'dir' or 'worktree' workspace. "
                    "Relative paths are rejected at dispatch."
                ),
            },
            "project": {
                "type": "string",
                "description": (
                    "Optional project id or slug to link the task to. When "
                    "set, the task becomes a git worktree under the project's "
                    "primary repo with a deterministic branch (project slug + "
                    "task id), instead of a random branch."
                ),
            },
            "triage": {
                "type": "boolean",
                "description": (
                    "If true, task lands in 'triage' instead of 'todo' "
                    "— a specifier profile is expected to flesh out "
                    "the body before work starts."
                ),
            },
            "idempotency_key": {
                "type": "string",
                "description": (
                    "If a non-archived task with this key already "
                    "exists, return that task's id instead of creating "
                    "a duplicate. Useful for retry-safe automation."
                ),
            },
            "max_runtime_seconds": {
                "type": "integer",
                "description": (
                    "Per-task runtime cap. When exceeded, the "
                    "dispatcher SIGTERMs the worker and re-queues the "
                    "task with outcome='timed_out'."
                ),
            },
            "initial_status": {
                "type": "string",
                "enum": ["running", "blocked"],
                "description": (
                    "Initial card status. Use 'blocked' for tasks that "
                    "require immediate human ops (R3 gate) to skip the "
                    "brief running-to-blocked transition. Defaults to "
                    "'running', which preserves the usual dispatch path."
                ),
            },
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Skill names to force-load into the dispatched "
                    "worker. The kanban lifecycle is already injected "
                    "automatically; use this to pin a task to a specialist "
                    "context — e.g. ['translation'] for a translation "
                    "task, ['github-code-review'] for a reviewer task. "
                    "The names must match skills installed on the "
                    "assignee's profile."
                ),
            },
            "goal_mode": {
                "type": "boolean",
                "description": (
                    "Run the dispatched worker in a goal loop. When true, "
                    "after each turn an auxiliary judge checks the worker's "
                    "response against this card's title/body; if the work "
                    "isn't done and budget remains, the worker keeps going "
                    "in the same session until the judge agrees it's "
                    "complete (or the goal-turn budget is exhausted, which "
                    "blocks the task for human review). Use this for "
                    "open-ended cards where one shot rarely finishes the "
                    "work. Defaults to false (classic single-shot worker)."
                ),
            },
            "goal_max_turns": {
                "type": "integer",
                "description": (
                    "Turn budget for goal_mode workers. Caps how many "
                    "continuation turns the worker may take before the task "
                    "is blocked for review. Ignored unless goal_mode is "
                    "true. Defaults to the goal-engine default (20)."
                ),
            },
            "board": _board_schema_prop(),
        },
        "required": ["title", "assignee"],
    },
}

KANBAN_UNBLOCK_SCHEMA = {
    "name": "kanban_unblock",
    "description": (
        "Unblock a Kanban task. It moves to ready when all parents are done, "
        "or todo while any parent remains open. Orchestrator-only — only "
        "profiles with the kanban toolset can unblock routed work; "
        "dispatcher-spawned task workers never see this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Blocked task id to move to ready or parent-gated todo.",
            },
            "board": _board_schema_prop(),
        },
        "required": ["task_id"],
    },
}

KANBAN_LINK_SCHEMA = {
    "name": "kanban_link",
    "description": (
        "Add a parent→child dependency edge after both tasks already "
        "exist. The child won't promote to 'ready' until all parents "
        "are 'done'. Cycles and self-links are rejected."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "parent_id": {"type": "string", "description": "Parent task id."},
            "child_id":  {"type": "string", "description": "Child task id."},
            "board": _board_schema_prop(),
        },
        "required": ["parent_id", "child_id"],
    },
}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="kanban_show",
    toolset="kanban",
    schema=KANBAN_SHOW_SCHEMA,
    handler=_handle_show,
    check_fn=_check_kanban_mode,
    emoji="📋",
)

registry.register(
    name="kanban_list",
    toolset="kanban",
    schema=KANBAN_LIST_SCHEMA,
    handler=_handle_list,
    check_fn=_check_kanban_orchestrator_mode,
    emoji="📋",
)

registry.register(
    name="kanban_prepare_planning",
    toolset="kanban",
    schema=KANBAN_PREPARE_PLANNING_SCHEMA,
    handler=_handle_prepare_planning,
    check_fn=_check_default_kanban_orchestrator_mode,
    emoji="📝",
)

registry.register(
    name="kanban_complete",
    toolset="kanban",
    schema=KANBAN_COMPLETE_SCHEMA,
    handler=_handle_complete,
    check_fn=_check_kanban_mode,
    emoji="✔",
)

registry.register(
    name="kanban_block",
    toolset="kanban",
    schema=KANBAN_BLOCK_SCHEMA,
    handler=_handle_block,
    check_fn=_check_kanban_mode,
    emoji="⏸",
)

registry.register(
    name="kanban_handoff",
    toolset="kanban",
    schema=KANBAN_HANDOFF_SCHEMA,
    handler=_handle_handoff,
    check_fn=_check_kanban_mode,
    emoji="🤝",
)

registry.register(
    name="kanban_heartbeat",
    toolset="kanban",
    schema=KANBAN_HEARTBEAT_SCHEMA,
    handler=_handle_heartbeat,
    check_fn=_check_kanban_mode,
    emoji="💓",
)

registry.register(
    name="kanban_comment",
    toolset="kanban",
    schema=KANBAN_COMMENT_SCHEMA,
    handler=_handle_comment,
    check_fn=_check_kanban_mode,
    emoji="💬",
)

registry.register(
    name="kanban_attach",
    toolset="kanban",
    schema=KANBAN_ATTACH_SCHEMA,
    handler=_handle_attach,
    check_fn=_check_kanban_mode,
    emoji="📎",
)

registry.register(
    name="kanban_attach_url",
    toolset="kanban",
    schema=KANBAN_ATTACH_URL_SCHEMA,
    handler=_handle_attach_url,
    check_fn=_check_kanban_mode,
    emoji="📎",
)

registry.register(
    name="kanban_attachments",
    toolset="kanban",
    schema=KANBAN_ATTACHMENTS_SCHEMA,
    handler=_handle_attachments,
    check_fn=_check_kanban_mode,
    emoji="📎",
)

registry.register(
    name="kanban_create",
    toolset="kanban",
    schema=KANBAN_CREATE_SCHEMA,
    handler=_handle_create,
    check_fn=_check_kanban_mode,
    emoji="➕",
)

registry.register(
    name="kanban_unblock",
    toolset="kanban",
    schema=KANBAN_UNBLOCK_SCHEMA,
    handler=_handle_unblock,
    check_fn=_check_kanban_orchestrator_mode,
    emoji="▶",
)

registry.register(
    name="kanban_link",
    toolset="kanban",
    schema=KANBAN_LINK_SCHEMA,
    handler=_handle_link,
    check_fn=_check_kanban_mode,
    emoji="🔗",
)
