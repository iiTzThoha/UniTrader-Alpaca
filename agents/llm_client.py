"""
Thin wrapper around the `openai` SDK, pointed at AI/ML API's
OpenAI-compatible endpoint instead of OpenAI's own.

Both Proposer and Critic use this same client/key - they're
differentiated by which MODEL NAME they pass per-call, not by
separate credentials. This still gives genuine independence between
the two agents' outputs (different underlying models reasoning
independently), which matters for the risk-governance story, while
only requiring the one AI/ML API key the user actually has.
"""

from __future__ import annotations

from functools import lru_cache

from openai import OpenAI

from config.settings import settings


@lru_cache(maxsize=1)
def get_llm_client() -> OpenAI:
    """Returns a cached OpenAI-SDK client pointed at AI/ML API's base URL."""
    if not settings.aiml_api_key:
        raise RuntimeError(
            "AI/ML API credentials missing. Check your .env file has "
            "AIML_API_KEY set."
        )
    return OpenAI(
        api_key=settings.aiml_api_key,
        base_url=settings.aiml_base_url,
    )


def call_model(model: str, system_prompt: str, user_prompt: str, temperature: float = 0.3, max_tokens: int = 500) -> str:
    """Makes a single chat completion call and returns the raw text response.
    Kept simple/synchronous for now - both agents call this directly.
    max_tokens defaults to 500 - comfortably fits the JSON response shape both
    agents use (a few short fields + a 2-4 sentence rationale) without risking
    truncation mid-JSON, while capping runaway/verbose completions that were
    burning credit for no quality benefit."""
    client = get_llm_client()
    response = client.chat.completions.create(
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    return response.choices[0].message.content or ""


if __name__ == "__main__":
    # Smoke test: `python -m agents.llm_client`
    # Requires a real AIML_API_KEY in .env. Also prints the model name used
    # so we can confirm it's a valid AI/ML API model string.
    result = call_model(
        model=settings.proposer_model,
        system_prompt="You are a helpful assistant. Respond in one short sentence.",
        user_prompt="Say hello and confirm you're working.",
    )
    print(f"Model used: {settings.proposer_model}")
    print(f"Response: {result}")
