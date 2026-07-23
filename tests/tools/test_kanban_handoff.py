from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def planning_candidate(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "default@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Default Test"],
        check=True,
    )
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "base"],
        check=True,
        capture_output=True,
    )

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Prepare audit logging",
            body="Preserve me",
            workspace_kind="dir",
            workspace_path=str(repo),
            triage=True,
        )
    return {"task_id": task_id, "repo": repo}


def test_prepare_planning_tool_validates_git_and_prepares_same_card(planning_candidate):
    from tools import kanban_tools as kt

    out = kt._handle_prepare_planning({"task_id": planning_candidate["task_id"]})
    data = json.loads(out)

    assert data == {
        "ok": True,
        "task_id": planning_candidate["task_id"],
        "phase": "planning",
        "status": "ready",
        "assignee": "planner",
        "idempotent": False,
    }
    with kb.connect() as conn:
        task = kb.get_task(conn, planning_candidate["task_id"])
    assert task is not None
    assert task.title == "[Planning] Prepare audit logging"
    assert task.body == "Preserve me"
    assert task.workflow_template_id == kb.HUMAN_GATED_WORKFLOW_ID
    assert task.current_step_key == "planning"


def test_prepare_planning_retry_uses_durable_result_after_work_starts(
    planning_candidate,
):
    from tools import kanban_tools as kt

    first = json.loads(
        kt._handle_prepare_planning({"task_id": planning_candidate["task_id"]})
    )
    (planning_candidate["repo"] / "planner-notes.md").write_text(
        "work has started\n", encoding="utf-8"
    )
    retry = json.loads(
        kt._handle_prepare_planning({"task_id": planning_candidate["task_id"]})
    )

    assert first["ok"] is True
    assert retry["ok"] is True
    assert retry["idempotent"] is True


def test_prepare_planning_rejects_non_default_profile_without_env_hint(
    tmp_path, monkeypatch
):
    from tools import kanban_tools as kt

    profile_home = tmp_path / ".hermes" / "profiles" / "planner"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text("toolsets:\n  - kanban\n")
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    out = json.loads(kt._handle_prepare_planning({"task_id": "t_fake"}))
    assert "Default orchestrator" in out["error"]


def test_prepare_planning_rejects_workspace_change_during_transition(
    planning_candidate, monkeypatch
):
    from tools import kanban_tools as kt

    original = kb.prepare_planning_task

    def race_workspace(conn, task_id, **kwargs):
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE id = ?",
            ("/definitely/not/a/git/repo", task_id),
        )
        conn.commit()
        return original(conn, task_id, **kwargs)

    monkeypatch.setattr(kb, "prepare_planning_task", race_workspace)
    out = json.loads(
        kt._handle_prepare_planning({"task_id": planning_candidate["task_id"]})
    )

    assert "workspace changed" in out["error"]
    with kb.connect() as conn:
        task = kb.get_task(conn, planning_candidate["task_id"])
    assert task is not None
    assert task.status == "triage"
    assert task.workflow_template_id is None


def test_prepare_planning_tool_refuses_task_worker_without_mutation(
    planning_candidate, monkeypatch
):
    from tools import kanban_tools as kt

    monkeypatch.setenv("HERMES_KANBAN_TASK", planning_candidate["task_id"])
    out = json.loads(
        kt._handle_prepare_planning({"task_id": planning_candidate["task_id"]})
    )

    assert "orchestrator-only" in out["error"]
    with kb.connect() as conn:
        task = kb.get_task(conn, planning_candidate["task_id"])
    assert task is not None
    assert task.status == "triage"
    assert task.workflow_template_id is None


def test_prepare_planning_tool_rejects_non_git_workspace_before_mutation(
    planning_candidate
):
    from tools import kanban_tools as kt

    subprocess.run(
        ["rm", "-rf", str(planning_candidate["repo"] / ".git")],
        check=True,
    )
    out = json.loads(
        kt._handle_prepare_planning({"task_id": planning_candidate["task_id"]})
    )

    assert "Git" in out["error"] or "git" in out["error"]
    with kb.connect() as conn:
        task = kb.get_task(conn, planning_candidate["task_id"])
    assert task is not None
    assert task.status == "triage"
    assert task.workflow_template_id is None


