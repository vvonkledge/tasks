#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pydantic[email]>=2.0", "pyyaml>=6.0", "pytest>=8.0",
#                 "pytest-cov>=5.0"]
# ///
"""Test suite for tasks.py.  Run:  uv run test_tasks.py"""

import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
TASKS_PY = HERE / "tasks.py"

_spec = importlib.util.spec_from_file_location("tasks_mod", TASKS_PY)
tasks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tasks)
df = tasks.df


# ------------------------------------------------------------------ fixtures

@pytest.fixture
def workspace(tmp_path, monkeypatch):
    (tmp_path / "schema-tasks.yaml").write_text(tasks.CONTRACT)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tasks, "PROG", "tasks.py")
    return tmp_path


@pytest.fixture
def bare(tmp_path, monkeypatch):
    """A workspace with no contract yet."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tasks, "PROG", "tasks.py")
    return tmp_path


@pytest.fixture
def cli(capsys):
    def run(*argv):
        code = tasks.main(list(argv))
        cap = capsys.readouterr()
        return code, cap.out, cap.err
    return run


def task(rid, path=None):
    return tasks._open(SimpleNamespace(file=path)).get(rid)


def all_tasks(path=None):
    return tasks._open(SimpleNamespace(file=path)).load()[0]


def raw_append(tmp_path, store="tasks.jsonl", **fields):
    """Write a record straight into the log, bypassing the CLI's guards."""
    rec = {"id": "x", "title": "X", "status": "todo", "verify": "true",
           "verify_kind": "cmd", "deps": [], "context": [], "owner": None,
           "reason": None, "updated": tasks._now()}
    rec.update(fields)
    with open(tmp_path / store, "a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec["id"]


def ago(days=0, hours=0, minutes=0):
    return (datetime.now(UTC) - timedelta(days=days, hours=hours,
                                          minutes=minutes)).isoformat()


def seed(cli, title="Parse CLI flags", verify="exit 0"):
    code, out, _ = cli("add", title, "--verify", verify)
    assert code == 0, out
    return out.splitlines()[0].split(": ", 1)[1]


# ------------------------------------------------------------- bootstrap (§6)

class TestBootstrap:
    def test_no_contract_is_structured_not_a_traceback(self, bare, cli):
        code, out, err = cli("list")
        assert code == 1 and err == ""
        assert "NO_CONTRACT" in out and "tasks.py init" in out

    def test_the_home_view_answers_instead_of_erroring(self, bare, cli):
        """8/7: an error here would shout in every unrelated repository."""
        code, out, err = cli()
        assert code == 0 and err == ""
        assert "no task store in" in out and "tasks.py init" in out

    def test_init_creates_the_contract(self, bare, cli):
        code, out, _ = cli("init")
        assert code == 0 and "created" in out
        assert (bare / "schema-tasks.yaml").is_file()

    def test_init_is_idempotent(self, bare, cli):
        cli("init")
        code, out, _ = cli("init")
        assert code == 0 and "unchanged" in out

    def test_init_honours_an_alternate_store_name(self, bare, cli):
        code, _out, _ = cli("-f", "queue.jsonl", "init")
        assert code == 0 and (bare / "schema-queue.yaml").is_file()

    def test_the_generated_contract_is_loadable(self, bare, cli):
        cli("init")
        model, key = df.load_contract("schema-tasks.yaml")
        assert key == "id"
        assert set(model.model_fields) == {
            "id", "title", "status", "verify", "verify_kind", "deps",
            "context", "owner", "reason", "updated"}

    def test_a_corrupt_contract_is_reported_not_raised(self, bare, cli):
        (bare / "schema-tasks.yaml").write_text("fields:\n  id: {type: nope}\n")
        code, out, err = cli("list")
        assert code == 2 and err == ""
        assert "CONTRACT_ERROR" in out


# ----------------------------------------------------------------- slugs (§2)

class TestSlug:
    def test_derives_a_short_id_from_the_title(self):
        assert tasks._slug("Parse CLI flags into a typed struct", set()) == "parse-cli-flags"

    def test_never_ends_on_a_stopword(self):
        assert tasks._slug("Emit TOON for", set()) == "emit-toon"

    def test_a_natural_title_ending_in_a_stopword_loses_it(self):
        assert tasks._slug("Ship it", set()) == "ship"

    def test_an_all_stopword_title_keeps_one_word(self):
        assert tasks._slug("The a an", set()) == "the"

    def test_a_title_with_no_word_characters_still_yields_an_id(self):
        assert tasks._slug("!!! ???", set()) == "task"

    def test_a_leading_digit_gets_a_letter_prefix(self):
        assert tasks._slug("9 lives", set()) == "t-9-lives"

    def test_a_very_short_title_is_padded_to_the_pattern(self):
        assert tasks._slug("Go", set()) == "go-t"

    def test_collisions_take_a_numeric_suffix(self):
        assert tasks._slug("Parse CLI flags", {"parse-cli-flags"}) == "parse-cli-flags-2"
        taken = {"parse-cli-flags", "parse-cli-flags-2"}
        assert tasks._slug("Parse CLI flags", taken) == "parse-cli-flags-3"

    def test_every_derived_id_satisfies_the_contract_pattern(self, bare, cli):
        cli("init")
        model, _ = df.load_contract("schema-tasks.yaml")
        for title in ("Go", "9 lives", "!!! ???", "The a an",
                      "A" * 200, "Ünïcodé tïtlé", "  spaced  out  "):
            rid = tasks._slug(title, set())
            model.model_validate({"id": rid, "title": "t", "verify": "true",
                                  "updated": tasks._now()})

    def test_id_space_exhaustion_is_structured(self):
        taken = {"go-t"} | {f"go-t-{n}" for n in range(2, 1000)}
        with pytest.raises(df.AxiError) as e:
            tasks._slug("Go", taken)
        assert e.value.code == "ID_EXHAUSTED"


# ------------------------------------------------------------------- add (§6)

class TestAdd:
    def test_creates_a_ready_task(self, workspace, cli):
        code, out, _ = cli("add", "Parse CLI flags", "--verify", "exit 0")
        assert code == 0
        assert "id: parse-cli-flags" in out and "ready: true" in out
        assert task("parse-cli-flags").status == "todo"

    def test_defaults_are_materialised(self, workspace, cli):
        seed(cli)
        t = task("parse-cli-flags")
        assert t.verify_kind == "cmd" and t.deps == [] and t.context == []
        assert t.owner is None and t.reason is None

    def test_prose_marks_the_verify_kind(self, workspace, cli):
        cli("add", "Ship", "--verify", "ops confirms", "--prose")
        assert task("ship").verify_kind == "prose"

    def test_context_pointers_are_repeatable(self, workspace, cli):
        cli("add", "Ship", "--verify", "exit 0",
            "--context", "a.py", "--context", "b.py")
        assert task("ship").context == ["a.py", "b.py"]

    def test_a_task_with_a_pending_dep_is_not_ready(self, workspace, cli):
        seed(cli)
        code, out, _ = cli("add", "Ship", "--verify", "exit 0",
                           "--dep", "parse-cli-flags")
        assert code == 0 and "ready: false" in out

    def test_a_dep_that_is_already_done_leaves_it_ready(self, workspace, cli):
        seed(cli)
        cli("start", "parse-cli-flags", "--owner", "m1")
        cli("done", "parse-cli-flags")
        code, out, _ = cli("add", "Ship", "--verify", "exit 0",
                           "--dep", "parse-cli-flags")
        assert code == 0 and "ready: true" in out

    def test_an_unknown_dep_is_refused(self, workspace, cli):
        code, out, _ = cli("add", "Ship", "--verify", "exit 0", "--dep", "nope")
        assert code == 2 and "UNKNOWN_DEP" in out
        assert all_tasks() == {}

    def test_a_verify_is_required(self, workspace, cli):
        code, out, _ = cli("add", "Ship")
        assert code == 2 and "USAGE_ERROR" in out

    def test_two_tasks_with_the_same_title_get_distinct_ids(self, workspace, cli):
        assert seed(cli) == "parse-cli-flags"
        assert seed(cli) == "parse-cli-flags-2"


# ----------------------------------------------------- dispatch and ownership

class TestStart:
    def test_dispatch_sets_owner_and_status(self, workspace, cli):
        seed(cli)
        code, out, _ = cli("start", "parse-cli-flags", "--owner", "minion-3")
        assert code == 0 and "owner: minion-3" in out
        t = task("parse-cli-flags")
        assert t.status == "doing" and t.owner == "minion-3"

    def test_the_same_owner_restarting_is_a_no_op(self, workspace, cli):
        seed(cli)
        cli("start", "parse-cli-flags", "--owner", "minion-3")
        before = task("parse-cli-flags").updated
        code, out, _ = cli("start", "parse-cli-flags", "--owner", "minion-3")
        assert code == 0 and "unchanged" in out
        assert task("parse-cli-flags").updated == before      # no write at all

    def test_a_second_minion_cannot_take_an_in_flight_task(self, workspace, cli):
        seed(cli)
        cli("start", "parse-cli-flags", "--owner", "minion-3")
        code, out, _ = cli("start", "parse-cli-flags", "--owner", "minion-7")
        assert code == 1 and "TASK_IN_FLIGHT" in out and "minion-3" in out
        assert task("parse-cli-flags").owner == "minion-3"

    def test_force_overrides_a_live_owner(self, workspace, cli):
        seed(cli)
        cli("start", "parse-cli-flags", "--owner", "minion-3")
        code, _, _ = cli("start", "parse-cli-flags", "--owner", "minion-7", "--force")
        assert code == 0 and task("parse-cli-flags").owner == "minion-7"

    def test_unmet_deps_block_dispatch(self, workspace, cli):
        seed(cli)
        cli("add", "Ship", "--verify", "exit 0", "--dep", "parse-cli-flags")
        code, out, _ = cli("start", "ship", "--owner", "m1")
        assert code == 1 and "DEPS_UNMET" in out and "parse-cli-flags" in out
        assert task("ship").status == "todo"

    def test_force_overrides_unmet_deps(self, workspace, cli):
        seed(cli)
        cli("add", "Ship", "--verify", "exit 0", "--dep", "parse-cli-flags")
        code, _, _ = cli("start", "ship", "--owner", "m1", "--force")
        assert code == 0 and task("ship").status == "doing"

    def test_a_done_task_cannot_be_restarted(self, workspace, cli):
        seed(cli)
        cli("start", "parse-cli-flags", "--owner", "m1")
        cli("done", "parse-cli-flags")
        code, out, _ = cli("start", "parse-cli-flags", "--owner", "m2")
        assert code == 1 and "INVALID_TRANSITION" in out

    def test_dispatch_clears_a_stale_reason(self, workspace, cli):
        seed(cli)
        cli("start", "parse-cli-flags", "--owner", "m1")
        cli("reset", "parse-cli-flags", "--reason", "m1 crashed")
        assert task("parse-cli-flags").reason == "m1 crashed"
        cli("start", "parse-cli-flags", "--owner", "m2")
        assert task("parse-cli-flags").reason is None

    def test_owner_is_required(self, workspace, cli):
        seed(cli)
        code, out, _ = cli("start", "parse-cli-flags")
        assert code == 2 and "USAGE_ERROR" in out

    def test_an_unknown_task_is_not_found(self, workspace, cli):
        code, out, _ = cli("start", "nope", "--owner", "m1")
        assert code == 1 and "NOT_FOUND" in out


# --------------------------------------------------------- verification (§6)

class TestDone:
    def test_a_passing_verify_completes_the_task(self, workspace, cli):
        seed(cli, verify="exit 0")
        cli("start", "parse-cli-flags", "--owner", "m1")
        code, out, _ = cli("done", "parse-cli-flags")
        assert code == 0 and "verified: true" in out
        assert task("parse-cli-flags").status == "done"

    def test_a_failing_verify_refuses_and_reports(self, workspace, cli):
        seed(cli, verify="echo nope >&2; exit 3")
        cli("start", "parse-cli-flags", "--owner", "m1")
        code, out, _ = cli("done", "parse-cli-flags")
        assert code == 1 and "VERIFY_FAILED" in out
        assert "exit_code: 3" in out and "nope" in out
        assert task("parse-cli-flags").status == "doing"      # unchanged

    def test_verify_runs_in_the_store_directory_not_the_cwd(self, tmp_path, monkeypatch, cli):
        home = tmp_path / "repo"
        home.mkdir()
        (home / "schema-tasks.yaml").write_text(tasks.CONTRACT)
        (home / "marker").write_text("x")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        monkeypatch.setattr(tasks, "PROG", "tasks.py")
        store = str(home / "tasks.jsonl")
        cli("-f", store, "add", "Check marker", "--verify", "test -f marker")
        cli("-f", store, "start", "check-marker", "--owner", "m1")
        code, out, _ = cli("-f", store, "done", "check-marker")
        assert code == 0 and "verified: true" in out

    def test_a_second_done_does_not_rerun_verify(self, workspace, cli):
        seed(cli, verify="test ! -f ran && touch ran")
        cli("start", "parse-cli-flags", "--owner", "m1")
        assert cli("done", "parse-cli-flags")[0] == 0
        code, out, _ = cli("done", "parse-cli-flags")         # would fail if rerun
        assert code == 0 and "unchanged" in out

    def test_a_hanging_verify_is_killed(self, workspace, cli):
        seed(cli, verify="sleep 30")
        cli("start", "parse-cli-flags", "--owner", "m1")
        code, out, _ = cli("done", "parse-cli-flags", "--timeout", "1")
        assert code == 1 and "VERIFY_FAILED" in out and "timed out" in out

    def test_prose_requires_a_reason(self, workspace, cli):
        cli("add", "Ship", "--verify", "ops confirms the demo", "--prose")
        cli("start", "ship", "--owner", "m1")
        code, out, _ = cli("done", "ship")
        assert code == 2 and "REASON_REQUIRED" in out
        assert "ops confirms the demo" in out                 # the criterion
        assert task("ship").status == "doing"

    def test_prose_with_a_reason_records_an_assertion(self, workspace, cli):
        cli("add", "Ship", "--verify", "ops confirms", "--prose")
        cli("start", "ship", "--owner", "m1")
        code, out, _ = cli("done", "ship", "--reason", "ops signed off")
        assert code == 0 and "verified: asserted" in out
        assert task("ship").reason == "ops signed off"

    def test_force_without_a_reason_is_refused(self, workspace, cli):
        seed(cli, verify="exit 1")
        cli("start", "parse-cli-flags", "--owner", "m1")
        code, out, _ = cli("done", "parse-cli-flags", "--force")
        assert code == 2 and "REASON_REQUIRED" in out

    def test_force_records_why_and_marks_it_unverified(self, workspace, cli):
        seed(cli, verify="exit 1")
        cli("start", "parse-cli-flags", "--owner", "m1")
        code, out, _ = cli("done", "parse-cli-flags", "--force",
                           "--reason", "checked by hand")
        assert code == 0 and "verified: false" in out
        t = task("parse-cli-flags")
        assert t.status == "done" and t.reason == "checked by hand"

    def test_a_blocked_task_cannot_be_finished(self, workspace, cli):
        seed(cli)
        cli("start", "parse-cli-flags", "--owner", "m1")
        cli("block", "parse-cli-flags", "--reason", "stuck")
        code, out, _ = cli("done", "parse-cli-flags")
        assert code == 1 and "INVALID_TRANSITION" in out and "stuck" in out

    def test_a_todo_can_be_finished_without_dispatch(self, workspace, cli):
        seed(cli, verify="exit 0")
        code, _, _ = cli("done", "parse-cli-flags")
        assert code == 0 and task("parse-cli-flags").status == "done"

    def test_owner_survives_completion_for_the_record(self, workspace, cli):
        seed(cli)
        cli("start", "parse-cli-flags", "--owner", "minion-3")
        cli("done", "parse-cli-flags")
        assert task("parse-cli-flags").owner == "minion-3"


# ------------------------------------------------------------ block / unblock

class TestBlock:
    def test_block_records_the_reason(self, workspace, cli):
        seed(cli)
        cli("start", "parse-cli-flags", "--owner", "m1")
        code, _out, _ = cli("block", "parse-cli-flags", "--reason", "needs backoff")
        assert code == 0
        t = task("parse-cli-flags")
        assert t.status == "blocked" and t.reason == "needs backoff"

    def test_blocking_twice_with_the_same_reason_is_a_no_op(self, workspace, cli):
        seed(cli)
        cli("block", "parse-cli-flags", "--reason", "same")
        before = task("parse-cli-flags").updated
        code, out, _ = cli("block", "parse-cli-flags", "--reason", "same")
        assert code == 0 and "unchanged" in out
        assert task("parse-cli-flags").updated == before

    def test_a_new_reason_overwrites(self, workspace, cli):
        seed(cli)
        cli("block", "parse-cli-flags", "--reason", "first")
        cli("block", "parse-cli-flags", "--reason", "second")
        assert task("parse-cli-flags").reason == "second"

    def test_a_done_task_cannot_be_blocked(self, workspace, cli):
        seed(cli)
        cli("done", "parse-cli-flags")
        code, out, _ = cli("block", "parse-cli-flags", "--reason", "x")
        assert code == 1 and "INVALID_TRANSITION" in out

    def test_a_reason_is_required(self, workspace, cli):
        seed(cli)
        code, out, _ = cli("block", "parse-cli-flags")
        assert code == 2 and "USAGE_ERROR" in out

    def test_unblock_returns_it_to_the_queue_and_clears_the_reason(self, workspace, cli):
        seed(cli)
        cli("block", "parse-cli-flags", "--reason", "stuck")
        code, out, _ = cli("unblock", "parse-cli-flags")
        assert code == 0 and "ready: true" in out
        t = task("parse-cli-flags")
        assert t.status == "todo" and t.reason is None

    def test_unblock_reports_remaining_deps(self, workspace, cli):
        seed(cli)
        cli("add", "Ship", "--verify", "exit 0", "--dep", "parse-cli-flags")
        cli("block", "ship", "--reason", "stuck")
        code, out, _ = cli("unblock", "ship")
        assert code == 0 and "ready: false" in out

    def test_unblock_on_a_todo_is_a_no_op(self, workspace, cli):
        seed(cli)
        code, out, _ = cli("unblock", "parse-cli-flags")
        assert code == 0 and "unchanged" in out

    def test_unblock_on_an_in_flight_task_is_refused(self, workspace, cli):
        seed(cli)
        cli("start", "parse-cli-flags", "--owner", "m1")
        code, out, _ = cli("unblock", "parse-cli-flags")
        assert code == 1 and "INVALID_TRANSITION" in out


# ------------------------------------------------------------ orphan reaping

class TestReset:
    def test_reset_reclaims_an_in_flight_task(self, workspace, cli):
        seed(cli)
        cli("start", "parse-cli-flags", "--owner", "minion-3")
        code, out, _ = cli("reset", "parse-cli-flags", "--reason", "orchestrator died")
        assert code == 0 and "reclaimed_from: minion-3" in out
        t = task("parse-cli-flags")
        assert t.status == "todo" and t.owner is None

    def test_the_reset_reason_is_persisted(self, workspace, cli):
        seed(cli)
        cli("start", "parse-cli-flags", "--owner", "m1")
        cli("reset", "parse-cli-flags", "--reason", "m1 crashed")
        assert task("parse-cli-flags").reason == "m1 crashed"

    def test_reset_on_a_todo_is_a_no_op(self, workspace, cli):
        seed(cli)
        code, out, _ = cli("reset", "parse-cli-flags", "--reason", "x")
        assert code == 0 and "unchanged" in out

    def test_reset_never_discards_a_verified_result(self, workspace, cli):
        seed(cli)
        cli("done", "parse-cli-flags")
        code, out, _ = cli("reset", "parse-cli-flags", "--reason", "x")
        assert code == 1 and "INVALID_TRANSITION" in out
        assert task("parse-cli-flags").status == "done"

    def test_reset_also_clears_a_blocked_task(self, workspace, cli):
        seed(cli)
        cli("start", "parse-cli-flags", "--owner", "m1")
        cli("block", "parse-cli-flags", "--reason", "stuck")
        code, _, _ = cli("reset", "parse-cli-flags", "--reason", "requeue")
        assert code == 0 and task("parse-cli-flags").status == "todo"


# ------------------------------------------------------------- the dep graph

class TestDep:
    def test_adds_an_edge(self, workspace, cli):
        seed(cli)
        cli("add", "Ship", "--verify", "exit 0")
        code, _out, _ = cli("dep", "ship", "--on", "parse-cli-flags")
        assert code == 0 and task("ship").deps == ["parse-cli-flags"]

    def test_a_duplicate_edge_is_a_no_op(self, workspace, cli):
        seed(cli)
        cli("add", "Ship", "--verify", "exit 0", "--dep", "parse-cli-flags")
        before = task("ship").updated
        code, out, _ = cli("dep", "ship", "--on", "parse-cli-flags")
        assert code == 0 and "unchanged" in out
        assert task("ship").updated == before

    def test_a_cycle_is_refused_before_it_is_written(self, workspace, cli):
        seed(cli)
        cli("add", "Ship", "--verify", "exit 0", "--dep", "parse-cli-flags")
        code, out, _ = cli("dep", "parse-cli-flags", "--on", "ship")
        assert code == 2 and "DEP_CYCLE" in out
        assert task("parse-cli-flags").deps == []

    def test_a_longer_cycle_is_refused(self, workspace, cli):
        cli("add", "A", "--verify", "exit 0")
        cli("add", "B", "--verify", "exit 0", "--dep", "a-t")
        cli("add", "C", "--verify", "exit 0", "--dep", "b-t")
        code, out, _ = cli("dep", "a-t", "--on", "c-t")
        assert code == 2 and "DEP_CYCLE" in out

    def test_self_dependency_is_refused(self, workspace, cli):
        seed(cli)
        code, out, _ = cli("dep", "parse-cli-flags", "--on", "parse-cli-flags")
        assert code == 2 and "DEP_CYCLE" in out

    def test_an_unknown_target_is_refused(self, workspace, cli):
        seed(cli)
        code, out, _ = cli("dep", "parse-cli-flags", "--on", "nope")
        assert code == 2 and "UNKNOWN_DEP" in out

    def test_an_in_flight_task_is_read_only(self, workspace, cli):
        seed(cli)
        cli("add", "Ship", "--verify", "exit 0")
        cli("start", "ship", "--owner", "minion-3")
        code, out, _ = cli("dep", "ship", "--on", "parse-cli-flags")
        assert code == 1 and "TASK_IN_FLIGHT" in out
        assert task("ship").deps == []

    def test_force_edits_an_in_flight_task(self, workspace, cli):
        seed(cli)
        cli("add", "Ship", "--verify", "exit 0")
        cli("start", "ship", "--owner", "minion-3")
        code, _, _ = cli("dep", "ship", "--on", "parse-cli-flags", "--force")
        assert code == 0 and task("ship").deps == ["parse-cli-flags"]

    def test_a_dropped_dep_counts_as_satisfied(self, workspace, cli):
        seed(cli)
        cli("add", "Ship", "--verify", "exit 0", "--dep", "parse-cli-flags")
        cli("drop", "parse-cli-flags", "--reason", "superseded")
        code, out, _ = cli("show", "ship")
        assert code == 0 and "ready: true" in out


# -------------------------------------------------------------------- drop

class TestDrop:
    def test_drop_removes_the_task(self, workspace, cli):
        seed(cli)
        code, out, _ = cli("drop", "parse-cli-flags", "--reason", "superseded")
        assert code == 0 and "dropped" in out
        assert task("parse-cli-flags") is None

    def test_the_reason_survives_in_the_raw_log(self, workspace, cli):
        seed(cli)
        cli("drop", "parse-cli-flags", "--reason", "superseded by the stdlib")
        log = (workspace / "tasks.jsonl").read_text()
        assert "superseded by the stdlib" in log

    def test_dropping_an_absent_task_is_a_no_op(self, workspace, cli):
        code, out, _ = cli("drop", "nope", "--reason", "x")
        assert code == 0 and "absent" in out

    def test_an_in_flight_task_is_not_dropped(self, workspace, cli):
        seed(cli)
        cli("start", "parse-cli-flags", "--owner", "minion-3")
        code, out, _ = cli("drop", "parse-cli-flags", "--reason", "x")
        assert code == 1 and "TASK_IN_FLIGHT" in out
        assert task("parse-cli-flags") is not None

    def test_force_drops_an_in_flight_task(self, workspace, cli):
        seed(cli)
        cli("start", "parse-cli-flags", "--owner", "minion-3")
        code, _, _ = cli("drop", "parse-cli-flags", "--reason", "x", "--force")
        assert code == 0 and task("parse-cli-flags") is None


# ------------------------------------------------------- the home view (§4,5,8)

class TestHome:
    def test_an_empty_store_is_definitive(self, workspace, cli):
        code, out, _ = cli()
        assert code == 0 and "tasks: 0 of 0 total" in out
        assert "usage" not in out.lower()

    def test_in_flight_work_comes_before_the_ready_set(self, workspace, cli):
        seed(cli)
        cli("add", "Ship", "--verify", "exit 0")
        cli("start", "parse-cli-flags", "--owner", "minion-3")
        _code, out, _ = cli()
        assert out.index("doing[") < out.index("ready[")
        assert "minion-3" in out

    def test_counts_are_precomputed(self, workspace, cli):
        seed(cli)
        cli("add", "Ship", "--verify", "exit 0")
        cli("add", "Third", "--verify", "exit 0")
        cli("start", "ship", "--owner", "m1")
        cli("block", "third", "--reason", "stuck")
        _code, out, _ = cli()
        assert "counts[1]{doing,ready,todo,done,blocked,stale}" in out
        assert "1,1,1,0,1,0" in out

    def test_blocked_reasons_are_surfaced(self, workspace, cli):
        seed(cli)
        cli("block", "parse-cli-flags", "--reason", "needs a backoff helper")
        _code, out, _ = cli()
        assert "blocked[1]{id,reason}" in out and "needs a backoff helper" in out

    def test_the_ready_cap_is_revealed_as_a_hint_not_a_data_field(self, workspace, cli):
        """9: pagination belongs in help, never encoded into the TOON shape."""
        for n in range(tasks.HOME_LIMIT + 2):
            cli("add", f"Task number {n}", "--verify", "exit 0")
        _code, out, _ = cli()
        assert f"ready[{tasks.HOME_LIMIT}]" in out
        assert "ready_shown" not in out
        assert f"see all {tasks.HOME_LIMIT + 2} ready tasks" in out

    def test_nothing_queued_names_the_cause(self, workspace, cli):
        seed(cli)
        cli("start", "parse-cli-flags", "--owner", "m1")
        _code, out, _ = cli()
        assert "cause:" in out and "in flight" in out

    def test_everything_waiting_on_deps_names_the_cause(self, workspace, cli):
        seed(cli)
        cli("add", "Ship", "--verify", "exit 0", "--dep", "parse-cli-flags")
        cli("block", "parse-cli-flags", "--reason", "stuck")
        _code, out, _ = cli()
        assert "cause:" in out and "waiting on deps" in out

    def test_a_hand_edited_cycle_is_detected_and_named(self, workspace, cli):
        raw_append(workspace, id="cycle-a", deps=["cycle-b"])
        raw_append(workspace, id="cycle-b", deps=["cycle-a"])
        _code, out, _ = cli()
        assert "dependency cycle" in out
        assert "cycle[3]: cycle-a,cycle-b,cycle-a" in out
        assert "break the cycle" in out

    def test_stale_todos_are_counted(self, workspace, cli):
        seed(cli)
        raw_append(workspace, id="old-idea", updated=ago(days=tasks.STALE_DAYS + 1))
        _code, out, _ = cli()
        assert out.splitlines()[out.splitlines().index(
            "counts[1]{doing,ready,todo,done,blocked,stale}:") + 1].endswith(",1")

    def test_unreadable_lines_are_reported_not_fatal(self, workspace, cli):
        seed(cli)
        with open(workspace / "tasks.jsonl", "a") as f:
            f.write("{not json\n")
        code, out, _ = cli()
        assert code == 0 and "unreadable_lines: 1" in out

    def test_a_fully_done_queue_still_suggests_a_next_step(self, workspace, cli):
        seed(cli)
        cli("done", "parse-cli-flags")
        code, out, _ = cli()
        assert code == 0 and "to queue more work" in out

    def test_every_output_carries_a_next_step(self, workspace, cli):
        seed(cli)
        for argv in ((), ("list",), ("show", "parse-cli-flags")):
            code, out, _ = cli(*argv)
            assert code == 0 and "help[" in out


# ------------------------------------------------------------- list and show

class TestListShow:
    def test_list_is_a_minimal_schema(self, workspace, cli):
        seed(cli)
        _code, out, _ = cli("list")
        assert "tasks[1]{id,status,title}" in out

    def test_list_reports_the_total(self, workspace, cli):
        seed(cli)
        cli("add", "Ship", "--verify", "exit 0")
        _code, out, _ = cli("list", "--status", "todo")
        assert "count: 2 of 2 total" in out

    def test_status_filter(self, workspace, cli):
        seed(cli)
        cli("add", "Ship", "--verify", "exit 0")
        cli("block", "ship", "--reason", "stuck")
        _code, out, _ = cli("list", "--status", "blocked")
        assert "count: 1 of 2 total" in out and "ship" in out

    def test_stale_filter(self, workspace, cli):
        seed(cli)
        raw_append(workspace, id="old-idea", updated=ago(days=tasks.STALE_DAYS + 1))
        _code, out, _ = cli("list", "--stale")
        assert "count: 1 of 2 total" in out and "old-idea" in out

    def test_limit(self, workspace, cli):
        for n in range(5):
            cli("add", f"Task number {n}", "--verify", "exit 0")
        _code, out, _ = cli("list", "--limit", "2")
        assert "count: 2 of 5 total" in out

    def test_an_empty_filter_result_is_definitive(self, workspace, cli):
        seed(cli)
        code, out, _ = cli("list", "--status", "done")
        assert code == 0 and "count: 0 of 1 total" in out and "tasks[" not in out

    def test_list_reports_unreadable_lines(self, workspace, cli):
        seed(cli)
        with open("tasks.jsonl", "a") as f:
            f.write("{not json\n")
        _code, out, _ = cli("list")
        assert "unreadable_lines: 1" in out

    def test_an_invalid_status_is_a_usage_error(self, workspace, cli):
        code, out, _ = cli("list", "--status", "nonsense")
        assert code == 2 and "USAGE_ERROR" in out

    def test_show_returns_every_field(self, workspace, cli):
        seed(cli)
        _code, out, _ = cli("show", "parse-cli-flags")
        for f in ("id", "title", "status", "verify", "verify_kind", "deps",
                  "context", "owner", "reason", "updated"):
            assert f"{f}:" in out or f"{f}[" in out

    def test_show_lists_unmet_deps(self, workspace, cli):
        seed(cli)
        cli("add", "Ship", "--verify", "exit 0", "--dep", "parse-cli-flags")
        _code, out, _ = cli("show", "ship")
        assert "ready: false" in out and "unmet_deps[1]: parse-cli-flags" in out

    def test_show_truncates_a_long_verify(self, workspace, cli):
        long = "echo " + "x" * (df.DETAIL_TRUNCATE + 50)
        cli("add", "Long verify", "--verify", long)
        _code, out, _ = cli("show", "long-verify")
        assert "verify_length:" in out and "..." in out

    def test_full_defeats_truncation(self, workspace, cli):
        long = "echo " + "x" * (df.DETAIL_TRUNCATE + 50)
        cli("add", "Long verify", "--verify", long)
        _code, out, _ = cli("show", "long-verify", "--full")
        assert "verify_length:" not in out and long in out

    def test_show_on_an_unknown_id(self, workspace, cli):
        code, out, _ = cli("show", "nope")
        assert code == 1 and "NOT_FOUND" in out


# --------------------------------------------------------------- CLI surface

class TestSurface:
    def test_version_needs_no_store(self, bare, cli, capsys):
        code = tasks.main(["--version"])
        assert code == 0 and capsys.readouterr().out.strip() == tasks.VERSION

    def test_an_unknown_flag_fails_loud(self, workspace, cli):
        code, out, _ = cli("list", "--nope")
        assert code == 2 and "USAGE_ERROR" in out

    def test_flag_prefixes_are_not_accepted(self, workspace, cli):
        code, out, _ = cli("list", "--stal")
        assert code == 2 and "USAGE_ERROR" in out

    def test_a_usage_error_points_at_the_subcommand_help(self, workspace, cli):
        _code, out, _ = cli("start", "x")
        assert "tasks.py start --help" in out

    def test_nothing_is_ever_written_to_stderr(self, workspace, cli):
        seed(cli)
        for argv in ((), ("list",), ("show", "nope"), ("list", "--nope"),
                     ("start", "nope", "--owner", "m")):
            _, _, err = cli(*argv)
            assert err == ""

    def test_every_subcommand_has_an_example(self):
        _parser, reg = tasks.build_parser()
        for name, sp in reg.items():
            assert "tasks.py" in (sp.epilog or ""), name

    def test_an_io_error_is_structured(self, workspace, cli, monkeypatch):
        seed(cli)
        monkeypatch.setattr(df.Store, "load",
                            lambda *a, **k: (_ for _ in ()).throw(OSError(13, "denied")))
        code, out, err = cli("list")
        assert code == 1 and err == "" and "IO_ERROR" in out


# --------------------------------------------------------------- derivations

class TestDerivations:
    def test_age_formats_by_magnitude(self):
        now = datetime.now(UTC)
        assert tasks._age(now - timedelta(minutes=30)) == "30m"
        assert tasks._age(now - timedelta(hours=5)) == "5h"
        assert tasks._age(now - timedelta(days=3)) == "3d"

    def test_a_naive_timestamp_is_read_as_utc(self):
        naive = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2)
        assert tasks._age(naive) == "2h"
        assert 0.07 < tasks._days(naive) < 0.10

    def test_ready_excludes_everything_but_unblocked_todos(self, workspace, cli):
        seed(cli)
        cli("add", "Ship", "--verify", "exit 0", "--dep", "parse-cli-flags")
        cli("add", "Third", "--verify", "exit 0")
        cli("start", "third", "--owner", "m1")
        t = all_tasks()
        assert [x.id for x in tasks._ready(t)] == ["parse-cli-flags"]

    def test_no_cycle_returns_none(self, workspace, cli):
        seed(cli)
        cli("add", "Ship", "--verify", "exit 0", "--dep", "parse-cli-flags")
        assert tasks._cycle(all_tasks()) is None

    def test_a_dangling_dep_is_ignored_by_the_cycle_walk(self, workspace, cli):
        raw_append(workspace, id="lonely", deps=["ghost"])
        assert tasks._cycle(all_tasks()) is None


