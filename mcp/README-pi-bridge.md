# pi-bridge — you prompt, Claude plans, MiniMax executes, Claude verifies

Built 2026-08-06. Hardened 2026-11-06 (launcher production program v2). Lets Claude Code
hand mechanical implementation work to a cheap model (MiniMax-M3) while staying on your
subscription, then verify the result itself.

---

## How you use it: you don't. You just prompt.

There is nothing to type. Ask for work the way you always have:

> "Add retry-with-backoff to the three fetch helpers in retrieval.py"

Claude writes the plan, dispatches it to MiniMax, runs the tests, reads the diff, and reports.
A directive injected once per session (by `context-guard-hook.py`) tells it to work this way.

**You only need to know three things:**

1. **It requires a fresh session.** MCP servers load at startup. If Claude says the tool
   isn't available, `/clear` or restart Claude Code.
2. **It only fires for bulk/mechanical work.** One-line edits stay inline — a dispatch
   round-trip costs more than the edit. That's deliberate.
3. **Money rail work is refused by design.** See the fence below.

### Making it explicit

If you want to force or forbid delegation in a given turn:

- **Force:** "delegate this to the executor" / "use pi_execute for this"
- **Forbid:** "do this yourself" / "don't delegate"

### Checking it's alive

```bash
claude mcp list          # expect: pi-bridge: ✔ Connected
```

---

## What actually happens on a dispatch

```
your prompt
  └─ Claude PLANS          (stays here, on your subscription — this is the judgement)
       └─ pi_execute       MiniMax-M3 edits the files      ~40s, pennies
            └─ pi_gate     your tests/typecheck/lint       free, no LLM
                 └─ Claude VERIFIES the real diff (not the executor's summary)
```

The executor sees **only the plan text** — no conversation history, no context files by
default. That is what makes it cheap, and it is why the plan has to be self-contained.

---

## The two tools (Claude calls these, not you)

| Tool | Purpose |
|---|---|
| `pi_execute(plan, cwd, model?, timeout_s?, include_context_files?)` | Runs the plan through MiniMax. Returns the executor's report + `git diff --stat` + files newly touched. |
| `pi_gate(cwd, commands, timeout_s?)` | Runs deterministic verification. Returns a verdict plus only the failing tail. |

Defaults: `minimax/MiniMax-M3`, 900s timeout (hard ceiling 3600s), context files **off**.

---

## Safety rails (enforced in the server, not in a prompt)

- **Founder fence, two tiers.** Plans naming the money rail itself — `pricing.py`,
  `bridge.py`, `/Payments/`, `stripe`, `paddle`, `webhook`, `entitlement`, `PackPrice`,
  `MoneyRail`, `/Auth/`, `/Identity/`, `/Contracts?/`, `migrations/`, `alembic` — are
  **refused** outright (HARD). Money-ADJACENT surface — `checkout`, `/Endpoints?/`,
  `fulfilment` — is dispatched, but the run report cannot be read without seeing it
  (REVIEW). After the run, the fence checks the paths git says were actually written,
  not just the prose plan — the plan can omit a target; git cannot.
- **Git required.** It refuses to write to a non-git directory — the diff is the entire
  audit trail of what a non-interactive model did to your files.
- **One run per working tree.** A second concurrent `pi_execute` in the same cwd is
  refused instead of corrupting the dirty-file attribution.
- **Timeout ceiling.** `timeout_s` is clamped to 3600s; an unbounded timeout cannot hold
  a tree hostage.
- **Model is validated.** `model` must be `provider/name`; a bare name errors instead of
  dispatching to a provider nobody asked for.
- **Tamper-evident run log.** Every run appends an HMAC-SHA256-chained row to the run
  log (key at `~/.claude/state/pi-bridge-hmac-key`, 0600, outside the log). Verify with
  `python3 pi_bridge.py --verify`. Deleting or rewriting history breaks the chain loudly.
- **It reports honestly.** If the executor claims success but changed no files, or if HEAD
  moved because it committed, or if it timed out mid-edit, the result says so explicitly.
- **Pre-existing dirty files are excluded** from "files newly touched", so a concurrent
  session's work isn't misattributed to the executor. Renames attribute to the new path.
- **The fence test is CI-enforced.** `.github/workflows/pi-bridge-fence.yml` runs
  `mcp/test_pi_bridge_fence.py` on every PR and push to main.

---

## Why it's built this way (so nobody "fixes" it back)

Three findings, each measured, each of which looks like something else when it bites:

- **`pi -p` needs `-ne` (no extensions) or it never exits.** Without it the run completes
  the edit *correctly* and then hangs forever — `pi-crew` holds the event loop open and
  only releases on SIGTERM. Observed: 300s timeout, exit 143, work done. With `-ne`:
  exit 0 in 7s. This failure reads as a broken executor and is not one.
- **Text mode, never `--mode json`.** Identical task: text = 174 bytes, json = 101,318
  bytes of event stream. MCP results sit in Claude's context and are re-billed every
  later turn, so returning the stream would be a pure cost bug. A real dispatch returns
  ~1,300 chars.
- **pi-agents RPC cannot be driven from outside pi.** Its README states the event bus is
  process-local and that `start` outside an active session errors. The original design
  dispatched over that bus and was unbuildable; `pi -p` replaced it.

---

## Files

| Path | Role |
|---|---|
| `~/.claude/mcp/pi_bridge.py` | The server |
| `~/.claude/mcp/test_pi_bridge_fence.py` | Fence + hardening regression guard (CI-enforced) |
| `~/.claude.json` → `mcpServers["pi-bridge"]` | Registration (user scope) |
| `~/.claude/scripts/context-guard-hook.py` | `PEV_DIRECTIVE`, injected once per session |
| `~/.claude/state/pi-bridge-runs.jsonl` | HMAC-chained run log |
| `~/.claude/state/pi-bridge-hmac-key` | Chain key (0600, outside the log) |

## Turning it off

```bash
claude mcp remove pi-bridge -s user
```

The hook self-disables: the directive is gated on `pi_bridge_registered()`, so removing
the server also stops the injection. No second cleanup step.
