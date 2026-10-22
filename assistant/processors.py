"""Small frame processors used by the pipeline."""

from __future__ import annotations

from loguru import logger
from pipecat.frames.frames import Frame, LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

PASS_TOKEN = "<pass"


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

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMFullResponseStartFrame):
            self._held, self._text, self._deciding, self._dropping = [], "", True, False
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMTextFrame) and direction == FrameDirection.DOWNSTREAM:
            if self._dropping:
                return
            if self._deciding:
                self._held.append(frame)
                self._text += frame.text
                stripped = self._text.lstrip()
                if stripped.lower().startswith(PASS_TOKEN):
                    self._dropping, self._deciding = True, False
                    self._held = []
                    logger.info("LLM passed on this turn (not addressed to the bot)")
                    if self._on_pass:
                        await self._on_pass()
                    return
                if len(stripped) >= len(PASS_TOKEN) or not PASS_TOKEN.startswith(stripped.lower()[: len(PASS_TOKEN)]):
                    self._deciding = False
                    for held in self._held:
                        await self.push_frame(held, direction)
                    self._held = []
                return
            await self.push_frame(frame, direction)
            return

        if isinstance(frame, LLMFullResponseEndFrame):
            if self._deciding:
                for held in self._held:
                    await self.push_frame(held, direction)
            self._held, self._deciding, self._dropping = [], False, False
            await self.push_frame(frame, direction)
            return

        await self.push_frame(frame, direction)
