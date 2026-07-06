"""
Random utility skills for kenzy-llm.

These skills produce non-deterministic results and should only be called when
the user explicitly wants a random or chance-based outcome.
"""

from __future__ import annotations

import random
import re

from kenzy.llm.skills import FastResult, fast_intent, skill  # type: ignore[import]


@skill
async def flip_coin() -> str:
    """Flip a coin and return Heads or Tails.

    Use ONLY when the user explicitly asks to flip a coin or wants a random
    heads/tails result to make a decision by chance.
    """
    return random.choice(["Heads", "Tails"])


@skill
async def pick_number(min_value: int, max_value: int) -> str:
    """Pick a random integer between min_value and max_value (inclusive).

    Use ONLY when the user explicitly asks for a random number in a range,
    e.g. "pick a number between 1 and 100".  Do not use to answer questions
    that have a real numerical answer.
    """
    if min_value > max_value:
        min_value, max_value = max_value, min_value
    return str(random.randint(min_value, max_value))


@skill
async def roll_dice(sides: int = 6, rolls: int = 1) -> str:
    """Roll one or more dice and return the results.

    Use when the user asks to roll dice, e.g. "roll a d20" or "roll 3d6".
    Defaults to a single six-sided die.
    """
    if sides < 2:
        sides = 2
    if rolls < 1:
        rolls = 1
    results = [random.randint(1, sides) for _ in range(rolls)]
    if rolls == 1:
        return str(results[0])
    total = sum(results)
    rolls_str = ", ".join(str(r) for r in results)
    return f"{rolls_str} (total: {total})"


@skill
async def pick_from_list(options: list[str], count: int = 1) -> str:
    """Pick one or more random items from a list of options.

    Use when the user asks you to randomly choose or pick from a set of
    things they provide, e.g. "pick one of these restaurants: A, B, C" or
    "randomly choose 2 names from this list".  Do not use to make a
    recommendation based on merit — only when the user wants a random pick.
    """
    if not options:
        return "No options provided."
    count = max(1, min(count, len(options)))
    chosen = random.sample(options, count)
    if len(chosen) == 1:
        return chosen[0]
    return ", ".join(chosen[:-1]) + f", and {chosen[-1]}"


@skill
async def yes_no_maybe() -> str:
    """Randomly return Yes, No, or Maybe.

    Use ONLY when the user explicitly wants a random yes/no answer, asks you
    to decide by chance, or frames the question as a coin-flip style choice
    (e.g. "just pick for me", "give me a random answer").  Do NOT use for
    questions that have a factual, deterministic, or reasoned answer — answer
    those directly without calling this tool.
    """
    return random.choice(["Yes", "No", "Maybe"])


# ---------------------------------------------------------------------------
# Fast path — the canonical bare forms only. Anything with a tail ("flip a coin
# to decide whether I should paint the house") falls through to the LLM, which
# can reason about it. No model call, no network for the common phrasings.
# ---------------------------------------------------------------------------

_COIN_RE = re.compile(r"^(?:flip (?:a )?coin|heads or tails|coin flip|toss (?:a )?coin)$")
# "roll a die", "roll dice", "roll 3 dice", "roll a d20", "roll d20", "roll 3d6".
_DICE_NDM_RE = re.compile(r"^roll (\d+)d(\d+)$")  # D&D notation: N dice of M sides
_DICE_RE = re.compile(r"^roll (?:(\d+) )?(?:a )?(?:dice|die|d(\d+))$")
_NUMBER_RE = re.compile(
    r"^(?:pick|choose|give me|random) (?:a )?(?:random )?number "
    r"(?:between|from) (-?\d+) (?:and|to) (-?\d+)$"
)
_VOICE = "Speak naturally at a conversational pace."


@fast_intent(priority=40)
async def fast_random(utterance: str, room_id: str | None, speaker: str | None) -> FastResult:
    """Instant coin flips, dice rolls, and number picks — bare forms only."""
    text = re.sub(r"[^\w\s]", "", utterance).strip().lower()
    text = re.sub(r"\s+", " ", text)

    if _COIN_RE.match(text):
        return FastResult.handled(await flip_coin(), _VOICE)

    m = _DICE_NDM_RE.match(text)
    if m:
        return FastResult.handled(
            await roll_dice(sides=int(m.group(2)), rolls=int(m.group(1))), _VOICE
        )
    m = _DICE_RE.match(text)
    if m:
        rolls = int(m.group(1)) if m.group(1) else 1
        sides = int(m.group(2)) if m.group(2) else 6
        return FastResult.handled(await roll_dice(sides=sides, rolls=rolls), _VOICE)

    m = _NUMBER_RE.match(text)
    if m:
        return FastResult.handled(await pick_number(int(m.group(1)), int(m.group(2))), _VOICE)

    return FastResult.miss()