@pytest.fixture
def planning_worker(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "planner@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Planner Test"], check=True)
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True)
    base_commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    spec_rel = "docs/superpowers/specs/2026-07-22-audit-design.md"
    plan_rel = "docs/superpowers/plans/2026-07-22-audit.md"
    spec = repo / spec_rel
    plan = repo / plan_rel
    spec.parent.mkdir(parents=True)
    plan.parent.mkdir(parents=True)
    spec.write_text("# Audit logging specification\n", encoding="utf-8")
    plan.write_text("# Audit logging implementation plan\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", spec_rel, plan_rel], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "docs: plan audit logging"],
        check=True,
        capture_output=True,
    )
    planning_commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="[Planning] Add audit logging",
            body="Original rough requirement",
            assignee="planner",
            workspace_kind="dir",
            workspace_path=str(repo),
        )
        conn.execute(
            "UPDATE tasks SET workflow_template_id=?, current_step_key=? WHERE id=?",
            (kb.HUMAN_GATED_WORKFLOW_ID, "planning", task_id),
        )
        claimed = kb.claim_task(conn, task_id, claimer="planner:test")
        assert claimed is not None and claimed.current_run_id is not None
        run_id = claimed.current_run_id

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_PROFILE", "planner")
    return {
        "task_id": task_id,
        "repo": repo,
        "base_commit": base_commit,
        "planning_commit": planning_commit,
        "spec_rel": spec_rel,
        "plan_rel": plan_rel,
    }


def test_handoff_tool_verifies_git_generates_body_and_attaches_artifacts(planning_worker):
    from tools import kanban_tools as kt

    out = kt._handle_handoff(
        {
            "to_phase": "implementation",
            "planning_commit": planning_worker["planning_commit"],
            "specification": planning_worker["spec_rel"],
            "plan": planning_worker["plan_rel"],
        }
    )
    data = json.loads(out)
    assert data["ok"] is True
    assert data["phase"] == "implementation"
    assert data["idempotent"] is False

    with kb.connect() as conn:
        task = kb.get_task(conn, planning_worker["task_id"])
        attachments = kb.list_attachments(conn, planning_worker["task_id"])
        run = kb.latest_run(conn, planning_worker["task_id"])

    assert task is not None
    assert task.title == "[Implementation] Add audit logging"
    assert task.status == "blocked"
    assert task.assignee == "instructor"
    assert len(attachments) == 2
    by_name = {item.filename: item for item in attachments}
    assert set(by_name) == {
        Path(planning_worker["spec_rel"]).name,
        Path(planning_worker["plan_rel"]).name,
    }

    spec_hash = hashlib.sha256(
        (planning_worker["repo"] / planning_worker["spec_rel"]).read_bytes()
    ).hexdigest()
    plan_hash = hashlib.sha256(
        (planning_worker["repo"] / planning_worker["plan_rel"]).read_bytes()
    ).hexdigest()
    expected_body = f"""## Implementation handoff

- Repository: `{planning_worker['repo']}`
- Worktree: `{planning_worker['repo']}`
- Branch: `main`
- Base commit: `{planning_worker['base_commit']}`
- Planning commit: `{planning_worker['planning_commit']}`

### Artifacts
- Specification: `{planning_worker['spec_rel']}`
  SHA-256: `{spec_hash}`
  Attachment: `{Path(planning_worker['spec_rel']).name}` (ID `1`)
- Plan: `{planning_worker['plan_rel']}`
  SHA-256: `{plan_hash}`
  Attachment: `{Path(planning_worker['plan_rel']).name}` (ID `2`)

### Dependencies
- Required completed cards: `none`

### Approval
`Blocked → Ready` authorizes Instructor to implement exactly the planning commit and artifact hashes above. Instructor must block on any mismatch, unresolved dependency, or scope expansion."""
    assert task.body == expected_body
    assert run is not None
    assert run.metadata["planning_commit"] == planning_worker["planning_commit"]
    assert run.metadata["specification"]["sha256"] == spec_hash
    assert run.metadata["plan"]["sha256"] == plan_hash


def test_handoff_retry_is_idempotent_without_duplicate_attachments(planning_worker):
    from tools import kanban_tools as kt

    args = {
        "to_phase": "implementation",
        "planning_commit": planning_worker["planning_commit"],
        "specification": planning_worker["spec_rel"],
        "plan": planning_worker["plan_rel"],
    }
    first = json.loads(kt._handle_handoff(args))
    (planning_worker["repo"] / "README.md").write_text(
        "changed after successful handoff\n", encoding="utf-8"
    )
    second = json.loads(kt._handle_handoff(args))

    assert first["ok"] is True
    assert first["idempotent"] is False
    assert second["ok"] is True
    assert second["idempotent"] is True
    assert second["attachment_ids"] == first["attachment_ids"]

    with kb.connect() as conn:
        attachments = kb.list_attachments(conn, planning_worker["task_id"])
        phase_events = [
            event
            for event in kb.list_events(conn, planning_worker["task_id"])
            if event.kind == "phase_handoff"
        ]
    assert len(attachments) == 2
    assert len(phase_events) == 1


