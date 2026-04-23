"""
FastAPI server — accepts Anthropic /v1/messages requests and forwards them
to an OpenAI-compatible provider, converting formats in both directions.
"""

import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from batch import anthropic_batch_to_openai_jsonl, openai_batch_to_anthropic, openai_results_line_to_anthropic
from client import ProviderError, ProviderStream, close_shared_client, get_shared_client, open_provider_stream, post_json
from config import apply_provider_params, get_provider, load_config, resolve_route
from context_editor import apply_context_edits
from converter import anthropic_to_openai, openai_to_anthropic, stream_openai_to_anthropic
from debug import check_and_save_nonstreaming, check_and_save_streaming, log_openai_request

logger = logging.getLogger(__name__)

# Populated either by set_config() (single-process) or lifespan (multi-worker).
_config: dict = {}


def set_config(cfg: dict) -> None:
    global _config
    _config = cfg


def _build_config_from_env() -> dict | None:
    """Build config from individual CCR_* env vars (for gunicorn -e usage).
    Returns None if CCR_API_BASE_URL is not set."""
    api_base_url = os.environ.get("CCR_API_BASE_URL")
    if not api_base_url:
        return None

    params: dict = {}
    if (v := os.environ.get("CCR_TEMPERATURE")) is not None:
        params["temperature"] = float(v)
    if (v := os.environ.get("CCR_TOP_P")) is not None:
        params["top_p"] = float(v)
    if (v := os.environ.get("CCR_MAX_TOKENS")) is not None:
        params["max_tokens"] = int(v)
    if (v := os.environ.get("CCR_BUDGET_TOKENS")) is not None:
        params["reasoning"] = {"budget_tokens": int(v)}

    provider: dict = {
        "name": "default",
        "api_base_url": api_base_url,
        "api_key": os.environ.get("CCR_API_KEY") or os.environ.get("API_KEY", ""),
        "max_retries": int(os.environ.get("CCR_MAX_RETRIES", "3")),
    }
    if params:
        provider["params"] = params

    tokenizer_path = os.environ.get("CCR_TOKENIZER_PATH") or os.environ.get("TOKENIZER_PATH")
    if tokenizer_path:
        provider["tokenizer_path"] = tokenizer_path

    cfg: dict = {
        "API_TIMEOUT_MS": int(os.environ.get("CCR_API_TIMEOUT_MS", "850000")),
        "Providers": [provider],
        "Router": {"default": f"default,{os.environ.get('CCR_MODEL', '/model')}"},
    }
    if tokenizer_path:
        cfg["tokenizer_path"] = tokenizer_path
    return cfg


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _config
    if not _config:
        if inline := os.environ.get("CCR_CONFIG_JSON"):
            import json as _json
            try:
                _config = _json.loads(inline)
                logger.info("Config loaded from CCR_CONFIG_JSON (worker pid=%d)", os.getpid())
            except Exception as exc:
                logger.error("Failed to parse CCR_CONFIG_JSON: %s", exc)
        elif cfg := _build_config_from_env():
            _config = cfg
            logger.info("Config loaded from CCR_* env vars (worker pid=%d)", os.getpid())
        else:
            path = os.environ.get("CCR_CONFIG", "config.json")
            try:
                _config = load_config(path)
                logger.info("Config loaded from %s (worker pid=%d)", path, os.getpid())
            except Exception as exc:
                logger.error("Failed to load config %s: %s", path, exc)
    if not _config:
        logger.warning("No config loaded — routes are registered but all proxy endpoints will return 500")
    yield
    await close_shared_client()


