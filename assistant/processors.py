"""Small frame processors used by the pipeline."""

from __future__ import annotations

import re

from loguru import logger
from pipecat.frames.frames import Frame, LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame, TTSTextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

PASS_TOKEN = "<pass"
WAIT_TOKEN = "<wait"  # v6 (LLM_EOT)
# v5b: also after a "?" glued to the next sentence ("...set for?When would you like the alarm?", v5 scenario 4)
_SENTENCE_END = re.compile(r'[.!?]+["\')\]]*\s+|\?(?=[A-Z])')

# gpt-oss sometimes writes a non-breaking hyphen as the literal text "\u2011", and likes typographic
# punctuation. Both reach the TTS as odd sounds, so sentences are cleaned before they are spoken.
_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")
_DASH = re.compile(r"\s*[\u2014\u2015]\s*")
_ASCII = str.maketrans(
    {
        "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-",
        "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201c": '"', "\u201d": '"', "\u201e": '"',
        "\u2026": "...", "\u00a0": " ", "\u202f": " ", "\u2009": " ", "\u200b": "",
    }
)


# v2: gpt-oss-120b appends "<pass/>" after a real answer ("Tokyo. <pass/>"), and once wrote a tool call as text
# ('{"room": "all", "on": true}'). Neither may be spoken: tags and JSON objects are cut out of every sentence.
# v2 route test: the fast model once read out "Background task finished: ..." verbatim
_INTERNAL = re.compile(r"\bbackground (task|assistant)\b|this message is from the", re.IGNORECASE)
_TAG = re.compile(r"<\s*[/!]?\s*[a-zA-Z_|][^<>]{0,160}>")
_JSON = re.compile(r"\{[^{}]*\}")
# v3: gpt-oss leaked its tool-call channel as text once: "... seven AM. to=functions.delete_alarm Alarm deleted."
_TOOL_CHANNEL = re.compile(r"\s*\b(?:to=)?functions\.[A-Za-z_]\w*(?:\s*<\|[^|]*\|>)*")
# v5: gpt-oss wrote both sides of a confirmation ("Which alarm should I delete? Confirm delete alarm wake up at seven
# AM? Yes. Alarm deleted.") and once ended an answer with "We are done." After a question near the start of a reply,
# a second question or a bare "Yes." / "No." ends the reply; the filler sentence is dropped.
_FILLER = re.compile(r"^\s*(we are|we're|i am|i'm) (all )?(done|finished)[.!]?\s*$", re.IGNORECASE)
_ANSWER = re.compile(r"^\s*(yes|no|yeah|yep|nope)\b[.!,]", re.IGNORECASE)
# v7 (META_FILTER): the v6 A/B passes got "Twelve. We are done. (no further)Done. (no more)Okay. Stop. Done. We should
# stop. The answer already given. We must output only once. Finished. Okay." Model narration ends the reply.
_META = re.compile(
    r"^\s*(?:\((?:no further|no more|end|done|stop|silence|pause|no output|nothing (?:more|else))[^)]*\)"
    r"|(?:we are|we're|i am|i'm) (?:all )?(?:done|finished)\b"
    r"|(?:we|i)(?: (?:should|must|need to|will|can)|'ll) (?:stop|end|finish)(?: here| now)?[.!]?\s*$"
    r"|(?:we|i) (?:must|should) (?:only )?output\b"
    r"|the answer (?:is |was |has been )?(?:already )?(?:given|provided|complete)"
    r"|(?:answer|response) (?:already )?(?:given|provided|complete)"
    r"|end of (?:answer|response|output)|no further (?:output|response|text|action))",
    re.IGNORECASE,
)
META = False  # set by main.py (META_FILTER)
# v7 full run (s01): "Twelve.We are done.<pass/>" has no space after the full stop, so it reached the filter as one
# sentence and _META (anchored at the start) missed it. Narration after a sentence end inside a sentence is cut too.
_META_IN = re.compile(r"(?<=[.!?])\s*(?=" + _META.pattern[len(r"^\s*"):] + ")", re.IGNORECASE)


