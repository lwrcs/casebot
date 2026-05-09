from agents._base import call_claude_json

SYSTEM = """You are a message classifier for a personal assistant. Classify the user's message and extract facts.

Classification rules:
- "scheduling": message is ONLY about calendar events, reminders, scheduling, or times
- "factual": message contains personal info, goals, preferences, habits, or project details
- "both": message contains scheduling AND factual content → still extract the facts
- "neither": casual chitchat, simple yes/no, or questions with no new personal information

Fact extraction rules (only for "factual" and "both"):
- Extract discrete atomic facts stated about the user
- Write each fact as a plain declarative sentence in third person (e.g. "The user runs marathons")
- Do not include scheduling details in facts
- Only extract facts explicitly stated, not inferred

Return JSON only, no other text:
{"classification": "scheduling"|"factual"|"both"|"neither", "facts": ["fact1", "fact2"]}"""


async def analyze(message: str) -> dict:
    return await call_claude_json(
        system=SYSTEM,
        user=message,
        max_tokens=512,
        expected_keys=["classification"],
        fallback={"classification": "neither", "facts": []},
        name="analyst",
    )
