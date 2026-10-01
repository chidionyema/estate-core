---
name: worktree-and-gate
description: How to make a git worktree in this repo that actually works, and the state of the POPDD pre-commit gate. Load before `git worktree add`, before diagnosing a commit that failed with only an exit code, or when ruff or a missing .venv walls a commit.
---

# Working in a git worktree, and the POPDD gate

This was 1,742 tokens of CLAUDE.md, injected into every session. Most sessions never make a
worktree. The rules that must survive without it are still in CLAUDE.md; everything below is the
history and the traps, and it loads on demand.

**The gate is ON as of 2026-08-21, so budget 7–25 minutes for every `git commit`.** This
paragraph has now been wrong in three directions, which is exactly why the two commands below
exist — read them, never this prose.

Measured 2026-08-21: `git config --get core.hooksPath` is empty, and
`.git/hooks/pre-commit` is a symlink to `.lux/hooks/pre-commit`, dated 20 Aug 09:11. So
`git commit` runs the full gate, in-process, holding `.git/index.lock` for the whole run.

The line this replaced said "As of 2026-08-17 there is NO pre-commit gate in this checkout.
Nothing stops a bad commit locally." That was true when written and false four days later. A
dated measurement of a setting somebody flips by hand has a shelf life, and this one is kept
only as history — run the commands.

History, because both states have happened: the founder disabled the gate on 2026-08-14 by
moving `.git/hooks/pre-commit` aside. On 2026-08-15 at 18:57 someone set `core.hooksPath` to
`.git/hooks-active`, which symlinked `pre-commit` to `.lux/hooks/pre-commit` — and
**`core.hooksPath` overrides the hooks directory entirely, so moving the old hook aside did
nothing while it was set.** That cost a session on 2026-08-16: a commit failed with only
"exit code 1" while the doc said no gate could have refused it. The setting has since been
unset again.

Check which it is, never trust this paragraph:

```bash
git config --get core.hooksPath          # set => THAT directory wins, not .git/hooks
ls -la "$(git rev-parse --git-path hooks)"/pre-commit
```

To actually disable it: `git config --unset core.hooksPath` (and only then does moving
`.git/hooks/pre-commit` aside take effect). To enable: point `core.hooksPath` at a directory
whose `pre-commit` links to `.lux/hooks/pre-commit`.

This cost a session on 2026-08-16: a commit failed with only "exit code 1", and the doc said no
gate could have refused it. The gate had refused it, on one test out of 4124.

**The gate CAN pass, and the number that said otherwise is dead.** This file used to carry "the
suite measures ~3185s serially against a 2400s ceiling, so the gate cannot pass". That sentence
was prose, not a measurement, and it was quoted as fact in a session on 2026-08-16 before anyone
checked it. `pytest.ini:42` sets `addopts = -n auto --dist loadfile`, so nothing runs serially.
Two timings, both real: the gate's own python-lane commands on clean `main` (`0e1e939`) measured
**1.7s of ruff plus 445.5s of pytest, 3925 passed and 3 skipped — 7m25s against the 2400s
ceiling at `scripts/popdd_verify.py:86`, 19% of it**; the merged tree on 2026-08-16, timed while
four CI jobs shared the box, measured **1281.41s, 4612 passed and 3 skipped — 21m21s, 53% of the
ceiling**. Both pass. If you are about to repeat a timing claim from this paragraph, time it
again: the suite grows, and the ceiling does not.

**Install it where git actually LOOKS.** `core.hooksPath` is set in `.git/config` to
`.git/hooks-active`, which makes `.git/hooks/` inert as a DIRECTORY — anything written there is
never read, so the re-enable line this file carried until 2026-08-15
(`ln -s ../../.lux/hooks/pre-commit .git/hooks/pre-commit`) was silently a no-op. The live
control point is:

```bash
# ON. Two deliberate choices. The target is ABSOLUTE, because the link lives in
# .git/hooks-active/ and a relative target would resolve against THAT directory. And it is the
# MAIN checkout's copy, not `--show-toplevel`, because hooks-active sits in the COMMON git dir
# and is shared by every worktree — one link, so the gate cannot be half-on.
ln -sfn "$(dirname "$(cd "$(git rev-parse --git-common-dir)" && pwd -P)")/.lux/hooks/pre-commit" \
        "$(git rev-parse --git-path hooks)/pre-commit"
# OFF
rm "$(git rev-parse --git-path hooks)/pre-commit"
```

Two things the gate now depends on, both of which fail by accusing something else. **`ruff` runs
REPO-WIDE** (`scripts/popdd_verify.py:166`), so one unformatted file anywhere walls every commit
in every worktree — `main` itself carried 12 such errors until they were cleared for this
(2b38ca3), and a worktree still sitting on an older base will fail ruff until it rebases. And
**every worktree needs `.venv` and `.lux/keys/agent.pem`**, neither of which `git worktree add`
creates; without them the gate is BLOCKED over a missing interpreter or an unsigned receipt.
`./scripts/setup_worktree.sh <path>` is the only correct way to make a worktree, and now it is
load-bearing rather than a convenience.

The wedge risk is smaller but not gone: the gate runs INSIDE the hook, so `git commit` holds
`.git/index.lock` for the whole run — now bounded at ~7.5 minutes rather than the 49 minutes that
blocked three sessions on 2026-08-14. `_run_step` kills the process GROUP and drains the pipes,
which is what fixed that specific hang. Preflight a change without committing:
`.venv/bin/python scripts/popdd_verify.py --staged`.

**One session, one worktree** still stands, for the index rather than the gate: sessions sharing
this checkout share one `.git/index`, and `git worktree add` succeeds even while that index is
locked, which is exactly the point. `scripts/popdd_verify.py::single_flight` still refuses a
second gate run in the same tree in under a second when you invoke it by hand
(pinned by `tests/unit/test_popdd_gate_cannot_wedge.py`).

For a Python-only change, skip `node_modules`: the `cp -Rc` clone is the slow part of setup
(>5 min) and the web lane never runs on a diff that contains no web files.

This checkout is often shared by two concurrent sessions, so a worktree is how you merge, build or test without touching another session's tree and index. But `git worktree add` produces a tree that **looks** complete and is not, and each gap fails by accusing something else. Always run:

```bash
git worktree add --detach ../my-worktree <ref>
./scripts/setup_worktree.sh ../my-worktree
```

It fixes four traps, each of which misdirects the diagnosis (detail: memory `worktree-setup-is-a-script-now.md`): **`node_modules` cannot be symlinked** (Turbopack rejects any symlink leaving the project root, same filesystem or not — use `cp -Rc`, an APFS copy-on-write clone); **`.lux/keys/agent.pem` is untracked**, so the shared POPDD hook runs then fails for want of a signing key, reading as a gate violation; **`.venv` is absent while `.lux/hooks/pre-commit:67` pins `.venv/bin/python` relative to cwd**, so commits die with `POPDD gate BLOCKED` over a missing interpreter (a symlink is fine here — `node_modules` is the odd one out); **`store/` and `storage/` are tracked runtime state that pytest writes to**, so never `git add -A` in a worktree.

Two more traps that outlive the setup script: `npm run build 2>&1 | tail` reports **tail's** exit status, so a failed build reads as `exit 0` — capture the build's own status before any pipe. And anything reading `<root>/.git/…` as a directory is a bug: in a worktree `.git` is a **file** containing `gitdir:`. Ask git instead (`git rev-parse --git-path hooks`, `--git-common-dir`), which also honours `core.hooksPath`; `tests/unit/test_popdd_gate_lanes.py` had exactly this defect and reported the POPDD gate uninstalled in a checkout where it was installed and working.
