"""Tests for models_catalog — free OpenRouter model discovery."""
from __future__ import annotations
import json
import time
from unittest.mock import MagicMock, patch

import fakeredis
import pytest

from event_bus.models_catalog import (
    _CACHE_KEY,
    _CACHE_TTL,
    _OLLAMA_SUGGESTIONS,
    get_free_models,
    get_ollama_suggestions,
    refresh_free_models,
)

_SAMPLE_DATA = {
    "data": [
        {
            "id": "google/gemma-3-27b-it:free",
            "name": "Gemma 3 27B",
            "context_length": 8192,
            "pricing": {"prompt": "0", "completion": "0"},
        },
        {
            "id": "openai/gpt-4o",
            "name": "GPT-4o",
            "context_length": 128000,
            "pricing": {"prompt": "0.000005", "completion": "0.000015"},
        },
        {
            "id": "mistral/ministral-3b:free",
            "name": "Ministral 3B",
            "context_length": 4096,
            "pricing": {"prompt": "0", "completion": "0"},
        },
        {
            "id": "bad/model",
            "name": "Bad Pricing",
            "context_length": 1024,
            "pricing": {"prompt": None, "completion": None},
        },
    ]
}


def _mock_httpx(data: dict):
    mock_resp = MagicMock()
    mock_resp.json.return_value = data
    mock_resp.raise_for_status.return_value = None
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get.return_value = mock_resp
    return mock_client


class TestGetFreeModels:
    def test_returns_cached_result_without_fetching(self):
        r = fakeredis.FakeRedis()
        cached = {"models": [{"id": "openrouter/x", "name": "X", "context_length": 0}], "count": 1, "cached_at": 1}
        r.setex(_CACHE_KEY, 100, json.dumps(cached))

        with patch("httpx.Client") as mock_cls:
            result = get_free_models(r)

        mock_cls.assert_not_called()
        assert result["count"] == 1
        assert result["models"][0]["id"] == "openrouter/x"

    def test_fetches_when_cache_empty(self):
        r = fakeredis.FakeRedis()
        mock_client = _mock_httpx(_SAMPLE_DATA)

        with patch("httpx.Client", return_value=mock_client):
            result = get_free_models(r)

        assert result["count"] == 2  # only the two with prompt=0, completion=0
        ids = [m["id"] for m in result["models"]]
        assert "openrouter/google/gemma-3-27b-it:free" in ids
        assert "openrouter/mistral/ministral-3b:free" in ids
        assert "openrouter/openai/gpt-4o" not in ids  # paid

    def test_result_stored_in_cache(self):
        r = fakeredis.FakeRedis()
        mock_client = _mock_httpx(_SAMPLE_DATA)

        with patch("httpx.Client", return_value=mock_client):
            get_free_models(r)

        cached = r.get(_CACHE_KEY)
        assert cached is not None
        data = json.loads(cached)
        assert data["count"] == 2

    def test_models_sorted_by_name(self):
        r = fakeredis.FakeRedis()
        mock_client = _mock_httpx(_SAMPLE_DATA)

        with patch("httpx.Client", return_value=mock_client):
            result = get_free_models(r)

        names = [m["name"] for m in result["models"]]
        assert names == sorted(names, key=str.lower)

    def test_bad_pricing_values_skipped(self):
        r = fakeredis.FakeRedis()
        bad_data = {
            "data": [
                {"id": "a/b", "name": "A", "context_length": 0, "pricing": {"prompt": "invalid", "completion": "0"}},
                {"id": "c/d", "name": "C", "context_length": 0, "pricing": {"prompt": "0", "completion": "0"}},
            ]
        }
        mock_client = _mock_httpx(bad_data)

        with patch("httpx.Client", return_value=mock_client):
            result = get_free_models(r)

        assert result["count"] == 1
        assert result["models"][0]["id"] == "openrouter/c/d"

    def test_api_key_sent_in_header(self):
        r = fakeredis.FakeRedis()
        mock_client = _mock_httpx({"data": []})

        with patch("httpx.Client", return_value=mock_client):
            get_free_models(r, api_key="sk-test-key")

        call_kwargs = mock_client.get.call_args
        assert call_kwargs[1]["headers"]["Authorization"] == "Bearer sk-test-key"

    def test_no_auth_header_without_key(self):
        r = fakeredis.FakeRedis()
        mock_client = _mock_httpx({"data": []})

        with patch("httpx.Client", return_value=mock_client):
            get_free_models(r, api_key="")

        call_kwargs = mock_client.get.call_args
        assert "Authorization" not in call_kwargs[1]["headers"]

    def test_cached_at_timestamp_present(self):
        r = fakeredis.FakeRedis()
        before = int(time.time())
        mock_client = _mock_httpx({"data": []})

        with patch("httpx.Client", return_value=mock_client):
            result = get_free_models(r)

        assert result["cached_at"] >= before

    def test_openrouter_prefix_added_to_ids(self):
        r = fakeredis.FakeRedis()
        data = {"data": [{"id": "google/gemma:free", "name": "Gemma", "context_length": 4096,
                          "pricing": {"prompt": "0", "completion": "0"}}]}
        mock_client = _mock_httpx(data)

        with patch("httpx.Client", return_value=mock_client):
            result = get_free_models(r)

        assert result["models"][0]["id"] == "openrouter/google/gemma:free"


class TestRefreshFreeModels:
    def test_bypasses_existing_cache(self):
        r = fakeredis.FakeRedis()
        old = {"models": [], "count": 0, "cached_at": 0}
        r.setex(_CACHE_KEY, 100, json.dumps(old))

        mock_client = _mock_httpx(_SAMPLE_DATA)
        with patch("httpx.Client", return_value=mock_client):
            result = refresh_free_models(r)

        mock_client.get.assert_called_once()
        assert result["count"] == 2

    def test_updates_cache_after_refresh(self):
        r = fakeredis.FakeRedis()
        r.setex(_CACHE_KEY, 100, json.dumps({"models": [], "count": 0, "cached_at": 0}))

        mock_client = _mock_httpx(_SAMPLE_DATA)
        with patch("httpx.Client", return_value=mock_client):
            refresh_free_models(r)

        cached = json.loads(r.get(_CACHE_KEY))
        assert cached["count"] == 2


class TestOllamaSuggestions:
    def test_returns_list(self):
        result = get_ollama_suggestions()
        assert isinstance(result, list)
        assert len(result) > 0

    def test_all_have_ollama_prefix(self):
        for m in get_ollama_suggestions():
            assert m["id"].startswith("ollama/")

    def test_returns_copy_not_original(self):
        a = get_ollama_suggestions()
        b = get_ollama_suggestions()
        a.append({"id": "x", "name": "x", "context_length": 0})
        assert len(a) != len(b)