# ----------------------------------------------------------- multi-writer (§6)

class TestConcurrency:
    def test_two_minions_finishing_different_tasks_both_land(self, workspace):
        """The safety claim: distinct keys means no lost update, because every
        append is under datafile's exclusive flock and folds last-write-wins."""
        store = str(workspace / "tasks.jsonl")
        for n in range(2):
            tasks.main(["-f", store, "add", f"Task number {n}", "--verify", "exit 0"])
            tasks.main(["-f", store, "start", f"task-number-{n}", "--owner", f"m{n}"])
        procs = [subprocess.Popen(
            [sys.executable, str(TASKS_PY), "-f", store, "done", f"task-number-{n}"],
            cwd=str(workspace), stdout=subprocess.DEVNULL) for n in range(2)]
        for p in procs:
            assert p.wait(timeout=60) == 0
        after = all_tasks(store)
        assert [after[f"task-number-{n}"].status for n in range(2)] == ["done", "done"]

    def test_a_torn_tail_does_not_swallow_the_next_record(self, workspace, cli):
        seed(cli)
        with open(workspace / "tasks.jsonl", "a") as f:
            f.write('{"id":"torn"')                    # no newline
        cli("add", "Ship", "--verify", "exit 0")
        assert task("ship") is not None


# ------------------------------------------------- AXI conformance (§2,3,6,10)