app = FastAPI(title="Claude Code Router (Python)", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Adaptive param stripping (for upstreams that reject reasoning-related
# fields with HTTP 400 UnsupportedParamsError instead of silently dropping).
#
# Flow:
#   1. First request for a (provider, model) pair goes out unmodified.
#   2. If upstream returns 400 with "does not support parameters: [...]",
#      we parse the list, intersect with _ADAPTIVE_STRIPPABLE_PARAMS
#      (reasoning-only — we never silently drop temperature/top_p/etc.),
#      cache the intersection, strip, and retry once.
#   3. Subsequent requests for the same pair pre-strip from cache, so the
#      400→retry round-trip happens only once per (provider, model).
#
# Known case: all-hands LiteLLM proxy → openrouter/moonshotai/kimi-k2.6
# rejects `thinking` and `reasoning_effort`. Claude/GPT/Gemini/GLM families
# accept them — this cache is per (provider, model), not per-prefix, so
# mixed deployments (e.g., openrouter/anthropic/claude-opus-4-7 alongside
# openrouter/moonshotai/kimi-k2.6) behave correctly without model-name
# heuristics.
# ---------------------------------------------------------------------------
_UNSUPPORTED_PARAMS_CACHE: dict[tuple[str, str], set[str]] = {}
_ADAPTIVE_STRIPPABLE_PARAMS: set[str] = {"thinking", "reasoning_effort"}
_UNSUPPORTED_PARAMS_RE = re.compile(
    r"does not support parameters:\s*\[([^\]]+)\]"
)


def _parse_unsupported_params(body: str | None) -> set[str]:
    """Extract param names from LiteLLM's UnsupportedParamsError body.

    Body may be raw text or JSON-wrapped; regex works on either form.
    """
    text = body or ""
    # LiteLLM errors are typically {"error":{"message":"..."}}; unwrap if so.
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            err = parsed.get("error")
            if isinstance(err, dict) and isinstance(err.get("message"), str):
                text = err["message"]
    except (json.JSONDecodeError, TypeError):
        pass

    m = _UNSUPPORTED_PARAMS_RE.search(text)
    if not m:
        return set()
    return {p.strip().strip("'\"") for p in m.group(1).split(",") if p.strip()}


def _apply_cached_strips(openai_req: dict, cache_key: tuple[str, str]) -> None:
    """Pre-strip params known to be unsupported by this (provider, model)."""
    for p in _UNSUPPORTED_PARAMS_CACHE.get(cache_key, ()):
        openai_req.pop(p, None)


def _try_adapt_after_400(
    exc: ProviderError,
    openai_req: dict,
    cache_key: tuple[str, str],
) -> set[str]:
    """If exc is an UnsupportedParamsError for strippable reasoning fields,
    update the cache, mutate openai_req to strip them, and return the set
    that was stripped. Otherwise return empty set (caller should re-raise).
    """
    if exc.status != 400:
        return set()
    unsupported = _parse_unsupported_params(exc.body)
    strippable = unsupported & _ADAPTIVE_STRIPPABLE_PARAMS
    # Only act if the strippable params are actually in the request (else
    # the retry would be identical and fail the same way).
    strippable = {p for p in strippable if p in openai_req}
    if not strippable:
        return set()
    _UNSUPPORTED_PARAMS_CACHE.setdefault(cache_key, set()).update(strippable)
    for p in strippable:
        openai_req.pop(p, None)
    logger.warning(
        "Upstream rejected %s for %s; stripped and will retry. "
        "Future requests for this (provider, model) will skip these.",
        sorted(strippable), cache_key,
    )
    return strippable


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _timeout() -> float:
    """Read API_TIMEOUT_MS from config, handling string or numeric values."""
    return float(_config.get("API_TIMEOUT_MS", 600_000)) / 1000


def _provider_headers(provider: dict) -> dict:
    api_key = provider.get("api_key", "")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _get_provider(anthropic_req: dict = None):
    """Determine which provider + model to use and return (provider, model, url)."""
    route = resolve_route(_config)
    if route is None:
        raise HTTPException(500, "No default route configured")

    provider_name, model = route
    provider = get_provider(_config, provider_name)
    if provider is None:
        raise HTTPException(500, f"Provider '{provider_name}' not found in config")

    return provider, model, provider["api_base_url"]


def _api_base(provider: dict) -> str:
    """Return the base URL (up to and including /v1) for a provider."""
    url = provider["api_base_url"]
    for suffix in ("/chat/completions", "/completions", "/models", "/batches", "/files"):
        if url.endswith(suffix):
            return url[: -len(suffix)]
    if url.rstrip("/").endswith("/v1"):
        return url.rstrip("/")
    return url.rsplit("/", 1)[0]


def _models_url(provider: dict) -> str:
    return _api_base(provider) + "/models"


def _batches_url(provider: dict) -> str:
    return _api_base(provider) + "/batches"


def _files_url(provider: dict) -> str:
    return _api_base(provider) + "/files"


_tokenizer_cache: dict[str, object] = {}  # tokenizer_path → tokenizer


def _get_tokenizer(tokenizer_path: str):
    if tokenizer_path not in _tokenizer_cache:
        from transformers import AutoTokenizer
        _tokenizer_cache[tokenizer_path] = AutoTokenizer.from_pretrained(tokenizer_path)
        logger.info("Loaded tokenizer from %s", tokenizer_path)
    return _tokenizer_cache[tokenizer_path]


def _extract_text_for_counting(req: dict) -> str:
    """Flatten all text content in an OpenAI-format request to a single string."""
    parts: list[str] = []
    for msg in req.get("messages", []):
        content = msg.get("content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif isinstance(block, dict) and block.get("type") == "tool_calls":
                    parts.append(json.dumps(block, ensure_ascii=False))
        # tool_calls at message level
        for tc in msg.get("tool_calls") or []:
            parts.append(json.dumps(tc.get("function", {}), ensure_ascii=False))
    for tool in req.get("tools", []):
        parts.append(json.dumps(tool.get("function", {}), ensure_ascii=False))
    return "\n".join(parts)


def _count_tokens_in_openai_req(req: dict, tokenizer_path: str) -> int:
    text = _extract_text_for_counting(req)
    tok = _get_tokenizer(tokenizer_path)
    return len(tok.encode(text))


# ---------------------------------------------------------------------------
# Main endpoint
# ---------------------------------------------------------------------------

@app.post("/v1/messages")
async def messages(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")

    try:
        provider, model, url = _get_provider(body)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Routing error")
        raise HTTPException(500, str(exc))

    # Override model in the original request so converter uses the routed model
    # If config model starts with "/" it means passthrough (use client's model)
    body = dict(body)
    if model.startswith("/"):
        model = body.get("model", model)
    else:
        body["model"] = model

    # Apply context_management edits (clear_thinking, clear_tool_uses)
    body = apply_context_edits(body)

    # Anthropic → OpenAI, then apply provider param defaults
    openai_req = anthropic_to_openai(body)
    openai_req = apply_provider_params(provider, openai_req)

    # Pre-strip any params already known to be unsupported by this
    # (provider, model); populated on-demand via _try_adapt_after_400.
    cache_key = (provider.get("name", ""), model)
    _apply_cached_strips(openai_req, cache_key)

    log_openai_request(openai_req)

    headers = _provider_headers(provider)
    max_retries: int = provider.get("max_retries", 3)
    timeout = _timeout()

    is_stream = openai_req.get("stream", False)

    if is_stream:
        # Eagerly connect to check provider status before committing to HTTP 200
        try:
            stream = await open_provider_stream(url, headers, openai_req, timeout, max_retries)
        except ProviderError as exc:
            if _try_adapt_after_400(exc, openai_req, cache_key):
                # Retry once with the newly-stripped request.
                log_openai_request(openai_req)
                try:
                    stream = await open_provider_stream(url, headers, openai_req, timeout, max_retries)
                except ProviderError as exc2:
                    raise HTTPException(exc2.status or 502, exc2.body or str(exc2))
                except Exception as exc2:
                    logger.exception("Stream reconnection error")
                    raise HTTPException(502, str(exc2))
            else:
                raise HTTPException(exc.status or 502, exc.body or str(exc))
        except Exception as exc:
            logger.exception("Stream connection error")
            raise HTTPException(502, str(exc))

        return StreamingResponse(
            _stream_response(openai_req, stream, model),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # Non-streaming
    try:
        openai_resp = await post_json(
            url, headers, openai_req, timeout=timeout, max_retries=max_retries
        )
    except ProviderError as exc:
        if _try_adapt_after_400(exc, openai_req, cache_key):
            # Retry once with the newly-stripped request.
            log_openai_request(openai_req)
            try:
                openai_resp = await post_json(
                    url, headers, openai_req, timeout=timeout, max_retries=max_retries
                )
            except ProviderError as exc2:
                logger.error("Provider error after adaptive retry: %s", exc2)
                raise HTTPException(exc2.status or 502, exc2.body or str(exc2))
            except Exception as exc2:
                logger.exception("Unexpected error on adaptive retry")
                raise HTTPException(502, str(exc2))
        else:
            logger.error("Provider error: %s", exc)
            raise HTTPException(exc.status or 502, exc.body or str(exc))
    except Exception as exc:
        logger.exception("Unexpected error calling provider")
        raise HTTPException(502, str(exc))

    check_and_save_nonstreaming(openai_req, openai_resp)
    anthropic_resp = openai_to_anthropic(openai_resp, model)
    return anthropic_resp


async def _stream_response(
    openai_req: dict,
    stream: ProviderStream,
    model: str,
) -> AsyncIterator[str]:
    import json as _json
    from debug import is_enabled as _debug_enabled

    message_id = f"msg_{uuid.uuid4().hex[:24]}"
    _text_buf: list[str] | None = [] if _debug_enabled() else None

    try:
        async for event in stream_openai_to_anthropic(stream, message_id, model):
            # Accumulate text for debug check (zero cost when CCR_DEBUG is off)
            if _text_buf is not None:
                for line in event.split("\n"):
                    if line.startswith("data: "):
                        try:
                            parsed = _json.loads(line[6:])
                            delta = parsed.get("delta", {})
                            if delta.get("type") in ("text_delta", "thinking_delta"):
                                _text_buf.append(delta.get("text") or delta.get("thinking") or "")
                        except _json.JSONDecodeError:
                            pass
            yield event
    except Exception as exc:
        logger.exception("Streaming error")
        error_event = {
            "type": "error",
            "error": {"type": "api_error", "message": str(exc)},
        }
        yield f"event: error\ndata: {_json.dumps(error_event)}\n\n"
        return
    finally:
        await stream.aclose()
        if _text_buf is not None:
            check_and_save_streaming(openai_req, "".join(_text_buf))


# ---------------------------------------------------------------------------
# Token counting  POST /v1/messages/count_tokens
# ---------------------------------------------------------------------------

@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")

    try:
        provider, model, _ = _get_provider()
    except HTTPException:
        raise

    body = dict(body)
    body["model"] = model

    openai_req = anthropic_to_openai(body)
    openai_req = apply_provider_params(provider, openai_req)

    tokenizer_path = provider.get("tokenizer_path") or _config.get("tokenizer_path")
    if not tokenizer_path:
        return {"input_tokens": 0}
    try:
        input_tokens = _count_tokens_in_openai_req(openai_req, tokenizer_path)
    except Exception as exc:
        logger.exception("Token counting error")
        raise HTTPException(500, f"Token counting failed: {exc}")

    return {"input_tokens": input_tokens}


# ---------------------------------------------------------------------------
# Proxy POST /tokens/clear to upstream adapter
# ---------------------------------------------------------------------------

@app.post("/tokens/clear")
async def tokens_clear(request: Request):
    """Proxy /tokens/clear to the upstream chat_to_generate_adapter."""
    body = await request.json()
    provider, model, _ = _get_provider()
    api_base = _api_base(provider)
    # Strip /v1 (with or without trailing slash) to get the adapter root
    adapter_root = api_base.removesuffix("/v1").removesuffix("/")
    url = f"{adapter_root}/tokens/clear"
    api_key = provider.get("api_key", "")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
            resp = await client.post(
                url,
                json=body,
                headers=headers,
            )
            return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")
    except Exception as exc:
        logger.exception("tokens/clear proxy error")
        raise HTTPException(502, f"Upstream /tokens/clear failed: {exc}")


# ---------------------------------------------------------------------------
# Models  GET /v1/models  and  GET /v1/models/{model_id}
# ---------------------------------------------------------------------------

def _openai_model_to_anthropic(m: dict) -> dict:
    import datetime
    ts = m.get("created", 0)
    try:
        created_at = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat()
    except Exception:
        created_at = "1970-01-01T00:00:00+00:00"
    return {
        "type": "model",
        "id": m.get("id", ""),
        "display_name": m.get("id", ""),
        "created_at": created_at,
    }


@app.get("/v1/models")
async def list_models(before_id: str | None = None,
                      after_id: str | None = None,
                      limit: int = 20):
    try:
        provider, _, _ = _get_provider()
    except HTTPException:
        raise

    url = _models_url(provider)
    headers = _provider_headers(provider)
    timeout = _timeout()
    params: dict = {"limit": limit}
    if before_id:
        params["before"] = before_id
    if after_id:
        params["after"] = after_id

    try:
        client = get_shared_client()
        r = await client.get(url, headers=headers, params=params, timeout=timeout)
        if r.status_code >= 400:
            raise HTTPException(r.status_code, r.text)
        data = r.json()
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Error fetching models")
        raise HTTPException(502, str(exc))

    models = [_openai_model_to_anthropic(m) for m in data.get("data", [])]
    return {
        "data": models,
        "has_more": data.get("has_more", False),
        "first_id": models[0]["id"] if models else None,
        "last_id":  models[-1]["id"] if models else None,
    }


@app.get("/v1/models/{model_id:path}")
async def get_model(model_id: str):
    try:
        provider, _, _ = _get_provider()
    except HTTPException:
        raise

    url = _models_url(provider) + f"/{model_id}"
    headers = _provider_headers(provider)
    timeout = _timeout()

    try:
        client = get_shared_client()
        r = await client.get(url, headers=headers, timeout=timeout)
        if r.status_code >= 400:
            raise HTTPException(r.status_code, r.text)
        return _openai_model_to_anthropic(r.json())
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Error fetching model %s", model_id)
        raise HTTPException(502, str(exc))


# ---------------------------------------------------------------------------
# Legacy Text Completions  POST /v1/complete
# ---------------------------------------------------------------------------

def _parse_legacy_prompt(prompt: str) -> list[dict]:
    """
    Convert a legacy \\n\\nHuman: / \\n\\nAssistant: prompt string into a messages list.
    Falls back to a single user message if the format is not recognized.
    """
    import re

    has_human = '\n\nHuman: ' in prompt
    has_assistant_start = prompt.lstrip().startswith('\n\nAssistant: ')

    if not has_human and not has_assistant_start:
        return [{"role": "user", "content": prompt.strip()}]

    messages = []
    # Split on delimiters, keeping track of which delimiter was matched
    parts = re.split(r'(\n\nHuman: |\n\nAssistant: )', prompt)
    current_role = "user" if has_human else "assistant"
    i = 0
    while i < len(parts):
        text = parts[i]
        if text in ('\n\nHuman: ', '\n\nAssistant: '):
            current_role = "user" if 'Human' in text else "assistant"
            i += 1
            continue
        text = text.strip()
        if text:
            messages.append({"role": current_role, "content": text})
        i += 1

    return messages or [{"role": "user", "content": prompt.strip()}]


@app.post("/v1/complete")
async def legacy_complete(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")

    try:
        provider, model, url = _get_provider()
    except HTTPException:
        raise

    messages = _parse_legacy_prompt(body.get("prompt", ""))
    openai_req: dict = {
        "model": model,
        "messages": messages,
        "stream": False,
        "max_tokens": body.get("max_tokens_to_sample", 1024),
    }
    for field in ("temperature", "top_p", "top_k"):
        if body.get(field) is not None:
            openai_req[field] = body[field]
    if body.get("stop_sequences"):
        openai_req["stop"] = body["stop_sequences"]

    openai_req = apply_provider_params(provider, openai_req)
    headers = _provider_headers(provider)
    timeout = _timeout()

    try:
        openai_resp = await post_json(url, headers, openai_req, timeout=timeout)
    except ProviderError as exc:
        raise HTTPException(exc.status or 502, exc.body or str(exc))
    except Exception as exc:
        logger.exception("Legacy completion error")
        raise HTTPException(502, str(exc))

    choice = openai_resp["choices"][0]
    finish = choice.get("finish_reason", "stop")
    stop_reason = "max_tokens" if finish == "length" else "stop_sequence"
    text = (choice.get("message") or {}).get("content") or ""

    return {
        "id": openai_resp.get("id", f"compl_{uuid.uuid4().hex[:24]}"),
        "type": "completion",
        "completion": text,
        "stop_reason": stop_reason,
        "model": model,
    }


# ---------------------------------------------------------------------------
# Batch API
# ---------------------------------------------------------------------------

async def _httpx_get(url: str, headers: dict, timeout: float, **kwargs) -> dict:
    client = get_shared_client()
    r = await client.get(url, headers=headers, timeout=timeout, **kwargs)
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text)
    return r.json()


async def _httpx_delete(url: str, headers: dict, timeout: float) -> dict:
    client = get_shared_client()
    r = await client.delete(url, headers=headers, timeout=timeout)
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text)
    return r.json() if r.text else {}


def _self_base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@app.post("/v1/messages/batches")
async def create_batch(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON body")

    try:
        provider, model, _ = _get_provider()
    except HTTPException:
        raise

    requests_list = body.get("requests", [])
    if not requests_list:
        raise HTTPException(400, "requests list is empty")

    # Convert to OpenAI JSONL and upload as a file
    jsonl_content = anthropic_batch_to_openai_jsonl(requests_list, model)
    jsonl_bytes = jsonl_content.encode()

    headers = _provider_headers(provider)
    timeout = _timeout()
    files_url = _files_url(provider)
    batches_url = _batches_url(provider)

    try:
        # Upload input file (use a fresh client for multipart upload)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            r = await client.post(
                files_url,
                headers={k: v for k, v in headers.items() if k != "Content-Type"},
                files={"file": ("batch_input.jsonl", jsonl_bytes, "application/jsonl")},
                data={"purpose": "batch"},
            )
        if r.status_code >= 400:
            raise HTTPException(r.status_code, r.text)
        file_id = r.json()["id"]

        # Create the batch
        batch_resp = await post_json(batches_url, headers, {
            "input_file_id": file_id,
            "endpoint": "/v1/chat/completions",
            "completion_window": "24h",
        }, timeout=timeout)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Batch create error")
        raise HTTPException(502, str(exc))

    return openai_batch_to_anthropic(batch_resp, _self_base_url(request))


@app.get("/v1/messages/batches")
async def list_batches(request: Request,
                       before_id: str | None = None,
                       after_id: str | None = None,
                       limit: int = 20):
    try:
        provider, _, _ = _get_provider()
    except HTTPException:
        raise

    headers = _provider_headers(provider)
    timeout = _timeout()
    url = _batches_url(provider)
    params = {"limit": limit}
    if before_id:
        params["before"] = before_id
    if after_id:
        params["after"] = after_id

    try:
        data = await _httpx_get(url, headers, timeout, params=params)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Batch list error")
        raise HTTPException(502, str(exc))

    base = _self_base_url(request)
    batches = [openai_batch_to_anthropic(b, base) for b in data.get("data", [])]
    return {
        "data": batches,
        "has_more": data.get("has_more", False),
        "first_id": batches[0]["id"] if batches else None,
        "last_id":  batches[-1]["id"] if batches else None,
    }


@app.get("/v1/messages/batches/{batch_id}")
async def get_batch(batch_id: str, request: Request):
    try:
        provider, _, _ = _get_provider()
    except HTTPException:
        raise

    headers = _provider_headers(provider)
    timeout = _timeout()

    try:
        data = await _httpx_get(_batches_url(provider) + f"/{batch_id}", headers, timeout)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Batch get error")
        raise HTTPException(502, str(exc))

    return openai_batch_to_anthropic(data, _self_base_url(request))


@app.post("/v1/messages/batches/{batch_id}/cancel")
async def cancel_batch(batch_id: str, request: Request):
    try:
        provider, _, _ = _get_provider()
    except HTTPException:
        raise

    headers = _provider_headers(provider)
    timeout = _timeout()

    try:
        client = get_shared_client()
        r = await client.post(
            _batches_url(provider) + f"/{batch_id}/cancel",
            headers=headers,
            timeout=timeout,
        )
        if r.status_code >= 400:
            raise HTTPException(r.status_code, r.text)
        data = r.json()
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Batch cancel error")
        raise HTTPException(502, str(exc))

    return openai_batch_to_anthropic(data, _self_base_url(request))


@app.delete("/v1/messages/batches/{batch_id}")
async def delete_batch(batch_id: str):
    try:
        provider, _, _ = _get_provider()
    except HTTPException:
        raise

    headers = _provider_headers(provider)
    timeout = _timeout()

    try:
        await _httpx_delete(_batches_url(provider) + f"/{batch_id}", headers, timeout)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Batch delete error")
        raise HTTPException(502, str(exc))

    return {"id": batch_id, "type": "message_batch_deleted"}


@app.get("/v1/messages/batches/{batch_id}/results")
async def batch_results(batch_id: str, request: Request):
    """Stream batch results as Anthropic JSONL (one result per line)."""
    try:
        provider, model, _ = _get_provider()
    except HTTPException:
        raise

    headers = _provider_headers(provider)
    timeout = _timeout()

    # Get the batch to find output_file_id
    try:
        batch = await _httpx_get(_batches_url(provider) + f"/{batch_id}", headers, timeout)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, str(exc))

    file_id = batch.get("output_file_id")
    if not file_id:
        status = batch.get("status", "unknown")
        raise HTTPException(404, f"Batch results not available yet (status: {status})")

    async def _stream_results():
        url = _files_url(provider) + f"/{file_id}/content"
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout), trust_env=False) as client:
            async with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    yield json.dumps({
                        "type": "error",
                        "error": {"type": "api_error", "message": body.decode()},
                    }) + "\n"
                    return
                buffer = ""
                async for chunk in resp.aiter_text():
                    buffer += chunk
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        converted = openai_results_line_to_anthropic(line, model)
                        if converted:
                            yield converted + "\n"
                if buffer.strip():
                    converted = openai_results_line_to_anthropic(buffer, model)
                    if converted:
                        yield converted + "\n"

    return StreamingResponse(
        _stream_results(),
        media_type="application/x-jsonlines",
    )


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8891, timeout_keep_alive=1500, reload=False)