# v10 (fd-bench 6 Nov): a long answer came back as markdown and she read "star star root causes star star" aloud
_MD_HEADING = re.compile(r"(?m)^\s*#{1,6}\s+")
_MD_BULLET = re.compile(r"(?m)^\s*[-+*]\s+(?=\S)")
_MD_MARK = re.compile(r"\*+|`+|(?<!\w)_{2,}|_{2,}(?!\w)")


def markdown_removed(text: str) -> str:
    """Text without markdown emphasis, headings, bullets and inline-code marks (the words stay)."""
    if not any(c in text for c in "*#`_-+"):
        return text
    text = _MD_HEADING.sub("", text)
    text = _MD_BULLET.sub("", text)
    return _MD_MARK.sub("", text)


def unspeakable_removed(text: str) -> str:
    """Text without markup tags, markdown and JSON objects; an unclosed "{" drops the rest of the text."""
    text = markdown_removed(text)
    text = _TOOL_CHANNEL.sub("", text)
    text = _TAG.sub("", text)
    prev = None
    while prev != text:  # innermost objects first, so nested JSON goes too
        prev, text = text, _JSON.sub("", text)
    if "{" in text:
        text = text[: text.index("{")]
    return text.replace("}", "")


def speakable(text: str) -> str:
    """Text as the TTS should get it: literal \\uXXXX escapes decoded, typographic punctuation to ASCII."""
    text = _ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)
    return _DASH.sub(", ", text).translate(_ASCII)


