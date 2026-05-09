from agents._base import call_claude_json

SYSTEM = """You are a message router for a personal assistant.

Determine what resources a message needs and which model tier to use.

needs_memory_query: true if the message asks about the user's personal info, preferences, goals, projects, habits, or past context that would be stored in memory
needs_calendar: true if the message involves scheduling, checking the calendar, creating/updating events, or asking about the user's schedule
query_tags: if needs_memory_query is true, list the tag names most likely to match relevant stored facts (pick from the user's likely tag vocabulary — short lowercase words)
use_sonnet: true if the message requires complex reasoning, goal-aware suggestions, or ANY calendar write operation (creating, editing, rescheduling, or cancelling events — date arithmetic must be exact). false only for read-only calendar lookups ("what's on my calendar?"), simple memory queries, or behavior/status updates.

Return JSON only, no other text:
{"needs_memory_query": bool, "needs_calendar": bool, "query_tags": ["tag1", "tag2"], "use_sonnet": bool}"""

FALLBACK = {"needs_memory_query": False, "needs_calendar": False, "query_tags": [], "use_sonnet": True}


async def route(message: str, recent_turns: list[dict]) -> dict:
    context_lines = []
    for turn in recent_turns[-3:]:
        role = "User" if turn["role"] == "user" else "Assistant"
        content = turn["content"]
        if isinstance(content, list):
            content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
        context_lines.append(f"{role}: {content[:200]}")

    context_str = "\n".join(context_lines) if context_lines else "(no prior context)"
    prompt = f"Recent context:\n{context_str}\n\nNew message: {message}"

    return await call_claude_json(
        system=SYSTEM,
        user=prompt,
        max_tokens=150,
        expected_keys=["needs_memory_query", "needs_calendar", "use_sonnet"],
        fallback=FALLBACK,
        name="listener",
    )
