"""Story 5.4 — per-run transcript capture + seed threading."""
from __future__ import annotations

import json

import fakeredis
import pytest

from reviewer import transcript as t
from reviewer.llm import complete


class TestRedact:
    def test_masks_credential_shapes(self):
        assert "[REDACTED]" in t.redact("key sk-ant-abcdef1234567 end")
        assert "[REDACTED]" in t.redact("sk-or-v1-abcdef1234567")
        assert "[REDACTED]" in t.redact("token ghp_ABCDEFGHIJ0123456789 x")
        assert "[REDACTED]" in t.redact("Authorization: Bearer abcdefghij1234")

    def test_plain_text_unchanged(self):
        assert t.redact("hello world") == "hello world"


class TestWriteTranscript:
    def test_writes_redacted_jsonl(self, tmp_path):
        p = t.write_transcript("run-1", {
            "role": "reviewer", "model": "m", "seed": 7,
            "messages": [{"role": "user", "content": "tok sk-ant-supersecret12345"}],
            "response": "ok"}, base_dir=tmp_path)
        assert p == tmp_path / "run-1" / "transcript.jsonl"
        line = json.loads(p.read_text().splitlines()[0])
        assert line["seed"] == 7 and line["model"] == "m"
        assert "[REDACTED]" in line["messages"][0]["content"]

    def test_no_run_id_is_noop(self, tmp_path):
        assert t.write_transcript("", {"x": 1}, base_dir=tmp_path) is None

    def test_appends(self, tmp_path):
        t.write_transcript("r", {"a": 1}, base_dir=tmp_path)
        t.write_transcript("r", {"a": 2}, base_dir=tmp_path)
        assert len((tmp_path / "r" / "transcript.jsonl").read_text().splitlines()) == 2

    def test_rotates_when_oversized(self, tmp_path, monkeypatch):
        monkeypatch.setattr(t, "MAX_TRANSCRIPT_BYTES", 5)
        t.write_transcript("r", {"a": "xxxxxxxxxx"}, base_dir=tmp_path)  # > 5 bytes
        t.write_transcript("r", {"a": "y"}, base_dir=tmp_path)          # triggers rotation
        assert (tmp_path / "r" / "transcript.jsonl.1").exists()
        assert (tmp_path / "r" / "transcript.jsonl").exists()


class _Resp:
    class _Choice:
        class _Msg:
            content = "VERDICT"
        message = _Msg()
    choices = [_Choice()]


class TestSeedAndTranscriptThreading:
    def test_seed_passed_and_transcript_written(self, tmp_path, monkeypatch):
        from reviewer.config import settings
        monkeypatch.setattr(settings, "transcript_dir", str(tmp_path))
        captured = {}
        monkeypatch.setattr("reviewer.llm.litellm.completion",
                            lambda **kw: captured.update(kw) or _Resp())
        monkeypatch.setattr("redis.from_url", lambda *a, **k: fakeredis.FakeRedis())

        out = complete("openrouter/x", [{"role": "user", "content": "q"}], seed=42,
                       telemetry_role="reviewer", telemetry_run="run-9")

        assert out == "VERDICT"
        assert captured["seed"] == 42
        line = json.loads((tmp_path / "run-9" / "transcript.jsonl").read_text().splitlines()[0])
        assert line["seed"] == 42 and line["role"] == "reviewer" and line["response"] == "VERDICT"

    def test_no_run_writes_no_transcript(self, tmp_path, monkeypatch):
        from reviewer.config import settings
        monkeypatch.setattr(settings, "transcript_dir", str(tmp_path))
        monkeypatch.setattr("reviewer.llm.litellm.completion", lambda **kw: _Resp())
        monkeypatch.setattr("redis.from_url", lambda *a, **k: fakeredis.FakeRedis())
        complete("openrouter/x", [{"role": "user", "content": "q"}], telemetry_role="reviewer")
        assert not any(tmp_path.rglob("transcript.jsonl"))
