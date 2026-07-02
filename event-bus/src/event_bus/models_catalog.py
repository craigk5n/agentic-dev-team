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
_META_KEY = "openrouter:model_meta"     # id -> capability/pricing/context metadata
_CACHE_TTL = 7200  # 2 hours
_OR_MODELS_URL = "https://openrouter.ai/api/v1/models"


def _norm_id(model: str) -> str:
    """Our model strings prefix OpenRouter ids with 'openrouter/'; strip it to match the
    API's own id (e.g. 'openrouter/anthropic/x' -> 'anthropic/x'). Non-OpenRouter models
    (claude-code/*, ollama/*, anthropic/*) simply won't be found — callers no-op."""
    m = (model or "").strip()
    return m[len("openrouter/"):] if m.startswith("openrouter/") else m


def get_model_meta(r: "redis.Redis", model: str) -> dict | None:
    """Cached metadata for one model, or None if unknown/uncached. Shape:
    {context_length, price_prompt, price_completion, structured, tools, reasoning,
     input_modalities, expiration_date, knowledge_cutoff}."""
    try:
        raw = r.get(_META_KEY)
        if not raw:
            return None
        return json.loads(raw).get(_norm_id(model))
    except Exception:
        return None


def supports_structured(r: "redis.Redis", model: str) -> bool:
    """True when the model advertises structured-output / response_format support — used
    to send a JSON response_format so planning/verdict calls can't return prose."""
    meta = get_model_meta(r, model)
    return bool(meta and meta.get("structured"))

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
    meta: dict[str, dict] = {}
    for m in resp.json().get("data", []):
        pricing = m.get("pricing", {})
        # Free = explicitly priced at 0/0. A MISSING price is treated as non-free (a
        # paid model that omitted the field), matching the original behaviour.
        try:
            is_free = (float(pricing.get("prompt") or "1") == 0
                       and float(pricing.get("completion") or "1") == 0)
        except (ValueError, TypeError):
            is_free = False
        try:
            p_prompt = float(pricing.get("prompt") or 0)
            p_completion = float(pricing.get("completion") or 0)
        except (ValueError, TypeError):
            p_prompt = p_completion = 0.0
        sp = m.get("supported_parameters") or []
        arch = m.get("architecture") or {}
        # Full metadata for every model — powers structured-output routing, cost
        # estimates, and context-fit checks.
        meta[m["id"]] = {
            "name": m.get("name", m["id"]),
            "context_length": m.get("context_length") or 0,
            "price_prompt": p_prompt,           # USD per input token
            "price_completion": p_completion,   # USD per output token
            "structured": ("structured_outputs" in sp) or ("response_format" in sp),
            "tools": "tools" in sp,
            "reasoning": bool(m.get("reasoning")),
            "input_modalities": arch.get("input_modalities") or ["text"],
            "expiration_date": m.get("expiration_date"),
            "knowledge_cutoff": m.get("knowledge_cutoff"),
            "free": is_free,
        }
        if is_free:
            free.append({
                "id": f"openrouter/{m['id']}",
                "name": m.get("name", m["id"]),
                "context_length": m.get("context_length", 0),
            })

    free.sort(key=lambda x: x["name"].lower())
    result = {"models": free, "count": len(free), "cached_at": int(time.time())}
    r.setex(_CACHE_KEY, _CACHE_TTL, json.dumps(result))
    r.setex(_META_KEY, _CACHE_TTL, json.dumps(meta))
    return result


def get_ollama_suggestions() -> list[dict]:
    return list(_OLLAMA_SUGGESTIONS)
