import time
import uuid

import anthropic
import openai

# USD per 1M tokens, first-party pricing (Anthropic + OpenAI).
MODEL_PRICES = {
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "gpt-5": {"input": 1.25, "output": 10.00},
    "gpt-5-mini": {"input": 0.25, "output": 2.00},
}

# These are just the fallback defaults now - per-topic overrides live in topics.organize_model/
# answer_model/fact_check_model (NULL = use these), set from the LIFF page. Unlike itineraryManager's
# /模型 (a global `settings`-table override, chat-command only), this is per-topic and UI-driven -
# see main.py's /api/topics/{id}/model and docs/plan.md for why /模型 itself was skipped.
#
# Temporarily on gpt-5-mini instead of Claude: the Anthropic key in .env turned out to be
# invalid during Phase 1 testing, and rather than block the pipeline test on fixing it, we're
# testing the OpenAI path instead. Switch these three back to claude-sonnet-5/claude-haiku-4-5
# once ANTHROPIC_API_KEY is confirmed working - the provider is picked purely from the model ID
# prefix below (same pattern itineraryManager uses for /模型), so flipping these constants is
# the only change needed either way.
ORGANIZE_MODEL = "gpt-5-mini"
ANSWER_MODEL = "gpt-5-mini"
FACT_CHECK_MODEL = "gpt-5-mini"


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
        # Same reasoning as thinking={"type":"disabled"} above - gpt-5 is a reasoning model and
        # would otherwise spend part of the token budget on hidden reasoning by default.
        reasoning_effort="minimal",
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