def test_handoff_retry_normalizes_repository_relative_paths(planning_worker):
    from tools import kanban_tools as kt

    args = _handoff_args(planning_worker)
    args["specification"] = f"./{args['specification']}"
    args["plan"] = f"./{args['plan']}"

    first = json.loads(kt._handle_handoff(args))
    second = json.loads(kt._handle_handoff(args))

    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert second["attachment_ids"] == first["attachment_ids"]


def test_handoff_rejects_swapped_specification_and_plan_paths(planning_worker):
    from tools import kanban_tools as kt

    out = kt._handle_handoff(
        {
            "to_phase": "implementation",
            "planning_commit": planning_worker["planning_commit"],
            "specification": planning_worker["plan_rel"],
            "plan": planning_worker["spec_rel"],
        }
    )

    assert "specification must be under docs/superpowers/specs/" in out
    with kb.connect() as conn:
        task = kb.get_task(conn, planning_worker["task_id"])
        assert task is not None
        assert task.status == "running"
        assert task.current_step_key == "planning"
        assert kb.list_attachments(conn, planning_worker["task_id"]) == []


def test_handoff_rejects_scratch_workspace(planning_worker):
    from tools import kanban_tools as kt

    with kb.connect() as conn:
        conn.execute(
            "UPDATE tasks SET workspace_kind='scratch' WHERE id=?",
            (planning_worker["task_id"],),
        )

    out = kt._handle_handoff(
        {
            "to_phase": "implementation",
            "planning_commit": planning_worker["planning_commit"],
            "specification": planning_worker["spec_rel"],
            "plan": planning_worker["plan_rel"],
        }
    )

    assert "persistent Git workspace_kind 'dir' or 'worktree'" in out
    with kb.connect() as conn:
        task = kb.get_task(conn, planning_worker["task_id"])
        assert task is not None
        assert task.status == "running"
        assert task.current_step_key == "planning"
        assert kb.list_attachments(conn, planning_worker["task_id"]) == []


def _handoff_args(planning_worker, **overrides):
    args = {
        "to_phase": "implementation",
        "planning_commit": planning_worker["planning_commit"],
        "specification": planning_worker["spec_rel"],
        "plan": planning_worker["plan_rel"],
    }
    args.update(overrides)
    return args


def test_handoff_rejects_dirty_worktree_without_mutation(planning_worker):
    from tools import kanban_tools as kt

    (planning_worker["repo"] / "README.md").write_text("dirty\n", encoding="utf-8")
    out = kt._handle_handoff(_handoff_args(planning_worker))

    assert "requires a clean worktree" in out
    with kb.connect() as conn:
        task = kb.get_task(conn, planning_worker["task_id"])
        assert task is not None and task.status == "running"
        assert task.current_step_key == "planning"
        assert kb.list_attachments(conn, planning_worker["task_id"]) == []


