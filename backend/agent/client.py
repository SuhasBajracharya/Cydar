"""DeepSeek client via the OpenAI Python SDK (OpenAI-compatible endpoint).

- Base URL + API key come from env (`DEEPSEEK_BASE_URL`, `DEEPSEEK_API_KEY`);
  never hardcoded. Model id via `DEEPSEEK_MODEL` (default "deepseek-chat").
- `generate_plan()` = one-shot (non-streaming), `stream_plan()` = generator
  of text deltas for SSE streaming.
- All API calls go through retry-with-backoff (`_with_retry`), because token
  spend and rate limits matter. Only transient errors are retried; anything
  else (e.g. auth) fails fast wrapped in `AgentAPIError`.
"""

import random
import time

from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)

from .prompts import SYSTEM_PROMPT
from .providers import DEFAULT_PROVIDER, resolve

# Transient failures worth retrying (rate limits, blips, 5xx, timeouts).
RETRYABLE = (RateLimitError, APIConnectionError, APITimeoutError, InternalServerError)

MAX_ATTEMPTS = 3
BASE_DELAY_SECONDS = 1.0


class AgentAPIError(Exception):
    """Anything that went wrong talking to the LLM backend."""


def _unexpected_body_hint(body):
    """Short diagnosis when the endpoint returns a page instead of API JSON."""
    snippet = body[:120].lstrip()
    if snippet.startswith("<"):
        return (
            "The endpoint returned an HTML page, not an OpenAI-compatible API "
            "response. DEEPSEEK_BASE_URL is probably the gateway's website — "
            "it usually needs the API path suffix, e.g. https://host/v1."
        )
    return f"The endpoint returned plain text, not an API response: {snippet!r}"


def _delta_text(delta):
    """Extract display text from a stream delta.

    Plain OpenAI endpoints use `delta.content`, but DeepSeek-style models
    behind gateways often put their thinking in `reasoning_content` (DeepSeek
    native) or `reasoning` (OpenRouter convention) with `content` arriving
    only at the end — or sometimes not at all. Read all three so a run can
    never silently save as empty.
    """
    return (
        getattr(delta, "content", None)
        or getattr(delta, "reasoning_content", None)
        or getattr(delta, "reasoning", None)
        or ""
    )


def _message_text(message):
    """Same as _delta_text but for a one-shot (non-streaming) message."""
    text = (
        getattr(message, "content", None)
        or getattr(message, "reasoning_content", None)
        or getattr(message, "reasoning", None)
        or ""
    )
    return text if isinstance(text, str) else str(text)


def get_client(provider_id=None):
    """Build the OpenAI SDK client for one registry provider.

    Returns (client, model_id): the model is resolved here too so views can
    snapshot exactly what was requested onto each AgentRun.
    """
    base_url, api_key, model_id, headers = resolve(provider_id)
    client = OpenAI(base_url=base_url, api_key=api_key, default_headers=headers or None)
    return client, model_id


def _with_retry(call):
    """Run `call()` (zero-arg callable), retrying transient errors."""
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return call()
        except RETRYABLE as exc:
            last_error = exc
            if attempt == MAX_ATTEMPTS:
                break
            # Exponential backoff with a little jitter: 1s, 2s, 4s, ...
            delay = BASE_DELAY_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            time.sleep(delay)
        except Exception as exc:  # auth errors, bad request, etc: fail fast
            raise AgentAPIError(f"LLM request failed: {exc}") from exc
    raise AgentAPIError(f"LLM request failed after {MAX_ATTEMPTS} attempts: {last_error}")


def build_user_prompt(domain, scope_rules, in_scope="", out_of_scope="", automation_level=""):
    """Compose the per-target user message sent alongside SYSTEM_PROMPT."""
    return (
        f"Target domain/scope: {domain}\n"
        f"In-scope assets: {in_scope or '(not specified — assume the domain only)'}\n"
        f"Out-of-scope: {out_of_scope or '(not specified)'}\n"
        f"Allowed automation level: {automation_level or '(not specified — be conservative)'}\n\n"
        f"Program scope rules (verbatim):\n{scope_rules}\n\n"
        "Propose a recon/testing plan for this scope."
    )


def chat_complete(messages, provider=None, model=None):
    """Multi-turn one-shot completion for the autonomous loop.

    `messages` is a full OpenAI-style history (system + turns). Returns the
    assistant text. Transport errors raise AgentAPIError (with retry);
    validation problems raise ProviderError from get_client/resolve.
    """
    provider = provider or DEFAULT_PROVIDER

    def call():
        client, model_id = get_client(provider)
        response = client.chat.completions.create(
            model=model or model_id,
            messages=messages,
        )
        # Raw-body guard (endpoint served a page, not the API).
        if isinstance(response, str):
            raise AgentAPIError(f"LLM request failed: {_unexpected_body_hint(response)}")
        text = _message_text(response.choices[0].message)
        if not text:
            raise AgentAPIError(
                "LLM returned an empty reply. Check the model id and that "
                "your key has access to it."
            )
        return text

    return _with_retry(call)


def generate_plan(user_prompt, system_prompt=None, model=None, provider=None):
    """One-shot plan generation. Returns the full text (non-streaming).

    `provider` is a registry id from providers.py; `model` an explicit model
    id (falls back to the provider default). Validation errors raise
    ProviderError (a 400-class problem); transport errors raise AgentAPIError.
    """
    provider = provider or DEFAULT_PROVIDER
    system_prompt = system_prompt or SYSTEM_PROMPT
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    return chat_complete(messages, provider=provider, model=model)


def stream_plan(user_prompt, system_prompt=None, model=None, provider=None):
    """Streaming plan generation. Yields text deltas as they arrive.

    The initial request is retried; once the stream starts, mid-stream
    errors are raised as AgentAPIError (no resume — just re-run).
    """
    provider = provider or DEFAULT_PROVIDER
    system_prompt = system_prompt or SYSTEM_PROMPT

    def call():
        client, model_id = get_client(provider)
        return client.chat.completions.create(
            model=model or model_id,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            stream=True,
        )

    stream = _with_retry(call)
    if isinstance(stream, str):
        raise AgentAPIError(f"LLM request failed: {_unexpected_body_hint(stream)}")
    yielded_any = False
    try:
        for chunk in stream:
            # A str item means the "stream" is a raw page/line, not SSE events.
            if isinstance(chunk, str):
                if chunk.strip() and chunk.strip() != "[DONE]":
                    raise AgentAPIError(f"LLM request failed: {_unexpected_body_hint(chunk)}")
                continue
            text = _delta_text(chunk.choices[0].delta) if chunk.choices else ""
            if text:
                yielded_any = True
                yield text
    except RETRYABLE as exc:
        raise AgentAPIError(f"LLM stream interrupted: {exc}") from exc
    if not yielded_any:
        # A plan prompt never legitimately returns nothing — fail loudly so
        # the run is marked error (retryable) instead of saved as an empty ok.
        raise AgentAPIError(
            "LLM stream produced no text. Check the provider's base URL points at "
            "the API (not the gateway website), the model id, and your key."
        )
