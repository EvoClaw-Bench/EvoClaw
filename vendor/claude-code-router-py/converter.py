"""Bidirectional conversion between Anthropic and OpenAI API formats."""

import json
import uuid
from typing import Any, AsyncIterator


# ---------------------------------------------------------------------------
# Anthropic → OpenAI  (request)
# ---------------------------------------------------------------------------

def anthropic_to_openai(req: dict) -> dict:
    """Convert an Anthropic /v1/messages request body to OpenAI chat format."""
    messages: list[dict] = []

    # System prompt
    system = req.get("system")
    if system:
        if isinstance(system, list):
            text = "\n".join(
                b["text"] for b in system if b.get("type") == "text" and b.get("text")
            )
        else:
            text = system
        if text:
            messages.append({"role": "system", "content": text})

    # Conversation messages
    for msg in req.get("messages", []):
        role: str = msg["role"]
        content = msg["content"]

        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue

        if role == "user":
            user_parts: list[dict] = []
            tool_results: list[dict] = []

            for block in content:
                btype = block.get("type")
                if btype == "text":
                    user_parts.append({"type": "text", "text": block["text"]})
                elif btype == "image":
                    src = block.get("source", {})
                    if src.get("type") == "base64":
                        url = f"data:{src['media_type']};base64,{src['data']}"
                    else:
                        url = src.get("url", "")
                    user_parts.append({"type": "image_url", "image_url": {"url": url}})
                elif btype == "tool_result":
                    tool_results.append(block)

            if user_parts:
                # Simplify to plain string when only text
                if all(p["type"] == "text" for p in user_parts):
                    messages.append({
                        "role": "user",
                        "content": "".join(p["text"] for p in user_parts),
                    })
                else:
                    messages.append({"role": "user", "content": user_parts})

            for tr in tool_results:
                tr_content = tr.get("content", "")
                if isinstance(tr_content, list):
                    tr_content = "\n".join(
                        b.get("text", "") for b in tr_content if b.get("type") == "text"
                    )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tr["tool_use_id"],
                    "content": tr_content or "",
                })

        elif role == "assistant":
            text_parts: list[str] = []
            tool_calls: list[dict] = []
            reasoning_parts: list[str] = []

            for block in content:
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(block.get("text", ""))
                elif btype == "tool_use":
                    tool_calls.append({
                        "id": block["id"],
                        "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    })
                elif btype == "thinking":
                    # Preserve reasoning across the round-trip. openai_to_anthropic
                    # turned upstream `reasoning_content` into `thinking` blocks;
                    # we must put them back on the outgoing assistant message so
                    # the upstream sees a consistent conversation. Moonshot kimi
                    # (and other reasoning models via LiteLLM/OpenRouter) reject
                    # tool_call histories where some assistant turns carry
                    # reasoning_content and others don't. Providers that don't
                    # understand reasoning_content ignore it (OpenAI spec).
                    t = block.get("thinking", "")
                    if t:
                        reasoning_parts.append(t)

            msg_obj: dict[str, Any] = {"role": "assistant"}
            msg_obj["content"] = "\n".join(text_parts) if text_parts else ""
            if tool_calls:
                msg_obj["tool_calls"] = tool_calls
            if reasoning_parts:
                msg_obj["reasoning_content"] = "\n".join(reasoning_parts)
            messages.append(msg_obj)

    # Build base request
    openai_req: dict[str, Any] = {
        "model": req.get("model", ""),
        "messages": messages,
        "stream": req.get("stream", False),
    }

    if req.get("stream"):
        openai_req["stream_options"] = {"include_usage": True}

    # Sampling parameters
    for field in ("max_tokens", "temperature", "top_p", "top_k"):
        if req.get(field) is not None:
            openai_req[field] = req[field]

    # Stop sequences
    if req.get("stop_sequences"):
        openai_req["stop"] = req["stop_sequences"]

    # Tools
    if req.get("tools"):
        openai_req["tools"] = []
        _TOOL_KNOWN = {"name", "description", "input_schema", "strict", "type", "cache_control"}
        for t in req["tools"]:
            fn: dict = {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {}),
            }
            if t.get("strict") is not None:
                fn["strict"] = t["strict"]
            # Forward any unknown fields (e.g. defer_loading) as-is
            for k, v in t.items():
                if k not in _TOOL_KNOWN:
                    fn[k] = v
            openai_req["tools"].append({"type": "function", "function": fn})

    # Tool choice
    tc = req.get("tool_choice")
    if tc:
        tc_type = tc.get("type")
        if tc_type == "auto":
            openai_req["tool_choice"] = "auto"
        elif tc_type == "any":
            openai_req["tool_choice"] = "required"
        elif tc_type == "tool":
            openai_req["tool_choice"] = {
                "type": "function",
                "function": {"name": tc["name"]},
            }
        # tool_choice "none" is not a standard Anthropic value; omit it
        # disable_parallel_tool_use on any variant
        if tc.get("disable_parallel_tool_use"):
            openai_req["parallel_tool_calls"] = False

    # Thinking / extended reasoning
    # enabled  → forward as-is (budget_tokens required by Anthropic spec)
    # adaptive → forward as-is (no budget_tokens; provider decides)
    # disabled → omit (no thinking field sent)
    thinking = req.get("thinking")
    if thinking:
        t_type = thinking.get("type")
        if t_type in ("enabled", "adaptive"):
            openai_req["thinking"] = thinking
        # type == "disabled": intentionally omitted

    # output_config: effort → reasoning_effort, format → response_format
    output_config = req.get("output_config") or {}
    effort = output_config.get("effort")
    if effort is not None:
        # Anthropic: low / medium / high / max
        # OpenAI:    low / medium / high / xhigh
        openai_req["reasoning_effort"] = "xhigh" if effort == "max" else effort
    fmt = output_config.get("format")
    if fmt and fmt.get("type") == "json_schema":
        openai_req["response_format"] = {
            "type": "json_schema",
            "json_schema": fmt.get("schema", {}),
        }

    # metadata.user_id → user
    user_id = (req.get("metadata") or {}).get("user_id")
    if user_id:
        openai_req["user"] = user_id

    return openai_req


# ---------------------------------------------------------------------------
# OpenAI → Anthropic  (non-streaming response)
# ---------------------------------------------------------------------------

_FINISH_TO_STOP: dict[str, str] = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "stop_sequence",
}