def test_handoff_rejects_planning_commit_with_extra_file(planning_worker):
    from tools import kanban_tools as kt

    extra = planning_worker["repo"] / "product.py"
    extra.write_text("print('not planning')\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(planning_worker["repo"]), "add", "product.py"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(planning_worker["repo"]), "commit", "--amend", "--no-edit"],
        check=True,
        capture_output=True,
    )
    amended = subprocess.run(
        ["git", "-C", str(planning_worker["repo"]), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    out = kt._handle_handoff(
        _handoff_args(planning_worker, planning_commit=amended)
    )
    assert "must change exactly the specification and plan" in out
    assert "product.py" in out
    with kb.connect() as conn:
        task = kb.get_task(conn, planning_worker["task_id"])
        assert task is not None and task.status == "running"
        assert kb.list_attachments(conn, planning_worker["task_id"]) == []


def test_handoff_stale_run_rolls_back_attachment_writes(planning_worker, monkeypatch):
    from tools import kanban_tools as kt

    with kb.connect() as conn:
        original_task = kb.get_task(conn, planning_worker["task_id"])
        assert original_task is not None and original_task.current_run_id is not None
        original_events = kb.list_events(conn, planning_worker["task_id"])
        monkeypatch.setenv(
            "HERMES_KANBAN_RUN_ID", str(original_task.current_run_id + 1)
        )

    out = kt._handle_handoff(_handoff_args(planning_worker))
    assert "stale phase handoff run" in out
    with kb.connect() as conn:
        task = kb.get_task(conn, planning_worker["task_id"])
        assert task is not None
        assert task.title == original_task.title
        assert task.body == original_task.body
        assert task.status == original_task.status == "running"
        assert task.assignee == original_task.assignee == "planner"
        assert task.current_step_key == original_task.current_step_key == "planning"
        assert task.current_run_id == original_task.current_run_id
        assert kb.list_attachments(conn, planning_worker["task_id"]) == []
        assert kb.list_events(conn, planning_worker["task_id"]) == original_events

    attachment_dir = kb.task_attachments_dir(planning_worker["task_id"])
    assert not attachment_dir.exists() or list(attachment_dir.iterdir()) == []


def test_post_commit_validation_error_preserves_blobs_and_fires_hook(
    planning_worker, monkeypatch
):
    from tools import kanban_tools as kt

    active_kb, probe_conn = kt._connect()
    probe_conn.close()
    hook_calls = []
    monkeypatch.setattr(
        active_kb,
        "_fire_kanban_lifecycle_hook",
        lambda event, task_id, **fields: hook_calls.append(
            (event, task_id, fields)
        ),
    )
    monkeypatch.setattr(
        active_kb,
        "_check_file_length_invariant",
        lambda conn: (_ for _ in ()).throw(
            sqlite3.DatabaseError("post-commit validation failed")
        ),
    )

    out = kt._handle_handoff(_handoff_args(planning_worker))

    assert "post-commit validation failed" in out
    with active_kb.connect() as conn:
        task = active_kb.get_task(conn, planning_worker["task_id"])
        attachments = active_kb.list_attachments(conn, planning_worker["task_id"])
    assert task is not None
    assert task.current_step_key == "implementation"
    assert task.status == "blocked"
    assert len(attachments) == 2
    assert all(Path(attachment.stored_path).is_file() for attachment in attachments)
    assert len(hook_calls) == 1
    assert hook_calls[0][0] == "kanban_task_blocked"
    assert hook_calls[0][1] == planning_worker["task_id"]


def test_second_artifact_staging_failure_cleans_first_temp(
    planning_worker, monkeypatch
):
    from tools import kanban_tools as kt

    real_stage = kt._stage_handoff_blob
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("second staging failed")
        return real_stage(*args, **kwargs)

    monkeypatch.setattr(kt, "_stage_handoff_blob", fail_second)
    out = kt._handle_handoff(_handoff_args(planning_worker))

    assert "second staging failed" in out
    attachment_dir = kb.task_attachments_dir(planning_worker["task_id"])
    assert not attachment_dir.exists() or list(attachment_dir.iterdir()) == []


def test_handoff_accepts_git_worktree_workspace(planning_worker):
    from tools import kanban_tools as kt

    active_kb, probe_conn = kt._connect()
    probe_conn.close()
    with active_kb.connect() as conn:
        conn.execute(
            "UPDATE tasks SET workspace_kind = 'worktree' WHERE id = ?",
            (planning_worker["task_id"],),
        )

    result = json.loads(kt._handle_handoff(_handoff_args(planning_worker)))

    assert result["ok"] is True
    assert result["phase"] == "implementation"
    with active_kb.connect() as conn:
        task = active_kb.get_task(conn, planning_worker["task_id"])
        attachments = active_kb.list_attachments(conn, planning_worker["task_id"])
    assert task is not None
    assert task.workspace_kind == "worktree"
    assert task.current_step_key == "implementation"
    assert len(attachments) == 2


def test_handoff_rejects_workspace_change_after_git_preflight(
    planning_worker, tmp_path, monkeypatch
):
    from tools import kanban_tools as kt

    replacement_workspace = tmp_path / "replacement-repo"
    replacement_workspace.mkdir()
    real_stage = kt._stage_handoff_blob
    changed = False
    active_kb = None

    def change_workspace(module, *args, **kwargs):
        nonlocal changed, active_kb
        active_kb = module
        if not changed:
            changed = True
            with module.connect() as mutation_conn:
                mutation_conn.execute(
                    "UPDATE tasks SET workspace_path = ? WHERE id = ?",
                    (str(replacement_workspace), planning_worker["task_id"]),
                )
        return real_stage(module, *args, **kwargs)

    monkeypatch.setattr(kt, "_stage_handoff_blob", change_workspace)
    out = kt._handle_handoff(_handoff_args(planning_worker))

    assert "workspace changed during handoff" in out
    assert active_kb is not None
    with active_kb.connect() as conn:
        task = active_kb.get_task(conn, planning_worker["task_id"])
        attachments = active_kb.list_attachments(conn, planning_worker["task_id"])
    assert task is not None
    assert task.workspace_path == str(replacement_workspace)
    assert task.current_step_key == "planning"
    assert attachments == []


def test_stage_helper_removes_temp_when_write_fails(planning_worker, monkeypatch):
    from tools import kanban_tools as kt

    real_write_bytes = Path.write_bytes

    def fail_temp_write(path, data):
        if path.name.startswith(".handoff-"):
            raise OSError("temp write failed")
        return real_write_bytes(path, data)

    monkeypatch.setattr(Path, "write_bytes", fail_temp_write)
    with pytest.raises(OSError, match="temp write failed"):
        kt._stage_handoff_blob(
            kb,
            planning_worker["task_id"],
            b"artifact",
            board=None,
        )

    attachment_dir = kb.task_attachments_dir(planning_worker["task_id"])
    assert not attachment_dir.exists() or list(attachment_dir.iterdir()) == []


def test_task_scoped_handoff_rejects_board_override(planning_worker):
    from tools import kanban_tools as kt

    args = _handoff_args(planning_worker)
    args["board"] = "other-board"

    out = kt._handle_handoff(args)

    assert "must not include a board override" in out
    assert "board" not in kt.KANBAN_HANDOFF_SCHEMA["parameters"]["properties"]
    with kb.connect() as conn:
        task = kb.get_task(conn, planning_worker["task_id"])
        attachments = kb.list_attachments(conn, planning_worker["task_id"])
    assert task is not None
    assert task.current_step_key == "planning"
    assert attachments == []


def test_handoff_rejects_oversized_git_blob_before_reading(
    planning_worker, monkeypatch
):
    from tools import kanban_tools as kt

    active_kb, probe_conn = kt._connect()
    probe_conn.close()
    monkeypatch.setattr(active_kb, "KANBAN_ATTACHMENT_MAX_BYTES", 1)
    monkeypatch.setattr(
        kt,
        "_git_bytes",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("oversized blob must not be read")
        ),
    )

    out = kt._handle_handoff(_handoff_args(planning_worker))

    assert "exceeds the attachment size limit" in out
    assert "oversized blob must not be read" not in out
    with active_kb.connect() as conn:
        task = active_kb.get_task(conn, planning_worker["task_id"])
        attachments = active_kb.list_attachments(conn, planning_worker["task_id"])
    assert task is not None
    assert task.current_step_key == "planning"
    assert attachments == []


def test_handoff_rejects_non_planner_and_foreign_task(planning_worker, monkeypatch):
    from tools import kanban_tools as kt

    monkeypatch.setenv("HERMES_PROFILE", "instructor")
    out = kt._handle_handoff(_handoff_args(planning_worker))
    assert "requires profile 'planner'" in out

    monkeypatch.setenv("HERMES_PROFILE", "planner")
    with kb.connect() as conn:
        foreign = kb.create_task(conn, title="foreign", assignee="planner")
    out = kt._handle_handoff(
        _handoff_args(planning_worker, task_id=foreign)
    )
    assert "worker is scoped to task" in out
    assert "refusing to mutate" in out

    with kb.connect() as conn:
        task = kb.get_task(conn, planning_worker["task_id"])
        assert task is not None and task.status == "running"
        assert kb.list_attachments(conn, planning_worker["task_id"]) == []


def test_artifact_validator_rejects_symlink(planning_worker, tmp_path):
    from tools import kanban_tools as kt

    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    link = planning_worker["repo"] / "docs" / "linked.md"
    link.symlink_to(outside)

    with pytest.raises(ValueError, match="must not be a symlink"):
        kt._validated_repo_artifact(
            planning_worker["repo"], "docs/linked.md", label="specification"
        )


def test_concurrent_identical_handoff_keeps_only_winner_attachments(planning_worker):
    from tools import kanban_tools as kt

    start = threading.Barrier(2)

    def call_handoff():
        start.wait(timeout=5)
        return json.loads(kt._handle_handoff(_handoff_args(planning_worker)))

    with ThreadPoolExecutor(max_workers=2) as executor:
        outputs = list(executor.map(lambda _: call_handoff(), range(2)))

    assert all(out["ok"] is True for out in outputs)
    assert sorted(out["idempotent"] for out in outputs) == [False, True]
    with kb.connect() as conn:
        task = kb.get_task(conn, planning_worker["task_id"])
        attachments = kb.list_attachments(conn, planning_worker["task_id"])
        events = kb.list_events(conn, planning_worker["task_id"])
        handoff_run = kb.list_runs(
            conn,
            planning_worker["task_id"],
            include_active=False,
            state_type="outcome",
            state_name="handed_off",
        )[-1]
    winner_ids = [
        handoff_run.metadata["specification"]["attachment_id"],
        handoff_run.metadata["plan"]["attachment_id"],
    ]
    assert all(out["attachment_ids"] == winner_ids for out in outputs)
    assert [attachment.id for attachment in attachments] == winner_ids
    assert sum(event.kind == "phase_handoff" for event in events) == 1
    assert sum(
        event.kind == "blocked"
        and event.payload.get("kind") == "phase_approval"
        for event in events
    ) == 1
    assert f"(ID `{winner_ids[0]}`)" in task.body
    assert f"(ID `{winner_ids[1]}`)" in task.body


def test_retry_reuses_exact_orphaned_planner_attachments(planning_worker):
    from tools import kanban_tools as kt

    with kb.connect() as conn:
        orphan_ids = []
        for rel_path in (planning_worker["spec_rel"], planning_worker["plan_rel"]):
            source = planning_worker["repo"] / rel_path
            orphan_ids.append(
                kb.store_attachment_bytes(
                    conn,
                    planning_worker["task_id"],
                    source.name,
                    source.read_bytes(),
                    content_type="text/markdown",
                    uploaded_by="planner-handoff",
                )
            )

    out = json.loads(kt._handle_handoff(_handoff_args(planning_worker)))

    assert out["ok"] is True
    assert out["attachment_ids"] == orphan_ids
    with kb.connect() as conn:
        attachments = kb.list_attachments(conn, planning_worker["task_id"])
    assert [attachment.id for attachment in attachments] == orphan_ids


def test_handoff_reads_artifacts_from_declared_commit_not_mutable_worktree(
    planning_worker, monkeypatch
):
    from tools import kanban_tools as kt

    spec_path = planning_worker["repo"] / planning_worker["spec_rel"]
    committed_bytes = spec_path.read_bytes()
    real_git = kt._git
    mutated = False

    def git_with_race(repo, *args):
        nonlocal mutated
        result = real_git(repo, *args)
        if (
            not mutated
            and args[:2] == ("cat-file", "-e")
            and args[-1].endswith(planning_worker["plan_rel"])
        ):
            mutated = True
            spec_path.write_text("raced mutable bytes\n", encoding="utf-8")
        return result

    monkeypatch.setattr(kt, "_git", git_with_race)
    out = json.loads(kt._handle_handoff(_handoff_args(planning_worker)))

    assert out["ok"] is True
    with kb.connect() as conn:
        run = kb.list_runs(
            conn,
            planning_worker["task_id"],
            include_active=False,
            state_type="outcome",
            state_name="handed_off",
        )[-1]
        spec_attachment = kb.get_attachment(
            conn, run.metadata["specification"]["attachment_id"]
        )
    assert spec_attachment is not None
    assert Path(spec_attachment.stored_path).read_bytes() == committed_bytes
    assert run.metadata["specification"]["sha256"] == hashlib.sha256(
        committed_bytes
    ).hexdigest()


@pytest.fixture
def implementation_worker(planning_worker, monkeypatch, tmp_path):
    """A claimed Instructor run with a real Planning authorization and fake gh."""
    from tools import kanban_tools as kt

    planning = json.loads(kt._handle_handoff(_handoff_args(planning_worker)))
    assert planning["ok"] is True

    repo = planning_worker["repo"]
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            "https://github.com/NousResearch/hermes-agent.git",
        ],
        check=True,
    )
    with kb.connect() as conn:
        conn.execute(
            "UPDATE tasks SET branch_name = 'main' WHERE id = ?",
            (planning_worker["task_id"],),
        )
        assert kb.unblock_task(conn, planning_worker["task_id"]) is True
        claimed = kb.claim_task(
            conn, planning_worker["task_id"], claimer="instructor:test"
        )
        assert claimed is not None and claimed.current_run_id is not None
        run_id = claimed.current_run_id

    (repo / "audit.py").write_text("AUDIT_ENABLED = True\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "audit.py"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "feat: add audit logging"],
        check=True,
        capture_output=True,
    )
    implementation_head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "open(os.environ['GH_ARGV_LOG'], 'w').write('\\n'.join(sys.argv[1:]))\n"
        "if os.environ.get('GH_FAKE_ERROR'):\n"
        "    print(os.environ['GH_FAKE_ERROR'], file=sys.stderr)\n"
        "    raise SystemExit(1)\n"
        "print(os.environ['GH_FAKE_OUTPUT'])\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{__import__('os').environ['PATH']}")
    monkeypatch.setenv("GH_ARGV_LOG", str(tmp_path / "gh-argv"))
    monkeypatch.setenv("HERMES_PROFILE", "instructor")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    pr = {
        "number": 42,
        "url": "https://github.com/NousResearch/hermes-agent/pull/42",
        "state": "OPEN",
        "isDraft": False,
        "headRefName": "main",
        "headRefOid": implementation_head,
        "baseRefName": "main",
        "headRepository": {"nameWithOwner": "NousResearch/hermes-agent"},
    }
    monkeypatch.setenv("GH_FAKE_OUTPUT", json.dumps(pr))
    return {
        **planning_worker,
        "implementation_head": implementation_head,
        "run_id": run_id,
        "pr": pr,
        "gh_argv_log": tmp_path / "gh-argv",
    }