class TestIdentity:
    def test_the_home_view_names_the_tool(self, workspace, cli):
        _code, out, _ = cli()
        assert "bin: " in out and "tasks.py" in out
        assert f"description: {tasks.DESCRIPTION}" in out
        assert f"version: {tasks.VERSION}" in out

    def test_the_home_path_collapses_the_user_home(self, workspace, cli):
        _code, out, _ = cli()
        bin_line = next(x for x in out.splitlines() if x.startswith("bin: "))
        assert str(Path.home()) not in bin_line

    def test_identity_precedes_the_live_data(self, workspace, cli):
        seed(cli)
        cli("start", "parse-cli-flags", "--owner", "m1")
        _code, out, _ = cli()
        assert out.index("bin: ") < out.index("doing[")

    def test_an_empty_store_is_still_identified(self, workspace, cli):
        _code, out, _ = cli()
        assert out.index("bin: ") < out.index("tasks: 0 of 0 total")

    def test_the_description_needs_no_toon_quoting(self):
        assert "," not in tasks.DESCRIPTION and ":" not in tasks.DESCRIPTION


class TestFields:
    def test_the_default_schema_stays_minimal(self, workspace, cli):
        seed(cli)
        _code, out, _ = cli("list")
        assert f"tasks[1]{{{','.join(tasks.LIST_FIELDS)}}}" in out

    def test_fields_selects_columns(self, workspace, cli):
        seed(cli)
        _code, out, _ = cli("list", "--fields", "id,owner")
        assert "tasks[1]{id,owner}" in out and "title" not in out

    def test_a_list_valued_field_does_not_break_the_table(self, workspace, cli):
        seed(cli)
        cli("add", "Ship", "--verify", "exit 0", "--dep", "parse-cli-flags")
        _code, out, _ = cli("list", "--fields", "id,deps")
        assert "tasks[2]{id,deps}" in out and "parse-cli-flags" in out

    def test_an_unknown_field_lists_the_valid_ones(self, workspace, cli):
        seed(cli)
        code, out, _ = cli("list", "--fields", "id,nope")
        assert code == 2 and "USAGE_ERROR" in out
        assert "nope" in out and "Valid fields:" in out and "verify_kind" in out

    def test_a_bad_field_is_rejected_before_the_log_is_read(self, workspace, cli, monkeypatch):
        seed(cli)
        monkeypatch.setattr(df.Store, "load",
                            lambda *a, **k: pytest.fail("read the log on a usage error"))
        assert cli("list", "--fields", "nope")[0] == 2


