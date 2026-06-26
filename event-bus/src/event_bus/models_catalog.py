"""
Free-model discovery for OpenRouter and static Ollama suggestions.

OpenRouter results cached in Redis under "openrouter:free_models" for 2 hours.
No auth is required to list models; pass api_key to raise rate limits.
"""
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import redis

_CACHE_KEY = "openrouter:free_models"
_CACHE_TTL = 7200  # 2 hours
_OR_MODELS_URL = "https://openrouter.ai/api/v1/models"

_OLLAMA_SUGGESTIONS = [
    {"id": "ollama/mistral", "name": "Mistral 7B", "context_length": 8192},
    {"id": "ollama/llama3", "name": "Llama 3 8B", "context_length": 8192},
    {"id": "ollama/codellama", "name": "CodeLlama 13B", "context_length": 16384},
    {"id": "ollama/deepseek-coder", "name": "DeepSeek Coder 6.7B", "context_length": 16384},
    {"id": "ollama/qwen2.5-coder", "name": "Qwen 2.5 Coder 7B", "context_length": 32768},
    {"id": "ollama/phi3", "name": "Phi-3 Mini", "context_length": 4096},
]


def get_free_models(r: "redis.Redis", api_key: str = "") -> dict:
    """Return cached free models; fetch from OpenRouter if cache is empty."""
    cached = r.get(_CACHE_KEY)
    if cached:
        return json.loads(cached)
    return _fetch_and_cache(r, api_key)


def refresh_free_models(r: "redis.Redis", api_key: str = "") -> dict:
    """Force-refresh the free models cache from OpenRouter."""
    return _fetch_and_cache(r, api_key)


def _fetch_and_cache(r: "redis.Redis", api_key: str = "") -> dict:
    import httpx

    headers: dict = {"HTTP-Referer": "http://localhost", "X-Title": "dev-agents"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    with httpx.Client(timeout=15) as client:
        resp = client.get(_OR_MODELS_URL, headers=headers)
        resp.raise_for_status()

    free: list[dict] = []
    for m in resp.json().get("data", []):
        pricing = m.get("pricing", {})
        try:
            if (
                float(pricing.get("prompt") or "1") == 0
                and float(pricing.get("completion") or "1") == 0
            ):
                free.append({
                    "id": f"openrouter/{m['id']}",
                    "name": m.get("name", m["id"]),
                    "context_length": m.get("context_length", 0),
                })
        except (ValueError, TypeError):
            continue

    free.sort(key=lambda x: x["name"].lower())
    result = {"models": free, "count": len(free), "cached_at": int(time.time())}
    r.setex(_CACHE_KEY, _CACHE_TTL, json.dumps(result))
    return result


def get_ollama_suggestions() -> list[dict]:
    return list(_OLLAMA_SUGGESTIONS)
