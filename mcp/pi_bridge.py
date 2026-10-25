#!/usr/bin/env python3
"""pi-bridge — dispatch implementation work to MiniMax via a headless `pi` process.

Claude Code stays the interface and stays on the subscription; this server hands an
already-written, file-level plan to a cheap executor and hands back a SUMMARY.

Transport decision (measured 2026-08-06, receipts in the session that built this):
  - pi-agents RPC is NOT usable from here. `pi-agents/README.md`: the event bus is
    "process-local" and "A `start` request made outside an active session returns an
    error." An external MCP server can never emit into it.
  - `pi -p` (non-interactive) is the entry point that works.
  - `-ne` (no extensions) is REQUIRED. Without it the run completes the edit and then
    never exits — pi-crew holds the event loop open and only stops on SIGTERM
    (observed: 300s timeout, work done, process alive). With `-ne`: exit 0 in 7s.
  - TEXT mode, never `--mode json`. Same task: text = 174 bytes, json = 101_318 bytes.
    MCP tool results are resident in Claude's context and re-billed every later turn,
    so the stream is a cost bug. We return the final report + a diffstat, never a diff.

The gate is deliberately separate from the executor (`pi_gate`): a deterministic
typecheck/lint/test run costs nothing and must be spent before any LLM verification.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "pi-bridge"
SERVER_VERSION = "1.0.0"

DEFAULT_MODEL = "minimax/MiniMax-M3"
# Every executor run lands as one row here (LAW 28: an instrument nobody reads is not an
# instrument; founder 2026-08-27: "hypothesis is minimax speeds us up massively ... investigate").
# `python3 pi_bridge.py --runs` reads it back per model: runs, exit-0 share, median elapsed.
RUN_LOG = os.path.expanduser(os.environ.get("PI_BRIDGE_RUN_LOG", "~/.claude/state/pi-bridge-runs.jsonl"))


# The server is a long-lived process: Python reads this file once at start. Every session's
# MCP server that started before a change to this file keeps running the old bytes, which is
# how the run log stayed empty for a day after log_run landed (crew#533: five servers from
# 26 Aug, log_run written 27 Aug). The row carries the code stamp so --runs can say which
# servers are stale, and pi_execute prints a warning when the file on disk is newer than it.
CODE_PATH = os.path.abspath(__file__)
CODE_MTIME = os.path.getmtime(CODE_PATH) if os.path.exists(CODE_PATH) else 0.0


def code_is_stale() -> bool:
    try:
        return os.path.getmtime(CODE_PATH) > CODE_MTIME + 1
    except OSError:
        return False


def log_run(row: dict, path: str | None = None) -> bool:
    """Append one row. Never silent: a failed write is one line on stderr (LAW 28)."""
    path = path or RUN_LOG
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **row}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        return True
    except (OSError, TypeError, ValueError) as e:
        sys.stderr.write(f"pi-bridge: run row NOT written to {path}: {type(e).__name__}: {e}\n")
        sys.stderr.flush()
        return False


def runs_report(path: str = RUN_LOG) -> str:
    """Per model: runs, share exiting 0, median wall-clock, files touched."""
    import statistics
    try:
        with open(path, encoding="utf-8") as fh:
            rows = [json.loads(ln) for ln in fh if ln.strip()]
    except (OSError, ValueError):
        return "no runs recorded at " + path
    if not rows:
        return "no runs recorded at " + path
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(str(r.get("model") or "?"), []).append(r)
    lines = [f"{'model':<28} {'runs':>4} {'exit0':>6} {'median_s':>8} {'touched':>7}"]
    for m, rs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        ok = sum(1 for r in rs if r.get("rc") == 0) / len(rs) * 100
        med = statistics.median(float(r.get("elapsed", 0)) for r in rs)
        lines.append(f"{m:<28} {len(rs):>4} {ok:>5.0f}% {med:>8.0f} {sum(int(r.get('touched', 0)) for r in rs):>7}")
    return "\n".join(lines)
DEFAULT_TIMEOUT_S = 900
MAX_REPORT_CHARS = 4000
MAX_GATE_TAIL_LINES = 40

# --- Founder fence -----------------------------------------------------------------
# Money rail / identity / contract / migrations never leave Claude. architect-planner.md
# states this in prose to the planner; prose is not enforcement, so it is also a hard
# check here.
#
# The fence used to ban the literal prefix `store_platform/`. That is a directory, not a
# risk: 414 source files live under it and roughly 40 are money surface, so it refused
# the storefront UI, the design system and a read-only screenshot harness in order to
# protect StripeProvider.cs. A fence that blocks work it was never meant to block gets
# routed around by hand, which is how it stops being a fence.
#
# Two changes, and the second is what makes the first safe:
#   1. The patterns name the money surface itself, not its parent directory.
#   2. The fence now runs a SECOND time, after the executor, against the paths git says
#      it actually wrote. The pre-check only ever read a prose plan and could be walked
#      past without lying: "update the payment provider adapter" contains no fenced
#      token, so it passed, and the executor was free to write
#      store_platform/src/Store.Api/Payments/StripeProvider.cs unchecked. git cannot be
#      talked around.
#   3. Two tiers, because one list was doing two jobs at one strictness. `\bcheckout`
#      is a DOMAIN WORD, not a money surface: it appears in every plan that so much as
#      refuses a purchase, so it refused the commerce-mode work (a config switch and two
#      HTTP refusal codes) which mints nothing and charges nobody. Measured 2026-08-15:
#      the P1 plan of docs/SUBSCRIPTION_PROGRAM.md §17 came back
#      `REFUSED ... ['Checkout', 'checkout']`. Every genuinely dangerous checkout
#      operation is already named by another token — `\bstripe`, `/Payments/`,
#      `\bwebhook` — so HARD loses nothing by dropping it.
#
# HARD    = never dispatched, and a breach if the executor writes it anyway.
# REVIEW  = dispatched freely, but the run report cannot be read without seeing it.
# The point of the split is that "the executor does everything and Claude reviews" needs
# a tier that means REVIEW. Before this, the only way to say "read this carefully" was
# to say "never", and never is what gets routed around by hand at full price.
HARD_PATTERNS = [
    # the money rail proper
    r"\bbridge\.py\b",
    r"\bpricing\.py\b",
    r"/Payments?/",
    # No TRAILING \b on these: it needs a non-word char, and CamelCase never gives one,
    # so `\bcheckout\b` silently missed CheckoutEndpoints.cs. Leading \b only.
    r"\bstripe",
    r"\bpaddle",
    r"\bwebhook",
    r"\bentitlement",
    r"PackPrice",
    r"MoneyRail",
    # identity
    r"/Auth/",
    r"/Identity/",
    # contract
    r"/Contracts?/",
    # migrations
    r"\bmigrations?/",
    r"\balembic\b",
]

# Allowed to be written; impossible to miss in the report afterwards.
REVIEW_PATTERNS = [
    r"\bcheckout",
    r"/Endpoints?/",
    r"\bfulfilment",
    r"\bfulfillment",
]

HARD_RE = re.compile("|".join(HARD_PATTERNS), re.IGNORECASE)
REVIEW_RE = re.compile("|".join(REVIEW_PATTERNS), re.IGNORECASE)
# Back-compat: FENCE_RE has always meant "refuse this".
FENCE_RE = HARD_RE
FENCE_PATTERNS = HARD_PATTERNS


def fence_violations(text: str) -> list[str]:
    """Prose pre-check. HARD only — REVIEW surface is allowed to be planned."""
    return sorted({m.group(0) for m in HARD_RE.finditer(text or "")})


def fenced_paths(paths: list[str]) -> list[str]:
    """The subset of `paths` that sits on fenced surface. Exact, unlike the prose check."""
    return [p for p in paths if HARD_RE.search(p)]


def review_paths(paths: list[str]) -> list[str]:
    """Written, allowed, and not to be signed off unread."""
    return [p for p in paths if REVIEW_RE.search(p) and not HARD_RE.search(p)]


# --- helpers -----------------------------------------------------------------------

def run(argv: list[str], cwd: str, timeout: int, env: dict | None = None):
    """Run a subprocess, never raising on non-zero. Returns (rc, stdout, stderr)."""
    try:
        p = subprocess.run(
            argv, cwd=cwd, timeout=timeout, capture_output=True, text=True,
            env=env or os.environ.copy(), stdin=subprocess.DEVNULL,
        )
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        out = e.stdout or ""
        err = e.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        return 124, out, err + f"\n[timed out after {timeout}s]"
    except FileNotFoundError as e:
        return 127, "", str(e)


def git(cwd: str, *args: str, timeout: int = 30):
    return run(["git", *args], cwd, timeout)


# Build droppings are not edits; listing them as "touched" hides the real changes.
NOISE_RE = re.compile(r"(__pycache__|\.pyc$|\.pyo$|\.pytest_cache|\.ruff_cache|"
                      r"\.mypy_cache|node_modules/|\.DS_Store$)")


def is_noise(path: str) -> bool:
    return bool(NOISE_RE.search(path))


def truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[:n] + f"\n…[truncated {len(s) - n} chars — full output stayed out of context]"


def tail(s: str, lines: int) -> str:
    ls = (s or "").strip().splitlines()
    if len(ls) <= lines:
        return "\n".join(ls)
    return f"…[{len(ls) - lines} earlier lines omitted]\n" + "\n".join(ls[-lines:])


# --- tools -------------------------------------------------------------------------

def tool_pi_execute(a: dict) -> str:
    plan = (a.get("plan") or "").strip()
    cwd = a.get("cwd") or ""
    model = a.get("model") or DEFAULT_MODEL
    timeout_s = int(a.get("timeout_s") or DEFAULT_TIMEOUT_S)
    include_context_files = bool(a.get("include_context_files"))

    if not plan:
        return "ERROR: `plan` is required. Write the file-level plan first; the executor does not design."
    if not cwd or not os.path.isabs(cwd):
        return "ERROR: `cwd` must be an absolute path."
    if not os.path.isdir(cwd):
        return f"ERROR: cwd does not exist: {cwd}"

    hits = fence_violations(plan)
    if hits:
        return ("REFUSED — founder fence.\n"
                f"The plan touches money-rail/identity/contract/migration surface: {hits}\n"
                "That work never leaves Claude Code. Implement it yourself in this session.")

    if not shutil.which("pi"):
        return "ERROR: `pi` not on PATH. This server shells out to the pi CLI."

    # Require a git repo: it is the only thing that makes the executor's writes
    # reviewable and reversible. Without it there is no way to see what it changed.
    rc, _, _ = git(cwd, "rev-parse", "--git-dir")
    if rc != 0:
        return (f"ERROR: {cwd} is not a git repository.\n"
                "Refusing to let a non-interactive executor write to an unversioned tree — "
                "the diff is the entire audit trail.")

    _, head_before, _ = git(cwd, "rev-parse", "HEAD")
    _, dirty_before, _ = git(cwd, "status", "--porcelain")
    pre_dirty = {ln[3:] for ln in dirty_before.splitlines() if len(ln) > 3}

    argv = [
        "pi", "-p", "-ne",              # -ne is load-bearing: without it pi never exits
        "--provider", model.split("/", 1)[0],
        "--model", model.split("/", 1)[-1],
        "--no-session",
        "--approve",
    ]
    if not include_context_files:
        argv.append("--no-context-files")
    argv.append(
        "You are the EXECUTOR. Implement the following approved plan exactly as written.\n"
        "Do NOT redesign, do NOT expand scope, do NOT 'improve' beyond it.\n"
        "Match the surrounding code's style, naming and comment density.\n"
        "Read every file before you edit it. If the plan is ambiguous or wrong, STOP and\n"
        "report the ambiguity instead of guessing.\n"
        "Report every file you changed and what you changed in it. If you skipped part of\n"
        "the plan or something did not work, say so explicitly — a verifier checks your work.\n\n"
        "=== PLAN ===\n" + plan
    )

    # The row is written in `finally`, so it lands for every outcome: exit 0, non-zero,
    # timeout, or an exception between launching pi and building the report.
    row = {"kind": "execute", "model": model, "rc": None, "elapsed": None, "touched": 0,
           "cwd": cwd, "timed_out": False, "head_moved": False, "error": None,
           "code_mtime": int(CODE_MTIME)}
    t0 = time.time()
    try:
        rc, out, err = run(argv, cwd, timeout_s)
        elapsed = time.time() - t0
        row.update(rc=rc, elapsed=round(elapsed, 1), timed_out=rc == 124)

        _, dirty_after, _ = git(cwd, "status", "--porcelain")
        post = {ln[3:]: ln[:2] for ln in dirty_after.splitlines() if len(ln) > 3}
        touched = sorted(p for p in post if p not in pre_dirty and not is_noise(p))
        _, diffstat, _ = git(cwd, "diff", "--stat")
        _, head_after, _ = git(cwd, "rev-parse", "HEAD")
        row.update(touched=len(touched), head_moved=head_before.strip() != head_after.strip())
    except BaseException as e:
        row.update(elapsed=round(time.time() - t0, 1), error=f"{type(e).__name__}: {e}")
        raise
    finally:
        log_run(row)

    parts = [
        f"executor: {model}   exit={rc}   elapsed={elapsed:.0f}s",
        f"cwd: {cwd}",
    ]
    if code_is_stale():
        parts.append("!! STALE SERVER — pi_bridge.py on disk is newer than this process. "
                     "Restart the MCP server (/mcp) so the next run uses the current code.")
    if rc == 124:
        parts.append("!! TIMED OUT — the tree may be half-edited. Check the diff before continuing.")
    if head_before.strip() != head_after.strip():
        parts.append(f"!! HEAD MOVED {head_before.strip()[:8]} -> {head_after.strip()[:8]} — the executor committed. Review it.")
    if pre_dirty:
        parts.append(f"note: {len(pre_dirty)} path(s) were ALREADY dirty before this run; "
                     "they are excluded from 'files newly touched' but appear in the diffstat.")

    # The fence, applied to what actually happened rather than to what was proposed.
    # Deliberately NOT auto-reverted: a revert would also destroy the legitimate part of
    # the same run, and a silent destructive action is worse than an unmissable flag when
    # the standing workflow already reads every diff. The revert command is printed so the
    # decision is one paste away.
    breaches = fenced_paths(touched)
    if breaches:
        parts.insert(0, "!! FENCE BREACH — the executor wrote to money-rail/identity/contract/"
                        "migration surface:\n  " + "\n  ".join(breaches) +
                        "\nThe plan did not name it, so the pre-check could not see it. Review "
                        "these files line by line before anything else, and revert unless you "
                        "are certain:\n  git -C " + cwd + " checkout -- " + " ".join(breaches))

    needs_review = review_paths(touched)
    if needs_review:
        parts.insert(0 if not breaches else 1,
                     "!! REVIEW REQUIRED — the executor wrote money-ADJACENT surface. Not a breach; "
                     "these are allowed. Read every line before accepting the run:\n  "
                     + "\n  ".join(needs_review))

    parts.append("\n--- files newly touched ---\n" + ("\n".join(touched) if touched else "(none)"))
    parts.append("\n--- git diff --stat ---\n" + (truncate(diffstat, 2000) or "(clean tree)"))
    parts.append("\n--- executor report ---\n" + (truncate(out, MAX_REPORT_CHARS) or "(no stdout)"))
    if err.strip():
        parts.append("\n--- stderr ---\n" + tail(err, 15))
    if not touched and rc == 0:
        parts.append("\n!! The executor reported success but changed NO files. Treat its report as unproven.")
    parts.append("\nNEXT: run `pi_gate` (free, deterministic) before spending any LLM verification.")
    return "\n".join(parts)


def tool_pi_gate(a: dict) -> str:
    cwd = a.get("cwd") or ""
    cmds = a.get("commands") or []
    timeout_s = int(a.get("timeout_s") or 600)

    if not cwd or not os.path.isabs(cwd) or not os.path.isdir(cwd):
        return "ERROR: `cwd` must be an existing absolute path."
    if not cmds:
        return "ERROR: `commands` is required — give the exact verification invocations from the plan."

    results, failed = [], 0
    for cmd in cmds:
        t0 = time.time()
        rc, out, err = run(["bash", "-lc", cmd], cwd, timeout_s)
        dt = time.time() - t0
        status = "PASS" if rc == 0 else ("TIMEOUT" if rc == 124 else "FAIL")
        if rc != 0:
            failed += 1
        block = f"[{status}] exit={rc} ({dt:.0f}s)  $ {cmd}"
        if rc != 0:
            combined = (out or "") + ("\n" + err if err else "")
            block += "\n" + tail(combined, MAX_GATE_TAIL_LINES)
        else:
            block += "\n" + tail(out, 3)
        results.append(block)

    verdict = "GATE PASS" if failed == 0 else f"GATE FAIL ({failed}/{len(cmds)} failed)"
    note = ("\nAll gates green. This is necessary, not sufficient — verify the diff against the plan."
            if failed == 0 else
            "\nSend these failures back to `pi_execute` as a correction plan, or fix them here.")
    return verdict + "\n\n" + "\n\n".join(results) + note


TOOLS = [
    {
        "name": "pi_execute",
        "description": (
            "Hand an ALREADY-WRITTEN, file-level implementation plan to a cheap executor "
            "(MiniMax via headless pi) that edits files in `cwd`. Use for mechanical bulk "
            "implementation once you have decided exactly what to change. It does not design, "
            "and it is not a substitute for your own judgement — you verify its work afterwards. "
            "Requires a git repo (the diff is the audit trail). Refuses money-rail/identity/"
            "contract/migration work. Returns a summary + diffstat, never a full diff."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "plan": {"type": "string", "description": "Exact file paths, functions/lines, edit intent per file, and the verification commands. Self-contained: the executor sees only this."},
                "cwd": {"type": "string", "description": "Absolute path to the git repo to edit."},
                "model": {"type": "string", "description": f"provider/model. Default {DEFAULT_MODEL}."},
                "timeout_s": {"type": "integer", "description": f"Default {DEFAULT_TIMEOUT_S}."},
                "include_context_files": {"type": "boolean", "description": "Let the executor load CLAUDE.md/AGENTS.md. Default false (costs tokens; the plan should carry the constraints)."},
            },
            "required": ["plan", "cwd"],
        },
    },
    {
        "name": "pi_gate",
        "description": (
            "Run deterministic verification commands (typecheck/lint/tests) in `cwd` and return "
            "a verdict plus only the failing tail. Free and LLM-free — always spend this before "
            "any model-based verification of executor output."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cwd": {"type": "string", "description": "Absolute path."},
                "commands": {"type": "array", "items": {"type": "string"}, "description": "Exact shell invocations, e.g. ['.venv/bin/python -m pytest -q tests/unit', 'npm run typecheck']."},
                "timeout_s": {"type": "integer", "description": "Per command. Default 600."},
            },
            "required": ["cwd", "commands"],
        },
    },
]

HANDLERS = {"pi_execute": tool_pi_execute, "pi_gate": tool_pi_gate}


# --- JSON-RPC stdio loop -----------------------------------------------------------

def reply(mid, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": mid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        method, mid, params = req.get("method"), req.get("id"), req.get("params") or {}

        if method == "initialize":
            reply(mid, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })
        elif method == "tools/list":
            reply(mid, {"tools": TOOLS})
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            fn = HANDLERS.get(name)
            if fn is None:
                reply(mid, error={"code": -32601, "message": f"unknown tool: {name}"})
                continue
            try:
                text = fn(args)
                reply(mid, {"content": [{"type": "text", "text": text}]})
            except Exception as e:  # never kill the server on one bad call
                reply(mid, {"content": [{"type": "text", "text": f"ERROR: {type(e).__name__}: {e}"}],
                            "isError": True})
        elif mid is not None:
            reply(mid, error={"code": -32601, "message": f"unknown method: {method}"})
        # notifications (no id) are ignored


if __name__ == "__main__":
    if "--runs" in sys.argv[1:]:
        print(runs_report(sys.argv[sys.argv.index("--runs") + 1] if len(sys.argv) > sys.argv.index("--runs") + 1 else RUN_LOG))
        sys.exit(0)
    main()
