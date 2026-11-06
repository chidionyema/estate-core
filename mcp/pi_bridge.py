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

Production hardening (2026-11-06, launcher program v2):
  - MAX_TIMEOUT_S caps the caller-supplied timeout — an agent can no longer pass an
    unbounded timeout and hold a working tree hostage.
  - `model` must be `provider/name`; a bare model name used to mis-split silently and
    dispatch to a provider that was never asked for.
  - Renames (`R  old -> new`) are parsed correctly out of git porcelain; the old code
    read the whole "old -> new" string as one path and could mis-attribute writes.
  - One run per working tree: a flock on a per-cwd lockfile refuses a second
    concurrent pi_execute instead of corrupting the dirty-file attribution.
  - The run log is now an HMAC-SHA256 chain (key lives outside the log at
    ~/.claude/state/pi-bridge-hmac-key, 0600): each row commits to the hash of the
    previous row, so silent deletion or rewriting of history is detectable with
    `python3 pi_bridge.py --verify`.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import sys
import time

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "pi-bridge"
SERVER_VERSION = "1.1.0"

DEFAULT_MODEL = "minimax/MiniMax-M3"
# Every executor run lands as one row here (LAW 28: an instrument nobody reads is not an
# instrument; founder 2026-08-27: "hypothesis is minimax speeds us up massively ... investigate").
# `python3 pi_bridge.py --runs` reads it back per model: runs, exit-0 share, median elapsed.
RUN_LOG = os.path.expanduser(os.environ.get("PI_BRIDGE_RUN_LOG", "~/.claude/state/pi-bridge-runs.jsonl"))
# The chain key lives OUTSIDE the log: an attacker (or a buggy writer) who can edit the
# log still cannot recompute the chain without the key. Created on first use, 0600.
HMAC_KEY_PATH = os.path.expanduser(os.environ.get("PI_BRIDGE_HMAC_KEY", "~/.claude/state/pi-bridge-hmac-key"))
LOCK_DIR = os.path.expanduser(os.environ.get("PI_BRIDGE_LOCK_DIR", "~/.claude/state/pi-bridge-locks"))
GENESIS_HASH = "0" * 64


