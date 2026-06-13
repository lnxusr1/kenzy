"""
Assistant identity skills for kenzy-llm.
"""

from __future__ import annotations

from importlib.metadata import version, PackageNotFoundError

from kenzy.llm.skills import skill  # type: ignore[import]


@skill
async def get_assistant_version() -> str:
    """Return the current installed version of the Kenzy assistant.

    Use when the user asks what version you are, or asks about your version number.
    """
    try:
        return f"Kenzy version {version('kenzy')}"
    except PackageNotFoundError:
        return "Version information is not available."