def _review_args(worker, **overrides):
    args = {
        "to_phase": "review",
        "pull_request": worker["pr"]["url"],
        "implementation_head": worker["implementation_head"],
        "base_branch": "main",
        "verification_commands": [
            {"command": "python -m pytest tests/tools/test_kanban_handoff.py -q", "exit_code": 0},
            {"command": "ruff check tools/kanban_tools.py", "exit_code": 0},
        ],
    }
    args.update(overrides)
    return args


def test_review_handoff_verifies_boundary_and_builds_compact_handoff(
    implementation_worker,
):
    from tools import kanban_tools as kt

    result = json.loads(kt._handle_handoff(_review_args(implementation_worker)))

    assert result == {
        "ok": True,
        "task_id": implementation_worker["task_id"],
        "phase": "review",
        "run_id": implementation_worker["run_id"],
        "idempotent": False,
        "pull_request": implementation_worker["pr"]["url"],
    }
    assert implementation_worker["gh_argv_log"].read_text().splitlines()[:3] == [
        "pr",
        "view",
        implementation_worker["pr"]["url"],
    ]
    with kb.connect() as conn:
        task = kb.get_task(conn, implementation_worker["task_id"])
        run = kb.latest_run(conn, implementation_worker["task_id"])
        events = kb.list_events(conn, implementation_worker["task_id"])
    assert task is not None and task.current_step_key == "review"
    assert task.status == "blocked" and task.assignee is None
    assert "Repository:" in task.body
    assert f"Worktree: `{implementation_worker['repo']}`" in task.body
    assert "Branch: `main`" in task.body
    assert "Base branch: `main`" in task.body
    assert f"Planning commit: `{implementation_worker['planning_commit']}`" in task.body
    assert f"Implementation head: `{implementation_worker['implementation_head']}`" in task.body
    assert "PR: `#42`" in task.body
    assert "Changed files: `1`" in task.body and "audit.py" in task.body
    assert "exit `0`" in task.body
    assert "Human review and merge required; Instructor cannot complete this card." in task.body
    assert run is not None
    assert run.metadata["planning_commit"] == implementation_worker["planning_commit"]
    assert run.metadata["implementation_head"] == implementation_worker["implementation_head"]
    assert run.metadata["pull_request"] == {
        "number": 42,
        "url": implementation_worker["pr"]["url"],
    }
    persisted = json.dumps({"metadata": run.metadata, "events": [e.payload for e in events]})
    assert "raw_output" not in persisted and "token" not in persisted


