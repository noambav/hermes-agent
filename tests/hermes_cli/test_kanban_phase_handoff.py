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


def _review_metadata(**overrides):
    metadata = {
        "planning_commit": "a" * 40,
        "implementation_head": "b" * 40,
        "branch": "feat/audit-logging",
        "base_branch": "main",
        "pull_request": {
            "number": 42,
            "url": "https://github.com/NousResearch/hermes-agent/pull/42",
        },
        "verification_commands": [
            {"command": "python -m pytest tests/audit -q", "exit_code": 0}
        ],
        "verification_digest": "c" * 64,
        "publication_attempt_count": 1,
    }
    metadata.update(overrides)
    return metadata


def _claimed_implementation_task(conn):
    parent, task_id, planning_run_id = _claimed_planning_task(conn)
    conn.execute(
        "UPDATE tasks SET priority = 17, branch_name = ?, project_id = ? WHERE id = ?",
        ("feat/audit-logging", "hermes-agent", task_id),
    )
    kb.handoff_task(
        conn,
        task_id,
        to_step="implementation",
        body="## Implementation handoff\n\nPlanning authorization is durable.",
        metadata={"planning_commit": "a" * 40},
        transition_key=f"implementation:{'a' * 40}",
        expected_run_id=planning_run_id,
    )
    assert kb.unblock_task(conn, task_id) is True
    claimed = kb.claim_task(conn, task_id, claimer="instructor:test")
    assert claimed is not None
    return parent, task_id, claimed.current_run_id


def test_review_handoff_atomically_closes_instructor_run_and_preserves_card(
    kanban_home,
):
    with kb.connect() as conn:
        parent, task_id, run_id = _claimed_implementation_task(conn)
        comment_id = kb.add_comment(
            conn, task_id, author="human", body="Keep review on this card"
        )
        attachment_id = kb.add_attachment(
            conn,
            task_id,
            filename="plan.md",
            stored_path="/tmp/plan.md",
            size=12,
            uploaded_by="planner",
        )
        conn.execute(
            "UPDATE tasks SET worker_pid = 1234 WHERE id = ?",
            (task_id,),
        )
        conn.execute(
            "UPDATE task_runs SET worker_pid = 1234 WHERE id = ?",
            (run_id,),
        )
        implementation_body = kb.get_task(conn, task_id).body

        result = kb.handoff_task_to_review(
            conn,
            task_id,
            body="## Review handoff\n\nHuman review and merge required.",
            metadata=_review_metadata(),
            expected_run_id=run_id,
            expected_profile="instructor",
        )

        task = kb.get_task(conn, task_id)
        run = kb.latest_run(conn, task_id)
        assert result.task_id == task_id
        assert result.run_id == run_id
        assert result.idempotent is False
        assert task is not None
        assert task.title == "[Review] Add audit logging"
        assert task.body.startswith("## Review handoff")
        assert task.current_step_key == "review"
        assert task.status == "blocked"
        assert task.assignee is None
        assert task.current_run_id is None
        assert task.claim_lock is None
        assert task.claim_expires is None
        assert task.worker_pid is None
        assert task.workspace_kind == "dir"
        assert task.workspace_path == "/tmp/project"
        assert task.branch_name == "feat/audit-logging"
        assert task.project_id == "hermes-agent"
        assert task.priority == 17
        assert kb.parent_ids(conn, task_id) == [parent]
        assert kb.list_comments(conn, task_id)[0].id == comment_id
        assert kb.list_attachments(conn, task_id)[0].id == attachment_id
        assert run is not None
        assert run.status == "blocked"
        assert run.outcome == "handed_off"
        assert run.worker_pid is None
        assert run.metadata["implementation_body"] == implementation_body
        assert run.metadata["planning_commit"] == "a" * 40

        events = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "phase_handoff"
            and event.payload.get("to_step") == "review"
        ]
        assert len(events) == 1
        assert events[0].payload["from_body"] == implementation_body
        assert events[0].payload["handoff_id"] == result.handoff_id


