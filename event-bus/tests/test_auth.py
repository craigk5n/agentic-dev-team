"""Unit tests for event_bus.auth — board HTTP Basic Auth helpers."""

import base64

from event_bus.auth import check_basic_auth, is_exempt


def _hdr(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


class TestCheckBasicAuth:
    def test_valid_credentials(self):
        assert check_basic_auth(_hdr("admin", "s3cret"), "admin", "s3cret") is True

    def test_wrong_password(self):
        assert check_basic_auth(_hdr("admin", "nope"), "admin", "s3cret") is False

    def test_wrong_user(self):
        assert check_basic_auth(_hdr("bob", "s3cret"), "admin", "s3cret") is False

    def test_missing_header(self):
        assert check_basic_auth("", "admin", "s3cret") is False

    def test_non_basic_scheme(self):
        assert check_basic_auth("Bearer abc123", "admin", "s3cret") is False

    def test_malformed_base64(self):
        assert check_basic_auth("Basic !!!not-base64!!!", "admin", "s3cret") is False

    def test_no_colon_in_decoded(self):
        bad = "Basic " + base64.b64encode(b"nocolonhere").decode()
        assert check_basic_auth(bad, "admin", "s3cret") is False


class TestIsExempt:
    def test_exempt_machine_paths(self):
        assert is_exempt("/health")
        assert is_exempt("/webhook/forgejo")
        assert is_exempt("/internal/pr-merged")

    def test_protected_paths(self):
        assert not is_exempt("/")
        assert not is_exempt("/ui/")
        assert not is_exempt("/api/items")
        assert not is_exempt("/api/config")
        assert not is_exempt("/metrics")
