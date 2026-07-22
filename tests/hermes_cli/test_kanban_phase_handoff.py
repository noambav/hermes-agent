from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


WORKFLOW = "human-gated-development-v1"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty Kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _claimed_planning_task(conn, *, title: str = "[Planning] Add audit logging"):
    parent = kb.create_task(conn, title="merged dependency", assignee="planner")
    assert kb.complete_task(conn, parent, summary="merged") is True
    child = kb.create_task(
        conn,
        title=title,
        body="Original planning request",
        assignee="planner",
        workspace_kind="dir",
        workspace_path="/tmp/project",
        parents=[parent],
    )
    conn.execute(
        "UPDATE tasks SET workflow_template_id = ?, current_step_key = ? WHERE id = ?",
        (WORKFLOW, "planning", child),
    )
    claimed = kb.claim_task(conn, child, claimer="planner:test")
    assert claimed is not None
    return parent, child, claimed.current_run_id


def test_planning_handoff_atomically_becomes_sticky_implementation_gate(kanban_home):
    with kb.connect() as conn:
        parent, task_id, run_id = _claimed_planning_task(conn)
        before_comments = kb.add_comment(
            conn, task_id, author="human", body="Keep the audit format stable"
        )

        result = kb.handoff_task(
            conn,
            task_id,
            to_step="implementation",
            body="## Implementation handoff\n\n- Planning commit: `abc123`",
            metadata={"planning_commit": "abc123"},
            transition_key="implementation:abc123",
            expected_run_id=run_id,
        )

        task = kb.get_task(conn, task_id)
        assert result.task_id == task_id
        assert result.from_step == "planning"
        assert result.to_step == "implementation"
        assert result.idempotent is False
        assert task is not None
        assert task.title == "[Implementation] Add audit logging"
        assert task.body.startswith("## Implementation handoff")
        assert task.assignee == "instructor"
        assert task.status == "blocked"
        assert task.workflow_template_id == WORKFLOW
        assert task.current_step_key == "implementation"
        assert task.current_run_id is None
        assert task.claim_lock is None
        assert task.block_kind is None
        assert task.block_recurrences == 0
        assert kb.parent_ids(conn, task_id) == [parent]
        assert kb.list_comments(conn, task_id)[0].id == before_comments

        run = kb.latest_run(conn, task_id)
        assert run is not None
        assert run.status == "blocked"
        assert run.outcome == "handed_off"
        assert run.metadata["planning_commit"] == "abc123"

        events = kb.list_events(conn, task_id)
        assert [event.kind for event in events[-2:]] == ["phase_handoff", "blocked"]
        assert events[-2].payload["from_step"] == "planning"
        assert events[-2].payload["to_step"] == "implementation"
        assert events[-2].payload["from_title"] == "[Planning] Add audit logging"
        assert events[-2].payload["from_body"] == "Original planning request"

        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, task_id).status == "blocked"

        retry = kb.handoff_task(
            conn,
            task_id,
            to_step="implementation",
            body="ignored on retry",
            metadata={"planning_commit": "abc123"},
            transition_key="implementation:abc123",
            expected_run_id=run_id,
        )
        assert retry.idempotent is True
        assert len(
            [
                event
                for event in kb.list_events(conn, task_id)
                if event.kind == "phase_handoff"
            ]
        ) == 1
        with pytest.raises(ValueError, match="different handoff"):
            kb.handoff_task(
                conn,
                task_id,
                to_step="implementation",
                body="conflicting retry",
                metadata={"planning_commit": "different"},
                transition_key="implementation:different",
                expected_run_id=run_id,
            )


def test_handoff_rejects_missing_source_prefix_without_mutation(kanban_home):
    with kb.connect() as conn:
        _parent, task_id, run_id = _claimed_planning_task(
            conn, title="Add audit logging"
        )
        before = kb.get_task(conn, task_id)

        with pytest.raises(ValueError, match=r"\[Planning\].*prefix"):
            kb.handoff_task(
                conn,
                task_id,
                to_step="implementation",
                body="generated body",
                metadata={"planning_commit": "abc123"},
                transition_key="implementation:abc123",
                expected_run_id=run_id,
            )

        after = kb.get_task(conn, task_id)
        assert after is not None and before is not None
        assert after.title == before.title
        assert after.body == before.body
        assert after.status == "running"
        assert after.assignee == "planner"
        assert after.current_step_key == "planning"
        assert after.current_run_id == run_id
        assert not [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "phase_handoff"
        ]


def test_staged_handoff_attachment_enforces_native_size_cap(
    kanban_home, tmp_path, monkeypatch
):
    with kb.connect() as conn:
        _, task_id, _ = _claimed_planning_task(conn)
        staged = tmp_path / "staged.md"
        staged.write_bytes(b"too large")
        monkeypatch.setattr(kb, "KANBAN_ATTACHMENT_MAX_BYTES", 1)

        with pytest.raises(kb.AttachmentTooLarge):
            with kb.write_txn(conn):
                kb.add_staged_attachment_in_txn(
                    conn,
                    task_id,
                    filename="staged.md",
                    staged_path=staged,
                    size=staged.stat().st_size,
                )

        assert staged.is_file()
        assert kb.list_attachments(conn, task_id) == []