@pytest.mark.parametrize(
    ("missing", "message"),
    [
        ("pull_request", "pull_request is required"),
        ("implementation_head", "implementation_head is required"),
        ("base_branch", "base_branch is required"),
        ("verification_commands", "verification_commands are required"),
    ],
)
def test_review_handoff_requires_inputs_and_instructor_profile(
    implementation_worker, monkeypatch, missing, message
):
    from tools import kanban_tools as kt

    args = _review_args(implementation_worker)
    args.pop(missing)
    assert message in kt._handle_handoff(args)

    monkeypatch.setenv("HERMES_PROFILE", "planner")
    assert "requires profile 'instructor'" in kt._handle_handoff(
        _review_args(implementation_worker)
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("dirty", "clean worktree"),
        ("head", "must equal worktree HEAD"),
        ("base", "base branch"),
        ("attachment", "attachment hash"),
        ("artifact_changed", "planning artifact changed"),
    ],
)
def test_review_handoff_rejects_git_or_planning_authorization_mismatch(
    implementation_worker, monkeypatch, mutation, message
):
    from tools import kanban_tools as kt

    args = _review_args(implementation_worker)
    repo = implementation_worker["repo"]
    if mutation == "dirty":
        (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    elif mutation == "head":
        args["implementation_head"] = "f" * 40
    elif mutation == "base":
        args["base_branch"] = "release"
    elif mutation == "attachment":
        with kb.connect() as conn:
            planning_run = kb.list_runs(
                conn,
                implementation_worker["task_id"],
                include_active=False,
                state_type="outcome",
                state_name="handed_off",
            )[0]
            attachment = kb.get_attachment(
                conn, planning_run.metadata["specification"]["attachment_id"]
            )
        assert attachment is not None
        Path(attachment.stored_path).write_text("tampered\n", encoding="utf-8")
    else:
        spec = repo / implementation_worker["spec_rel"]
        spec.write_text("changed after planning\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", implementation_worker["spec_rel"]], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "change planning artifact"],
            check=True,
            capture_output=True,
        )
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        args["implementation_head"] = head
        pr = dict(implementation_worker["pr"], headRefOid=head)
        monkeypatch.setenv("GH_FAKE_OUTPUT", json.dumps(pr))

    assert message in kt._handle_handoff(args)


