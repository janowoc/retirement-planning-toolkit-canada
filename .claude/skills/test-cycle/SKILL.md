---
name: test-cycle
description: Run the plan -> implement (Sonnet) -> verify (Opus) -> commit-gate workflow for a test suite in this repo. Use when asked to /test-cycle <topic>, or to implement an existing plan from .claude/plans/.
---

# Test cycle

A three-stage workflow with a deliberate model split: plan on Opus, implement on
Sonnet, verify on Opus. The stages are separated so the code is not judged by
whoever wrote it.

`$ARGUMENTS` is the topic slug, e.g. `alberta-schedule`. The plan file is
`.claude/plans/<slug>.md`.

## Stage 1 — Plan (you, Opus, in the main thread)

Do not spawn an agent for this. You are already Opus and already have the repo
context; a fresh planner would re-derive it at Opus rates.

If `.claude/plans/<slug>.md` already exists, read it and confirm with the user
whether to reuse or replace it. Otherwise write it now. The plan is the handoff
artifact — the implementer starts **cold** and sees nothing of this conversation,
so it must stand alone. It needs:

- The behaviour under test, stated as observable claims, not as file names.
- For each claim, the expected value **and its source** (a `docs/CANADA_RULES.md`
  section, or a citation with year and issuing authority). If you cannot source a
  value, say so in the plan rather than leaving the implementer to invent one.
- Which test file each case belongs in, and whether it extends an existing
  parameterized test or needs a new one.
- Anything explicitly out of scope.

`.claude/plans/` is gitignored, so the plan never reaches a commit.

## Stage 2 — Implement (Sonnet subagent)

Dispatch the `test-implementer` agent. Pass it the **absolute path** to the plan
file and the branch name — it cannot see this conversation, so a path plus a
one-line goal is the entire brief. Do not paste the plan contents inline; the
file is the contract.

Do not use `subagent_type: "fork"` here. Forks always run the parent model, so
the Sonnet routing would be silently ignored.

## Stage 3 — Verify (Opus subagent)

Dispatch the `test-verifier` agent with the same plan path. It is read-only by
design and will report rather than repair.

**Relay its findings to the user verbatim, including the pytest output and the
`git status --short` block.** Subagent reports are not shown to the user
directly, so anything you summarize away is lost. Never compress a FAIL into a
reassuring sentence.

If the verdict is FAIL, do not re-dispatch the implementer automatically. Show
the user the findings and let them decide whether to fix, revise the plan, or
abandon.

## Stage 4 — Commit gate

**Stop here and wait for explicit approval.** Do not commit as part of this
workflow.

When approved:

- Stage **explicit paths**, never `git add -A` or `git commit -a`. Stray files
  the agents produced must be removed, not swept in.
- Topic branches for upstream PRs are cut from `upstream/main`; fork-local
  tooling belongs on `main`. Do not mix the two in one commit.
- If the change touches `docs/`, confirm the `.fr.md` counterpart is in the same
  commit — `tests/test_docs_parity.py` fails otherwise, and `docs/**` triggers
  the Pages deploy workflow.