def test_handoff_lifecycle_hook_observes_commit_and_fires_once(
    kanban_home, monkeypatch
):
    observed = []

    def capture(event, task_id, **fields):
        with kb.connect() as observer:
            task = kb.get_task(observer, task_id)
        observed.append((event, task, fields))

    monkeypatch.setattr(kb, "_fire_kanban_lifecycle_hook", capture)
    with kb.connect() as conn:
        _, task_id, run_id = _claimed_planning_task(conn)
        observed.clear()
        first = kb.handoff_task(
            conn,
            task_id,
            to_step="implementation",
            body="handoff body",
            metadata={"planning_commit": "abc123"},
            transition_key="implementation:abc123",
            expected_run_id=run_id,
        )
        retry = kb.handoff_task(
            conn,
            task_id,
            to_step="implementation",
            body="ignored retry",
            metadata={"planning_commit": "abc123"},
            transition_key="implementation:abc123",
            expected_run_id=run_id,
        )

    assert first.idempotent is False
    assert retry.idempotent is True
    assert len(observed) == 1
    event, task, fields = observed[0]
    assert event == "kanban_task_blocked"
    assert task is not None
    assert task.current_step_key == "implementation"
    assert task.status == "blocked"
    assert fields["run_id"] == first.run_id
    assert fields["assignee"] == "instructor"


def test_internal_handoff_mode_requires_active_transaction(
    kanban_home, monkeypatch
):
    hook_calls = []
    monkeypatch.setattr(
        kb,
        "_fire_kanban_lifecycle_hook",
        lambda *args, **kwargs: hook_calls.append((args, kwargs)),
    )
    with kb.connect() as conn:
        _, task_id, run_id = _claimed_planning_task(conn)
        hook_calls.clear()
        before = kb.get_task(conn, task_id)

        with pytest.raises(RuntimeError, match="active transaction"):
            kb.handoff_task(
                conn,
                task_id,
                to_step="implementation",
                body="handoff body",
                metadata={"planning_commit": "abc123"},
                transition_key="implementation:abc123",
                expected_run_id=run_id,
                _within_transaction=True,
            )

        after = kb.get_task(conn, task_id)
    assert before is not None and after is not None
    assert after.title == before.title
    assert after.status == before.status
    assert after.current_step_key == before.current_step_key
    assert after.current_run_id == before.current_run_id
    assert hook_calls == []


def test_direct_handoff_notifies_after_post_commit_validation_error(
    kanban_home, monkeypatch
):
    hook_calls = []
    monkeypatch.setattr(
        kb,
        "_fire_kanban_lifecycle_hook",
        lambda event, task_id, **fields: hook_calls.append(
            (event, task_id, fields)
        ),
    )
    with kb.connect() as conn:
        _, task_id, run_id = _claimed_planning_task(conn)
        hook_calls.clear()
        monkeypatch.setattr(
            kb,
            "_check_file_length_invariant",
            lambda current: (_ for _ in ()).throw(
                sqlite3.DatabaseError("post-commit validation failed")
            ),
        )

        with pytest.raises(sqlite3.DatabaseError, match="post-commit validation"):
            kb.handoff_task(
                conn,
                task_id,
                to_step="implementation",
                body="handoff body",
                metadata={"planning_commit": "abc123"},
                transition_key="implementation:abc123",
                expected_run_id=run_id,
            )

        task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.current_step_key == "implementation"
    assert task.status == "blocked"
    assert len(hook_calls) == 1
    assert hook_calls[0][0] == "kanban_task_blocked"
    assert hook_calls[0][1] == task_id


@pytest.mark.parametrize(
    "corruption",
    ["workflow", "status", "assignee", "event_scope"],
)
def test_idempotent_handoff_revalidates_workflow_scope(
    kanban_home, corruption
):
    with kb.connect() as conn:
        _, task_id, run_id = _claimed_planning_task(conn)
        kb.handoff_task(
            conn,
            task_id,
            to_step="implementation",
            body="handoff body",
            metadata={"planning_commit": "abc123"},
            transition_key="implementation:abc123",
            expected_run_id=run_id,
        )

        if corruption == "workflow":
            conn.execute(
                "UPDATE tasks SET workflow_template_id = 'unrelated-v1' WHERE id = ?",
                (task_id,),
            )
        elif corruption == "status":
            conn.execute(
                "UPDATE tasks SET status = 'ready' WHERE id = ?",
                (task_id,),
            )
        elif corruption == "assignee":
            conn.execute(
                "UPDATE tasks SET assignee = 'planner' WHERE id = ?",
                (task_id,),
            )
        else:
            event = conn.execute(
                "SELECT id, payload FROM task_events "
                "WHERE task_id = ? AND kind = 'phase_handoff' "
                "ORDER BY id DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            payload = json.loads(event["payload"])
            payload["workflow_template_id"] = "unrelated-v1"
            conn.execute(
                "UPDATE task_events SET payload = ? WHERE id = ?",
                (json.dumps(payload), event["id"]),
            )

        with pytest.raises(ValueError, match="idempotent handoff"):
            kb.handoff_task(
                conn,
                task_id,
                to_step="implementation",
                body="ignored retry",
                metadata={"planning_commit": "abc123"},
                transition_key="implementation:abc123",
                expected_run_id=run_id,
            )
