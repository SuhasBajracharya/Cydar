"""Multi-provider registry for OpenAI-compatible LLM gateways.

Add a key to `.env` and the provider appears (configured) in the web UI
picker — no code changes. Each entry:

- `name`: display name in the picker.
- `key_env`: env var holding the API key. Provider is `configured`
  (selectable) only when this is set — unless `requires_key` is False
  (local endpoints like Ollama need no key and are always configured;
  `key_env` is then only an optional override).
- `requires_key`: set False for keyless local providers (default True).
- `url_env` / `default_url`: base URL override + default. Must be the API
  root (usually ending in /v1), never the gateway's website.
- `default_model`: used when a target has no model chosen yet.
- `models`: curated starting list for the picker. These go stale — the UI
  always offers a "Custom…" free-text field, so any new id works.
- `extra_headers`: optional function returning extra HTTP headers
  (e.g. OpenRouter's attribution headers).

Keys are read here at request time and never leave the backend: the
/api/providers/ endpoint only reports `configured: true/false`.

DB-stored keys (ProviderConfig model) take precedence over env vars.
The /api/providers/<pid>/config/ endpoint lets operators manage keys
from the web UI without editing .env or restarting Django.
"""

import json
import logging
import os
import time as _time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

# In-memory cache of successful /v1/models listings: {pid: (monotonic_ts, [ids])}.
_REMOTE_MODELS_CACHE = {}
_MODELS_CACHE_TTL_S = 600


def _openrouter_headers():
    headers = {}
    referer = os.environ.get("OPENROUTER_REFERER")
    title = os.environ.get("OPENROUTER_TITLE")
    if referer:
        headers["HTTP-Referer"] = referer
    if title:
        headers["X-Title"] = title
    return headers


PROVIDERS = {
    "deepseek": {
        "name": "DeepSeek",
        "key_env": "DEEPSEEK_API_KEY",
        "url_env": "DEEPSEEK_BASE_URL",
        "default_url": "https://api.deepseek.com",
        "default_model": "deepseek-chat",
        "default_model_env": "DEEPSEEK_MODEL",  # legacy single-model var, still honored
        "models": ["deepseek-chat", "deepseek-reasoner"],
    },
    "openrouter": {
        "name": "OpenRouter",
        "key_env": "OPENROUTER_API_KEY",
        "url_env": "OPENROUTER_BASE_URL",
        "default_url": "https://openrouter.ai/api/v1",
        "default_model": "deepseek/deepseek-chat",
        "models": [
            "deepseek/deepseek-chat",
            "deepseek/deepseek-r1",
            "openai/gpt-4o",
            "openai/gpt-4o-mini",
            "anthropic/claude-sonnet-4",
            "google/gemini-2.5-flash",
        ],
        "extra_headers": _openrouter_headers,
    },
    "agentrouter": {
        "name": "AgentRouter",
        "key_env": "AGENTROUTER_API_KEY",
        "url_env": "AGENTROUTER_BASE_URL",
        "default_url": "https://agentrouter.org/v1",
        "default_model": "deepseek-chat",
        # Bare ids (their docs use no provider/ prefix); "Custom…" covers the rest.
        "models": [
            "deepseek-chat",
            "deepseek-r1",
            "gpt-4o",
            "claude-sonnet-4-5-20250929",
            "glm-4.5-air",
        ],
    },
    "nvidia": {
        "name": "NVIDIA NIM",
        "key_env": "NVIDIA_API_KEY",
        "url_env": "NVIDIA_BASE_URL",
        "default_url": "https://integrate.api.nvidia.com/v1",
        "default_model": "deepseek-ai/deepseek-r1",
        "models": [
            "deepseek-ai/deepseek-r1",
            "meta/llama-3.1-8b-instruct",
            "meta/llama-3.1-70b-instruct",
            "mistralai/mixtral-8x7b-instruct-v0.1",
            "nvidia/llama-3.1-nemotron-70b-instruct",
        ],
    },
    "zen": {
        "name": "OpenCode Zen",
        "key_env": "OPENCODE_API_KEY",
        "url_env": "OPENCODE_ZEN_BASE_URL",
        "default_url": "https://opencode.ai/zen/v1",
        # Zen mixes chat-completions, responses-API and messages-API models
        # on one host and this client only speaks chat-completions, so no
        # safe curated list: check opencode.ai/docs/zen and paste a
        # chat-completions id via "Custom…".
        "default_model": "",
        "models": [],
    },
    "ollama": {
        "name": "Ollama (local)",
        "key_env": "OLLAMA_API_KEY",  # optional override only; never required
        "requires_key": False,
        "url_env": "OLLAMA_BASE_URL",
        "default_url": "http://localhost:11434/v1",
        "default_model": "qwen3:8b",
        "default_model_env": "OLLAMA_MODEL",
        # Any `ollama pull`ed tag works via "Custom…" — this is just the starter list.
        "models": ["qwen3:8b"],
        "help": "Needs `ollama serve` running locally plus `ollama pull <model>`.",
    },
}

DEFAULT_PROVIDER = "deepseek"


class ProviderError(Exception):
    """Bad/unknown provider selection or missing key (surfaced as HTTP 400)."""


# ---------------------------------------------------------------------------
# DB helpers — lazily import the model to avoid circular imports at module load
# ---------------------------------------------------------------------------


def _get_db_config(provider_id):
    """Return the ProviderConfig row for *provider_id*, or None."""
    from targets.models import ProviderConfig  # noqa: E402 — avoid circular import

    try:
        return ProviderConfig.objects.get(provider_id=provider_id)
    except ProviderConfig.DoesNotExist:
        return None


