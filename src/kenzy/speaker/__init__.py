"""Kenzy speaker-identification package.

The canonical default enrollment prompts live here (not in ``speaker.py``) so both
the ``kenzy-enroll`` CLI and the server-side voice-enrollment loop can share them
without importing the speaker service's heavy dependencies. The operative list is
``enroll_prompts`` in the speaker service config (dashboard-editable); this is only
the fallback when that's unset.
"""

from __future__ import annotations

DEFAULT_ENROLL_PROMPTS: list[str] = [
    "The weather outside is looking pretty good today.",
    "Can you turn off the lights in the living room please.",
    "What time does the movie start tonight?",
    "I'd like to set a reminder for tomorrow morning at eight.",
    "Please add milk and eggs to the shopping list.",
]
