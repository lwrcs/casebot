from agents._base import call_claude_json
from db import database as db

SYSTEM = """You are a tag pool manager for a personal assistant's memory system.

Given a message and a list of existing tags, propose new tags that should be added to the pool.

Rules for proposing new tags:
- Tags are single lowercase words or short hyphenated phrases (e.g. "fitness", "half-marathon", "side-project")
- Only propose tags representing concepts genuinely present in the message
- REJECT proposed tags that are semantically equivalent to existing ones:
  - Morphological variants: run/running/runner → if "running" exists, don't add "run" or "runner"
  - Synonyms: fitness/exercise/workout → pick one, reject the others
  - Specific/general overlap: "half-marathon" may coexist with "running", but "jogging" should not if "running" exists
- Return an empty list if no genuinely new tags are needed

Return JSON only, no other text:
{"new_tags": ["tag1", "tag2"]}"""


async def update_tag_pool(conn, discord_id: str, message: str) -> list[str]:
    existing = db.get_all_tags(conn, discord_id)
    existing_str = ", ".join(existing) if existing else "(none yet)"
    prompt = f"Existing tags: {existing_str}\n\nMessage: {message}"

    data = await call_claude_json(
        system=SYSTEM,
        user=prompt,
        max_tokens=256,
        expected_keys=["new_tags"],
        fallback={"new_tags": []},
        name="tag_manager",
    )
    new_tags = [t.lower().strip() for t in data.get("new_tags", []) if t and t.strip()]
    if new_tags:
        db.upsert_tags(conn, discord_id, new_tags)
    return new_tags
