"""Tests for the sandboxed coder entrypoint's result emission."""
import json

from coding_agent import sandbox_runner


def test_emit_result_writes_authoritative_file(tmp_path, capsys, monkeypatch):
    out = tmp_path / "output" / "result.json"
    monkeypatch.setattr(sandbox_runner, "_RESULT_PATH", str(out))
    result = {"status": "success", "pr_url": "http://x/pulls/1"}
    sandbox_runner._emit_result(result)
    assert json.loads(out.read_text()) == result           # file is the source of truth
    assert "CODING_RESULT:" in capsys.readouterr().out       # sentinel still printed (fallback)


def test_emit_result_survives_unwritable_path(capsys, monkeypatch):
    # An unwritable result path must not crash the run — the stdout sentinel carries it.
    monkeypatch.setattr(sandbox_runner, "_RESULT_PATH", "/proc/nope/result.json")
    sandbox_runner._emit_result({"status": "no_changes"})
    assert "CODING_RESULT:" in capsys.readouterr().out
