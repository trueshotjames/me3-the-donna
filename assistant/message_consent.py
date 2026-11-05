"""Hard-coded consent for send_message: a message is never sent without the user's explicit yes.

Two phases, enforced in code (fast_confirm.wrap_tools), not left to the LLM:
1. The first send_message call for a draft does NOT reach the tools server. The draft is stored and Donna reads
   it back: 'To Love: "I'll be late." Send it?'.
2. Only in the NEXT user turn, within MAX_AGE seconds, when that user turn is a clear yes and the model calls
   send_message again with the same contact and text, is the message forwarded (exactly the confirmed text).
Anything else (no, a change, a different text, a stale draft) never sends; a different draft is read back anew.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

MAX_AGE = 120.0  # seconds a read-back stays valid

_YES = re.compile(
    r"^\s*(yes|yeah|yep|yup|sure|ok|okay|go ahead|send it|send|do it|please do|please|correct|right|oo|opo|sige|g)\b",
    re.I,
)
_NOT = re.compile(r"\b(no|nope|not|don'?t|do not|wait|change|instead|but|hindi|wag|huwag|teka)\b", re.I)


def is_clear_yes(utterance: str) -> bool:
    u = (utterance or "").strip()
    return bool(_YES.search(u)) and not _NOT.search(u)


def read_back(contact: str, text: str) -> str:
    end = "" if text[-1:] in ".?!" else "."
    return f'To {contact}: "{text}{end}" Send it?'


def _norm(s: str) -> str:
    return re.sub(r"[^\w]+", " ", (s or "").casefold()).strip()


@dataclass
class Draft:
    contact: str
    text: str
    turn: int
    t: float


class MessageConsent:
    def __init__(self):
        self.pending: Draft | None = None

    def decide(self, contact: str, text: str, turn: int, last_user: str, now: float | None = None) -> tuple[str, str]:
        """('send', text to send) or ('ask', what Donna says). `turn` = index of the current user turn."""
        now = time.time() if now is None else now
        contact, text = (contact or "").strip(), (text or "").strip()
        d = self.pending
        same = d is not None and _norm(d.contact) == _norm(contact) and _norm(d.text) == _norm(text)
        if same and turn - d.turn == 1 and now - d.t <= MAX_AGE and is_clear_yes(last_user):
            self.pending = None
            return "send", d.text
        if same and d.turn == turn:
            return "ask", read_back(d.contact, d.text)  # asked twice in one turn: same read-back
        self.pending = Draft(contact, text, turn, now)
        return "ask", read_back(contact, text)


CONSENT = MessageConsent()