class TestTruncationHints:
    def test_show_offers_full_only_when_something_was_cut(self, workspace, cli):
        seed(cli)
        _code, out, _ = cli("show", "parse-cli-flags")
        assert "--full" not in out

    def test_show_offers_full_when_verify_is_cut(self, workspace, cli):
        cli("add", "Long", "--verify", "echo " + "x" * (df.DETAIL_TRUNCATE + 50))
        _code, out, _ = cli("show", "long")
        assert "verify_length:" in out and "show long --full" in out

    def test_a_truncated_verify_tail_reports_its_size(self, workspace, cli):
        cli("add", "Noisy", "--verify", "python3 -c \"print('x'*2000)\"; exit 1")
        code, out, _ = cli("done", "noisy")
        assert code == 1 and "output_length: 2000" in out

    def test_a_short_verify_tail_carries_no_size(self, workspace, cli):
        cli("add", "Quiet", "--verify", "echo small; exit 1")
        code, out, _ = cli("done", "quiet")
        assert code == 1 and "output_length" not in out

    def test_the_home_view_names_the_escape_hatch_when_it_clips(self, workspace, cli):
        seed(cli)
        cli("block", "parse-cli-flags", "--reason", "y" * (tasks.CELL + 50))
        _code, out, _ = cli()
        assert "untruncated" in out

    def test_list_points_at_show_when_it_clips(self, workspace, cli):
        cli("add", "T" * 110, "--verify", "exit 0")
        _code, out, _ = cli("list")
        assert "untruncated" in out

    def test_list_reveals_a_limit_cap(self, workspace, cli):
        for n in range(4):
            cli("add", f"Task number {n}", "--verify", "exit 0")
        _code, out, _ = cli("list", "--limit", "2")
        assert "list --limit 4` for all 4 matches" in out

    def test_no_cap_hint_when_everything_fits(self, workspace, cli):
        seed(cli)
        _code, out, _ = cli("list")
        assert "--limit" not in out