def openai_to_anthropic(resp: dict, original_model: str) -> dict:
    """Convert an OpenAI chat completion response to Anthropic message format."""
    choice = resp["choices"][0]
    message = choice["message"]
    finish_reason = choice.get("finish_reason") or "stop"
    stop_reason = _FINISH_TO_STOP.get(finish_reason, "end_turn")

    content: list[dict] = []

    # Thinking / reasoning (comes before text in Anthropic format)
    thinking_obj = message.get("thinking")
    reasoning = message.get("reasoning_content")
    if thinking_obj and thinking_obj.get("content"):
        block: dict[str, Any] = {"type": "thinking", "thinking": thinking_obj["content"]}
        if thinking_obj.get("signature"):
            block["signature"] = thinking_obj["signature"]
        content.append(block)
    elif reasoning:
        content.append({"type": "thinking", "thinking": reasoning})

    # Text
    text = message.get("content")
    if text:
        content.append({"type": "text", "text": text})

    # Tool calls — support both nested {"function": {"name", "arguments"}}
    # and flat {"name", "arguments"} formats
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or tc  # flat fallback
        try:
            input_obj = json.loads(fn.get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            input_obj = {"_raw": fn.get("arguments", "")}
        content.append({
            "type": "tool_use",
            "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:24]}"),
            "name": fn.get("name", ""),
            "input": input_obj,
        })

    usage = resp.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    cache_read = details.get("cached_tokens", 0)
    cache_created = details.get("cache_creation_tokens", 0)

    return {
        "id": resp.get("id") or f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": original_model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": max(0, usage.get("prompt_tokens", 0) - cache_read - cache_created),
            "output_tokens": usage.get("completion_tokens", 0),
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_created,
        },
    }


# ---------------------------------------------------------------------------
# OpenAI → Anthropic  (streaming)
# ---------------------------------------------------------------------------

