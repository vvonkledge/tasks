# tasks

An agent task manager over [datafile](../datafile). One orchestrator creates and
dispatches tasks; each minion updates only the task it owns.

For the other half of an event-driven orchestrator - where webhooks land before
the orchestrator decides whether they are work at all - see
[inbox](../inbox), which follows the same design over the same store.

```sh
tasks.py add "Parse CLI flags" --verify "pytest -k flags"
tasks.py start parse-cli-flags --owner minion-3
tasks.py done parse-cli-flags          # runs the verify command; refuses on non-zero
```

## Why

A task manager for agents answers one question, cheaply, for a caller with zero
context: *what should I work on next, and how will I know I am done?* Everything
here follows from that, plus one thing a markdown checklist can never do - it is
the only state two agents that never share a context window can both see.

Three consequences worth stating up front:

- **Verification is a command, not a description.** `done` executes it. A minion
  cannot claim a task is finished without the check passing, and the override
  leaves a recorded reason the orchestrator sees on its next read.
- **There is no priority field.** Dependencies produce a real ready set; priority
  is a human negotiation artifact that agents assign arbitrarily.
- **`ready` is derived, never stored.** So are the counts and the stale set. The
  only orderable truth is the dependency graph.

New here? [RUNBOOK.md](RUNBOOK.md) walks through install, a first queue, the
ownership protocol, and recovery step by step. This README covers the design
and the reference.

## Install