class PlayedTextTap(FrameProcessor):
    """v8 (PLAYED_TEXT): her sentences as they finish playing.

    Pipecat queues each sentence's TTSTextFrame behind that sentence's audio, and the output transport pushes it on
    only after the audio has gone to the speaker. The observers see the frame when the TTS service first pushes it,
    which is as soon as the sentence is synthesised (faster than real time), so the chat and the spoken history showed
    sentences she never got to say when she was cut off. This tap sits after the output transport and reports only
    sentences that were played. A sentence cut off part-way is not reported (its text frame is dropped on the
    interruption), so the record errs on the side of what she certainly said.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.listeners: list = []

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, TTSTextFrame) and frame.text.strip():
            for fn in self.listeners:
                try:
                    fn(frame)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"played-text listener failed: {e!r}")
        await self.push_frame(frame, direction)


class PassFilter(FrameProcessor):
    """Drops an LLM response that starts with <pass/> (speech not addressed to the bot).

    Text tokens are held until enough characters arrive to tell whether the
    response is a pass; a normal response is released with at most a few
    tokens of delay.
    """

    def __init__(self, on_pass=None, **kwargs):
        super().__init__(**kwargs)
        self._held: list[LLMTextFrame] = []
        self._text = ""
        self._deciding = False
        self._dropping = False
        self._on_pass = on_pass
        # v1: one response never says the same sentence twice ("No alarms set. No alarms set.")
        self._sentence = ""
        self._said: set[str] = set()
        self._in_code = False  # v2: inside a ``` block, which is never spoken
        self._sentences = 0  # v5: sentences spoken in this reply
        self._asked = False  # v5: a question was spoken near the start of this reply
        self._cut = False  # v5: the rest of this reply is invented dialogue

    async def _emit(self, text: str, direction: FrameDirection) -> None:
        """Pass text on sentence by sentence (the TTS speaks whole sentences anyway, so this adds no delay)."""
        self._sentence += text
        while True:
            m = _SENTENCE_END.search(self._sentence)
            if not m:
                return
            done, self._sentence = self._sentence[: m.end()], self._sentence[m.end():]
            await self._emit_sentence(done, direction)

    async def _emit_sentence(self, sentence: str, direction: FrameDirection) -> None:
        if "```" in sentence or self._in_code:
            keep, in_code = [], self._in_code
            for i, part in enumerate(sentence.split("```")):
                if i > 0:
                    in_code = not in_code
                if not in_code:
                    keep.append(part)
            self._in_code = in_code
            logger.info(f"dropped code from a reply: {sentence.strip()[:60]!r}")
            sentence = " ".join(keep)
            if not sentence.strip():
                return
        if _INTERNAL.search(sentence):
            logger.info(f"dropped an internal sentence: {sentence.strip()[:80]!r}")
            return
        cleaned = unspeakable_removed(sentence)
        if cleaned != sentence:
            logger.info(f"removed unspeakable text from a sentence: {sentence.strip()[:80]!r}")
            sentence = cleaned if cleaned.strip() else ""
        if not sentence.strip():
            if sentence:
                await self.push_frame(LLMTextFrame(sentence), direction)
            return
        if self._cut:
            logger.info(f"dropped invented dialogue: {sentence.strip()[:80]!r}")
            return
        if META and _META.match(sentence):
            self._cut = True
            logger.info(f"dropped model narration and the rest of the reply: {sentence.strip()[:80]!r}")
            return
        if META:
            m = _META_IN.search(sentence)
            if m:
                self._cut = True
                logger.info(f"dropped model narration and the rest of the reply: {sentence[m.start():].strip()[:80]!r}")
                sentence = sentence[: m.start()] + " "
                if not sentence.strip():
                    return
        if _FILLER.match(sentence):
            logger.info(f"dropped a filler sentence: {sentence.strip()!r}")
            return
        is_question = sentence.rstrip().rstrip("\"')]").endswith("?")
        if self._asked and (is_question or _ANSWER.match(sentence)):
            self._cut = True
            logger.info(f"dropped invented dialogue: {sentence.strip()[:80]!r}")
            return
        self._sentences += 1
        if is_question and self._sentences <= 2:
            self._asked = True
        key = re.sub(r"[^a-z0-9]+", " ", sentence.lower()).strip()
        if key and key in self._said:
            logger.info(f"dropped a repeated sentence: {sentence.strip()!r}")
            return
        self._said.add(key)
        await self.push_frame(LLMTextFrame(sentence), direction)
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMFullResponseStartFrame):
            self._held, self._text, self._deciding, self._dropping = [], "", True, False
            self._sentence, self._said, self._in_code = "", set(), False
            self._sentences, self._asked, self._cut = 0, False, False
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMTextFrame) and direction == FrameDirection.DOWNSTREAM:
            if self._dropping:
                return
            if self._deciding:
                self._held.append(frame)
                self._text += frame.text
                stripped = self._text.lstrip()
                low = stripped.lower()
                if low.startswith(WAIT_TOKEN):
                    # v6: the LLM's end-of-turn verdict "not finished"; never spoken
                    self._dropping, self._deciding = True, False
                    self._held = []
                    logger.info("LLM answered <wait/> (the user's sentence is not finished)")
                    return
                if low.startswith(PASS_TOKEN):
                    self._dropping, self._deciding = True, False
                    self._held = []
                    logger.info("LLM passed on this turn (not addressed to the bot)")
                    if self._on_pass:
                        await self._on_pass()
                    return
                if len(stripped) >= len(PASS_TOKEN) or not (
                    PASS_TOKEN.startswith(low[: len(PASS_TOKEN)]) or WAIT_TOKEN.startswith(low[: len(WAIT_TOKEN)])
                ):
                    self._deciding = False
                    for held in self._held:
                        await self._emit(held.text, direction)
                    self._held = []
                return
            await self._emit(frame.text, direction)
            return

        if isinstance(frame, LLMFullResponseEndFrame):
            if self._deciding:
                for held in self._held:
                    await self._emit(held.text, direction)
            await self._emit_sentence(self._sentence, direction)
            self._sentence = ""
            self._held, self._deciding, self._dropping = [], False, False
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)
