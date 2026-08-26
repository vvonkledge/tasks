---
name: agent-tasks
description: >-
  Queue, dispatch, and verify work for a fleet of agents from the command
  line. Use when an orchestrator needs to create tasks, hand one to a worker
  agent, record why something is blocked, reclaim an abandoned task, or find
  out what is ready to run next.
---

# agent-tasks

Agent task manager where one orchestrator writes and minions report.

## Ownership

One orchestrator creates tasks and dispatches them. Each worker updates
only the task it owns. A task in `doing` belongs to its owner, and the
orchestrator treats it as read-only; ownership transfers on `start` and
returns on any terminal transition. Workers never create tasks: a worker
that finds new work runs `block`, and the orchestrator queues the
unblocker and wires it with `dep`.

## Verification

Every task carries a `--verify` command that `done` runs; a non-zero exit
refuses the transition. `--prose` marks a criterion asserted instead, and
`done` then requires `--reason`. `--force --reason` overrides either and
records why, so a forced completion stays distinguishable from a verified
one.

## Commands

Run `tasks.py <command> --help` for the full reference on any of these.

### init

create the contract for the task store

```sh
tasks.py init
```

### add

create a task (orchestrator only)

```sh
tasks.py add --verify VERIFY [--prose] [--dep ID] [--context PATH] title
```

### show

one task with all fields

```sh
tasks.py show [--full] id
```

### list

tasks as a table

```sh
tasks.py list [--status {todo,doing,done,blocked}] [--stale] [--fields A,B,C] [--limit LIMIT]
```

### start

dispatch a task to a minion (orchestrator only)

```sh
tasks.py start --owner OWNER [--force] id
```

### done

finish a task; runs its verify command (owner only)

```sh
tasks.py done [--reason REASON] [--force] [--timeout TIMEOUT] id
```

### block

mark a task stuck (owner only)

```sh
tasks.py block --reason REASON id
```

### unblock

return a blocked task to the queue

```sh
tasks.py unblock id
```

### reset

reclaim an orphaned in-flight task

```sh
tasks.py reset --reason REASON id
```

### dep

make one task wait on another

```sh
tasks.py dep --on ID [--force] id
```

### setup

install the session-start integration (§7)

```sh
tasks.py setup [--scope {user,project}] [--app {all,claude,codex,opencode}] [--status] [--uninstall]
```

### skill

generate the on-demand agent skill (§7)

```sh
tasks.py skill [--out OUT] [--install] [--uninstall] [--scope {user,project}] [--app {all,claude,pi}] [--check]
```

### pi-package

generate the pi package (§7)

```sh
tasks.py pi-package [--out OUT] [--check]
```

### drop

remove a task from the queue

```sh
tasks.py drop --reason REASON [--force] id
```

## Output

TOON on stdout, including errors; nothing is written to stderr.
Exit 0 is success including idempotent no-ops, 1 means the request
could not be satisfied, 2 is a usage or contract error.
