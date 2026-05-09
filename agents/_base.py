"""Shared infrastructure for all agent modules.

Centralizes:
- Anthropic client construction (with max_retries=0 so we control retries ourselves)
- Async retry on OverloadedError
- JSON extraction (handles prose-prefixed responses)
- Required-key validation
- Raw response logging on failure
"""
import asyncio
import json
import logging
import re

import anthropic

from config import settings

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
RETRY_DELAYS = [3, 10]

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(
            api_key=settings.ANTHROPIC_API_KEY,
            max_retries=0,
        )
    return _client


def _extract_json(text: str) -> dict | None:
    """Try strict parse first; fall back to extracting the first {...} block."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


async def call_claude_json(
    *,
    system: str,
    user: str,
    max_tokens: int = 256,
    model: str = DEFAULT_MODEL,
    expected_keys: list[str] | None = None,
    fallback: dict | None = None,
    name: str = "agent",
) -> dict:
    """Call Claude expecting a JSON response. Returns fallback on any failure.

    `name` appears in log lines so we can tell which agent failed.
    """
    fallback = fallback if fallback is not None else {}
    client = _get_client()

    for attempt, delay in enumerate(RETRY_DELAYS + [None]):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            raw = response.content[0].text if response.content else ""
            parsed = _extract_json(raw)
            if parsed is None:
                logger.warning(f"[{name}] non-JSON response: {raw[:200]!r}")
                return fallback
            if expected_keys:
                missing = [k for k in expected_keys if k not in parsed]
                if missing:
                    logger.warning(f"[{name}] missing keys {missing} in: {raw[:200]!r}")
                    return fallback
            return parsed
        except anthropic.OverloadedError:
            if delay is None:
                logger.warning(f"[{name}] gave up after {len(RETRY_DELAYS)} retries on overload")
                return fallback
            await asyncio.sleep(delay)
        except Exception:
            logger.exception(f"[{name}] unexpected error")
            return fallback
