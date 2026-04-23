"""Model name aliases shared by all agent frameworks.

Some LiteLLM proxy deployments (notably all-hands) register models under
their full provider-prefixed IDs (e.g. `openrouter/moonshotai/kimi-k2.6`)
without also registering the bare short name. Writing the full name in
every trial config is noisy, so we accept the short name in YAML and
normalize to the full name inside the framework before the request goes
out.

GLM-class models do NOT appear here: the all-hands proxy registers
`glm-5` / `glm-5.1` directly, so the bare name resolves upstream with no
client-side help. Only add entries here when (a) the proxy-registered
name is long/inconvenient AND (b) a short alias is unambiguous.
"""

from __future__ import annotations

from typing import Dict

# Short alias -> canonical proxy-registered model ID.
MODEL_ALIASES: Dict[str, str] = {
    "kimi-k2.5": "openrouter/moonshotai/kimi-k2.5",
    "kimi-k2.6": "openrouter/moonshotai/kimi-k2.6",
}


def resolve_model_alias(model: str) -> str:
    """Return the canonical model ID for `model`, or `model` unchanged.

    Matches either the full string (`kimi-k2.6`) or the trailing path
    component (`litellm_proxy/kimi-k2.6` -> `litellm_proxy/openrouter/
    moonshotai/kimi-k2.6`), so callers that have already wrapped the
    name with a provider prefix still get the alias applied.
    """
    if not model:
        return model
    if model in MODEL_ALIASES:
        return MODEL_ALIASES[model]
    parts = model.split("/")
    leaf = parts[-1]
    if leaf in MODEL_ALIASES:
        parts[-1] = MODEL_ALIASES[leaf]
        return "/".join(parts)
    return model