def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def stream_openai_to_anthropic(
    openai_lines: AsyncIterator[bytes],
    message_id: str,
    model: str,
) -> AsyncIterator[str]:
    """
    Yield Anthropic SSE events converted from an OpenAI streaming response.

    State machine:
    - content_index  : next available content block index
    - text_idx       : content block index of the open text block (-1 = none)
    - thinking_idx   : content block index of the open thinking block (-1 = none)
    - tool_blocks    : {openai_tc_index: {block_idx, id, name}}
    """
    content_index = 0
    text_idx = -1
    thinking_idx = -1
    thinking_sig_sent = False
    tool_blocks: dict[int, dict] = {}  # openai tool index → state

    stop_reason = "end_turn"
    usage: dict = {}

    # --- message_start ---
    yield _sse("message_start", {
        "type": "message_start",
        "message": {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })
    yield _sse("ping", {"type": "ping"})

    async for raw in openai_lines:
        line = raw.strip()
        if not line:
            continue
        if not line.startswith(b"data: "):
            continue
        payload = line[6:]
        if payload == b"[DONE]":
            break

        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue

        # Accumulate usage
        if chunk.get("usage"):
            usage = chunk["usage"]

        choices = chunk.get("choices") or []
        if not choices:
            continue

        choice = choices[0]
        delta = choice.get("delta") or {}
        finish_reason = choice.get("finish_reason")

        # ---- thinking delta ----
        # Two formats: structured delta.thinking (OpenAI native) or
        # plain string delta.reasoning_content (common in third-party providers)
        thinking_delta = delta.get("thinking")
        reasoning_content = delta.get("reasoning_content")

        if thinking_delta or (reasoning_content is not None and reasoning_content != ""):
            if thinking_delta:
                t_content = thinking_delta.get("content", "")
                t_sig = thinking_delta.get("signature")
            else:
                t_content = reasoning_content
                t_sig = None

            if t_content or t_sig:
                if thinking_idx == -1:
                    # Open thinking block
                    thinking_idx = content_index
                    content_index += 1
                    thinking_sig_sent = False
                    yield _sse("content_block_start", {
                        "type": "content_block_start",
                        "index": thinking_idx,
                        "content_block": {"type": "thinking", "thinking": ""},
                    })

                if t_content:
                    yield _sse("content_block_delta", {
                        "type": "content_block_delta",
                        "index": thinking_idx,
                        "delta": {"type": "thinking_delta", "thinking": t_content},
                    })

                if t_sig and not thinking_sig_sent:
                    thinking_sig_sent = True
                    yield _sse("content_block_delta", {
                        "type": "content_block_delta",
                        "index": thinking_idx,
                        "delta": {"type": "signature_delta", "signature": t_sig},
                    })
                    yield _sse("content_block_stop", {
                        "type": "content_block_stop",
                        "index": thinking_idx,
                    })
                    thinking_idx = -1  # closed

        # ---- text delta ----
        text_chunk = delta.get("content")
        if text_chunk is not None and text_chunk != "":
            if text_idx == -1:
                # Close thinking block if still open
                if thinking_idx != -1:
                    yield _sse("content_block_stop", {
                        "type": "content_block_stop",
                        "index": thinking_idx,
                    })
                    thinking_idx = -1
                text_idx = content_index
                content_index += 1
                yield _sse("content_block_start", {
                    "type": "content_block_start",
                    "index": text_idx,
                    "content_block": {"type": "text", "text": ""},
                })
            yield _sse("content_block_delta", {
                "type": "content_block_delta",
                "index": text_idx,
                "delta": {"type": "text_delta", "text": text_chunk},
            })

        # ---- tool call deltas ----
        for tc_delta in delta.get("tool_calls") or []:
            tc_idx: int = tc_delta.get("index", 0)

            if tc_idx not in tool_blocks:
                # Close any open text block first
                if text_idx != -1:
                    yield _sse("content_block_stop", {
                        "type": "content_block_stop",
                        "index": text_idx,
                    })
                    text_idx = -1
                # Close the previously open tool block (if any) before opening a new one
                if tool_blocks:
                    last_tb = list(tool_blocks.values())[-1]
                    yield _sse("content_block_stop", {
                        "type": "content_block_stop",
                        "index": last_tb["block_idx"],
                    })
                    last_tb["closed"] = True

                block_idx = content_index
                content_index += 1
                tc_id = tc_delta.get("id", f"toolu_{uuid.uuid4().hex[:24]}")
                tc_fn = tc_delta.get("function") or tc_delta  # flat fallback
                tc_name = tc_fn.get("name", "")
                tool_blocks[tc_idx] = {
                    "block_idx": block_idx,
                    "id": tc_id,
                    "name": tc_name,
                }
                yield _sse("content_block_start", {
                    "type": "content_block_start",
                    "index": block_idx,
                    "content_block": {
                        "type": "tool_use",
                        "id": tc_id,
                        "name": tc_name,
                        "input": {},
                    },
                })

            tc_fn_d = tc_delta.get("function") or tc_delta  # flat fallback
            args = tc_fn_d.get("arguments", "")
            if args:
                yield _sse("content_block_delta", {
                    "type": "content_block_delta",
                    "index": tool_blocks[tc_idx]["block_idx"],
                    "delta": {"type": "input_json_delta", "partial_json": args},
                })

        # ---- finish reason ----
        if finish_reason:
            stop_reason = _FINISH_TO_STOP.get(finish_reason, "end_turn")

    # --- close any open blocks ---
    if thinking_idx != -1:
        yield _sse("content_block_stop", {
            "type": "content_block_stop",
            "index": thinking_idx,
        })
    if text_idx != -1:
        yield _sse("content_block_stop", {
            "type": "content_block_stop",
            "index": text_idx,
        })
    for tb in tool_blocks.values():
        if not tb.get("closed"):
            yield _sse("content_block_stop", {
                "type": "content_block_stop",
                "index": tb["block_idx"],
            })

    # --- message_delta + message_stop ---
    details = (usage.get("prompt_tokens_details") or {})
    cache_read = details.get("cached_tokens", 0)
    cache_created = details.get("cache_creation_tokens", 0)
    yield _sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {
            "input_tokens": max(0, usage.get("prompt_tokens", 0) - cache_read - cache_created),
            "output_tokens": usage.get("completion_tokens", 0),
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_created,
        },
    })
    yield _sse("message_stop", {"type": "message_stop"})
