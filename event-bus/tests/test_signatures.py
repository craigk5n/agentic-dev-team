import hashlib
import hmac
import pytest

from event_bus.signatures import verify_forgejo, verify_plane


def _make_sig(payload: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


class TestVerifyForgejo:
    def test_valid_signature(self):
        payload = b'{"action":"opened"}'
        secret = "mysecret"
        sig = _make_sig(payload, secret)
        assert verify_forgejo(payload, sig, secret) is True

    def test_wrong_secret(self):
        payload = b'{"action":"opened"}'
        sig = _make_sig(payload, "correct-secret")
        assert verify_forgejo(payload, sig, "wrong-secret") is False

    def test_tampered_payload(self):
        secret = "mysecret"
        original = b'{"action":"opened"}'
        sig = _make_sig(original, secret)
        tampered = b'{"action":"closed"}'
        assert verify_forgejo(tampered, sig, secret) is False

    def test_empty_secret_always_false(self):
        payload = b'{"action":"opened"}'
        # Even if attacker somehow generates the right sig, empty secret rejects
        assert verify_forgejo(payload, "", "") is False

    def test_whitespace_trimmed_from_header(self):
        payload = b'{"action":"opened"}'
        secret = "mysecret"
        sig = _make_sig(payload, secret)
        assert verify_forgejo(payload, f" {sig} ", secret) is True


class TestVerifyPlane:
    def test_valid_signature(self):
        payload = b'{"event":"issue","action":"updated"}'
        secret = "planesecret"
        sig = _make_sig(payload, secret)
        assert verify_plane(payload, sig, secret) is True

    def test_wrong_secret(self):
        payload = b'{"event":"issue"}'
        sig = _make_sig(payload, "correct")
        assert verify_plane(payload, sig, "wrong") is False

    def test_empty_secret_always_false(self):
        assert verify_plane(b"data", "", "") is False
