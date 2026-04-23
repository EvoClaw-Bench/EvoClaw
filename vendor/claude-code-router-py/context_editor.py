"""Proxy-level implementation of Anthropic context_management edits.

Implements clear_thinking and clear_tool_uses strategies locally
(pure message manipulation, no LLM calls required).
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


# Default tool clearing config applied when Claude Code doesn't send
# clear_tool_uses but context is growing large.
_DEFAULT_CLEAR_TOOL_USES = {
    "type": "clear_tool_uses_20250919",
    "trigger": {"type": "input_tokens", "value": 120000},
    "keep": {"type": "tool_uses", "value": 5},
}


def apply_context_edits(body: dict) -> dict:
    """Apply context_management edits to an Anthropic-format request body.

    Extracts the context_management parameter, applies supported edits
    (clear_thinking, clear_tool_uses) to the messages array, and removes
    the parameter before returning. Unsupported edits are silently skipped.

    When context_management is present but doesn't include clear_tool_uses,
    a default clear_tool_uses strategy is applied automatically to prevent
    context overflow on non-Anthropic backends.

    Returns the modified body (mutated in-place).
    """
    cm = body.pop("context_management", None)
    if not cm:
        return body

    edits = cm.get("edits", [])
    if not edits:
        return body

    # Auto-inject clear_tool_uses if not explicitly included
    has_clear_tool_uses = any(e.get("type") == "clear_tool_uses_20250919" for e in edits)
    if not has_clear_tool_uses:
        edits.append(dict(_DEFAULT_CLEAR_TOOL_USES))

    messages = body.get("messages", [])
    applied: list[dict] = []

    for edit in edits:
        edit_type = edit.get("type", "")

        if edit_type == "clear_thinking_20251015":
            result = _clear_thinking(messages, edit)
            applied.append(result)

        elif edit_type == "clear_tool_uses_20250919":
            result = _clear_tool_uses(messages, edit)
            applied.append(result)

        elif edit_type == "compact_20260112":
            # Server-side compaction requires LLM call — skip for now
            pass

    if applied:
        total_cleared = sum(a.get("cleared_input_tokens", 0) for a in applied)
        logger.info(f"Context edits applied: {[a['type'] for a in applied]}, "
                     f"~{total_cleared} tokens freed")

    # Store applied edits for response injection
    body["_applied_edits"] = applied
    return body


def _estimate_tokens(obj: Any) -> int:
    """Rough token estimate: ~4 chars per token."""
    return len(str(obj)) // 4


def _clear_thinking(messages: list, edit: dict) -> dict:
    """Remove old thinking blocks from assistant messages.

    Keeps thinking from the N most recent assistant turns (default: 1).

    IMPORTANT: Never clears thinking from assistant messages that also
    contain tool_use blocks. Reasoning-model upstreams (Moonshot kimi,
    and others reached via LiteLLM/OpenRouter) validate that if any
    assistant tool_call message has reasoning_content, ALL tool_call
    messages in the conversation must. Stripping thinking from an older
    tool-call turn produces a mixed state and upstream returns HTTP 400
    "thinking is enabled but reasoning_content is missing in assistant
    tool call message at index N". We still clear thinking from pure
    text-only assistant turns, where the validator is lenient.
    """
    keep = edit.get("keep", {"type": "thinking_turns", "value": 1})
    if keep == "all":
        return {"type": "clear_thinking_20251015",
                "cleared_thinking_turns": 0, "cleared_input_tokens": 0}

    keep_turns = keep.get("value", 1) if isinstance(keep, dict) else 1

    # Find assistant turns that have thinking blocks AND no tool_use —
    # tool-call turns must keep their thinking for upstream consistency.
    turns_with_thinking: list[int] = []
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        has_thinking = any(b.get("type") == "thinking" for b in content)
        has_tool_use = any(b.get("type") == "tool_use" for b in content)
        if has_thinking and not has_tool_use:
            turns_with_thinking.append(i)

    # Clear all but the last keep_turns
    if keep_turns > 0:
        to_clear = turns_with_thinking[:-keep_turns]
    else:
        to_clear = turns_with_thinking

    cleared_turns = 0
    cleared_tokens = 0
    for idx in to_clear:
        msg = messages[idx]
        old_content = msg["content"]
        new_content = []
        for block in old_content:
            if block.get("type") == "thinking":
                cleared_tokens += _estimate_tokens(block)
            else:
                new_content.append(block)
        msg["content"] = new_content
        cleared_turns += 1

    if cleared_turns > 0:
        logger.info(f"clear_thinking: cleared {cleared_turns} turns, "
                     f"~{cleared_tokens} tokens")

    return {"type": "clear_thinking_20251015",
            "cleared_thinking_turns": cleared_turns,
            "cleared_input_tokens": cleared_tokens}


def _clear_tool_uses(messages: list, edit: dict) -> dict:
    """Remove old tool_use/tool_result pairs from messages.

    When estimated tokens exceed trigger, clears oldest tool results
    while keeping the N most recent pairs.
    """
    trigger_cfg = edit.get("trigger", {"type": "input_tokens", "value": 100000})
    trigger_value = trigger_cfg.get("value", 100000)
    keep_cfg = edit.get("keep", {"type": "tool_uses", "value": 3})
    keep_count = keep_cfg.get("value", 3)
    exclude = set(edit.get("exclude_tools", []))
    clear_inputs = edit.get("clear_tool_inputs", False)
    clear_at_least_cfg = edit.get("clear_at_least")
    clear_at_least = clear_at_least_cfg.get("value", 0) if clear_at_least_cfg else 0

    # Estimate total tokens
    estimated_tokens = sum(_estimate_tokens(m) for m in messages)
    if estimated_tokens < trigger_value:
        return {"type": "clear_tool_uses_20250919",
                "cleared_tool_uses": 0, "cleared_input_tokens": 0}

    # Collect all tool_use/tool_result pairs
    # tool_use blocks appear in assistant messages, tool_result blocks in user messages
    tool_use_map: dict[str, tuple[int, int, str]] = {}  # id -> (msg_idx, block_idx, name)
    tool_pairs: list[tuple] = []  # (use_msg_idx, use_block_idx, id, name, result_msg_idx, result_block_idx)

    for i, msg in enumerate(messages):
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for j, block in enumerate(content):
            if block.get("type") == "tool_use":
                tool_use_map[block["id"]] = (i, j, block.get("name", ""))
            elif block.get("type") == "tool_result":
                tu_id = block.get("tool_use_id")
                if tu_id in tool_use_map:
                    mi, bi, name = tool_use_map[tu_id]
                    tool_pairs.append((mi, bi, tu_id, name, i, j))

    # Filter out excluded tools
    clearable = [p for p in tool_pairs if p[3] not in exclude]

    # Keep the last keep_count, clear the rest
    if keep_count > 0 and len(clearable) > keep_count:
        to_clear = clearable[:-keep_count]
    else:
        to_clear = []

    cleared = 0
    cleared_tokens = 0
    for use_mi, use_bi, tu_id, name, res_mi, res_bi in to_clear:
        # Clear tool result content
        result_block = messages[res_mi]["content"][res_bi]
        old_content = result_block.get("content", "")
        old_tokens = _estimate_tokens(old_content)
        result_block["content"] = "[Tool result cleared]"
        cleared_tokens += old_tokens

        # Optionally clear tool input
        if clear_inputs:
            use_block = messages[use_mi]["content"][use_bi]
            old_input = use_block.get("input", {})
            old_tokens_input = _estimate_tokens(old_input)
            use_block["input"] = {}
            cleared_tokens += old_tokens_input

        cleared += 1

        # Stop if we've cleared enough
        if clear_at_least and cleared_tokens >= clear_at_least:
            break

    if cleared > 0:
        logger.info(f"clear_tool_uses: cleared {cleared} pairs, "
                     f"~{cleared_tokens} tokens (trigger={trigger_value}, "
                     f"estimated={estimated_tokens})")

    return {"type": "clear_tool_uses_20250919",
            "cleared_tool_uses": cleared,
            "cleared_input_tokens": cleared_tokens}
