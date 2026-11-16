"""How tokens count against a subscription limit: unit, weights and priors.

Anthropic does not publish these. The values are community measurements,
read on 2026-11-16 (claude-usage-tracker, Atlas ADE, the Wokey gateway
dataset, limitcast, she-llac). They are PRIORS: ``budget_calibrate`` corrects
the per-percent rates for each account from the user's own logs. The limits
changed several times per quarter in 2026, by 10 to 25% per step.

Unit: the weighted token, an Opus 5.5 input-equivalent token.

    weighted = model weight * (input + cache write + 5 * output + 0.01 * cache read)

Four independent sources agree that a cache read counts 0 to 2% of an input
token and that output counts about 5 times input.
"""

from __future__ import annotations

from claude_swap import balance

UNIT = "weighted tokens (Opus 5.5 input-equivalent)"
DEFINITION = "model weight * (input + cache write + 5 * output + 0.01 * cache read)"
PRIORS_READ_ON = "2026-11-16"

TYPE_WEIGHTS = {"input": 1.0, "cacheWrite": 1.0, "output": 5.0, "cacheRead": 0.01}

# First match wins: put the specific name before the family name.
MODEL_WEIGHTS = (
    ("fable-5-2", 3.0),
    ("fable", 3.8),
    ("opus-5-5", 1.0),
    ("opus", 1.25),
    ("sonnet", 0.6),
    ("haiku-5-5", 0.025),
    ("haiku", 0.25),
)
DEFAULT_MODEL_WEIGHT = 1.0

# Weighted tokens for 1% of a Pro account. Other plans scale by capacity.
PRIOR_PER_PERCENT_PRO = {"week": 300_000.0, "five": 30_000.0}
# The prior counts as this many percent points of the account's own data.
PRIOR_POINTS = {"week": 10.0, "five": 30.0}


def model_weight(model) -> float:
    name = model.lower() if isinstance(model, str) else ""
    for needle, weight in MODEL_WEIGHTS:
        if needle in name:
            return weight
    return DEFAULT_MODEL_WEIGHT


def _count(usage: dict, key: str) -> int:
    value = usage.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def weighted_tokens(model, usage: dict) -> float:
    """Weighted tokens of one assistant message, from its ``usage`` object."""
    raw = (
        TYPE_WEIGHTS["input"] * _count(usage, "input_tokens")
        + TYPE_WEIGHTS["cacheWrite"] * _count(usage, "cache_creation_input_tokens")
        + TYPE_WEIGHTS["output"] * _count(usage, "output_tokens")
        + TYPE_WEIGHTS["cacheRead"] * _count(usage, "cache_read_input_tokens")
    )
    return model_weight(model) * raw


def prior_rate(plan, window: str) -> float:
    """Community estimate of weighted tokens per percent point, for one plan."""
    return PRIOR_PER_PERCENT_PRO[window] * balance.plan_capacity(plan)


def blended_rate(plan, window: str, points: float, tokens: float) -> float:
    """The account's own measurement pulled toward the prior while it is small."""
    weight = PRIOR_POINTS[window]
    return (weight * prior_rate(plan, window) + max(0.0, tokens)) / (weight + max(0.0, points))


def confidence(points: float) -> str:
    """How much of the rate is the account's own data (weekly points)."""
    if points >= 100.0:
        return "high"
    if points >= 30.0:
        return "medium"
    if points >= 10.0:
        return "low"
    return "prior"


def meta() -> dict:
    """What an agent needs to turn its own token counts into this unit."""
    return {
        "unit": UNIT,
        "definition": DEFINITION,
        "typeWeights": dict(TYPE_WEIGHTS),
        "modelWeights": {name: weight for name, weight in MODEL_WEIGHTS},
        "priorsReadOn": PRIORS_READ_ON,
        "confidenceWords": "prior = community estimate only; low, medium, high = share of the account's own data",
    }
