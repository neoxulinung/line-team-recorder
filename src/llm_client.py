import time
import uuid

import anthropic
import openai

# USD per 1M tokens, first-party pricing (Anthropic + OpenAI). Update this table if pricing
# changes. Checked 2026-09: gpt-5/gpt-5-mini (both OpenAI's own general-purpose line) are
# superseded by the gpt-5.6 tier below - dropped from the picker menu rather than left in as a
# stale, no-longer-current option (still callable via the API until OpenAI's Dec 11 2026
# retirement date, just no longer the current line). gpt-5.6-sol's $4/$20 is promotional
# pricing confirmed live against OpenAI's pricing page, guaranteed at least through Nov 21
# 2026 - re-check before then. GPT-6 Astra and GPT-5.5/5.4 exist too (per OpenAI's own pricing
# docs) but aren't listed here yet - no public source gives their exact API model ID string,
# and guessing one risks a runtime failure the first time someone actually picks it.
MODEL_PRICES = {
    "claude-opus-5": {"input": 5.00, "output": 25.00},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "gpt-5.6-sol": {"input": 4.00, "output": 20.00},
    "gpt-5.6-terra": {"input": 2.00, "output": 12.00},
    "gpt-5.6-luna": {"input": 0.20, "output": 1.20},
}

# These are just the fallback defaults now - per-topic overrides live in topics.organize_model/
# answer_model/fact_check_model (NULL = use these), set from the LIFF page. Unlike itineraryManager's
# /模型 (a global `settings`-table override, chat-command only), this is per-topic and UI-driven -
# see main.py's /api/topics/{id}/model and docs/plan.md for why /模型 itself was skipped.
#
# gpt-5.6-luna (not Claude): gpt-5-mini was superseded by the gpt-5.6 tier (see MODEL_PRICES
# above), so this stays on the OpenAI path rather than switching back to
# claude-sonnet-5/claude-haiku-4-5 - the provider is picked purely from the model ID prefix
# below (same pattern itineraryManager uses for /模型), so flipping these constants is the
# only change needed either way.
ORGANIZE_MODEL = "gpt-5.6-luna"
ANSWER_MODEL = "gpt-5.6-luna"
FACT_CHECK_MODEL = "gpt-5.6-luna"


async def call_llm(
    env, purpose: str, topic_id: str | None, model: str, system: str, user_content: str, max_tokens: int = 8000,
) -> str:
    if model.startswith("gpt-"):
        text, input_tokens, output_tokens = await _call_openai(env, model, system, user_content, max_tokens)
    else:
        text, input_tokens, output_tokens = await _call_anthropic(env, model, system, user_content, max_tokens)
    await _log_usage(env, purpose, topic_id, model, input_tokens, output_tokens)
    return text


async def _call_anthropic(env, model: str, system: str, user_content: str, max_tokens: int) -> tuple[str, int, int]:
    # No connect/read timeout split, no httpx2, no max_retries=0 fight against a platform-level
    # kill: Cloud Run has no uncatchable ceiling like Cloudflare's ~30s ctx.waitUntil() budget, so
    # a single generous, genuinely-catchable timeout is enough. max_retries=0 kept anyway - one
    # clean attempt, one clean failure, no reason to let a retry loop run long just because it can.
    client = anthropic.AsyncAnthropic(api_key=env.anthropic_api_key, timeout=120.0, max_retries=0)
    # thinking explicitly disabled: itineraryManager found live that claude-sonnet-5 can emit an
    # unprompted 'thinking' block that eats most of the output budget. Model behavior, not
    # platform-specific - applies here too.
    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        thinking={"type": "disabled"},
        messages=[{"role": "user", "content": user_content}],
    )
    text = ""
    for block in response.content:
        if block.type == "text":
            text = block.text
            break
    return text, response.usage.input_tokens, response.usage.output_tokens


async def _call_openai(env, model: str, system: str, user_content: str, max_tokens: int) -> tuple[str, int, int]:
    client = openai.AsyncOpenAI(api_key=env.openai_api_key, timeout=120.0, max_retries=0)
    response = await client.chat.completions.create(
        model=model,
        max_completion_tokens=max_tokens,
        # Same reasoning as thinking={"type":"disabled"} above - this is a reasoning model and
        # would otherwise spend part of the token budget on hidden reasoning by default. "none"
        # not "minimal": confirmed live against gpt-5.6 that "minimal" is no longer an accepted
        # value (400 Unsupported value) - the accepted range is now none/low/medium/high/xhigh.
        reasoning_effort="none",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
    )
    text = response.choices[0].message.content or ""
    return text, response.usage.prompt_tokens, response.usage.completion_tokens


async def _log_usage(env, purpose, topic_id, model, input_tokens, output_tokens) -> None:
    prices = MODEL_PRICES.get(model, {"input": 0, "output": 0})
    cost = (input_tokens * prices["input"] + output_tokens * prices["output"]) / 1_000_000
    await env.db.query(
        "INSERT INTO llm_usage (id, topic_id, purpose, model, input_tokens, output_tokens, estimated_cost_usd, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [str(uuid.uuid4()), topic_id, purpose, model, input_tokens, output_tokens, cost, int(time.time())],
    )