class TestSelfCorrectingErrors:
    def test_an_unknown_subcommand_flag_lists_that_subcommands_flags(self, workspace, cli):
        code, out, _ = cli("list", "--stat", "todo")
        assert code == 2
        assert "valid flags for `list`" in out
        for flag in ("--status", "--stale", "--limit", "--fields"):
            assert flag in out

    def test_globals_are_named_as_position_sensitive(self, workspace, cli):
        _code, out, _ = cli("list", "--stat")
        assert "globals, before the subcommand" in out

    def test_the_named_globals_actually_work_in_that_position(self, workspace, cli):
        """The hint would be worse than useless if the form it names failed."""
        assert cli("-f", "tasks.jsonl", "list")[0] == 0

    def test_an_unknown_global_flag_lists_the_commands(self, workspace, cli):
        code, out, _ = cli("--nope")
        assert code == 2 and "commands: " in out
        assert "start" in out and "unblock" in out

    def test_flag_suggestions_do_not_repeat_across_subcommands(self, workspace, cli):
        _code, out, _ = cli("start", "x", "--nope")
        assert "--owner" in out and "--status" not in out


# ------------------------------------------------ session integrations (§7)

@pytest.fixture
def sandbox_home(tmp_path, monkeypatch):
    """setup writes into ~; never let a test near the real one."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(tasks.df, "_path_alias", lambda me: None)   # deterministic
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tasks, "PROG", "tasks.py")
    return home


def claude_hooks(home):
    path = home / ".claude" / "settings.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return [h for g in (data.get("hooks") or {}).get("SessionStart") or []
            for h in g.get("hooks") or []]


class TestSetup:
    def test_it_installs_for_all_three_apps(self, sandbox_home, cli):
        code, out, _ = cli("setup")
        assert code == 0
        for app in ("claude", "codex", "opencode"):
            assert f"{app},installed" in out
        assert (sandbox_home / ".config" / "opencode" / "plugins"
                / f"axi-{tasks.TOOL_NAME}.js").is_file()

    def test_the_installed_command_carries_the_ambient_flag(self, sandbox_home, cli):
        cli("setup", "--app", "claude")
        hooks = claude_hooks(sandbox_home)
        assert len(hooks) == 1
        assert hooks[0]["command"].endswith(tasks.AMBIENT_FLAG)
        assert hooks[0]["timeout"] == tasks.HOOK_TIMEOUT

    def test_reinstalling_is_a_silent_no_op(self, sandbox_home, cli):
        cli("setup")
        _code, out, _ = cli("setup")
        for app in ("claude", "codex", "opencode"):
            assert f"{app},unchanged (no-op)" in out

    def test_a_moved_executable_is_repaired(self, sandbox_home, cli):
        cli("setup", "--app", "claude")
        path = sandbox_home / ".claude" / "settings.json"
        data = json.loads(path.read_text())
        data["hooks"]["SessionStart"][0]["hooks"][0]["command"] = (
            f"/old/tasks.py {tasks.AMBIENT_FLAG}")
        path.write_text(json.dumps(data))
        _code, out, _ = cli("setup", "--app", "claude")
        assert "claude,repaired" in out
        assert len(claude_hooks(sandbox_home)) == 1          # repaired, not doubled

    def test_a_stale_plugin_is_rewritten(self, sandbox_home, cli):
        cli("setup", "--app", "opencode")
        plugin = (sandbox_home / ".config" / "opencode" / "plugins"
                  / f"axi-{tasks.TOOL_NAME}.js")
        plugin.write_text(f"// {tasks.OPENCODE_PREFIX} {tasks.TOOL_NAME}\n// stale\n")
        _code, out, _ = cli("setup", "--app", "opencode")
        assert "opencode,repaired" in out and "runHomeView" in plugin.read_text()

    def test_uninstall_removes_every_target(self, sandbox_home, cli):
        cli("setup")
        _code, out, _ = cli("setup", "--uninstall")
        for app in ("claude", "codex", "opencode"):
            assert f"{app},removed" in out
        assert claude_hooks(sandbox_home) == []
        assert not (sandbox_home / ".config" / "opencode" / "plugins"
                    / f"axi-{tasks.TOOL_NAME}.js").exists()

    def test_uninstalling_twice_is_a_no_op(self, sandbox_home, cli):
        cli("setup")
        cli("setup", "--uninstall")
        _code, out, _ = cli("setup", "--uninstall")
        for app in ("claude", "codex", "opencode"):
            assert f"{app},already absent (no-op)" in out

    def test_another_tools_hook_is_never_touched(self, sandbox_home, cli):
        """The marker is what keeps two AXIs from evicting each other."""
        path = sandbox_home / ".claude" / "settings.json"
        path.parent.mkdir(parents=True)
        foreign = {"type": "command", "command": "uv run datafile.py", "timeout": 10}
        path.write_text(json.dumps(
            {"hooks": {"SessionStart": [{"matcher": "", "hooks": [foreign]}]}}))
        cli("setup", "--app", "claude")
        assert len(claude_hooks(sandbox_home)) == 2
        cli("setup", "--app", "claude", "--uninstall")
        assert claude_hooks(sandbox_home) == [foreign]

    def test_a_shared_hook_group_keeps_its_other_hooks(self, sandbox_home, cli):
        """Some agents put every SessionStart hook in one group; removing ours
        must trim that group rather than delete everyone else's."""
        path = sandbox_home / ".claude" / "settings.json"
        path.parent.mkdir(parents=True)
        foreign = {"type": "command", "command": "uv run datafile.py", "timeout": 10}
        ours = {"type": "command", "command": f"uv run tasks.py {tasks.AMBIENT_FLAG}",
                "timeout": tasks.HOOK_TIMEOUT}
        path.write_text(json.dumps(
            {"hooks": {"SessionStart": [{"matcher": "", "hooks": [foreign, ours]}]}}))
        _code, out, _ = cli("setup", "--app", "claude", "--uninstall")
        assert "claude,removed" in out
        assert claude_hooks(sandbox_home) == [foreign]

    def test_uninstall_ignores_a_group_of_only_foreign_hooks(self, sandbox_home, cli):
        path = sandbox_home / ".claude" / "settings.json"
        path.parent.mkdir(parents=True)
        foreign = {"type": "command", "command": "uv run datafile.py", "timeout": 10}
        path.write_text(json.dumps(
            {"hooks": {"SessionStart": [{"matcher": "", "hooks": [foreign]}]}}))
        _code, out, _ = cli("setup", "--app", "claude", "--uninstall")
        assert "claude,already absent (no-op)" in out
        assert claude_hooks(sandbox_home) == [foreign]

    def test_unrelated_settings_survive(self, sandbox_home, cli):
        path = sandbox_home / ".claude" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"model": "opus", "env": {"A": "1"}}))
        cli("setup", "--app", "claude")
        cli("setup", "--app", "claude", "--uninstall")
        data = json.loads(path.read_text())
        assert data["model"] == "opus" and data["env"] == {"A": "1"}
        assert "hooks" not in data                       # cleaned up after itself

    def test_codex_gets_the_feature_flag(self, sandbox_home, cli):
        _code, out, _ = cli("setup", "--app", "codex")
        cfg = sandbox_home / ".codex" / "config.toml"
        assert "hooks = true" in cfg.read_text() and "[features]" in cfg.read_text()
        assert "codex_hooks_feature: enabled" in out

    def test_the_feature_flag_is_only_written_once(self, sandbox_home, cli):
        cli("setup", "--app", "codex")
        _code, out, _ = cli("setup", "--app", "codex")
        assert "already enabled" in out
        cfg = (sandbox_home / ".codex" / "config.toml").read_text()
        assert cfg.count("hooks = true") == 1

    def test_status_reports_without_changing_anything(self, sandbox_home, cli):
        _code, out, _ = cli("setup", "--status")
        assert "claude,absent" in out
        assert not (sandbox_home / ".claude").exists()
        assert claude_hooks(sandbox_home) == []
        cli("setup")
        _code, out, _ = cli("setup", "--status")
        assert "claude,installed" in out

    def test_project_scope_stays_out_of_home(self, sandbox_home, cli, tmp_path):
        cli("setup", "--scope", "project", "--app", "claude")
        assert (tmp_path / ".claude" / "settings.json").is_file()
        assert not (sandbox_home / ".claude").exists()

    def test_a_missing_uv_is_structured(self, sandbox_home, cli, monkeypatch):
        monkeypatch.setattr(tasks.shutil, "which", lambda n: None)
        code, out, _ = cli("setup")
        assert code == 1 and "SETUP_ERROR" in out and "Install uv" in out

    def test_an_unreadable_settings_file_is_structured(self, sandbox_home, cli):
        path = sandbox_home / ".claude" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        code, out, _ = cli("setup", "--app", "claude")
        assert code == 1 and "SETUP_ERROR" in out