def _chain_key() -> bytes:
    try:
        with open(HMAC_KEY_PATH, "rb") as fh:
            return fh.read().strip()
    except OSError:
        os.makedirs(os.path.dirname(HMAC_KEY_PATH), exist_ok=True)
        key = os.urandom(32).hex().encode()
        fd = os.open(HMAC_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        return key


def _last_hash(path: str = RUN_LOG) -> str:
    prev = GENESIS_HASH
    try:
        with open(path, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln:
                    try:
                        prev = json.loads(ln).get("hmac", prev)
                    except ValueError:
                        pass
    except OSError:
        pass
    return prev


def log_run(row: dict) -> None:
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **row}
    prev = _last_hash(RUN_LOG)
    payload = json.dumps({"prev": prev, **row}, sort_keys=True)
    row["hmac"] = hmac.new(_chain_key(), payload.encode(), hashlib.sha256).hexdigest()
    try:
        os.makedirs(os.path.dirname(RUN_LOG), exist_ok=True)
        with open(RUN_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


def verify_chain(path: str = RUN_LOG) -> str:
    """Recompute the HMAC chain over the log. Any gap, reorder or edit fails loudly."""
    prev, n, bad = GENESIS_HASH, 0, []
    key = _chain_key()
    try:
        with open(path, encoding="utf-8") as fh:
            lines = [ln for ln in fh if ln.strip()]
    except OSError:
        return "no run log at " + path
    for i, ln in enumerate(lines, 1):
        n += 1
        try:
            row = json.loads(ln)
        except ValueError:
            bad.append(f"line {i}: not JSON")
            continue
        mac = row.pop("hmac", None)
        payload = json.dumps({"prev": prev, **row}, sort_keys=True)
        want = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
        if mac is None or not hmac.compare_digest(mac, want):
            bad.append(f"line {i}: chain broken")
        prev = mac or want
    if bad:
        return f"CHAIN FAIL ({len(bad)}/{n} rows bad)\n" + "\n".join(bad[:10])
    return f"CHAIN OK — {n} rows, unbroken"


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
# Hard ceiling on the caller-supplied timeout. An agent that asks for timeout_s=999999
# gets one hour, not three weeks — a runaway executor must not hold a tree indefinitely.
MAX_TIMEOUT_S = 3600
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


def parse_model(model: str) -> tuple[str, str]:
    """Split `provider/name`. A bare name used to mis-split silently: provider and model
    both became the full string, dispatching to a provider nobody asked for."""
    if "/" not in (model or ""):
        raise ValueError(f"model must be 'provider/name', got {model!r}")
    provider, _, name = model.partition("/")
    if not provider or not name:
        raise ValueError(f"model must be 'provider/name', got {model!r}")
    return provider, name


def porcelain_paths(status_output: str) -> dict[str, str]:
    """Parse `git status --porcelain` into {path: XY}.

    Rename entries look like `R  old -> new`; the path that exists on disk now is the
    NEW one. Reading the whole `old -> new` string as a path mis-attributes the write.
    """
    out: dict[str, str] = {}
    for ln in status_output.splitlines():
        if len(ln) <= 3:
            continue
        path = ln[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        out[path] = ln[:2]
    return out


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
    try:
        timeout_s = min(int(a.get("timeout_s") or DEFAULT_TIMEOUT_S), MAX_TIMEOUT_S)
    except (TypeError, ValueError):
        return f"ERROR: `timeout_s` must be an integer (max {MAX_TIMEOUT_S})."
    include_context_files = bool(a.get("include_context_files"))

    if not plan:
        return "ERROR: `plan` is required. Write the file-level plan first; the executor does not design."
    if not cwd or not os.path.isabs(cwd):
        return "ERROR: `cwd` must be an absolute path."
    if not os.path.isdir(cwd):
        return f"ERROR: cwd does not exist: {cwd}"
    try:
        provider, model_name = parse_model(model)
    except ValueError as e:
        return f"ERROR: {e}"

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

    # One run per working tree. Two concurrent pi_execute calls in the same cwd make
    # the dirty-file attribution meaningless — refuse the second instead of lying.
    os.makedirs(LOCK_DIR, exist_ok=True)
    lock_path = os.path.join(LOCK_DIR, hashlib.sha256(cwd.encode()).hexdigest()[:16] + ".lock")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        return (f"ERROR: another pi_execute run is active in {cwd}.\n"
                "Wait for it to finish (or investigate it) before dispatching again.")

    try:
        _, head_before, _ = git(cwd, "rev-parse", "HEAD")
        _, dirty_before, _ = git(cwd, "status", "--porcelain")
        pre_dirty = set(porcelain_paths(dirty_before))

        argv = [
            "pi", "-p", "-ne",              # -ne is load-bearing: without it pi never exits
            "--provider", provider,
            "--model", model_name,
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

        t0 = time.time()
        rc, out, err = run(argv, cwd, timeout_s)
        elapsed = time.time() - t0

        _, dirty_after, _ = git(cwd, "status", "--porcelain")
        post = porcelain_paths(dirty_after)
        touched = sorted(p for p in post if p not in pre_dirty and not is_noise(p))
        _, diffstat, _ = git(cwd, "diff", "--stat")
        _, head_after, _ = git(cwd, "rev-parse", "HEAD")
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    log_run({"kind": "execute", "model": model, "rc": rc, "elapsed": round(elapsed, 1),
             "touched": len(touched), "cwd": cwd, "timed_out": rc == 124,
             "head_moved": head_before.strip() != head_after.strip()})
    parts = [
        f"executor: {model}   exit={rc}   elapsed={elapsed:.0f}s",
        f"cwd: {cwd}",
    ]
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
    try:
        timeout_s = min(int(a.get("timeout_s") or 600), MAX_TIMEOUT_S)
    except (TypeError, ValueError):
        return f"ERROR: `timeout_s` must be an integer (max {MAX_TIMEOUT_S})."

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
            "contract/migration work. One run per working tree. Returns a summary + diffstat, "
            "never a full diff."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "plan": {"type": "string", "description": "Exact file paths, functions/lines, edit intent per file, and the verification commands. Self-contained: the executor sees only this."},
                "cwd": {"type": "string", "description": "Absolute path to the git repo to edit."},
                "model": {"type": "string", "description": f"provider/name. Default {DEFAULT_MODEL}."},
                "timeout_s": {"type": "integer", "description": f"Default {DEFAULT_TIMEOUT_S}, hard ceiling {MAX_TIMEOUT_S}."},
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
                "timeout_s": {"type": "integer", "description": f"Per command. Default 600, hard ceiling {MAX_TIMEOUT_S}."},
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
    argv = sys.argv[1:]
    if "--verify" in argv:
        print(verify_chain(argv[argv.index("--verify") + 1] if len(argv) > argv.index("--verify") + 1 else RUN_LOG))
        sys.exit(0)
    if "--runs" in argv:
        print(runs_report(argv[argv.index("--runs") + 1] if len(argv) > argv.index("--runs") + 1 else RUN_LOG))
        sys.exit(0)
    main()
