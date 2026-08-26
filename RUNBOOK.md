# tasks runbook

A task-oriented guide for someone who has never run this tool. The
[README](README.md) explains *why* the design is what it is; this file explains
*what to type*, in order, who types it, and what to do when something goes
wrong.

Work through sections 1-3 and you have a working queue in about five minutes.
Sections 4 onward are day-2 operations: keep them for reference.

This tool has two distinct users and they run different commands. Section 4 is
the one to read if you are wiring up a fleet; everything before it is the same
for both.

---

## 1. Install and verify

**Prerequisites:** [uv](https://docs.astral.sh/uv/) and `datafile.py`, plus
[just](https://just.systems) if you want the install recipe below. Everything
else here runs without it. Python and the dependencies (pydantic, PyYAML) are
declared inline in `tasks.py` via PEP 723 and are fetched automatically on
first run.

`tasks.py` deliberately **does not vendor** `datafile.py`. It is the storage
engine, it lives in its own repository, and this tool builds on its internals.
The lookup runs in this order, first hit wins:

| Order | Location |
| --- | --- |
| 1 | `$TASKS_DATAFILE` |
| 2 | `datafile.py` next to `tasks.py` |
| 3 | `../datafile/datafile.py` (sibling checkout) |
| 4 | `datafile` or `datafile.py` on `$PATH` |

The sibling layout is the intended one, and it is what CI reproduces:

```sh
git clone <datafile repo> datafile
git clone <this repo> tasks && cd tasks
uv run tasks.py --version
```

If that prints `0.1.0` you are done. If it prints this, the engine is missing:

```
error: datafile.py not found
code: DATAFILE_MISSING
help[2]:
  Set TASKS_DATAFILE=/path/to/datafile.py and retry
  Or put `datafile` on your PATH
```

`--version` answers before importing anything, so it costs interpreter startup
and nothing else. It is the cheapest probe that the install works.

**Put it on your PATH** so you can type `tasks` from any directory:

```sh
just install          # symlinks ~/.local/bin/tasks -> ./tasks.py
tasks --version       # confirm
```

If `just install` reports that the bindir is not on your `PATH`, add the line it
prints to your shell profile and open a new shell. To install elsewhere:
`just bindir=/usr/local/bin install`. To remove: `just uninstall`.

The install is a **symlink, not a copy**, so the command always tracks your
checkout. `git pull` updates the installed tool with no reinstall step.

**Linking changes where the engine is looked up.** The table above is relative
to *the path you invoked*, and `abspath()` does not resolve symlinks, so from
`~/.local/bin` candidates 2 and 3 both point into the bindir. A sibling checkout
that works fine under `uv run tasks.py` stops being found the moment you link:

```
error: datafile.py not found
code: DATAFILE_MISSING
```

`just install` runs the tool once after linking and refuses to report success
without diagnosing this, so you find out now rather than in a session hook. Fix
it either way, and prefer the first:

```sh
(cd ../datafile && just install)                 # candidate 4, resolved via PATH
export TASKS_DATAFILE="$PWD/../datafile/datafile.py"   # candidate 1
```

One payoff for linking: the tool takes its name from how it was invoked, so
every suggestion it prints becomes `tasks …` rather than `tasks.py …`.

> The rest of this runbook writes `tasks`. If you skipped the install step,
> write `uv run tasks.py` instead everywhere.

**Sanity check.** Run `tasks` with no arguments in any directory:

```
bin: ~/vvonkledge/sandbox/tasks/tasks.py
description: Agent task manager where one orchestrator writes and minions report
version: 0.1.0
tasks: no task store in /path/to/here
help[1]:
  Run `tasks.py init` to create one here
```

The bare command is always safe, is scoped to the current directory, and tells
you what exists here and what to do next. When you are lost, run it.

---

## 2. Your first queue

A queue is two files you control:

| File | What it is | Commit it? |
| --- | --- | --- |
| `schema-tasks.yaml` | the contract every task must satisfy | yes |
| `tasks.jsonl` | the live queue, one JSON record per version | **no** |

The `.jsonl` is runtime state, not source. It is already in `.gitignore`, along
with its `.idx`, `.lock`, and `.quarantine` sidecars. Committing a live queue
would mean merge-conflicting on a file whose whole job is to be the one thing
two agents agree on.

### Step 1 - write the contract

```sh
tasks init
```

```
contract: ~/work/myproject/schema-tasks.yaml
status: created
store: tasks.jsonl
help[1]:
  Run `tasks.py add "<title>" --verify "<cmd>"` to create the first task
```

`init` writes the nine-field contract and nothing else; the store appears on the
first `add`. It is idempotent: run it again and it reports `status: unchanged`
rather than overwriting a contract you edited.

### Step 2 - queue some work

```sh
tasks add "Parse CLI flags" --verify "test -f flags.txt"
```

```
id: parse-cli-flags
status: todo
ready: true
help[1]:
  Run `tasks.py start parse-cli-flags --owner <minion>` to dispatch it
```

You do not choose the id. It is slugged from the title, capped at a word
boundary, and deduplicated with a numeric suffix if it collides. `ready` tells
you immediately whether this task can be dispatched right now.

Add one that has to wait, and give the eventual worker something to read:

```sh
tasks add "Wire the parser into main" \
  --verify "test -f main.txt" \
  --dep parse-cli-flags \
  --context README.md
```

```
id: wire-the-parser
status: todo
ready: false
help[1]:
  Run `tasks.py show wire-the-parser` to see what it is waiting on
```

`--dep` and `--context` both repeat: pass them once per value.

**`--context` is the single highest-leverage field in the tool.** A minion
starts cold, with none of the conversation that produced the task. `--context`
is the list of paths it should read before touching anything. Spend the effort
here.

### Step 3 - see the queue

```sh
tasks
```

```
bin: ~/vvonkledge/sandbox/tasks/tasks.py
description: Agent task manager where one orchestrator writes and minions report
version: 0.1.0
ready[2]{id,title}:
  parse-cli-flags,Parse CLI flags
  write-the-release-notes,Write the release notes
counts[1]{doing,ready,todo,done,blocked,stale}:
  0,2,3,0,0,0
help[1]:
  Run `tasks.py start parse-cli-flags --owner <minion>` to dispatch the next task
```

Three tasks exist, two are dispatchable, and the third is waiting on a
dependency. `ready` is **derived on every read**, never stored. The only
orderable truth in the store is the dependency graph.

The home view answers "where did I leave off, and what is next?" in that order:
in-flight work first, then the ready set, then what is stuck.

### Step 4 - run one task all the way through

```sh
tasks start parse-cli-flags --owner minion-3
```

```
id: parse-cli-flags
status: doing
owner: minion-3
verify_kind: cmd
help[2]:
  Run `tasks.py done parse-cli-flags` when the work verifies
  Run `tasks.py block parse-cli-flags --reason "<why>"` if it cannot proceed
```

Do the work, then finish it:

```sh
tasks done parse-cli-flags
```

```
id: parse-cli-flags
status: done
verified: true
help[1]:
  Run `tasks.py` for the next ready task
```

`verified: true` means `done` **ran** `test -f flags.txt` and it exited zero. It
is not a claim, it is a result. Section 5 covers what happens when it fails.

That is the whole loop. You now have a queue.

---

## 3. Everyday operations

| I want to | Command |
| --- | --- |
| See the queue here | `tasks` (no arguments) |
| Queue work | `tasks add "<title>" --verify "<cmd>"` |
| Hand a task to a worker | `tasks start <id> --owner <minion>` |
| Finish a task | `tasks done <id>` |
| Say a task is stuck | `tasks block <id> --reason "<why>"` |
| Put a stuck task back | `tasks unblock <id>` |
| Read one task in full | `tasks show <id>` |
| Scan many | `tasks list --status doing --fields id,owner,updated` |
| Reclaim an abandoned task | `tasks reset <id> --reason "<why>"` |
| Wire an ordering | `tasks dep <id> --on <other>` |
| Remove a task | `tasks drop <id> --reason "<why>"` |
| Find forgotten work | `tasks list --stale` |

Every subcommand carries `--help` with its flags and a worked example.

**`show` is the cold-start command.** It prints all nine fields plus the derived
unmet deps, so a worker that has just been handed an id needs exactly one call
before it can begin:

```
task:
  id: wire-the-parser
  title: Wire the parser into main
  status: todo
  verify: test -f main.txt
  verify_kind: cmd
  deps[1]: parse-cli-flags
  context[1]: README.md
  owner: null
  reason: null
  updated: "2026-08-26T08:28:56.525306Z"
ready: false
unmet_deps[1]: parse-cli-flags
```

**`list` truncates by default.** `count: 3 of 3 total` tells you whether you are
seeing everything; raise `--limit` when you are not. It defaults to three
columns, `id,status,title`, and `--fields` widens it. Because ids are slugged
from titles they stay self-describing, so `--fields id,status,owner` is usually
more useful than the default:

```sh
tasks list --fields id,status,owner,deps
```

**Mutations are idempotent.** A second `done` does not rerun verify, it reports
`result: unchanged`. `start` by the same owner is a no-op. `unblock` on a task
that is already `todo` is a no-op. `drop` on an id that does not exist is
`result: absent` with exit 0. All of them are safe to retry, and safe to run
from an agent that may lose its response and try again.

**`--stale` is the only time-based view.** It lists todos untouched for over 14
days, and its count is carried in the home view so you notice without asking.
Nothing expires on its own; see section 6 for why.

---

## 4. The ownership protocol

**This is the section that makes the tool safe under a fleet. Read it before
wiring more than one agent to a store.**

The concurrency risk was never two processes writing the file: `datafile` takes
an exclusive `flock` per append. The risk is **two writers touching the same
key**, because every transition is a read-modify-write and `put` replaces a
whole record.

There is no lock solving that. The topology solves it: every task is dispatched
to exactly one worker, so every key has one writer at a time.

### Who runs what

| Step | Who | Command | Result |
| --- | --- | --- | --- |
| create | orchestrator | `add "…" --verify …` | `todo` |
| dispatch | orchestrator | `start <id> --owner <minion>` | `doing` |
| work | minion | *nothing* | - |
| finish | minion | `done <id>` | `done` |
| give up | minion | `block <id> --reason "…"` | `blocked` |
| reconcile | orchestrator | reads | ownership has returned |

**The rule:** a task in `doing` belongs to its owner, and the orchestrator
treats it as read-only. Ownership transfers on `start` and returns on any
terminal transition.

A broken rule is a loud error, not a silent lost update. `dep` and `drop` refuse
an in-flight task:

```
error: "parse-cli-flags is in flight, owned by minion-3"
code: TASK_IN_FLIGHT
help[2]:
  Run `tasks.py drop parse-cli-flags --force` to override the owner
  Run `tasks.py reset parse-cli-flags --reason "<why>"` to reclaim it first
```

Take the second suggestion. `--force` exists for the case where you know the
owner is gone, and reaching for it while a worker is alive is how you get the
lost update the protocol is there to prevent.

### What each role is allowed to run

**Orchestrator:** everything. It owns `add`, `dep`, `drop`, `reset`, and the
bare home view.

**Minion:** `show <id>`, then `done <id>` or `block <id> --reason "…"`. That is
the whole surface.

Two consequences worth stating explicitly:

- **Minions never call `add`.** A minion that discovers new work runs
  `block <id> --reason "…"` and returns. The orchestrator queues the unblocker
  and wires it with `dep`. Letting workers queue work turns one dependency graph
  into as many uncoordinated graphs as you have workers.
- **Minions should not run the bare home view.** Fleet-wide state is the
  orchestrator's concern. A worker that can see the whole queue starts making
  scheduling decisions that are not its to make.

### owner is routing, not a lock

Nothing enforces `owner`. It is what an orchestrator that lost its context reads
to find out which minion holds what, and what you read in `list --status doing`
when something is late.

Note that `done` leaves `owner` set: after the fact it is the record of who did
the work. Only `reset` clears it, because only `reset` means "this is nobody's
now".

---

## 5. Verification

`done` is the only command that executes anything, and it is what makes a
completion mean something. A minion cannot claim a task is finished without the
check passing.

### The three kinds of completion

| Kind | How it is set | What `done` requires | Recorded as |
| --- | --- | --- | --- |
| command | `--verify "<cmd>"` (the default) | the command exits 0 | `verified: true` |
| prose | `--verify "<criterion>" --prose` | `--reason` describing how | `verified: asserted` |
| forced | either, plus `--force` | `--reason` | `verified: false` |

The command runs **from the store's directory**, not the caller's, and is killed
after `--timeout` seconds (default 300). Write verify commands that are
independent of where they are invoked from.

### When verification fails

```
error: verify failed for parse-cli-flags
code: VERIFY_FAILED
command: test -f flags.txt
exit_code: 1
output: ""
help[2]:
  Run `tasks.py block parse-cli-flags --reason "<why>"` if it cannot proceed
  Run `tasks.py done parse-cli-flags --force --reason "<why>"` to override
```

The task stays `doing`. Nothing was written. The last 20 lines of output come
back inline, so the worker can usually diagnose without a second call.

There are only three correct responses, in this order of preference:

1. **Fix the work** and run `done` again. Almost always this one.
2. **`block --reason`** if it cannot proceed, and hand it back.
3. **`done --force --reason "<why>"`** if the check itself is wrong.

Never delete or weaken a `--verify` to make `done` pass. Force it and say so:
the reason is stored on the record, and `show` distinguishes a forced completion
from a verified one forever.

### Prose is deliberately more expensive

```
error: this task verifies by prose criterion
code: REASON_REQUIRED
criterion: a human read them
help[1]:
  Run `tasks.py done write-the-release-notes --reason "<how it is satisfied>"`
```

Prose requires a written reason on every completion; a command requires nothing.
That asymmetry is the point. The friction is pressure toward machine-checkable
verification, so reach for `--prose` only when no command could ever decide it.

### Writing a good verify command

| Instead of | Write |
| --- | --- |
| `--verify "tests pass"` (prose) | `--verify "uv run test_tasks.py -q"` |
| `--verify "pytest"` | `--verify "pytest -k flags"` (scoped to this task) |
| `--verify "the file exists"` | `--verify "test -f src/flags.py"` |
| `--verify "code is clean"` | `--verify "uvx ruff check src/flags.py"` |

Scope it to the task. A verify that runs the whole suite makes every task in the
queue fail whenever any one thing is broken, and the signal is lost.

---

## 6. Dependencies and the ready set

`ready` means `status == todo` and every dependency is `done`. It is computed on
every read, so it can never be stale.

```sh
tasks dep wire-the-parser --on write-the-release-notes
```

```
id: wire-the-parser
deps[2]: parse-cli-flags,write-the-release-notes
ready: false
```

`start` enforces the edges, so a wrong dispatch is caught rather than executed:

```
error: wire-the-parser has 1 unmet dependency(ies)
code: DEPS_UNMET
unmet_deps[1]: parse-cli-flags
```

**Cycles are refused at write time**, with the full path shown, so the graph can
never contain one:

```
error: wire-the-parser -> write-the-release-notes would create a dependency cycle
code: DEP_CYCLE
cycle[3]: wire-the-parser,write-the-release-notes,wire-the-parser
```

**A dependency that no longer exists counts as satisfied.** It was dropped
deliberately and can never complete, so treating it as unmet would strand its
dependents forever.

**There is no priority field, and that is deliberate.** Dependencies produce a
real ready set that any caller derives identically. Priority is a human
negotiation artifact, and agents assign it arbitrarily, so a priority field is a
number that looks like an ordering without being one. If order matters, it is a
dependency; say so with `dep`.

### When nothing is ready

The home view names the cause rather than leaving you to work it out:

| `cause` | What happened | What to do |
| --- | --- | --- |
| `dependency cycle: no task can ever become ready` | should be impossible via `dep`; means the store was hand-edited | `drop` and re-add one task in the printed `cycle` without its dep |
| `nothing queued: N in flight, M blocked` | the fleet has everything | wait, or `add` more work |
| `every queued task is waiting on deps (M blocked)` | the graph is stalled behind blocked work | `list --status blocked` and clear a reason |

---

## 7. Recovery

The orchestrator dispatches, so it observes each minion's completion or death
and reconciles itself. Three failure shapes, three responses.

### A minion reports it is stuck

```sh
tasks block wire-the-parser --reason "needs a decision on the flag name"
```

```
id: wire-the-parser
status: blocked
reason: needs a decision on the flag name
help[1]:
  Run `tasks.py add "<title>" --verify "<cmd>"` then `tasks.py dep wire-the-parser --on <new-id>` to queue the unblocker
```

The reason surfaces in the home view, so the orchestrator sees it on its next
read without looking for it. Queue the unblocker, wire it with `dep`, and
`unblock` returns the task to the queue with its owner intact.

### A minion dies

The orchestrator dispatched it, so it knows. `reset` it and dispatch again.

### The orchestrator dies mid-flight

This is the genuine orphan. The next session opens on `doing` rows with owners
and no live fleet:

```sh
tasks list --status doing --fields id,owner,updated
tasks reset wire-the-parser --reason "orchestrator restarted; minion-4 is gone"
```

```
id: wire-the-parser
status: todo
reclaimed_from: minion-4
reason: orchestrator restarted; minion-4 is gone
help[1]:
  Run `tasks.py start wire-the-parser --owner <minion>` to redispatch it
```

The reason stays on the record as the explanation for why this is back in the
queue, and `start` clears it on redispatch, so it never outlives its usefulness.

**Reclaiming is deliberately manual, and there is no lease.** A lease that
expires on its own would reclaim tasks from minions that were merely slow, which
means two workers on one key: exactly the failure the ownership protocol exists
to prevent, now arriving on a timer. Age is visible in the home view and in
`--stale`; deciding a worker is dead stays a judgment call.

`reset` refuses a `done` task, exit 1, because that would discard a verified
result.

---

## 8. History and store health

`tasks.jsonl` is append-only, so **every version of every task is still in it**,
including the `reason` written immediately before a `drop`. There is no second
event store and no `history` command. When you want the trail, read the log:

```sh
grep '"id":"wire-the-parser"' tasks.jsonl
```

```
{"id":"wire-the-parser",...,"status":"todo","owner":null,"reason":null,...}
{"id":"wire-the-parser",...,"status":"doing","owner":"minion-4","reason":null,...}
{"id":"wire-the-parser",...,"status":"blocked","owner":"minion-4","reason":"needs a decision on the flag name",...}
{"id":"wire-the-parser",...,"status":"todo","owner":null,"reason":"orchestrator restarted; minion-4 is gone",...}
```

Oldest first, one line per transition, with the timestamp in `updated`. That is
the audit trail: who held it, why it stopped, why it came back.

The store is a `datafile` store, so its maintenance commands work on it directly
and are documented in that project's runbook. The two that matter here:

```sh
datafile -f tasks.jsonl validate    # read-only; run this first, always
datafile -f tasks.jsonl compact     # reclaim space from superseded versions
```

`compact` **discards the history above**. Every transition of every task is a
superseded version, which is exactly what `compact` drops. Never run it on a
store whose trail you still want; if you want both, copy the `.jsonl` first.

If the home view ever prints `unreadable_lines: n`, the file has lines that do
not parse or do not satisfy the contract. Good tasks still read fine and the
queue still works, so there is no emergency. Diagnose with `validate` before
acting, and read the contract-evolution section of the `datafile` runbook before
concluding the data is at fault: an edit to `schema-tasks.yaml` invalidates old
records retroactively, and that is the usual cause.

---

## 9. Reading output, and scripting against it

All output, **including errors, goes to stdout** in
[TOON](https://toonformat.dev). Nothing is ever written to stderr. If you are
capturing output, capture stdout.

Branch on the exit code, not on the text:

| Exit | Meaning | Typical cause |
| --- | --- | --- |
| 0 | success, including idempotent no-ops | anything worked |
| 1 | the request could not be satisfied | id not found, verify failed, task in flight |
| 2 | usage error, contract violation, or a missing required reason | bad flag, cycle, `--force` without `--reason` |

```sh
if tasks done "$id" >/dev/null; then
    echo "verified"
fi
```

Every error carries a machine-readable `code` and a suggested next command, so
an agent self-corrects in one turn rather than two. An unknown flag is rejected
by name with that subcommand's valid flags inlined.

---

## 10. Agent integration

Three ways to put this in front of an agent. They achieve the same thing, so
**you need one, not all of them.**

```sh
tasks setup            # session hook: Claude Code, Codex, OpenCode
tasks skill --install  # on-demand skill: Claude Code, pi
tasks pi-package && pi install "$PWD/pi-agent-tasks"   # pi, with ambient context
```

| Path | Costs | Best when |
| --- | --- | --- |
| `setup` | every session, in every repository | the agent should open with the queue already in front of it |
| `skill --install` | only when a task matches its trigger | the agent has no hook support, or you do not want per-session cost |
| `pi-package` | every pi session | you are on pi, which has neither a hook nor a plugin |

`setup --status` is a read-only report of what is installed where:

```
scope: user
targets[3]{app,status,path}:
  claude,absent,~/.claude/settings.json
  codex,absent,~/.codex/hooks.json
  opencode,absent,~/.config/opencode/plugins/axi-agent-tasks.js
codex_hooks_feature: enabled
```

Installs are idempotent and self-repairing: re-running `setup` after the script
moves rewrites the stored path instead of adding a second hook, and hooks
belonging to other tools are matched by marker and left alone. `setup
--uninstall` removes it.

The hook runs `tasks.py --ambient`, which differs from the bare home view in one
way: in a directory with no task store it prints **nothing** and exits 0.
Ambient context is paid on every session in every repository, so a tool with
nothing to say there costs nothing. `--ambient` also never exits non-zero, since
a task manager must not be able to break a session start, and a store it cannot
read is reported as `AMBIENT_DEGRADED` rather than swallowed.

The skill is generated from the argparse tree, so it cannot drift from the CLI.
`tasks skill --check` exits 1 when the committed copy is stale, which is how CI
gates it.

---

## 11. Troubleshooting

| Symptom | Code | What to do |
| --- | --- | --- |
| `datafile.py not found` | `DATAFILE_MISSING` | Section 1. Check out `datafile` as a sibling, or set `TASKS_DATAFILE` |
| `datafile.py not found`, but only after `just install` | `DATAFILE_MISSING` | The symlink moved the lookup into your bindir. Section 1: install `datafile` on PATH too, or set `TASKS_DATAFILE` |
| `no task store in <dir>` | - | Not an error. You are in a directory with no queue; `tasks init` or `cd` |
| `task not found: <id>` | `NOT_FOUND` | `tasks list` to see real ids. Ids are slugged, not the title you typed |
| `<id> has N unmet dependency(ies)` | `DEPS_UNMET` | `tasks show <id>` for the list, or finish them first |
| `<id> is in flight, owned by <m>` | `TASK_IN_FLIGHT` | `reset <id> --reason "…"` if the owner is gone. `--force` only if you are certain |
| `verify failed for <id>` | `VERIFY_FAILED` | Section 5. Fix the work, `block`, or `done --force --reason` |
| `this task verifies by prose criterion` | `REASON_REQUIRED` | `done <id> --reason "<how it is satisfied>"` |
| `--force requires --reason` | `REASON_REQUIRED` | An override without a recorded why is not allowed |
| `would create a dependency cycle` | `DEP_CYCLE` | The `cycle` field prints the whole path. Pick an edge and do not add it |
| `unknown dependency: <id>` | `UNKNOWN_DEP` | The dep must exist before you reference it. `add` it first |
| `<id> is already done` | `INVALID_TRANSITION` | `done` results are terminal. `add` a follow-up task instead |
| `<id> is <status>, not blocked` | `INVALID_TRANSITION` | `unblock` only applies to `blocked` |
| `<id> is blocked: <reason>` on `done` | `INVALID_TRANSITION` | `unblock <id>` first, then finish it |
| `<out> is out of date` | `SKILL_STALE` | You changed the CLI. `tasks skill --install` to regenerate, and commit it |
| `<dir> is out of date` | `PACKAGE_STALE` | `tasks pi-package` to regenerate. Only meaningful on the generating machine |
| `uv not found on PATH` | `SETUP_ERROR` | The generated hook shells out to `uv`. Install it |
| Home view shows `unreadable_lines: n` | - | Section 8. `datafile -f tasks.jsonl validate`, and suspect a contract edit first |
| A command appears to hang | - | Another writer holds the flock. Find it with `lsof tasks.jsonl.lock` |
| Two agents disagree about a task | - | Section 4. Something wrote a key it did not own; the trail in section 8 shows what |

**Concurrency.** Locking is advisory `flock`, so it only excludes other
processes that also take the lock. One writer per key at a time is the supported
model and it comes from the fleet's shape, not from the lock. Concurrent readers
are fine.

**Scale.** Reads load the whole store into memory, and `ready` is recomputed on
every read. That is comfortable well past any queue a fleet of agents can
actually work through. If you have enough tasks for this to matter, the problem
is the decomposition, not the store.

---

## 12. Working on tasks itself

```sh
uv run test_tasks.py -q                                       # what CI runs
uv run test_tasks.py --cov=. --cov-branch --cov-report=term-missing
uvx ruff check .                                              # lint
uvx ruff check --fix .                                        # apply safe fixes
uv run tasks.py skill --check                                 # drift gate
```

193 tests, 100% line and branch coverage of `tasks.py`, so **a new branch
without a test will fail CI**. Coverage is not the bar though. The suite is
built around the transitions that can silently corrupt state rather than around
the lines: a second minion taking an in-flight task, a `done` that skips
verification, a cycle entering the graph, a lost update from a concurrent write.
If you change behaviour in those areas, expect a specifically named test to
fail, and be sure you meant it.

**If you add or change a command or a flag, regenerate the skill and commit it.**
It is generated from the argparse tree, so the CLI change silently stales the
committed copy and CI catches it after the fact:

```sh
uv run tasks.py skill --install --scope project --app pi
```

`.github/workflows/ci.yml` runs lint, tests, and the skill drift gate. It checks
out **both repositories as siblings**, which is the layout `tasks.py` already
falls back to, so CI exercises the real `datafile` lookup rather than a special
case. It deliberately **does not pin a `datafile` revision**: this project builds
on that one's internals, so the job doubles as the integration test for that
coupling, and it should go red when `datafile` moves under it.

`pi-package --check` is deliberately not in CI. It bakes the generating
machine's absolute path into its output as a `PATH` fallback, so it can only
pass on the machine that generated it. The skill carries no machine-specific
path, which is why that is the one CI gates.

Section markers (`§7` and similar) in the source comments refer to the
[AXI](https://axi.md) design principles the tool is built to.

---

## Where to go next

- [README.md](README.md) - the design rationale: why verification is a command,
  why there is no priority field, and why `ready` is derived.
- `../datafile/RUNBOOK.md` - the storage engine underneath. Read its
  contract-evolution section before editing `schema-tasks.yaml`.
- `tasks <command> --help` - every subcommand carries its own flags, defaults,
  and a worked example.
- `tasks` with no arguments - when in doubt, it tells you what is here and what
  to run.