@pytest.mark.parametrize(
    ("corruption", "match"),
    [
        ("stale_run", "stale"),
        ("wrong_profile", "profile"),
        ("wrong_phase", "transition"),
        ("wrong_workflow", "workflow"),
        ("missing_planning_authorization", "planning_commit"),
        ("branch_mismatch", "branch"),
    ],
)
def test_review_handoff_rejects_invalid_authority_without_mutation(
    kanban_home, corruption, match
):
    with kb.connect() as conn:
        _, task_id, run_id = _claimed_implementation_task(conn)
        expected_run_id = run_id
        metadata = _review_metadata()
        if corruption == "stale_run":
            expected_run_id += 1
        elif corruption == "wrong_profile":
            conn.execute("UPDATE task_runs SET profile = 'planner' WHERE id = ?", (run_id,))
        elif corruption == "wrong_phase":
            conn.execute("UPDATE tasks SET current_step_key = 'planning' WHERE id = ?", (task_id,))
        elif corruption == "wrong_workflow":
            conn.execute(
                "UPDATE tasks SET workflow_template_id = 'other-v1' WHERE id = ?",
                (task_id,),
            )
        elif corruption == "missing_planning_authorization":
            metadata.pop("planning_commit")
        else:
            metadata["branch"] = "feat/other"
        before = kb.get_task(conn, task_id)
        before_run = kb.latest_run(conn, task_id)

        with pytest.raises(ValueError, match=match):
            kb.handoff_task_to_review(
                conn,
                task_id,
                body="review body",
                metadata=metadata,
                expected_run_id=expected_run_id,
                expected_profile="instructor",
            )

        after = kb.get_task(conn, task_id)
        after_run = kb.latest_run(conn, task_id)
        assert before is not None and after is not None
        assert after.title == before.title
        assert after.body == before.body
        assert after.status == before.status
        assert after.current_step_key == before.current_step_key
        assert after.current_run_id == before.current_run_id
        assert before_run is not None and after_run is not None
        assert after_run.status == before_run.status
        assert after_run.ended_at == before_run.ended_at
        assert not [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "phase_handoff"
            and event.payload.get("to_step") == "review"
        ]


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("pull_request", {"number": 43, "url": "https://example.test/pull/43"}),
        ("implementation_head", "d" * 40),
        ("branch", "feat/other"),
        ("verification_digest", "e" * 64),
    ],
)
def test_review_handoff_retry_is_idempotent_and_rejects_conflicts(
    kanban_home, field, replacement
):
    with kb.connect() as conn:
        _, task_id, run_id = _claimed_implementation_task(conn)
        metadata = _review_metadata()
        first = kb.handoff_task_to_review(
            conn,
            task_id,
            body="review body",
            metadata=metadata,
            expected_run_id=run_id,
            expected_profile="instructor",
        )
        retry = kb.handoff_task_to_review(
            conn,
            task_id,
            body="ignored after durable success",
            metadata=metadata,
            expected_run_id=run_id,
            expected_profile="instructor",
        )
        assert retry.idempotent is True
        assert retry.handoff_id == first.handoff_id
        assert len(
            [
                event
                for event in kb.list_events(conn, task_id)
                if event.kind == "phase_handoff"
                and event.payload.get("to_step") == "review"
            ]
        ) == 1

        conflict = _review_metadata()
        conflict[field] = replacement
        with pytest.raises(ValueError, match="different review handoff"):
            kb.handoff_task_to_review(
                conn,
                task_id,
                body="conflicting retry",
                metadata=conflict,
                expected_run_id=run_id,
                expected_profile="instructor",
            )


def test_review_gate_is_not_promoted_by_dispatcher_automation(kanban_home):
    with kb.connect() as conn:
        _, task_id, run_id = _claimed_implementation_task(conn)
        kb.handoff_task_to_review(
            conn,
            task_id,
            body="review body",
            metadata=_review_metadata(),
            expected_run_id=run_id,
            expected_profile="instructor",
        )
        child_id = kb.create_task(
            conn,
            title="Wait for human review",
            assignee="worker",
            parents=[task_id],
        )

        assert kb.recompute_ready(conn) == 0
        task = kb.get_task(conn, task_id)
        child = kb.get_task(conn, child_id)
        assert task is not None
        assert task.status == "blocked"
        assert task.assignee is None
        assert child is not None and child.status == "todo"
        assert kb.claim_task(conn, task_id, claimer="dispatcher:test") is None