Requires [uv](https://docs.astral.sh/uv/) and `datafile.py`, which is found via
`$TASKS_DATAFILE`, then next to `tasks.py`, then `../datafile/datafile.py`, then
`datafile` on `$PATH`.

```sh
just install        # symlinks ~/.local/bin/tasks -> ./tasks.py
tasks init          # writes schema-tasks.yaml next to tasks.jsonl
```

The lookup above is relative to the path you invoked, and `abspath()` does not
resolve symlinks, so linking moves it into the bindir and a sibling checkout
stops being found. `just install` runs the tool once afterwards and will not
report success without saying so.

Then pick the agent integration that fits. They achieve the same thing, so you
need one, not all of them:

```sh
tasks setup            # session hook: Claude Code, Codex, OpenCode
tasks skill --install  # on-demand skill: Claude Code, pi
tasks pi-package && pi install "$PWD/pi-agent-tasks"   # pi, with ambient context
```

## The ownership protocol

The concurrency risk was never two processes writing the file - `datafile` takes
an exclusive `flock` per append. The risk is **two writers touching the same
key**, because `put` replaces a whole record and every transition is a
read-modify-write around it.

Every task is dispatched to exactly one minion, so key-level exclusivity comes
from the shape of the fleet rather than from a lock:

| Step | Who writes | Command |
| --- | --- | --- |
| create | orchestrator | `add "…" --verify …` → `todo` |
| dispatch | orchestrator | `start <id> --owner <minion>` → `doing` |
| work | minion | nothing |
| finish | minion | `done <id>` or `block <id> --reason …` |
| reconcile | orchestrator | reads; ownership has returned |

**The rule:** a task in `doing` belongs to its owner, and the orchestrator treats
it as read-only. Ownership transfers on `start` and returns on any terminal
transition. `dep` and `drop` refuse on an in-flight task unless `--force`, so a
broken rule is a loud error rather than a silent lost update.

`owner` is a routing and debug field, not a lock. It is what an orchestrator that
lost its context reads to find out which minion holds what.

Minions never call `add`. A minion that discovers new work runs
`block <id> --reason "…"` and returns; the orchestrator queues the unblocker and
wires it with `dep`. Minions also should not run the bare home view - they get
`show <id>` and their two terminal verbs, and fleet-wide state stays the
orchestrator's concern.

## Recovery

The orchestrator dispatches, so it observes each minion's completion or death and
reconciles itself. The genuine orphan is when the **orchestrator** dies mid-flight:
the next session sees `doing` rows with owners and no live fleet, and runs
`reset <id> --reason "…"` to reclaim them.

Reclaiming is deliberately manual. A lease that expires on its own would reclaim
tasks from minions that were merely slow, which is worse than the problem it
solves.

## Commands

| Command | What it does |
| --- | --- |
| *(no arguments)* | in-flight work, the ready set, blocked reasons, counts |
| `init` | write the contract for the store |
| `add <title> --verify <s>` | create a task; `--prose`, `--dep`, `--project`, `--cwd`, `--context` |
| `show <id>` | one task, all fields, plus its unmet deps |
| `list` | table; `--status`, `--project`, `--stale`, `--fields`, `--limit` |
| `start <id> --owner <m>` | dispatch; refuses on unmet deps or a live owner |
| `done <id>` | run verify and finish; `--reason`, `--force` |
| `block <id> --reason <s>` | mark stuck |
| `unblock <id>` | return a blocked task to the queue |
| `reset <id> --reason <s>` | reclaim an orphaned in-flight task |
| `dep <id> --on <other>` | add an edge; refuses cycles |
| `drop <id> --reason <s>` | tombstone a task |
| `setup` | install the session hook; `--status`, `--uninstall`, `--scope`, `--app` |
| `skill` | generate the agent skill; `--install`, `--check`, `--out` |
| `pi-package` | generate the pi package; `--check`, `--out` |

Every subcommand has `--help` with its flags and an example.

## Verification

`--verify` is a shell command run from the task's `--cwd`, or from the store's
directory when it has none, killed after `--timeout` seconds (default 300).

**Set `--cwd` whenever the work is not in the store's own tree.** A store that
lives apart from the projects it tracks will otherwise verify against itself, and
a check like `test -f README.md` can *pass* there by accident. A false green is
the one failure this tool exists to prevent, so a `--cwd` that does not exist is
refused at `add` and again at `done` rather than falling back to the store.

`--prose` marks a criterion a human or agent asserts instead. `done` then requires
`--reason` describing how it was satisfied. Prose is deliberately more work than a
command: the friction is the pressure toward machine-checkable verification.

`--force --reason "…"` overrides either. The reason is stored, so a forced
completion is visible in `show` and distinguishable from a verified one.

## Ids

Ids are slugged from the title, capped at a word boundary, deduplicated with a
numeric suffix: `"Parse CLI flags into a typed struct"` → `parse-cli-flags`.
Callers never invent them, and the id stays self-describing in every later
output, which is why `list` can often be read without the title column.

## Schema

Eleven fields, in `schema-tasks.yaml`, which `init` writes and you can edit:

```yaml
id           # slug, primary key
title        # <= 120 chars
status       # todo | doing | done | blocked
verify       # command, or criterion when verify_kind is prose
verify_kind  # cmd | prose
deps         # task ids that must be done first
context      # pointers a cold-starting agent should read
project      # stable grouping handle; survives cwd being retargeted
cwd          # where the work happens and where verify runs
owner        # the minion holding it; routing, not a lock
reason       # why blocked, why reset, or why a done was forced
updated      # set on every transition
```

`cwd` is stored as written, so `~/src/app` stays readable in `show` and stays
portable across machines. A relative path resolves against the store, which is
what the store-directory default already meant.

`project` groups tasks that `cwd` cannot. An orchestrator that isolates each
minion in its own git worktree retargets `cwd` to that worktree at dispatch, so
`cwd` stops naming the project exactly when the work is in flight. `project` is
an opaque handle the orchestrator owns: `list --project <handle>` answers "what
is in flight for this project" whatever `cwd` currently points at.

A dependency that no longer exists counts as satisfied. It was dropped
deliberately and can never complete, so treating it as unmet would block its
dependents forever.

## History

`tasks.jsonl` is append-only, so every version of every task is still in it -
including the `reason` written just before a `drop`. There is no second event
store and no `history` command; when you want the trail, read the raw log:

```sh
grep '"id":"parse-cli-flags"' tasks.jsonl
```

## Tests

```sh
just check          # lint, the suite, and the generated skill
uv run test_tasks.py --cov=. --cov-branch --cov-report=term-missing
```

200 tests, 100% line and branch coverage of `tasks.py`. Coverage is not proof of
correctness, so the suite is built around the transitions that can silently
corrupt state rather than around the lines: a second minion taking an in-flight
task, a `done` that skips verification, a cycle entering the graph, a lost update
from a concurrent write.

CI runs the lint, the tests, and `tasks skill --check`. Because `tasks.py`
locates `datafile.py` at runtime rather than vendoring it, the workflow checks
both repositories out as siblings - the layout `tasks.py` already falls back to -
so CI exercises the real lookup instead of a special case. It deliberately does
not pin a `datafile` revision: this project builds on that one's internals, so
the job doubles as the integration test for that coupling.

## AXI

Built to the [AXI principles](https://axi.md). Output is TOON on stdout,
including errors; nothing is written to stderr. The home view identifies the
tool and shows live state rather than help text; lists default to three fields
with `--fields` to widen them; counts and the ready set are pre-computed so a
follow-up call is never needed to decide what to do next; empty results state the
zero and name its cause; mutations are idempotent; and an unknown flag is
rejected by name with that subcommand's valid flags inlined, so the agent
self-corrects in one turn rather than two.

### Ambient context (§7)

`tasks setup` registers a `SessionStart` integration so an agent opens every
session with the queue already in front of it - what is in flight, what is
ready, what is stuck - before it takes any action. Claude Code and Codex get a
native hook; OpenCode has no `SessionStart`, so it gets a managed plugin that
injects the same home view as ambient system context, cached once per session.
Codex additionally needs `[features] hooks = true`, which `setup` writes.

The hook runs `tasks.py --ambient`, which differs from the bare home view in one
way: where the current directory has no task store it prints **nothing** and
exits 0. Ambient context is paid on every session in every repository, so a tool
with nothing to say there should cost nothing. `--ambient` also never exits
non-zero - a task manager must not be able to break a session start - and a
store it cannot read is reported as `AMBIENT_DEGRADED` rather than swallowed.

Installs are idempotent and self-repairing: re-running `setup` after the script
moves rewrites the stored path instead of adding a second hook. Hooks belonging
to other tools are matched by marker and left alone, including when they share a
`SessionStart` group with ours.

pi is the exception among the four agents: it has neither a command hook nor an
OpenCode-style plugin, so `setup` does not cover it. `tasks pi-package` generates
a [pi package](https://pi.dev) instead - an extension that captures the queue at
`session_start` and appends it to the system prompt, bundled with the skill. The
extension shells out to `tasks` on `PATH` and falls back to the absolute path
baked in at generation time, which is why `pi-package --check` only means
anything on the machine that generated it. Check the skill in CI instead; it
carries no machine-specific path.

`tasks skill --install` is the alternative: an [Agent Skill](https://agentskills.io)
that loads on demand instead of on every session, and works in agents without
hook support. It is generated from the argparse tree, so it cannot drift from the
CLI, and `tasks skill --check` fails in CI when the committed copy is stale.
Being static, it carries no live state and no machine-specific path.

Ambient context (hook, plugin, or pi extension) and the on-demand skill are
complementary rather than cumulative - install whichever fits your agent.

## Exit codes

| Exit | Meaning |
| --- | --- |
| 0 | success, including idempotent no-ops |
| 1 | the request could not be satisfied (not found, verify failed, in flight) |
| 2 | usage error, contract violation, or a missing required reason |

Output is [TOON](https://toonformat.dev) on stdout, including errors. Mutations
are idempotent: a second `done` does not rerun verify, and `start` by the same
owner is a no-op.