class TestAmbient:
    def test_it_prints_nothing_where_there_is_no_store(self, bare, cli):
        code, out, err = cli(tasks.AMBIENT_FLAG)
        assert code == 0 and out == "" and err == ""

    def test_it_prints_the_home_view_where_there_is_one(self, workspace, cli):
        seed(cli)
        code, out, _ = cli(tasks.AMBIENT_FLAG)
        assert code == 0 and "parse-cli-flags" in out and "bin: " in out

    def test_a_broken_store_degrades_instead_of_failing(self, workspace, cli):
        (workspace / "schema-tasks.yaml").write_text("fields:\n  id: {type: nope}\n")
        code, out, _ = cli(tasks.AMBIENT_FLAG)
        assert code == 0 and "AMBIENT_DEGRADED" in out

    def test_an_io_failure_never_breaks_a_session_start(self, workspace, cli, monkeypatch):
        seed(cli)
        monkeypatch.setattr(df.Store, "load",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
        assert cli(tasks.AMBIENT_FLAG)[0] == 0


class TestSkill:
    def test_it_generates_and_reports_its_size(self, workspace, cli):
        code, out, _ = cli("skill")
        assert code == 0 and "generated" in out and "lines:" in out
        assert Path(tasks.SKILL_PATH).is_file()

    def test_regenerating_is_a_no_op(self, workspace, cli):
        cli("skill")
        _code, out, _ = cli("skill")
        assert "unchanged (no-op)" in out

    def test_check_passes_on_a_fresh_skill(self, workspace, cli):
        cli("skill")
        assert cli("skill", "--check")[0] == 0

    def test_check_fails_when_it_drifts(self, workspace, cli):
        cli("skill")
        Path(tasks.SKILL_PATH).write_text("stale\n")
        code, out, _ = cli("skill", "--check")
        assert code == 1 and "SKILL_STALE" in out

    def test_check_fails_when_it_is_missing(self, workspace, cli):
        code, out, _ = cli("skill", "--check")
        assert code == 1 and "SKILL_STALE" in out and "does not exist" in out

    def test_it_names_every_command(self, workspace, cli):
        """The drift gate is only worth having if the skill covers the surface."""
        _parser, reg = tasks.build_parser()
        text = tasks.render_skill()
        for name in reg:
            assert f"### {name}" in text

    def test_it_carries_no_live_state(self, workspace, cli):
        seed(cli)
        cli("start", "parse-cli-flags", "--owner", "minion-3")
        text = tasks.render_skill()
        assert "parse-cli-flags" not in text and "minion-3" not in text

    def test_it_carries_no_machine_specific_path(self, workspace):
        """A skill with an absolute path would fail --check on another machine."""
        text = tasks.render_skill()
        assert str(Path.home()) not in text and str(HERE) not in text

    def test_install_and_uninstall_round_trip(self, sandbox_home, cli):
        _code, out, _ = cli("skill", "--install")
        for app in tasks.SKILL_APPS:
            assert f"{app},installed" in out
            assert Path(tasks._skill_path("user", app)).is_file()
        _code, out, _ = cli("skill", "--install")
        assert "unchanged (no-op)" in out
        _code, out, _ = cli("skill", "--uninstall")
        for app in tasks.SKILL_APPS:
            assert f"{app},removed" in out
            assert not Path(tasks._skill_path("user", app)).exists()

    def test_uninstalling_twice_is_a_no_op(self, sandbox_home, cli):
        _code, out, _ = cli("skill", "--uninstall")
        assert "already absent (no-op)" in out

    def test_an_installed_copy_is_repaired(self, sandbox_home, cli):
        cli("skill", "--install", "--app", "claude")
        path = Path(tasks._skill_path("user", "claude"))
        path.write_text(f"---\nname: {tasks.SKILL_NAME}\n---\nstale\n")
        _code, out, _ = cli("skill", "--install", "--app", "claude")
        assert "regenerated" in out and "## Ownership" in path.read_text()

    def test_a_foreign_skill_is_never_deleted(self, sandbox_home, cli):
        path = Path(tasks._skill_path("user", "claude"))
        path.parent.mkdir(parents=True)
        path.write_text("---\nname: someone-elses\n---\n")
        code, out, _ = cli("skill", "--uninstall", "--app", "claude")
        assert code == 1 and "SKILL_FOREIGN" in out and path.is_file()

    def test_out_and_install_conflict(self, workspace, cli):
        code, out, _ = cli("skill", "--out", "x.md", "--install")
        assert code == 2 and "USAGE_ERROR" in out

    def test_out_writes_where_it_is_told(self, workspace, cli):
        cli("skill", "--out", "custom.md")
        assert "# agent-tasks" in Path("custom.md").read_text()


class TestPiPackage:
    """pi has no command hook, so its ambient path is an extension shipped in a
    package - a different mechanism from `skill --app pi`, which is on-demand."""

    def test_it_writes_every_part_of_the_package(self, sandbox_home, cli):
        code, _out, _ = cli("pi-package")
        assert code == 0
        root = Path(tasks.PI_PACKAGE_DIR)
        for rel in ("package.json", "README.md",
                    "extensions/ambient-context.ts",
                    f"skills/{tasks.SKILL_NAME}/SKILL.md"):
            assert (root / rel).is_file(), rel

    def test_the_manifest_registers_both_entry_points(self, sandbox_home, cli):
        cli("pi-package")
        m = json.loads((Path(tasks.PI_PACKAGE_DIR) / "package.json").read_text())
        assert m["name"] == tasks.PI_PACKAGE_DIR and m["type"] == "module"
        assert m["pi"] == {"extensions": ["./extensions"], "skills": ["./skills"]}
        assert "pi-package" in m["keywords"]

    def test_the_extension_prefers_the_portable_name(self, sandbox_home, cli):
        cli("pi-package")
        ts = (Path(tasks.PI_PACKAGE_DIR) / "extensions" / "ambient-context.ts").read_text()
        cands = json.loads(ts.split("CANDIDATES: string[][] = ")[1].split(";")[0])
        assert cands[0] == [tasks.BIN_NAME, tasks.AMBIENT_FLAG]
        assert cands[1][-1] == tasks.AMBIENT_FLAG      # absolute fallback, same flag

    def test_the_extension_runs_ambient_not_the_bare_home_view(self, sandbox_home, cli):
        """Without the flag it would inject an error in every storeless repo."""
        cli("pi-package")
        ts = (Path(tasks.PI_PACKAGE_DIR) / "extensions" / "ambient-context.ts").read_text()
        assert 'pi.on("session_start"' in ts and 'pi.on("before_agent_start"' in ts
        assert ts.count(tasks.AMBIENT_FLAG) >= 2

    def test_the_extension_escapes_its_template_literal(self, sandbox_home, cli):
        cli("pi-package")
        ts = (Path(tasks.PI_PACKAGE_DIR) / "extensions" / "ambient-context.ts").read_text()
        assert r"`${event.systemPrompt}\n\n${HEADER}\n${ambient}`" in ts

    def test_the_bundled_skill_matches_the_generated_one(self, sandbox_home, cli):
        cli("pi-package")
        bundled = (Path(tasks.PI_PACKAGE_DIR) / "skills" / tasks.SKILL_NAME
                   / "SKILL.md").read_text()
        assert bundled == tasks.render_skill()

    def test_regenerating_is_a_no_op(self, sandbox_home, cli):
        cli("pi-package")
        _code, out, _ = cli("pi-package")
        assert "unchanged (no-op)" in out

    def test_check_passes_on_a_fresh_package(self, sandbox_home, cli):
        cli("pi-package")
        assert cli("pi-package", "--check")[0] == 0

    def test_check_names_the_stale_files(self, sandbox_home, cli):
        cli("pi-package")
        (Path(tasks.PI_PACKAGE_DIR) / "package.json").write_text("{}")
        code, out, _ = cli("pi-package", "--check")
        assert code == 1 and "PACKAGE_STALE" in out and "package.json" in out

    def test_check_fails_when_the_package_is_absent(self, sandbox_home, cli):
        code, out, _ = cli("pi-package", "--check")
        assert code == 1 and "PACKAGE_STALE" in out

    def test_out_writes_where_it_is_told(self, sandbox_home, cli):
        cli("pi-package", "--out", "custom-pkg")
        assert Path("custom-pkg/package.json").is_file()

    def test_a_missing_uv_is_structured(self, sandbox_home, cli, monkeypatch):
        monkeypatch.setattr(tasks.shutil, "which", lambda n: None)
        code, out, _ = cli("pi-package")
        assert code == 1 and "SETUP_ERROR" in out


# --------------------------------------------------- engine lookup and entry

class TestDatafileLookup:
    """tasks.py refuses to vendor a copy of datafile.py, so how it finds one is
    load-bearing: a wrong answer is an import-time crash, not a CLI error."""

    def _only(self, monkeypatch, target=None):
        real = tasks.os.path.isfile
        def isfile(p):
            if str(p).endswith("datafile.py"):
                return target is not None and Path(p).resolve() == Path(target).resolve()
            return real(p)
        monkeypatch.setattr(tasks.os.path, "isfile", isfile)

    def test_the_env_var_wins(self, tmp_path, monkeypatch):
        stub = tmp_path / "elsewhere.py"
        stub.write_text("")
        monkeypatch.setenv("TASKS_DATAFILE", str(stub))
        assert tasks._find_datafile() == str(stub)

    def test_a_sibling_is_preferred_over_the_path(self, monkeypatch):
        monkeypatch.delenv("TASKS_DATAFILE", raising=False)
        monkeypatch.setattr(tasks.shutil, "which", lambda n: "/nowhere/datafile")
        assert tasks._find_datafile().endswith("datafile.py")

    def test_the_path_is_the_last_resort(self, tmp_path, monkeypatch):
        stub = tmp_path / "datafile.py"
        stub.write_text("")
        monkeypatch.delenv("TASKS_DATAFILE", raising=False)
        self._only(monkeypatch, stub)
        monkeypatch.setattr(tasks.shutil, "which",
                            lambda n: str(stub) if n == "datafile" else None)
        assert Path(tasks._find_datafile()).resolve() == stub.resolve()

    def test_nothing_found_returns_empty(self, monkeypatch):
        monkeypatch.delenv("TASKS_DATAFILE", raising=False)
        self._only(monkeypatch, None)
        monkeypatch.setattr(tasks.shutil, "which", lambda n: None)
        assert tasks._find_datafile() == ""


class TestEntryPoints:
    SRC = TASKS_PY.read_text()

    def _exec(self, monkeypatch, argv, src=None, name="__main__"):
        monkeypatch.setattr(sys, "argv", argv)
        g = {"__name__": name, "__file__": str(TASKS_PY)}
        code = compile(src or self.SRC, str(TASKS_PY), "exec")
        with pytest.raises(SystemExit) as e:
            exec(code, g)
        return e.value.code

    def test_a_version_probe_answers_before_the_heavy_imports(self, monkeypatch, capsys):
        assert self._exec(monkeypatch, ["tasks.py", "--version"]) == 0
        assert capsys.readouterr().out.strip() == tasks.VERSION

    def test_a_missing_engine_is_a_structured_message(self, monkeypatch, capsys):
        src = self.SRC.replace("_DF = _find_datafile()", '_DF = ""')
        assert self._exec(monkeypatch, ["tasks.py"], src=src) == 1
        out = capsys.readouterr().out
        assert "DATAFILE_MISSING" in out and "TASKS_DATAFILE" in out

    def test_running_as_a_script_dispatches_to_main(self, workspace, monkeypatch, capsys):
        assert self._exec(monkeypatch, ["tasks.py", "list"]) == 0
        assert "count: 0 of 0 total" in capsys.readouterr().out

    def test_the_invoked_name_is_used_in_suggestions(self, workspace, monkeypatch, capsys):
        """Installed as `tasks`, every suggested command must be runnable as typed."""
        self._exec(monkeypatch, ["/usr/local/bin/tasks", "list"])
        assert "Run `tasks list`" in capsys.readouterr().out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", *sys.argv[1:]]))
