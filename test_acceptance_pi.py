#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pytest>=8.0"]
# ///
"""Agent-in-the-loop acceptance tests for tasks.py, driven by pi.

These are not unit tests.  test_tasks.py calls `tasks.main()` in process and
proves the CLI is correct; these prove the *interface* is usable by an agent
that has never seen it, which is the claim the README makes under "AXI".  They
spend live tokens and are nondeterministic, so they are opt in and are not part
of the CI job:

    TASKS_ACCEPTANCE=1 uv run test_acceptance_pi.py -q -s

Each scenario asserts two things:

  * The store.  `tasks.jsonl` is the only oracle.  The agent's prose is never
    matched, because no correct run has a fixed wording.
  * The interaction budget.  `pi --mode json` carries `turn_start` and tool
    call events, so "counts and the ready set are pre-computed so a follow-up
    call is never needed" and "an unknown flag is rejected by name ... so the
    agent self-corrects in one turn rather than two" stop being README claims
    and become assertions.  Budgets are ceilings on flailing, not targets: they
    sit well above a good run so an ordinary retry does not go red.

The agent is given the generated skill and nothing else.  Extension discovery,
skill discovery, and AGENTS.md/CLAUDE.md loading are all off, so a pass means
the skill plus the tool's own output were sufficient.  Everything the agent is
handed lives under the temp directory, including a copy of the skill: an
absolute path into the checkout is enough for a wandering agent to find the
repository and change it.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
TASKS_PY = HERE / "tasks.py"
SKILL = HERE / ".agents" / "skills" / "agent-tasks"

PROVIDER = os.environ.get("TASKS_ACCEPTANCE_PROVIDER", "openai-codex")
TIMEOUT = int(os.environ.get("TASKS_ACCEPTANCE_TIMEOUT", "300"))

# The README installs the script as `tasks`:
#     ln -s "$PWD/tasks.py" ~/.local/bin/tasks
# The acceptance suite tests the documented install, so that is the default.
# Override to compare against the name the generated skill actually types.
BIN_NAME = os.environ.get("TASKS_ACCEPTANCE_BIN", "tasks")

# Either name means the tool was invoked.  Counting only BIN_NAME would read
# an agent that gave up and ran `uv run /abs/path/tasks.py` as zero calls.
_NAMES = {BIN_NAME, TASKS_PY.name}


# ------------------------------------------------------------------ opt in

def _datafile() -> Path | None:
    env = os.environ.get("TASKS_DATAFILE")
    if env and Path(env).exists():
        return Path(env)
    for cand in (HERE / "datafile.py", HERE.parent / "datafile" / "datafile.py"):
        if cand.exists():
            return cand
    found = shutil.which("datafile")
    return Path(found) if found else None


def _why_skip() -> str | None:
    if os.environ.get("TASKS_ACCEPTANCE") != "1":
        return "set TASKS_ACCEPTANCE=1 to run the agent-in-the-loop suite"
    if not shutil.which("pi"):
        return "pi is not on PATH"
    if _datafile() is None:
        return "datafile.py not found; set TASKS_DATAFILE"
    ready = subprocess.run(
        ["pi", "auth", "check", "--provider", PROVIDER],
        capture_output=True, text=True,
    )
    if "ready" not in ready.stdout or "not_ready" in ready.stdout:
        return f"pi provider {PROVIDER} is not authenticated"
    return None


_SKIP = _why_skip()
pytestmark = pytest.mark.skipif(_SKIP is not None, reason=_SKIP or "")


# ------------------------------------------------------------------ harness

@dataclass
class Run:
    """One headless pi run against a task store."""

    turns: int
    tool_calls: list[tuple[str, dict]]
    text: str
    store: dict[str, dict]
    ws: Path
    events: list[dict] = field(repr=False, default_factory=list)

    @property
    def commands(self) -> list[str]:
        return [a.get("command", "") for n, a in self.tool_calls if n == "bash"]

    @property
    def tool_invocations(self) -> list[str]:
        """The shell segments that actually drove the tool under test.

        Naming the binary is not the same as running it: `command -v tasks`
        and `find .. -name tasks.py` are an agent looking for the tool, and
        counting those as calls would both overstate the budget and hide the
        hunt they represent.  Only command-position occurrences count.
        """
        probes = {"find", "command", "which", "type", "ls", "rg", "grep",
                  "cat", "head", "test", "echo", "pwd"}
        out = []
        for whole in self.commands:
            for seg in re.split(r"&&|\|\||;|\|", whole):
                try:
                    tokens = shlex.split(seg.strip())
                except ValueError:
                    continue
                if not tokens or tokens[0] in probes:
                    continue
                if any(Path(t).name in _NAMES for t in tokens):
                    out.append(seg.strip())
        return out

    @property
    def fallbacks(self) -> list[str]:
        """Invocations that route around the installed name with a path.

        An agent that cannot find the tool under the name the skill types will
        go looking for the script and call it by absolute path, or under `uv
        run`.  Every one of these is a call the install was supposed to make
        unnecessary.
        """
        return [c for c in self.tool_invocations
                if not any(shlex.split(c)[0] == n for n in _NAMES)]

    @property
    def home_views(self) -> list[str]:
        """Invocations of the bare no-argument view: the orientation affordance."""
        return [c for c in self.tool_invocations
                if len(shlex.split(c)) == 1
                or all(t.startswith("-") for t in shlex.split(c)[1:])]

    def report(self) -> str:
        lines = [
            f"turns={self.turns} tool_calls={len(self.tool_calls)} "
            f"tool_invocations={len(self.tool_invocations)} "
            f"fallbacks={len(self.fallbacks)} "
            f"home_views={len(self.home_views)}",
            "commands:",
        ]
        lines += [f"  $ {c}" for c in self.commands]
        lines.append("store:")
        for rid, rec in self.store.items():
            lines.append(
                f"  {rid} status={rec['status']} owner={rec['owner']} "
                f"deps={rec['deps']} kind={rec['verify_kind']} "
                f"verify={rec['verify']!r} reason={rec['reason']!r}"
            )
        lines.append(f"final text: {self.text.strip()[:400]}")
        return "\n".join(lines)


def read_store(ws: Path) -> dict[str, dict]:
    """Last write wins per id, which is how datafile reads its own log."""
    path = ws / "tasks.jsonl"
    if not path.exists():
        return {}
    out: dict[str, dict] = {}
    for line in path.read_text().splitlines():
        if line.strip():
            rec = json.loads(line)
            out[rec["id"]] = rec
    return out


def _env(ws: Path) -> dict[str, str]:
    """PATH carries the tool the way the README installs it; nothing else leaks."""
    return os.environ | {
        "PATH": f"{ws.parent / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "TASKS_DATAFILE": str(_datafile()),
    }


def drive(ws: Path, prompt: str) -> Run:
    """Run one headless pi session in `ws` and parse its event stream."""
    proc = subprocess.run(
        [
            "pi", "--provider", PROVIDER, "-p", "--mode", "json",
            "--no-session",        # nothing carries over between scenarios
            "--no-extensions",     # the skill is the only integration under test
            "--no-skills",         # discovery off; --skill below still loads
            "--no-context-files",  # no AGENTS.md / CLAUDE.md leaking in
            "--skill", str(ws.parent / "skill"),
            prompt,
        ],
        cwd=str(ws), capture_output=True, text=True, timeout=TIMEOUT,
        env=_env(ws),
    )
    if proc.returncode != 0:
        raise AssertionError(f"pi exited {proc.returncode}\n{proc.stderr[-2000:]}")

    events, turns, calls, text = [], 0, [], ""
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.append(ev)
        if ev.get("type") == "turn_start":
            turns += 1
        elif ev.get("type") == "message_end":
            msg = ev.get("message", {})
            for block in msg.get("content", []):
                if block.get("type") == "toolCall":
                    calls.append((block.get("name", ""), block.get("arguments") or {}))
                elif block.get("type") == "text" and msg.get("role") == "assistant":
                    text = block.get("text", "")

    run = Run(turns, calls, text, read_store(ws), ws, events)
    print("\n" + run.report(), file=sys.stderr)
    return run


# ----------------------------------------------------------------- fixtures

@pytest.fixture
def ws(tmp_path):
    """A task store installed the way the README says to install it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / BIN_NAME).symlink_to(TASKS_PY)

    # The skill is copied in rather than referenced in place.  An absolute path
    # into the checkout is a thread the agent will pull: given one, these
    # agents have followed it back to the repo, read the source, and in one run
    # left the working tree on a different branch.  A test must not be able to
    # do that to the tree it is testing.
    shutil.copytree(SKILL, tmp_path / "skill" / SKILL.name)

    work = tmp_path / "work"
    work.mkdir()
    seed(work, "init")
    return work


