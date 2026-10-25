"""Every pi_execute run is a row on the run log, written before the result returns.

crew#533 CP1 reads `python3 pi_bridge.py --runs` and expects one row per run. The log stayed
empty for a day because (a) the MCP servers that ran MiniMax were started before `log_run`
existed and (b) `log_run` swallowed write errors with `pass`, so nothing said so. These pin:
a fake run lands; a failing run lands; a run that raises mid-flight lands; a write that
fails is one line on stderr, never silence.

Run: python3 -m pytest -q --color=no ~/.claude/mcp/test_pi_bridge_runs.py
"""
import importlib.util
import io
import json
import os
import subprocess
import sys

_SPEC = importlib.util.spec_from_file_location(
    "pi_bridge", os.path.join(os.path.dirname(os.path.abspath(__file__)), "pi_bridge.py")
)
pb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pb)


def _rows(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def test_fake_run_lands(tmp_path):
    log = tmp_path / "runs.jsonl"
    assert pb.log_run({"kind": "execute", "model": "minimax/MiniMax-M3", "rc": 0, "elapsed": 21.0}, str(log))
    rows = _rows(log)
    assert len(rows) == 1
    assert rows[0]["model"] == "minimax/MiniMax-M3" and rows[0]["rc"] == 0 and rows[0]["ts"].endswith("Z")


def test_failing_run_lands(tmp_path):
    log = tmp_path / "runs.jsonl"
    pb.log_run({"kind": "execute", "model": "minimax/MiniMax-M3", "rc": 0, "elapsed": 21.0}, str(log))
    assert pb.log_run({"kind": "execute", "model": "minimax/MiniMax-M3", "rc": 124, "elapsed": 900.0,
                       "timed_out": True}, str(log))
    rows = _rows(log)
    assert [r["rc"] for r in rows] == [0, 124]
    assert rows[1]["timed_out"] is True
    assert "2" in pb.runs_report(str(log)).splitlines()[1].split()  # both rows counted


def test_failed_write_is_loud_not_silent(tmp_path, monkeypatch):
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    assert pb.log_run({"kind": "execute", "rc": 1}, str(blocker / "runs.jsonl")) is False
    assert "run row NOT written" in err.getvalue()


def _git_repo(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "init"], check=True)


def test_pi_execute_row_lands_when_executor_fails(tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"
    repo = tmp_path / "repo"
    _git_repo(repo)
    monkeypatch.setattr(pb, "RUN_LOG", str(log))
    monkeypatch.setattr(pb.shutil, "which", lambda _: "/usr/bin/true")
    real_run = pb.run
    monkeypatch.setattr(pb, "run", lambda argv, cwd, timeout, env=None:
                        (1, "", "boom") if argv[0] == "pi" else real_run(argv, cwd, timeout, env))
    out = pb.tool_pi_execute({"plan": "touch nothing", "cwd": str(repo)})
    assert "exit=1" in out
    rows = _rows(log)
    assert len(rows) == 1 and rows[0]["rc"] == 1 and rows[0]["error"] is None


def test_pi_execute_row_lands_when_run_raises(tmp_path, monkeypatch):
    log = tmp_path / "runs.jsonl"
    repo = tmp_path / "repo"
    _git_repo(repo)
    monkeypatch.setattr(pb, "RUN_LOG", str(log))
    monkeypatch.setattr(pb.shutil, "which", lambda _: "/usr/bin/true")

    real_run = pb.run

    def explode(argv, cwd, timeout, env=None):
        if argv[0] == "pi":
            raise RuntimeError("pi vanished")
        return real_run(argv, cwd, timeout, env)

    monkeypatch.setattr(pb, "run", explode)
    try:
        pb.tool_pi_execute({"plan": "touch nothing", "cwd": str(repo)})
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass
    rows = _rows(log)
    assert len(rows) == 1 and rows[0]["rc"] is None and "pi vanished" in rows[0]["error"]
