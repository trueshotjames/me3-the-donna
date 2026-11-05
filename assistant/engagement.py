"""v5: cheap engagement check for false starts (fdbench v4: TV / radio 2/2 and side talk 1/2 got a reply).

No model, a few regexes per turn. Two cues mark a transcript as not meant for her:
- it opens by calling someone else by name ("Kayla, you're talking to the radio." "Tim, printer's jammed again");
- it sounds like a broadcast ("Breaking news ...", "Stay tuned", "Welcome back to the show").
The result goes into the per-turn note; the prompt says such a note means <pass/>. The LLM still decides, so a
real request that happens to start with a name she does not know is answered in the worst case, not lost.
"""

from __future__ import annotations

import re

# Capitalised words that often open a sentence followed by a comma, but are not a person being addressed
_NOT_NAMES = {
    "okay", "ok", "yes", "yeah", "yep", "no", "nope", "well", "so", "actually", "hey", "hi", "hello", "please", "now",
    "then", "also", "wait", "sorry", "thanks", "alright", "oh", "um", "umm", "uh", "right", "sure", "great", "hmm",
    "cool", "fine", "honestly", "look", "listen", "basically", "anyway", "mhmm", "good", "perfect", "nice", "true",
    "and", "but", "or", "because", "if", "when", "today", "tomorrow", "tonight", "yesterday", "first", "second",
    "next", "finally", "seriously", "really", "maybe", "of", "still", "again", "instead", "otherwise", "besides",
    "however", "plus", "also", "like", "see", "god", "wow", "ah", "aha", "huh", "man", "dude", "guys", "everyone",
    "everybody", "morning", "evening", "afternoon", "goodnight", "goodbye", "bye", "cheers", "quick", "question",
    "anyways", "meanwhile", "personally", "luckily", "unfortunately", "obviously", "apparently", "anyhow",
}
_VOCATIVE = re.compile(r"^\s*(?:hey|oh|ok(?:ay)?|so|um+|uh)?[,\s]*([A-Z][a-z]+)\s*,\s*(\S.*)$")
_BROADCAST = re.compile(
    r"\b(breaking news|this just in|stay tuned|welcome back to|coming up (next|after)|after the break|brought to "
    r"you by|tune in|ladies and gentlemen|dear (viewers|listeners)|our viewers|our listeners|back to you|in the "
    r"studio|you're listening to|you are listening to|you're watching|you are watching|live from|reporting from)\b",
    re.IGNORECASE,
)


def addressed_elsewhere(text: str, assistant_name: str = "Donna", known: tuple[str, ...] = ()) -> str | None:
    """A short reason when the transcript looks meant for someone or something else, else None."""
    text = (text or "").strip()
    if not text:
        return None
    m = _VOCATIVE.match(text)
    if m:
        name, rest = m.group(1), m.group(2)
        if (
            name.lower() not in _NOT_NAMES
            and name.lower() != assistant_name.lower()
            and name not in known
            and len(rest.split()) >= 2
        ):
            return f"spoken to {name}, not to you"
    if _BROADCAST.search(text):
        return "sounds like TV or radio, not the user"
    return None