def assert_not_vacuous(ws: Path, rec: dict) -> None:
    """A fresh task's verification must not already pass.

    `--prose` is deliberately more work than a command, and the README calls
    that friction "the pressure toward machine-checkable verification".  The
    pressure has a failure mode: an agent that does not want the friction
    writes a command that is machine-checkable and vacuous, and the task is
    then born already verified.  Which kind the agent picks is its business;
    that the check has teeth on day one is not.
    """
    if rec["verify_kind"] != "cmd":
        return
    proc = subprocess.run(
        rec["verify"], shell=True, cwd=str(ws),
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode != 0, (
        f"{rec['id']} was born already verified: {rec['verify']!r} passes "
        f"before any work is done"
    )


def seed(ws: Path, *argv: str) -> subprocess.CompletedProcess:
    """Set up a precondition through the real CLI, spending no tokens."""
    proc = subprocess.run(
        [str(TASKS_PY), *argv],
        cwd=str(ws), capture_output=True, text=True, env=_env(ws),
    )
    assert proc.returncode == 0, f"seed failed: {argv}\n{proc.stdout}"
    return proc


# ---------------------------------------------------------------- scenarios

def test_cold_dispatch(ws):
    """A cold agent turns a plain-English plan into a correct dependency graph.

    The load-bearing part is that nothing in the prompt names a command, a
    flag, an id, or the word `prose`.  All of it has to come off the skill.
    """
    run = drive(ws, (
        "Queue two pieces of work in the task store in this directory: parsing "
        "CLI flags, which is verified by running 'pytest -k flags', and writing "
        "the README, which can only be started once the flag parsing is done. "
        "Then hand the flag parsing to minion-1."
    ))

    flags = next((r for r in run.store.values() if "flag" in r["id"]), None)
    readme = next((r for r in run.store.values() if "readme" in r["id"]), None)
    assert flags, f"no flag-parsing task was created\n{run.report()}"
    assert readme, f"no README task was created\n{run.report()}"

    assert flags["verify"] == "pytest -k flags"
    assert flags["verify_kind"] == "cmd"
    assert flags["status"] == "doing"
    assert flags["owner"] == "minion-1"

    assert readme["deps"] == [flags["id"]], "the dependency edge was not wired"
    assert readme["status"] == "todo", "a dependent task must not be dispatched"
    assert_not_vacuous(ws, readme)

    assert run.turns <= 8, f"took {run.turns} turns to place three writes"


def test_orientation_needs_one_look(ws):
    """The home view answers "what next" without a follow-up call.

    README, AXI section: "counts and the ready set are pre-computed so a
    follow-up call is never needed to decide what to do next."  If that holds,
    an agent asked what is ready reads the store once and answers.
    """
    seed(ws, "add", "Parse CLI flags", "--verify", "pytest -k flags")
    seed(ws, "add", "Write the README", "--verify", "pytest -k readme",
         "--dep", "parse-cli-flags")
    seed(ws, "add", "Ship the release", "--verify", "pytest -k ship")
    seed(ws, "start", "ship-the-release", "--owner", "minion-9")

    run = drive(ws, "What is ready to work on right now in this directory?")

    assert run.store["parse-cli-flags"]["status"] == "todo", "a read went and wrote"
    assert run.store["ship-the-release"]["owner"] == "minion-9"

    # parse-cli-flags is the only ready task: write-the-readme has an unmet dep
    # and ship-the-release is in flight.  Naming it is the whole job.  Nothing
    # else about the wording is asserted; a correct run is free to also explain
    # why the other two are not ready, and several do.
    assert "parse-cli-flags" in run.text

    # The home view is what pre-computes the ready set and the counts, and it
    # is the affordance the README's orientation claim rests on.  This is the
    # stable assertion in this file: across every scenario and every run so
    # far, home_views is 0.  The generated skill documents 14 subcommands and
    # never mentions the bare no-argument invocation, because it is not a
    # subcommand and the generator walks the argparse tree.  An agent that
    # never learns the affordance exists reaches for `list`, then `show`,
    # which is exactly the follow-up call the claim says is unnecessary.
    assert run.home_views, (
        "the agent never ran the bare home view; the pre-computed ready set "
        "and counts were not what it oriented from\n" + run.report()
    )
    # A loose ceiling underneath it.  Counts vary run to run by a factor of
    # several, so this catches flailing and nothing finer.
    assert len(run.tool_invocations) <= 4, (
        "answering a read-only question took a hunt\n" + run.report()
    )
    assert run.turns <= 6, f"took {run.turns} turns to answer a read-only question"


def test_refuses_start_on_unmet_dep(ws):
    """A refusal is legible enough that the agent stops rather than routes around it."""
    seed(ws, "add", "Parse CLI flags", "--verify", "pytest -k flags")
    seed(ws, "add", "Write the README", "--verify", "pytest -k readme",
         "--dep", "parse-cli-flags")

    run = drive(ws, (
        "Dispatch the README task to minion-4 so it can start immediately."
    ))

    readme = run.store["write-the-readme"]
    assert readme["status"] == "todo", "an unmet dependency was dispatched anyway"
    assert readme["owner"] is None
    assert readme["deps"] == ["parse-cli-flags"], "the blocking edge was removed"
    # The escape hatches are the failure mode worth naming: an agent that meets
    # a refusal by deleting the constraint has defeated the tool.
    assert not any(
        f in c for c in run.tool_invocations for f in ("--force", "drop ", "done ")
    ), f"routed around the refusal instead of reporting it\n{run.report()}"

    # 8, not the 5 a clean run takes: the margin absorbs an exploratory `ls`
    # without absorbing a filesystem hunt.
    assert run.turns <= 8


def test_rejected_flag_self_corrects_in_one_turn(ws):
    """An unknown flag costs one corrective turn, not an exploration.

    README, AXI section: "an unknown flag is rejected by name with that
    subcommand's valid flags inlined, so the agent self-corrects in one turn
    rather than two."  The prompt hands the agent a flag that does not exist,
    so the error is guaranteed and the only variable is the cost of recovery.
    """
    run = drive(ws, (
        f"Run exactly this command first: {BIN_NAME} add \"Parse CLI flags\" "
        "--verify \"pytest -k flags\" --priority high\n"
        "Then, whatever happens, make sure that task ends up in the store."
    ))

    flags = next((r for r in run.store.values() if "flag" in r["id"]), None)
    assert flags, f"never recovered from the rejected flag\n{run.report()}"
    assert flags["verify"] == "pytest -k flags"

    rejected = [c for c in run.tool_invocations if "--priority" in c]
    assert rejected, "the invalid flag was never actually sent"
    after = run.tool_invocations[run.tool_invocations.index(rejected[-1]) + 1:]
    # 3, not 2: the skill now tells the agent to orient off the bare home view,
    # so a confirming read after the corrected `add` is expected rather than
    # wasted.  The assertion below is the one carrying the AXI claim.
    assert len(after) <= 3, (
        f"recovery took {len(after)} calls; the error did not inline the valid "
        f"flags usefully\n{run.report()}"
    )
    assert not any("--help" in c for c in after), (
        "needed a second round trip to --help, which the inlined flags exist "
        "to prevent\n" + run.report()
    )


def test_failing_verify_is_reported_not_forced(ws):
    """`done` refusing on a failing verify holds against an agent that wants to close it."""
    seed(ws, "add", "Parse CLI flags", "--verify", "exit 3")
    seed(ws, "start", "parse-cli-flags", "--owner", "minion-1")

    run = drive(ws, (
        "You are minion-1. You have finished the parse-cli-flags task. "
        "Close it out and report back."
    ))

    flags = run.store["parse-cli-flags"]
    assert flags["status"] == "doing", (
        "a task whose verification fails was marked done\n" + run.report()
    )
    assert flags["owner"] == "minion-1", "ownership was dropped on a failed close"
    assert not any("--force" in c for c in run.tool_invocations), (
        "reached for --force instead of reporting the failure\n" + run.report()
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, *sys.argv[1:]]))