def test_prepare_planning_reuses_card_and_preserves_durable_state(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="merged dependency", assignee="planner")
        assert kb.complete_task(conn, parent, summary="merged") is True
        task_id = kb.create_task(
            conn,
            title="Add audit logging",
            body="Preserve this exact request",
            priority=17,
            workspace_kind="dir",
            workspace_path="/tmp/project",
            parents=[parent],
            triage=True,
            created_by="human",
        )
        comment_id = kb.add_comment(
            conn, task_id, author="human", body="Keep the format stable"
        )

        result = kb.prepare_planning_task(
            conn,
            task_id,
            actor="default",
            expected_workspace_kind="dir",
            expected_workspace_path="/tmp/project",
        )

        task = kb.get_task(conn, task_id)
        assert result.task_id == task_id
        assert result.status == "ready"
        assert result.idempotent is False
        assert task is not None
        assert task.id == task_id
        assert task.title == "[Planning] Add audit logging"
        assert task.body == "Preserve this exact request"
        assert task.assignee == "planner"
        assert task.status == "ready"
        assert task.priority == 17
        assert task.workspace_kind == "dir"
        assert task.workspace_path == "/tmp/project"
        assert task.workflow_template_id == WORKFLOW
        assert task.current_step_key == "planning"
        assert kb.parent_ids(conn, task_id) == [parent]
        assert kb.list_comments(conn, task_id)[0].id == comment_id

        events = [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "planning_prepared"
        ]
        assert len(events) == 1
        assert events[0].payload == {
            "actor": "default",
            "workflow_template_id": WORKFLOW,
            "step": "planning",
            "from_status": "triage",
            "to_status": "ready",
            "from_title": "Add audit logging",
            "to_title": "[Planning] Add audit logging",
            "assignee": "planner",
        }

        retry = kb.prepare_planning_task(
            conn,
            task_id,
            actor="default",
            expected_workspace_kind="dir",
            expected_workspace_path="/tmp/project",
        )
        assert retry.idempotent is True
        assert retry.status == "ready"
        assert len(
            [
                event
                for event in kb.list_events(conn, task_id)
                if event.kind == "planning_prepared"
            ]
        ) == 1


def test_prepare_planning_keeps_open_dependency_in_todo(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="open dependency", assignee="planner")
        task_id = kb.create_task(
            conn,
            title="Dependent feature",
            workspace_kind="worktree",
            workspace_path="/tmp/project-worktree",
            parents=[parent],
            triage=True,
        )

        result = kb.prepare_planning_task(
            conn,
            task_id,
            actor="default",
            expected_workspace_kind="worktree",
            expected_workspace_path="/tmp/project-worktree",
        )

        task = kb.get_task(conn, task_id)
        assert result.status == "todo"
        assert task is not None
        assert task.status == "todo"
        assert task.assignee == "planner"
        assert task.current_step_key == "planning"


@pytest.mark.parametrize(
    ("title", "workspace_kind", "workspace_path", "match"),
    [
        ("Feature", "scratch", None, "persistent workspace"),
        ("[Implementation] Feature", "dir", "/tmp/project", "phase prefix"),
    ],
)
def test_prepare_planning_rejects_invalid_source_without_mutation(
    kanban_home, title, workspace_kind, workspace_path, match
):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title=title,
            body="unchanged",
            workspace_kind=workspace_kind,
            workspace_path=workspace_path,
            triage=True,
        )
        before = kb.get_task(conn, task_id)

        with pytest.raises(ValueError, match=match):
            kb.prepare_planning_task(
                conn,
                task_id,
                actor="default",
                expected_workspace_kind=workspace_kind,
                expected_workspace_path=workspace_path,
            )

        after = kb.get_task(conn, task_id)
        assert before is not None and after is not None
        assert after.title == before.title
        assert after.body == before.body
        assert after.status == "triage"
        assert after.assignee == before.assignee
        assert after.workflow_template_id is None
        assert after.current_step_key is None
        assert not [
            event
            for event in kb.list_events(conn, task_id)
            if event.kind == "planning_prepared"
        ]


def test_planning_card_requires_archived_parent_to_reach_done(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="archived dependency", assignee="planner")
        assert kb.archive_task(conn, parent) is True
        task_id = kb.create_task(
            conn,
            title="Dependent feature",
            workspace_kind="dir",
            workspace_path="/tmp/project",
            parents=[parent],
            triage=True,
        )
        result = kb.prepare_planning_task(
            conn,
            task_id,
            actor="default",
            expected_workspace_kind="dir",
            expected_workspace_path="/tmp/project",
        )
        assert result.status == "todo"

        assert kb.recompute_ready(conn) == 0
        promoted, reason = kb.promote_task(
            conn, task_id, actor="default", reason="should remain gated"
        )
        assert promoted is False
        assert reason is not None and parent in reason

        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
        assert kb.claim_task(conn, task_id, claimer="planner:test") is None
        task = kb.get_task(conn, task_id)
        assert task is not None and task.status == "todo"


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
