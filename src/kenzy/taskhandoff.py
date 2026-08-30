"""The background-task hand-off wording — one home, both pipelines.

The classic pipeline (kenzy.llm.llm) and the v6 conversation path
(kenzy.server.tasks) both tell the model the same thing when a deferred-pace
tool detaches: "you started it, it isn't done, don't invent results." The
wording is anti-hallucination-critical (it was hardened after a live incident
of the model fabricating a results list), so it must not drift between the two
paths. kenzy.llm may not import kenzy.server (the no-server-import wire
contract), so the shared text lives in this neutral top-level module both
sides import.
"""

from __future__ import annotations


def handoff_text(title: str) -> str:
    """The in-progress result the MODEL receives for a detached call — it must
    relay "started", never claim completion, and never invent results."""
    return (
        f"Started in the background: {title}. It is not done YET and has "
        "produced NO results — do not invent, predict, or imagine any. If "
        "you have not yet told the user it's started, say so in ONE short "
        "sentence; if you already did, add nothing. A later update in this "
        "conversation will carry the real result."
    )


__all__ = ["handoff_text"]
