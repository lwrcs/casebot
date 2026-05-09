from agents._base import call_claude_json

SYSTEM = """You are a fact tagger for a personal assistant's memory system.

Given a fact and a list of available tags, select the most relevant tags for that fact.

Rules:
- Only select tags from the provided list — do not invent new ones
- Select 2 to 6 tags that best describe what the fact is about
- Prefer more specific tags over generic ones when both apply
- Return an empty list only if truly no tags apply

Return JSON only, no other text:
{"tags": ["tag1", "tag2"]}"""


async def tag_fact(fact_content: str, all_tags: list[str]) -> list[str]:
    if not all_tags:
        return []

    prompt = f"Available tags: {', '.join(all_tags)}\n\nFact: {fact_content}"
    data = await call_claude_json(
        system=SYSTEM,
        user=prompt,
        max_tokens=128,
        expected_keys=["tags"],
        fallback={"tags": []},
        name="tagger",
    )
    return [t for t in data.get("tags", []) if t in all_tags]