@pytest.mark.parametrize(
    ("pr_update", "message"),
    [
        ({"state": "CLOSED"}, "open pull request"),
        ({"isDraft": True}, "draft"),
        ({"headRefName": "other"}, "head branch"),
        ({"headRefOid": "e" * 40}, "head SHA"),
        ({"baseRefName": "release"}, "base branch"),
        ({"headRepository": {"nameWithOwner": "fork/hermes-agent"}}, "repository"),
    ],
)
def test_review_handoff_rejects_conflicting_pull_request(
    implementation_worker, monkeypatch, pr_update, message
):
    from tools import kanban_tools as kt

    pr = {**implementation_worker["pr"], **pr_update}
    monkeypatch.setenv("GH_FAKE_OUTPUT", json.dumps(pr))
    assert message in kt._handle_handoff(_review_args(implementation_worker))


def test_review_handoff_rejects_missing_or_ambiguous_pr_and_redacts_error(
    implementation_worker, monkeypatch
):
    from tools import kanban_tools as kt

    monkeypatch.setenv("GH_FAKE_OUTPUT", json.dumps([implementation_worker["pr"]] * 2))
    assert "exactly one pull request" in kt._handle_handoff(
        _review_args(implementation_worker)
    )

    token = "ghp_" + "FAKEGITHUBTOKEN12345678901234"
    monkeypatch.setenv("GH_FAKE_ERROR", f"gh auth token {token}")
    out = kt._handle_handoff(_review_args(implementation_worker))
    assert token not in out
    assert "gh pr view failed" in out


def test_review_handoff_retry_is_idempotent_and_conflict_fails(
    implementation_worker, monkeypatch
):
    from tools import kanban_tools as kt

    args = _review_args(implementation_worker)
    first = json.loads(kt._handle_handoff(args))
    monkeypatch.setenv("GH_FAKE_ERROR", "retry must not call gh")
    second = json.loads(kt._handle_handoff(args))
    assert first["idempotent"] is False
    assert second["idempotent"] is True

    conflict = _review_args(implementation_worker, base_branch="release")
    assert "different review handoff" in kt._handle_handoff(conflict)


def test_review_handoff_schema_exposes_conditional_review_inputs():
    from tools import kanban_tools as kt

    schema = kt.KANBAN_HANDOFF_SCHEMA["parameters"]
    assert schema["properties"]["to_phase"]["enum"] == ["implementation", "review"]
    assert set(schema["properties"]) >= {
        "pull_request",
        "implementation_head",
        "base_branch",
        "verification_commands",
    }
    assert schema["required"] == ["to_phase"]
