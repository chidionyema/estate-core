---
name: ship-a-pr
description: The sequence for getting a change from a working tree to merged and running in production in this estate. Load before opening a PR, before pushing a branch, or when asked whether work has shipped.
---

# Shipping a change here

"Ship means shipped": commit, push, raise the PR, follow it to merged, then prove production
runs it. Stopping anywhere before the end is not shipping.

## Before you push

1. **Review your own diff with a fresh pair of eyes.** Run the `receipt-auditor` subagent on the
   change. It reads the diff with no memory of why you wrote it, which is the whole point — the
   session that wrote the code is the worst reviewer of it, because it already believes the
   reasoning. Six finding classes only: an unproven claim, a message and code that disagree, a
   claim already stale, a guard that cannot fail, a test that asserts structure rather than a
   rule, and correctness. Founder decision 2026-08-19: this is routine before every PR, not
   something to be asked for.
2. **Run the gate**: `.venv/bin/python scripts/popdd_verify.py --staged`. There is no pre-commit
   hook installed by default in this checkout — check with `git config --get core.hooksPath`
   and `ls -la "$(git rev-parse --git-path hooks)"/pre-commit` rather than assuming either way.
3. **Say which rung each new test is.** Founder ruling 2026-08-22, the ladder is in
   `~/.claude/AGENTS.md` under "How to test". Before writing a test, ask in order: can this be a
   type, a property, a differential case against the old path, or an incident test named for a bug
   that actually happened? If none apply, say so in the PR body and write nothing. A PR adding
   twenty `test_foo_returns_bar` cases fails review on policy, not taste.
4. **Stage explicit paths.** Never `git add -A` or `git add .`: `store/` and `storage/` are
   tracked runtime state that pytest writes to, so `-A` commits another process's output.

## Push and follow

5. `git push -u origin <branch>` then `gh pr create`. A pushed branch with no PR is invisible.
6. **Follow it to merged.** Watch the checks in the background; never poll in the foreground.
   CI runs on the Fly app `prospector-ci`, not on this Mac — the three local `mumchimp-mac*`
   runners are OFF by founder decision and must not be started. A queued PR is capacity.
7. **Prove production runs it.** Merged is not deployed. `.venv/bin/python scripts/live_checkout.py`
   answers where production is and how far behind it sits; `--update` rolls it forward.

## Then close the loop

8. If the work fixed something that can recur mechanically, a test fails if it recurs, and a
   memory file names the trap. Write it at the moment of the lesson.
