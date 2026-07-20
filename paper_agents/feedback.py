from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from paper_agents.openai_helpers import call_openai_json


class FeedbackAgent:
    """Turns natural-language feedback into an updated preference profile."""

    def run(self, profile: dict[str, Any], feedback_text: str) -> dict[str, Any]:
        system_prompt = """
You are Agent 3: Feedback Agent.
Update the user's research preference profile based on natural-language feedback.
Preserve useful existing interests unless the feedback clearly rejects them.
Keep the profile small and human-editable.
Return only valid JSON in this shape:
{
  "interests": ["..."],
  "positive_signals": ["..."],
  "negative_signals": ["..."],
  "notes": "short profile note",
  "feedback_history": [
    {
      "timestamp": "...",
      "feedback": "...",
      "interpreted_change": "..."
    }
  ]
}
""".strip()
        payload = {
            "current_profile": profile,
            "new_feedback": {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "feedback": feedback_text,
            },
        }
        updated = call_openai_json(system_prompt, payload)
        updated.setdefault("feedback_history", profile.get("feedback_history", []))
        return updated