def _get_effective_key(spec, provider_id):
    """Return (api_key, source).

    Keys come exclusively from ProviderConfig in the database (or 'local' for keyless providers like Ollama).
    No env var fallback is used for keys.
    """
    db = _get_db_config(provider_id)
    if db and db.api_key:
        return db.api_key, "database"
    return None, None


def _get_effective_url(spec, provider_id):
    """Return the base URL — DB override > default_url."""
    db = _get_db_config(provider_id)
    if db and db.base_url:
        return db.base_url
    return spec.get("default_url", "")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def provider_status():
    """Picker data for GET /api/providers/. No secrets included."""
    out = []
    for pid, spec in PROVIDERS.items():
        requires_key = spec.get("requires_key", True)
        _key, source = _get_effective_key(spec, pid)
        configured = True if not requires_key else bool(_key)
        if not requires_key and source is None:
            source = "local"  # keyless local provider (Ollama)
        out.append(
            {
                "id": pid,
                "name": spec["name"],
                "configured": configured,
                "requires_key": requires_key,
                "key_env": spec["key_env"],  # so the UI can say which var to set
                "default_model": spec.get("default_model", ""),
                "models": list(spec.get("models", [])),
                "help": spec.get("help", ""),
                "source": source,  # "database" | "environment" | "local" | null
                "default_url": spec.get("default_url", ""),
                "base_url": _get_effective_url(spec, pid),
            }
        )
    return out


def resolve(provider_id, model=None):
    """Return (base_url, api_key, model_id, headers) or raise ProviderError."""
    provider_id = (provider_id or DEFAULT_PROVIDER).strip() or DEFAULT_PROVIDER
    spec = PROVIDERS.get(provider_id)
    if spec is None:
        raise ProviderError(f"Unknown provider {provider_id!r}.")
    requires_key = spec.get("requires_key", True)
    api_key, _source = _get_effective_key(spec, provider_id)
    if not api_key:
        if requires_key:
            raise ProviderError(
                f"Provider {spec['name']} has no key: add it on the Providers page."
            )
        # Keyless local endpoint (Ollama): the OpenAI SDK still wants a
        # non-empty key, and Ollama's /v1 ignores its value.
        api_key = "ollama"
    model_id = (model or "").strip()
    if not model_id and spec.get("default_model_env"):
        model_id = os.environ.get(spec["default_model_env"], "").strip()
    if not model_id:
        model_id = spec.get("default_model", "")
    if not model_id:
        raise ProviderError(
            f"Provider {spec['name']} needs a model id (none selected, no default)."
        )
    base_url = _get_effective_url(spec, provider_id)
    extra = spec.get("extra_headers")
    return base_url, api_key, model_id, extra() if extra else {}


def fetch_remote_models(provider_id, force_refresh=False):
    """Query the provider's /v1/models endpoint and return a list of model IDs.

    Returns (models_list, error_string). On success error is None;
    on failure models_list is [] and error describes what went wrong.

    Successful results are cached in memory for _MODELS_CACHE_TTL_S so
    repeated picker opens don't hammer the upstream gateway.
    """
    global _REMOTE_MODELS_CACHE
    now = _time.monotonic()
    if not force_refresh:
        hit = _REMOTE_MODELS_CACHE.get(provider_id)
        if hit and (now - hit[0]) < _MODELS_CACHE_TTL_S:
            return list(hit[1]), None
    spec = PROVIDERS.get(provider_id)
    if spec is None:
        return [], f"Unknown provider {provider_id!r}."

    requires_key = spec.get("requires_key", True)
    api_key, _source = _get_effective_key(spec, provider_id)
    if not api_key and requires_key:
        return [], f"No API key configured for {spec['name']}."
    base_url = _get_effective_url(spec, provider_id)

    # Ollama has a native /api/tags endpoint; its /v1/models also works.
    url = f"{base_url.rstrip('/')}/models"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    extra = spec.get("extra_headers")
    if extra:
        headers.update(extra())

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return [], f"{spec['name']} returned HTTP {exc.code}."
    except urllib.error.URLError as exc:
        return [], f"Failed to connect to {spec['name']}: {exc.reason}"
    except TimeoutError:
        return [], f"Timed out connecting to {spec['name']} ({url})."
    except Exception as exc:
        logger.warning("fetch_remote_models(%s) failed: %s", provider_id, exc)
        return [], str(exc)

    # Parse the response — OpenAI-style { data: [{id: ...}, ...] }
    models = []
    if isinstance(data, dict) and "data" in data:
        for m in data["data"]:
            mid = m.get("id") if isinstance(m, dict) else None
            if mid:
                models.append(mid)
    elif isinstance(data, dict) and "models" in data:
        # Ollama /api/tags format: { models: [{name: ...}, ...] }
        for m in data["models"]:
            name = m.get("name") if isinstance(m, dict) else None
            if name:
                models.append(name)
    elif isinstance(data, list):
        for m in data:
            mid = m.get("id") if isinstance(m, dict) else str(m)
            if mid:
                models.append(mid)

    models.sort()
    _REMOTE_MODELS_CACHE[provider_id] = (_time.monotonic(), list(models))
    return models, None


def test_provider_key(provider_id):
    """Validate a provider's API key by hitting /v1/models.

    Returns (success: bool, detail: str).
    """
    models, error = fetch_remote_models(provider_id)
    if error:
        return False, error
    return True, f"Key is valid — {len(models)} model(s) accessible."
