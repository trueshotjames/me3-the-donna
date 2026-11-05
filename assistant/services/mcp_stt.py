"""Pipecat STT service that streams audio through our ASR MCP server.

Pipecat's MCP client only wires tools to the LLM, so speech goes through this
custom STTService: every ~60 ms of microphone audio is pushed to the ASR MCP
server with `asr_push`, and a background task long-polls `asr_events` and turns
the server's Flux events into Pipecat frames exactly as Pipecat's own Deepgram
Flux service would:

    StartOfTurn    -> ProposedUserStartedSpeakingFrame (barge-in when the bot talks)
    Update         -> InterimTranscriptionFrame
    EagerEndOfTurn -> EagerTranscriptionFrame (LLM starts early, answer is held)
    TurnResumed    -> EagerEndOfTurnCancelFrame
    EndOfTurn      -> TranscriptionFrame(finalized=True) + ProposedUserStoppedSpeakingFrame

Audio gate (GATE_ENABLED=1): audio only goes to Flux while it can matter, to limit Flux use.
    open   when the local VAD hears a voice (tuned for high recall), or while the bot is
           speaking and for BOT_HANGOVER_SECS after (so Flux decides barge-in), and
           first sends the last PREROLL_MS of audio kept in memory (words said before the
           VAD fired).
    close  when the VAD is quiet, the bot is not speaking, and Flux has ended its turn
           (or never started one within GATE_NOISE_TAIL_SECS: noise), or as a backstop
           GATE_CAP_SECS after the VAD went quiet.

Timing: every pushed chunk's capture time and its position in the Flux stream are kept,
so Flux word times (seconds of audio) map back to the wall-clock moment you spoke.
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from collections import deque
from typing import AsyncGenerator

from loguru import logger
from mcp import StdioServerParameters
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    InterimTranscriptionFrame,
    ProposedUserStartedSpeakingFrame,
    ProposedUserStoppedSpeakingFrame,
    StartFrame,
    STTMetadataFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessorSetup
from pipecat.services.settings import STTSettings
from pipecat.services.stt_service import STTService
from pipecat.turns.eager_end_of_turn_mixin import EagerEndOfTurnSTTServiceMixin
from pipecat.turns.user_start import (
    ExternalUserTurnStartStrategy,
    TranscriptionUserTurnStartStrategy,
    VADUserTurnStartStrategy,
)
from pipecat.turns.user_stop import EagerUserTurnStopStrategy, ExternalUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.utils.time import time_now_iso8601

from assistant.mcp_bridge import MCPBridge
from assistant.turn_metrics import METRICS
from assistant.voice_gate import FLAGS, norm

# v3: a transcript ending on one of these words is not a finished request ("Remind me to", "Turn on the")
DANGLING_WORDS = {
    "the", "a", "an", "to", "of", "in", "at", "for", "with", "and", "or", "but", "my", "your", "his", "her",
    "their", "our", "from", "into", "about", "by", "um", "umm", "uh", "uhm", "er", "erm", "actually",
}
PARTICLES = {"on", "off", "up", "down"}  # dangling only right after the verb: "Turn on" (but "Turn it on" is done)
OBJECT_VERBS = {"turn", "switch", "shut", "power"}  # "Turn on" needs an object; "Hang up" does not (full run, t6)


# v5: Flux mis-hearings of short commands seen in the benchmarks ("Call Dad" -> "Cold ad", "Call Mom" -> "Coal mom",
# "Remind me to ..." -> "Remind me too!" which then missed the dangling-word hold). Only short transcripts that
# start this way are changed, so "Is it cold outside?" stays as it is.
_CONTACTS = "mom|dad|sam|alex|work"
_ASR_FIXES = [
    (re.compile(r"^\s*(?:cold|coal|cole|col|caul)\s+ad\b", re.I), "Call Dad"),
    (re.compile(rf"^\s*(?:cold|coal|cole|col|caul)\s+({_CONTACTS})\b", re.I), lambda m: f"Call {m.group(1).title()}"),
    (re.compile(r"^\s*(remind|tell|ask)\s+me\s+too\s*[!.,]?\s*$", re.I), lambda m: f"{m.group(1).title()} me to"),
    # v6: "Make it a bit warmer in here." -> "Make it a bit water in here." (v5b t6)
    (re.compile(r"\b(a (?:bit|little|lot)|it|much|slightly)\s+water\b", re.I), lambda m: f"{m.group(1)} warmer"),
]

# v6: words that, alone, are a listener's backchannel rather than a request (fdbench v1.5 backchannel clips)
BACKCHANNEL = {
    "uh", "huh", "uh-huh", "uhhuh", "mhm", "mm", "mmm", "hmm", "mm-hmm", "mmhmm", "hm", "um", "umm", "okay", "ok",
    "right", "yeah", "yep", "yup", "sure", "alright", "cool", "nice", "wow", "oh", "ah", "aha", "i", "see",
    "got", "it", "gotcha", "true", "really", "interesting", "great", "good", "exactly", "totally", "indeed",
    # v7: more spellings Flux uses for a listener's noises
    "kay", "yea", "ya", "yes", "ooh", "mhmm", "mmhmm", "uhuh", "hum", "huh-uh", "aw",
}

# v7 (EOT_ADAPTIVE): words a speaker trails off on mid-thought ("... and then", "... because", "like")
# Without final punctuation (a Flux partial) any of TRAILING_OPEN; with it, only TRAILING_CLOSED and 6+ words.
TRAILING_CLOSED = {"so", "because", "cause", "'cause", "like", "although", "though", "unless", "whether", "until",
                   "um", "uh", "and", "or", "but", "umm", "uhm", "er", "erm", "plus", "except", "whereas", "nor"}
TRAILING_OPEN = TRAILING_CLOSED | {"then", "if", "when", "that", "which", "while", "where", "since", "also", "just",
                                   "basically", "i", "we", "you", "it's", "is", "was", "were", "are", "be", "kind", "sort",
                                   # v8: more words that need something after them (general, not from the clips)
                                   "the", "a", "an", "my", "your", "our", "their", "this", "these", "those", "to",
                                   "with", "for", "of", "as", "than", "mean", "think"}
# a long sentence ending on one of these with a full stop is finished ("... everything people are talking about.")
STRANDED = {"about", "for", "with", "from", "to", "of", "in", "at", "by", "into", "on", "up", "out", "off", "over"}
# a statement starting with one of these is a request or a question, not a remark (no statement hold)
REQUEST_START = {
    "what", "what's", "whats", "where", "where's", "when", "who", "who's", "whom", "whose", "why", "how", "how's",
    "which", "is", "are", "am", "was", "were", "do", "does", "did", "can", "could", "would", "will", "should", "shall",
    "may", "might", "have", "has", "had", "turn", "switch", "set", "remind", "call", "text", "send", "message", "play",
    "pause", "stop", "resume", "skip", "tell", "give", "show", "read", "delete", "remove", "cancel", "add", "create",
    "make", "build", "open", "close", "find", "search", "look", "check", "start", "dim", "brighten", "lower", "raise",
    "increase", "decrease", "put", "schedule", "book", "order", "translate", "define", "explain", "describe", "list",
    "repeat", "say", "speak", "help", "wake", "count", "hang", "mute", "unmute", "please", "let's", "lets", "let",
    "outline", "detail", "summarize", "summarise", "write", "change", "update", "edit", "compare", "plan", "track",
    "get", "bring", "keep", "go", "dial", "answer", "shut", "power", "change", "hey", "pi", "wait", "hold", "actually",
    "no", "not", "never", "don't", "dont", "forget", "nevermind", "thanks", "thank", "goodbye", "bye", "good",
}
STATEMENT_MIN_WORDS = 2
# a command word anywhere ("If it rains, remind me ...", "Shorter, please", "Now turn it off") makes it a request
COMMAND_WORDS = {
    "remind", "turn", "switch", "set", "call", "text", "send", "play", "pause", "stop", "delete", "remove", "cancel",
    "dim", "brighten", "please", "timer", "alarm", "reminder", "reminders", "alarms", "weather", "thermostat",
    "light", "lights", "music", "message", "shorter", "longer", "louder", "quieter", "slower", "faster", "repeat",
    "website", "search", "look", "tell", "show", "explain", "describe", "translate", "help", "minutes", "minute",
    "hours", "hour", "morning", "afternoon", "evening", "tonight", "tomorrow", "pm", "am", "o'clock",
}
# short answers and follow-ups ("Yes. Delete it.", "Then tomorrow,", "And Paris") are not remarks
SHORT_START = {"yes", "no", "yeah", "yep", "nope", "sure", "okay", "ok", "right", "now", "then", "and", "also", "so"}


def is_statement(transcript: str, prev_reply: str = "") -> bool:
    """v7 (EOT_ADAPTIVE): a remark rather than a request, a question or an answer to her question."""
    t = transcript.strip()
    if not t or t.endswith("?") or prev_reply.rstrip().rstrip("\"')]").endswith("?"):
        return False
    w = _words(t)
    if len(w) < STATEMENT_MIN_WORDS or backchannel_only(t) or w[0] in REQUEST_START:
        return False
    if any(x in COMMAND_WORDS for x in w) or (w[0] in SHORT_START and len(w) <= 6):
        return False
    return True


def backchannel_only(transcript: str) -> bool:
    """v6: is the transcript nothing but backchannel words ("Okay.", "Mm-hmm, right.", "I see")?"""
    w = _words(transcript)
    return bool(w) and len(w) <= 4 and all(x in BACKCHANNEL for x in w)


_WARMER = _ASR_FIXES[-1][0]


def asr_fix(transcript: str) -> str:
    """v5: the transcript with known mis-hearings of short commands corrected."""
    if transcript and _WARMER.search(transcript) and len(transcript.split()) <= 10:  # v6: any length up to 10 words
        fixed = _WARMER.sub(_ASR_FIXES[-1][1], transcript, count=1)
        logger.info(f"ASR fix: [{transcript}] -> [{fixed}]")
        return fixed
    if not transcript or len(transcript.split()) > 6:
        return transcript
    for pattern, repl in _ASR_FIXES:
        if pattern is _WARMER:
            continue
        fixed = pattern.sub(repl, transcript, count=1)
        if fixed != transcript:
            logger.info(f"ASR fix: [{transcript}] -> [{fixed}]")
            return fixed
    return transcript


def _words(s: str) -> list[str]:
    return "".join(c.lower() if c.isalnum() or c == "'" else " " for c in s).split()


ADAPTIVE = False  # v7 (EOT_ADAPTIVE), set by MCPSTTService


def dangling(transcript: str) -> bool:
    """v3: does the transcript stop in the middle of a phrase, so more words are coming?"""
    if transcript.rstrip().endswith(("-", "—", "...")):
        return True
    w = _words(transcript)
    if not w:
        return False
    if ADAPTIVE:
        closed = transcript.rstrip().endswith((".", "!", "?"))
        if closed and len(w) >= 8 and w[-1] in STRANDED:
            return False  # v7: "... everything people are talking about." (v6 fdbench held it 1.5 s)
        if (w[-1] in TRAILING_CLOSED and len(w) >= 6) or (not closed and w[-1] in TRAILING_OPEN and len(w) >= 3):
            return True  # v7: "... and then", "... because", "like" (Candor pauses)
    if w[-1] in DANGLING_WORDS:
        return True
    return len(w) == 2 and w[0] in OBJECT_VERBS and w[1] in PARTICLES


class MCPSTTService(EagerEndOfTurnSTTServiceMixin, STTService):
    """Streaming STT over our ASR MCP server (which wraps Deepgram Flux)."""

    vad_hook_filter = False  # v8 (VAD_HOOK_FILTER), set by main.py

    def __init__(
        self,
        *,
        server_params: StdioServerParameters,
        sample_rate: int = 16000,
        push_interval_ms: int = 60,
        eot_threshold: float = 0.7,
        eager_eot_threshold: float = 0.5,
        eot_timeout_ms: int = 5000,
        enable_eager_end_of_turn: bool = True,
        turn_start: str = "asr",
        should_interrupt: bool = True,
        gate_enabled: bool = True,
        preroll_ms: int = 500,
        bot_hangover_secs: float = 1.5,
        gate_noise_tail_secs: float = 0.8,
        gate_cap_secs: float | None = None,
        smart_turn: bool = False,
        smart_turn_threshold: float = 0.5,
        vad_stop_secs: float = 0.2,
        smart_turn_threads: int = 1,
        dangling_hold_secs: float = 1.0,
        unsure_hold_p: float = 0.0,
        unsure_hold_secs: float = 1.2,
        unsure_hold_min_words: int = 4,
        llm_eot: bool = False,
        llm_eot_partial: bool = False,
        llm_wait_hold_secs: float = 0.8,
        voice_gate=None,
        floor_control: bool = False,
        floor_max_defer_secs: float = 0.6,
        duck=None,
        duck_conf: float = 0.6,
        duck_frames: int = 2,
        duck_min_volume: float = 0.45,
        duck_decide_secs: float = 0.5,
        duck_no_turn_secs: float = 0.8,
        duck_max_secs: float = 2.5,
        eot_adaptive: bool = False,
        statement_hold_secs: float = 0.6,
        **kwargs,
    ):
        super().__init__(
            sample_rate=sample_rate,
            enable_eager_end_of_turn=enable_eager_end_of_turn,
            settings=STTSettings(model="deepgram-flux-via-mcp", language=None),
            **kwargs,
        )
        self._server_params = server_params
        self._push_bytes = max(1, sample_rate * 2 * push_interval_ms // 1000)
        self._eot_threshold = eot_threshold
        self._eager_eot_threshold = eager_eot_threshold if enable_eager_end_of_turn else None
        self._eot_timeout_ms = eot_timeout_ms
        self._turn_start = turn_start
        self._should_interrupt = should_interrupt

        self._bridge = MCPBridge("asr", stdio=server_params)
        self._session_id: str | None = None
        self._buffer = bytearray()
        self._send_queue: asyncio.Queue[tuple[bytes, float] | None] = asyncio.Queue(maxsize=64)
        self._sender: asyncio.Task | None = None
        self._poller: asyncio.Task | None = None
        self._user_is_speaking = False
        self._stats = {
            "pushes": 0,
            "push_ms_total": 0.0,
            "events": 0,
            "eager": 0,
            "eager_cancelled": 0,
            "end_of_turn": 0,
            "dropped": 0,
            "audio_total_s": 0.0,
            "audio_sent_s": 0.0,
            "gate_opens": 0,
        }
        self._eager_pending = False
        # audio gate
        self._push_secs = self._push_bytes / (sample_rate * 2)
        self._gate_enabled = gate_enabled
        self._gate_open = not gate_enabled
        self._ring: deque[tuple[bytes, float]] = deque(maxlen=max(1, round(preroll_ms / 1000 / self._push_secs)))
        self._bot_hangover = bot_hangover_secs
        self._noise_tail = gate_noise_tail_secs
        self._gate_cap = gate_cap_secs if gate_cap_secs is not None else eot_timeout_ms / 1000 + 1.0
        self._vad_speaking = False
        self._t_vad_stopped = 0.0
        self._bot_speaking = False
        self._t_bot_stopped = float("-inf")
        self._flux_turn_open = False
        self._t_gate_opened = 0.0
        self._buf_t: float | None = None  # capture time of the first byte in self._buffer
        # (conn, stream offset s, capture wall time, duration s) per pushed chunk
        self._chunk_map: deque[tuple[int, float, float, float]] = deque(maxlen=4000)
        # v2: local end of turn (Smart Turn v3 on the Pi), see _smart_turn_check
        self._smart_turn = smart_turn
        self._st_rate = sample_rate
        self._st_threshold = smart_turn_threshold
        self._vad_stop_secs = vad_stop_secs
        self._st_model = None
        self._st_audio: deque[tuple[bytes, float]] = deque()  # (pcm, capture time), last ~10 s of mic audio
        self._st_turn_start: float | None = None  # first voice of the current user turn
        self._st_complete_at: float | None = None  # Smart Turn said "complete"; waiting for a transcript that covers it
        self._st_speech_end: float | None = None
        self._local_ended = False  # this Flux turn was already ended locally
        self._local_transcript = ""
        self._local_t = 0.0
        self._last_update: dict | None = None
        self._eager_event: dict | None = None
        self._stats.update(
            {"st_runs": 0, "st_complete": 0, "st_ms_total": 0.0, "st_ms_max": 0.0, "local_eot": 0,
             "local_eot_saved_ms_total": 0.0, "local_eot_corrected": 0, "local_eot_reopened": 0}
        )
        self._st_threads = max(1, smart_turn_threads)
        # v3: a turn that ends on a dangling word ("Turn on the") is held, and joined to the next one if they go on
        self._dangling_hold = dangling_hold_secs
        self._held: tuple[dict, str, float | None] | None = None  # (Flux event, full transcript, server time)
        self._held_deadline = 0.0
        self._held_task: asyncio.Task | None = None
        self._prefix = ""  # words of a held turn that the current Flux turn continues
        self._stats.update({"dangling_held": 0, "dangling_joined": 0, "dangling_released": 0, "dangling_no_local": 0})
        # v4: Flux's end of turn is held while Smart Turn says the sentence is clearly not finished (pause mid-sentence)
        self._unsure_p = unsure_hold_p if smart_turn else 0.0
        self._unsure_secs = unsure_hold_secs
        self._unsure_min_words = unsure_hold_min_words
        self._st_last: tuple[float, float] | None = None  # (VAD stop time it judged, p)
        self._st_task: asyncio.Task | None = None
        self._stats.update({"unsure_held": 0, "unsure_waited_st": 0})
        # v6: the fast LLM judges the end of turn (LLM_EOT), started on partial transcripts (LLM_EOT_PARTIAL)
        self._llm_eot = llm_eot
        self._llm_eot_partial = llm_eot_partial
        self._llm_wait_hold = llm_wait_hold_secs
        self._llm_wait_t: float | None = None  # VAD stop time of the silence the LLM answered <wait/> for
        self._eager_src = ""  # "flux" or "partial"
        self._eager_text = ""  # transcript of the early reply in flight
        self._last_commit = ""  # normalised transcript of the last committed turn
        self._far_cache: tuple[float, bool] = (0.0, False)
        # v6: near-voice gate and floor control (backchannels while she speaks)
        self._gate_v = voice_gate
        # v7 (DUCK_BARGE_IN): floor control with her audio paused while it decides (assistant/duck.py)
        self._duck = duck
        self._floor = floor_control or duck is not None
        self._floor_max_defer = duck_decide_secs if duck is not None else floor_max_defer_secs
        self._duck_conf = duck_conf
        self._duck_frames = max(1, duck_frames)
        self._duck_min_vol = duck_min_volume
        self._duck_no_turn = duck_no_turn_secs
        self._duck_max = duck_max_secs
        self._duck_run = 0
        self._duck_first = 0.0
        self._duck_t = 0.0
        self._duck_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # v7 (EOT_ADAPTIVE): statement hold, trailing words, cut-off "done" ignored on statements
        global ADAPTIVE
        ADAPTIVE = eot_adaptive
        self._adaptive = eot_adaptive
        self._statement_hold = statement_hold_secs
        self.prev_reply = lambda: ""  # her last reply's text (main.py sets it), so answers to her question are not held
        self._floor_pending = False
        self._floor_taken_t: float | None = None  # this turn interrupted her (floor taken) at this time
        self._floor_task: asyncio.Task | None = None
        self._turn_t0: float | None = None  # first audio of the current user turn, for the gate's level
        self._keep_audio = smart_turn or voice_gate is not None or floor_control or duck is not None
        self._stats.update({
            "llm_verdict_go": 0, "llm_verdict_wait": 0, "llm_verdict_stale": 0, "llm_local_eot": 0, "llm_wait_held": 0,
            "partial_eager": 0, "flux_eager_dup": 0, "partial_cancelled_vad": 0,
            "floor_deferred": 0, "floor_taken_words": 0, "floor_taken_time": 0, "floor_kept_backchannel": 0,
            "floor_kept_far": 0, "floor_resume_note": 0, "gate_far_turns": 0,
            "duck_paused": 0, "duck_skipped_far": 0, "duck_no_turn": 0, "duck_kept": 0, "duck_taken": 0,
            "duck_max": 0, "duck_onset_ms": [], "statement_held": 0, "statement_joined": 0, "go_statement_ignored": 0,
        })

    # ----------------------------------------------------------------- metadata

    def can_generate_metrics(self) -> bool:
        return True

    @property
    def supports_ttfs(self) -> bool:
        return False  # the server (Flux) decides the turn boundary

    def service_metadata_frame(self) -> STTMetadataFrame:
        frame = super().service_metadata_frame()
        frame.user_turn_strategies = self._strategies()
        return frame

    def _strategies(self) -> UserTurnStrategies:
        """Turn strategies.

        'asr': Flux's StartOfTurn starts the turn (one cloud round trip later).
        'vad': local Silero VAD starts the turn, so barge-in is a local reflex
               (~150-250 ms). Flux's StartOfTurn and the transcript itself are
               fallbacks, so a quiet mic that never trips the VAD still gets a
               turn. Flux decides when the turn ends either way.
        """
        if self._turn_start == "asr":
            return self.recommended_user_turn_strategies(enable_interruptions=self._should_interrupt)
        stop = (
            EagerUserTurnStopStrategy(match_policy=self._eager_match_policy)
            if self._enable_eager_end_of_turn
            else ExternalUserTurnStopStrategy()
        )
        return UserTurnStrategies(
            start=[
                VADUserTurnStartStrategy(enable_interruptions=self._should_interrupt),
                ExternalUserTurnStartStrategy(enable_interruptions=self._should_interrupt),
                TranscriptionUserTurnStartStrategy(),
            ],
            stop=[stop],
        )

    # ---------------------------------------------------------------- lifecycle

    async def setup(self, setup: FrameProcessorSetup):
        await super().setup(setup)
        self._loop = asyncio.get_running_loop()
        if self._smart_turn and self._st_model is None and not getattr(self, "_st_loading", False):
            self._st_loading = True  # setup can run twice; load the model once
            try:
                self._st_model = await asyncio.to_thread(self._load_smart_turn)
            except Exception as e:  # noqa: BLE001
                logger.error(f"{self}: Smart Turn v3 failed to load ({e!r}); Flux alone ends turns")
                self._smart_turn = False
        await self._connect()

    def _load_smart_turn(self):
        from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3

        t0 = time.perf_counter()
        # v3: SMART_TURN_THREADS (default 1: the Pi also runs VAD, voice tone and the pipeline)
        model = LocalSmartTurnAnalyzerV3(cpu_count=self._st_threads)
        model.set_sample_rate(self._st_rate)
        import numpy as np

        model._predict_endpoint(np.zeros(16000, dtype=np.float32))  # warm-up run
        logger.info(
            f"{self}: Smart Turn v3 loaded in {(time.perf_counter() - t0) * 1000:.0f} ms"
            f" (threshold {self._st_threshold}, {self._st_threads} thread(s))"
        )
        return model

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._disconnect()

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._disconnect()

    async def cleanup(self):
        await super().cleanup()
        await self._disconnect()

    async def _connect(self):
        if self._session_id:
            return
        try:
            await self._bridge.start()
            info = await self._bridge.call_json(
                "asr_start",
                {
                    "sample_rate": self.sample_rate or 16000,
                    "eot_threshold": self._eot_threshold,
                    "eager_eot_threshold": self._eager_eot_threshold,
                    "eot_timeout_ms": self._eot_timeout_ms,
                },
            )
            self._session_id = info["session"]
            self._sender = asyncio.create_task(self._sender_loop(), name="asr-sender")
            self._poller = asyncio.create_task(self._poll_loop(), name="asr-poller")
            logger.info(f"{self}: ASR session {self._session_id} ({info.get('model')})")
            await self._call_event_handler("on_connected")
        except Exception as e:  # noqa: BLE001
            logger.error(f"{self}: could not start ASR MCP server: {e!r}")
            await self._call_event_handler("on_connection_error", f"{e}")
            await self.push_error(error_msg=f"ASR MCP server failed to start: {e}", exception=e)

    async def _disconnect(self):
        for task in (self._sender, self._poller, self._held_task):
            if task:
                task.cancel()
        self._sender = self._poller = None
        if self._session_id and self._bridge.connected:
            try:
                await self._bridge.call("asr_stop", {"session": self._session_id}, timeout=5)
            except Exception:  # noqa: BLE001
                pass
        self._session_id = None
        await self._bridge.close()
        await self._call_event_handler("on_disconnected")

    # -------------------------------------------------------------------- audio

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        now = time.time()
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._vad_speaking = True
            self._t_vad_started = now
            METRICS.vad_started(now)
            if self._held is not None:
                # v3: they are talking again; give Flux time to open the turn that continues the held one
                self._held_deadline = max(self._held_deadline, now + 1.5)
            if self._turn_t0 is None:
                self._turn_t0 = now - 0.4
            if self._eager_pending and self._eager_src == "partial" and not self._local_ended:
                # v6: they went on talking after the pause the partial transcript was sent for
                self._stats["partial_cancelled_vad"] += 1
                self._eager_pending = False
                self._eager_event = None
                logger.info(f"{self}: user resumed; early reply on the partial transcript discarded")
                METRICS.turn_resumed(now)
                await self._cancel_eager_end_of_turn()
            if self._llm_eot and not self._smart_turn:
                self._st_complete_at = None
                if self._local_ended and self._flux_turn_open and not self._bot_speaking:
                    await self._reopen_turn("the user kept talking before the reply started")
            if self._smart_turn:
                self._st_complete_at = None  # still talking: an earlier "complete" no longer holds
                if self._st_turn_start is None:
                    self._st_turn_start = now - 0.5  # the VAD fires a little after the first word
                if self._local_ended and self._flux_turn_open and not self._bot_speaking:
                    await self._reopen_turn("the user kept talking before the reply started")
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._vad_speaking = False
            self._t_vad_stopped = now
            METRICS.vad_stopped(now)
            if self._held is not None and not self._smart_turn:
                # v6 smoke: "Remind me to ... umm ... call Mom": without Smart Turn the hold ran out 0.3 s before
                # the rest came (s07). A filler during the hold means more is coming: count the wait from its end.
                self._held_deadline = max(self._held_deadline, now + 1.5)
            if self._smart_turn and self._st_model is not None and self._flux_turn_open and not self._local_ended:
                self._st_task = asyncio.create_task(self._smart_turn_check(now))
            await self._maybe_partial_eager()
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            self._t_bot_stopped = now
        await super().process_frame(frame, direction)

    def _update_gate(self, now: float) -> None:
        if not self._gate_enabled:
            return
        bot_window = self._bot_speaking or (now - self._t_bot_stopped < self._bot_hangover)
        active = self._vad_speaking or bot_window
        if not self._gate_open:
            if active:
                reason = "voice" if self._vad_speaking else "bot speaking"
                preroll = list(self._ring)
                self._ring.clear()
                self._gate_open = True
                self._t_gate_opened = now
                self._stats["gate_opens"] += 1
                preroll_ms = len(preroll) * self._push_secs * 1000
                logger.info(f"{self}: gate opened ({reason}), sending {preroll_ms:.0f} ms of pre-roll")
                METRICS.gate_opened(now, reason, preroll_ms)
                for item in preroll:
                    self._enqueue(item)
            return
        if active:
            return
        quiet = now - max(self._t_vad_stopped, self._t_gate_opened, self._t_bot_stopped + self._bot_hangover)
        if (not self._flux_turn_open and quiet >= self._noise_tail) or quiet >= self._gate_cap:
            self._gate_open = False
            why = "turn ended" if not self._flux_turn_open else "backstop, Flux never ended the turn"
            logger.info(f"{self}: gate closed after {now - self._t_gate_opened:.1f} s ({why})")

    def _enqueue(self, item: tuple[bytes, float]) -> None:
        self._stats["audio_sent_s"] += self._push_secs
        try:
            self._send_queue.put_nowait(item)
        except asyncio.QueueFull:
            # Never block the audio path; drop the oldest block instead.
            self._send_queue.get_nowait()
            self._send_queue.put_nowait(item)
            self._stats["dropped"] += 1
            METRICS.asr_dropped()

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        """Cut microphone audio into ~60 ms blocks; send them through the gate (or keep as pre-roll)."""
        if not self._session_id:
            yield None
            return
        now = time.time()
        if not self._buffer:
            # The frame arrives when its last sample was captured.
            self._buf_t = now - len(audio) / (self.sample_rate * 2)
        self._buffer.extend(audio)
        if self._keep_audio:
            self._st_audio.append((audio, now))
            while self._st_audio and now - self._st_audio[0][1] > 10.0:
                self._st_audio.popleft()
        self._update_gate(now)
        while len(self._buffer) >= self._push_bytes:
            chunk = bytes(self._buffer[: self._push_bytes])
            del self._buffer[: self._push_bytes]
            item = (chunk, self._buf_t or now)
            self._buf_t = (self._buf_t or now) + self._push_secs
            self._stats["audio_total_s"] += self._push_secs
            if self._gate_open:
                self._enqueue(item)
            else:
                self._ring.append(item)
        yield None

    async def _sender_loop(self):
        while True:
            item = await self._send_queue.get()
            if item is None:
                return
            chunk, captured = item
            t0 = time.time()
            result = None
            payload = {"session": self._session_id, "pcm_base64": base64.b64encode(chunk).decode("ascii")}
            for attempt in range(2):
                try:
                    result = await self._bridge.call_json("asr_push", payload, timeout=5)
                    break
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"{self}: asr_push failed (attempt {attempt + 1}/2): {e}")
                    self._stats["push_retries"] = self._stats.get("push_retries", 0) + 1
                    await asyncio.sleep(0.05)
            if result is None:
                continue
            ms = (time.time() - t0) * 1000
            self._stats["pushes"] += 1
            self._stats["push_ms_total"] += ms
            METRICS.asr_push(ms)
            if isinstance(result, dict) and "offset" in result:
                self._chunk_map.append((result.get("conn", 0), float(result["offset"]), captured, self._push_secs))

    def _wall(self, conn: int | None, stream_secs: float | None) -> float | None:
        """Map a time in the Flux stream (seconds of audio on a connection) to when it was captured."""
        if stream_secs is None:
            return None
        for c, off, captured, dur in reversed(self._chunk_map):
            if conn is not None and c != conn:
                continue
            if off <= stream_secs < off + dur + 1e-6:
                return captured + (stream_secs - off)
            if off + dur < stream_secs - 30:
                break
        return None

    # ------------------------------------------------------------------- events

    async def _poll_loop(self):
        while True:
            try:
                data = await self._bridge.call_json(
                    "asr_events", {"session": self._session_id, "wait_ms": 2000}, timeout=10
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"{self}: asr_events failed: {e}")
                await asyncio.sleep(0.5)
                continue
            for event in (data or {}).get("events", []):
                self._stats["events"] += 1
                try:
                    await self._handle_event(event)
                except Exception as e:  # noqa: BLE001
                    logger.error(f"{self}: error handling ASR event {event}: {e!r}")

    async def _handle_event(self, event: dict):
        kind = event.get("event")
        if event.get("transcript"):
            event["transcript"] = asr_fix(event["transcript"].strip())  # v5
        transcript = (event.get("transcript") or "").strip()
        now = time.time()
        t_server = event.get("t")
        if kind == "StartOfTurn":
            if self._held is not None:
                # v3: the held turn goes on ("Turn on the" ... "kitchen light"): join the two
                self._prefix = self._held[1]
                self._held = None
                if self._held_task:
                    self._held_task.cancel()
                    self._held_task = None
                self._stats["dangling_joined"] += 1
                if self._eager_pending:
                    # v7: a statement hold keeps the early reply running; the user went on, so it is no use now
                    self._stats["statement_joined"] += 1
                    self._eager_pending = False
                    self._eager_event = None
                    await self._cancel_eager_end_of_turn()
                logger.info(f"{self}: Flux start of turn continues the held turn [{self._prefix}]")
            logger.info(f"{self}: Flux start of turn")
            self._user_is_speaking = True
            self._flux_turn_open = True
            self._local_ended = False
            self._st_complete_at = None
            self._last_update = self._eager_event = None
            if self._st_turn_start is None:
                self._st_turn_start = now - 1.0
            if self._turn_t0 is None:
                self._turn_t0 = now - 1.0
            self._floor_taken_t = None
            self._llm_wait_t = None
            METRICS.start_of_turn(now, t_server)
            if self._floor and self._bot_speaking:
                # v6: she is talking; a backchannel or a far-away voice must not cut her off (see _floor_timer)
                self._floor_pending = True
                self._stats["floor_deferred"] += 1
                if self._duck is not None and self._duck.pause("Flux start of turn while she speaks"):
                    self._duck_started(time.time(), "flux")
                logger.info(f"{self}: Flux start of turn while she speaks; deciding whether it takes the floor")
                if self._floor_task:
                    self._floor_task.cancel()
                self._floor_task = asyncio.create_task(self._floor_timer(), name="floor-timer")
            else:
                await self.broadcast_frame(ProposedUserStartedSpeakingFrame)
            await self._call_event_handler("on_start_of_turn", transcript)
        elif kind == "Update":
            heard = self._wall(event.get("conn"), event.get("audio_window_end"))
            if heard is not None:
                METRICS.asr_update_lag((now - heard) * 1000)
            self._last_update = event
            if self._floor_pending and transcript and not backchannel_only(self._full(transcript)) and not self._far_now():
                await self._floor_take(f"words: {self._full(transcript)[:60]!r}")
            if transcript and not self._local_ended and not self._floor_pending:
                await self.push_frame(
                    InterimTranscriptionFrame(self._full(transcript), self._user_id, time_now_iso8601(), None, result=event)
                )
            if self._st_complete_at is not None:
                await self._try_local_end()
            await self._maybe_partial_eager()
        elif kind == "EagerEndOfTurn":
            if self._local_ended:
                logger.debug(f"{self}: Flux eager end of turn after the local end of turn; ignored")
                return
            transcript = self._full(transcript)
            if self._floor_pending:
                if backchannel_only(transcript) or self._far_now():
                    logger.info(f"{self}: Flux eager end of turn on a backchannel or far voice while she speaks; ignored")
                    if self._duck is not None and self._duck.paused:
                        self._stats["duck_kept"] += 1
                        self._duck.resume(f"backchannel or far voice: {transcript[:40]!r}")
                    return
                await self._floor_take("eager end of turn")
            if dangling(transcript):
                logger.info(f"{self}: Flux eager end of turn on a dangling word; no early reply: [{transcript}]")
                return
            if self._eager_pending and self._eager_src == "partial":
                if norm(transcript) == norm(self._eager_text):
                    # v6: the partial transcript already started this reply; Flux agrees, nothing to redo
                    self._stats["flux_eager_dup"] += 1
                    logger.info(f"{self}: Flux eager end of turn matches the early reply already running: [{transcript}]")
                    if self._st_complete_at is not None:
                        await self._try_local_end()
                    return
                await self._cancel_eager_end_of_turn()
            self._stats["eager"] += 1
            self._eager_pending = True
            self._eager_event = event
            self._eager_src = "flux"
            self._eager_text = transcript
            logger.info(f"{self}: Flux eager end of turn (LLM starts early): [{transcript}]")
            METRICS.eager(now, t_server, transcript)
            self._gate_check(transcript)
            await self._push_eager_end_of_turn(transcript, user_id=self._user_id, result=event)
            if self._st_complete_at is not None:
                await self._try_local_end()
        elif kind == "TurnResumed":
            if self._local_ended and self._llm_eot and not self._smart_turn and not (
                self._vad_speaking or getattr(self, "_t_vad_started", 0.0) > (self._local_t or 0.0)
            ):
                # v6 Pi run (s04): Flux un-did its own eager end of turn on "Set an alarm" with no new speech, then
                # re-sent "Set an alarm." as a new turn, which cut off her answer. Without speech it is ignored.
                self._stats["resume_ignored_no_vad"] = self._stats.get("resume_ignored_no_vad", 0) + 1
                logger.info(f"{self}: Flux turn resumed but the VAD heard no speech; the turn stays ended")
                return
            elif self._local_ended:
                await self._reopen_turn("Flux heard the user resume")
                return
            if self._eager_pending:
                self._stats["eager_cancelled"] += 1
                logger.info(f"{self}: Flux turn resumed; early reply discarded")
                METRICS.turn_resumed(now)
            self._eager_pending = False
            self._eager_event = None
            await self._cancel_eager_end_of_turn()
        elif kind == "EndOfTurn" and not self._local_ended and await self._await_smart_turn():
            # v4: the Smart Turn check of this silence was still running; it ended the turn locally meanwhile
            await self._flux_end_after_local(event, transcript, time.time(), t_server)
        elif kind == "EndOfTurn" and self._local_ended:
            await self._flux_end_after_local(event, transcript, now, t_server)
        elif kind == "EndOfTurn":
            self._st_turn_start = None
            self._st_complete_at = None
            self._stats["end_of_turn"] += 1
            self._flux_turn_open = False
            transcript = self._full(transcript)
            self._prefix = ""
            if self._floor_pending:
                far = self._far_now()
                if not transcript or backchannel_only(transcript) or far:
                    # v6: "Mm-hmm" / "Okay" / the TV while she talks: she keeps the floor and keeps talking
                    self._floor_pending = False
                    if self._floor_task:
                        self._floor_task.cancel()
                        self._floor_task = None
                    self._user_is_speaking = False
                    self._turn_t0 = None
                    self._stats["floor_kept_far" if far else "floor_kept_backchannel"] += 1
                    if self._eager_pending:
                        self._eager_pending = False
                        await self._cancel_eager_end_of_turn()
                    logger.info(f"{self}: {'far voice' if far else 'backchannel'} while she speaks; she keeps talking: [{transcript}]")
                    if self._duck is not None and self._duck.paused:
                        self._stats["duck_kept"] += 1
                        self._duck.resume(f"{'far voice' if far else 'backchannel'}: {transcript[:40]!r}")
                    METRICS.turn_resumed(now)
                    return
                await self._floor_take("end of turn")
            if transcript and self._dangling_hold > 0 and dangling(transcript):
                await self._hold(event, transcript, now, t_server)
                return
            if (
                self._llm_eot and self._llm_wait_hold > 0 and self._llm_wait_t is not None
                and self._llm_wait_t == self._t_vad_stopped and not self._vad_speaking
                and not transcript.rstrip().endswith("?")
            ):
                # v6: the fast LLM judged this silence a pause mid-sentence; give the user a moment to go on
                self._llm_wait_t = None
                self._stats["llm_wait_held"] += 1
                await self._hold(event, transcript, now, t_server, secs=self._llm_wait_hold, why="the LLM's <wait/>")
                return
            if (
                self._adaptive and self._statement_hold > 0 and not self._vad_speaking
                and is_statement(transcript, self.prev_reply())
            ):
                # v7: a remark, not a request: people pause mid-thought (fdbench Candor). The early reply keeps running.
                self._stats["statement_held"] += 1
                await self._hold(event, transcript, now, t_server, secs=self._statement_hold, why="a statement",
                                 keep_eager=True)
                return
            p = self._unsure(transcript)
            if p is not None:
                self._stats["unsure_held"] += 1
                await self._hold(event, transcript, now, t_server, secs=self._unsure_secs, why=f"Smart Turn p={p:.2f}")
                return
            early = "early reply already running" if self._eager_pending else "no early reply"
            self._eager_pending = False
            logger.info(f"{self}: Flux end of turn ({early}): [{transcript}]")
            await self._commit_end_of_turn(event, transcript, now, t_server)
        elif kind == "Error":
            await self.push_error(error_msg=f"ASR server error: {event.get('message')}")
        elif kind in ("Connected", "Reconnected"):
            logger.info(f"{self}: ASR server {kind.lower()}")
        else:
            logger.debug(f"{self}: unhandled ASR event {kind}")

    def _full(self, transcript: str) -> str:
        """v3: the transcript with the words of a held turn it continues in front."""
        if not self._prefix:
            return transcript
        return f"{self._prefix.rstrip(' ,-')} {transcript}".strip() if transcript else self._prefix

    async def _commit_end_of_turn(self, event: dict, transcript: str, now: float, t_server: float | None) -> None:
        self._user_is_speaking = False
        self._flux_turn_open = False
        self._record_end_of_turn(event, transcript, now, t_server)
        self._clear_eager_end_of_turn()
        self._on_commit(transcript)
        if transcript:
            await self.push_frame(
                TranscriptionFrame(
                    transcript, self._user_id, time_now_iso8601(), None, result=event, finalized=True
                )
            )
        await self.broadcast_frame(ProposedUserStoppedSpeakingFrame)
        await self._call_event_handler("on_end_of_turn", transcript)

    # ------------------------------------------------------- v6: voice gate, floor control, LLM end of turn

    def _turn_audio(self) -> bytes:
        start = (self._turn_t0 if self._turn_t0 is not None else time.time() - 8.0) - 0.1
        return b"".join(pcm for pcm, t in self._st_audio if t >= start)

    def _gate_check(self, transcript: str):
        """Judge the current turn's voice; a far voice marks the transcript as not for her (LLM answers <pass/>)."""
        if self._gate_v is None or not transcript:
            return None
        v = self._gate_v.judge(self._turn_audio())
        self._far_cache = (time.time(), v.far)
        if v.reason:
            FLAGS.set(transcript, passed=v.reason)
        return v

    def _far_now(self) -> bool:
        """Is the voice heard since this turn started far away (TV, radio)? Cached for 0.2 s."""
        if self._gate_v is None or not self._gate_v.enabled:
            return False
        t, far = self._far_cache
        if time.time() - t < 0.2:
            return far
        pcm = self._turn_audio()
        from assistant.voice_gate import speech_level_db

        level = speech_level_db(pcm, self._st_rate or 16000)
        far = level is not None and level < self._gate_v.far_db()
        self._far_cache = (time.time(), far)
        return far

    def _on_commit(self, transcript: str) -> None:
        """A user turn is committed: gate verdict for the LLM, learn the user's voice, resume note after "Okay"."""
        if transcript:
            v = self._gate_check(transcript)
            if v is not None and not v.far and not v.speaker_far:
                self._gate_v.learn(self._turn_audio(), v)
            elif v is not None and v.far:
                self._stats["gate_far_turns"] += 1
            if self._floor_taken_t is not None and backchannel_only(transcript):
                # v6: "Okay" cut her off after all: she picks up where she stopped instead of answering "Okay"
                self._stats["floor_resume_note"] += 1
                FLAGS.set(transcript, note="the user only acknowledged you while you were speaking; continue what "
                          "you were saying from where you stopped, in a few words, without repeating it")
        self._last_commit = norm(transcript)
        self._turn_t0 = None
        self._floor_taken_t = None

    async def _floor_take(self, why: str) -> None:
        """v6: the overlapping speech is a real turn: interrupt her now."""
        if not self._floor_pending:
            return
        self._floor_pending = False
        if self._floor_task and self._floor_task is not asyncio.current_task():
            self._floor_task.cancel()
        self._floor_task = None
        self._floor_taken_t = time.time()
        self._stats["floor_taken_time" if why == "time" else "floor_taken_words"] += 1
        if self._duck is not None and self._duck.paused:
            self._stats["duck_taken"] += 1  # her audio is already paused; the interruption drops the rest
        logger.info(f"{self}: the user takes the floor ({why}); interrupting her")
        await self.broadcast_frame(ProposedUserStartedSpeakingFrame)

    async def _floor_timer(self) -> None:
        try:
            await asyncio.sleep(self._floor_max_defer)
            while self._floor_pending:
                if not self._far_now():
                    words = (self._last_update or {}).get("transcript") or ""
                    if not self._bot_speaking:
                        await self._floor_take("she finished")
                    elif self._vad_speaking or (words and not backchannel_only(self._full(words))):
                        await self._floor_take("time")
                    if not self._floor_pending:
                        return
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------- v7: duck-and-decide barge-in

    def vad_frame(self, t: float, conf: float, vol: float) -> None:
        """Silero's per-frame result (VAD worker thread). Matters while she is speaking, or (v9) while music plays."""
        from assistant.music import MUSIC

        if MUSIC.enabled and MUSIC.playing and not MUSIC.ducked and not self._bot_speaking and self._loop is not None:
            if conf >= self._duck_conf or getattr(self, "_music_run", 0):
                try:
                    self._loop.call_soon_threadsafe(self._music_frame, conf, vol)
                except RuntimeError:
                    pass
            return
        if self._duck is None or self._loop is None or not (self._bot_speaking or self._duck_run):
            return
        if self.vad_hook_filter and not self._duck_run and (conf < self._duck_conf or vol < self._duck_min_vol):
            return  # v8 (VAD_HOOK_FILTER): an unvoiced frame with no run to reset changes nothing; skip the loop wake-up
        try:
            self._loop.call_soon_threadsafe(self._duck_frame, t, conf, vol)
        except RuntimeError:
            pass

    def _duck_frame(self, t: float, conf: float, vol: float) -> None:
        if not self._bot_speaking or self._duck.paused:
            self._duck_run = 0
            return
        if conf < self._duck_conf or vol < self._duck_min_vol:
            self._duck_run = 0
            return
        self._duck_run += 1
        if self._duck_run == 1:
            self._duck_first = t - 0.032
        if self._duck_run < self._duck_frames:
            return
        self._duck_run = 0
        if self._duck_far():
            self._stats["duck_skipped_far"] += 1
            return
        now = time.time()
        onset_ms = (now - self._duck_first) * 1000
        if self._duck.pause(f"user speech onset, {onset_ms:.0f} ms after the first voiced frame"):
            self._stats["duck_onset_ms"].append(round(onset_ms))
            self._duck_started(now, "vad")

    def _music_frame(self, conf: float, vol: float) -> None:
        """v9: music is playing and she is quiet: a few voiced frames from a NEAR voice duck the music at once."""
        from assistant.music import MUSIC

        if conf < self._duck_conf:
            self._music_run = 0
            return
        self._music_run = getattr(self, "_music_run", 0) + 1
        if self._music_run < max(3, self._duck_frames):
            return
        self._music_run = 0
        if self._duck_far():
            self._stats["music_onset_far"] = self._stats.get("music_onset_far", 0) + 1
            return
        MUSIC.near_voice("near voice onset")

    def _duck_far(self) -> bool:
        """Is the onset a far-away voice (TV, radio)? Judged on the last ~0.3 s of mic audio by the voice gate."""
        if self._gate_v is None or not self._gate_v.enabled:
            return False
        from assistant.voice_gate import speech_level_db

        start = time.time() - 0.32
        pcm = b"".join(p for p, t in self._st_audio if t >= start)
        level = speech_level_db(pcm, self._st_rate or 16000)
        return level is not None and level < self._gate_v.far_db()

    def _duck_started(self, now: float, by: str) -> None:
        self._duck_t = now
        self._stats["duck_paused"] += 1
        if self._duck_task:
            self._duck_task.cancel()
        self._duck_task = asyncio.get_running_loop().create_task(self._duck_watch(now, by), name="duck-watch")

    async def _duck_watch(self, t0: float, by: str) -> None:
        """Her audio is paused: resume it if no real turn comes; take the floor if it never settles."""
        try:
            while self._duck.paused and self._duck_t == t0:
                now = time.time()
                quiet = 0.0 if self._vad_speaking else now - max(self._t_vad_stopped, t0)
                if not self._floor_pending and not self._flux_turn_open:
                    if not self._vad_speaking and (now - t0 >= self._duck_no_turn or (now - t0 >= 0.55 and quiet >= 0.3)):
                        # a cough, a noise, an echo, a short "mm-hm" Flux never made a turn of (smoke: 843 ms pause)
                        self._stats["duck_no_turn"] += 1
                        self._duck.resume("no turn from Flux")
                        return
                elif not self._floor_pending and self._flux_turn_open and self._user_is_speaking:
                    return  # the floor was taken; the interruption ends the pause
                elif self._floor_pending and quiet >= 0.25:
                    words = self._full((self._last_update or {}).get("transcript") or "")
                    if words and backchannel_only(words):
                        # "Okay." and the user stopped: resume now; Flux's end of turn settles the floor later
                        # (smoke: waiting for it paused her 1291 ms). More words would still take the floor.
                        self._stats["duck_kept"] += 1
                        self._duck.resume(f"backchannel: {words[:40]!r}")
                        return
                if now - t0 >= self._duck_max:
                    self._stats["duck_max"] += 1
                    words = self._full((self._last_update or {}).get("transcript") or "")
                    if self._floor_pending and (self._vad_speaking or (words and not backchannel_only(words))):
                        await self._floor_take("duck: still talking")
                    else:
                        if self._floor_pending:
                            self._floor_pending = False
                            if self._floor_task:
                                self._floor_task.cancel()
                                self._floor_task = None
                        self._duck.resume("nothing came of it")
                    return
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            pass

    async def _maybe_partial_eager(self) -> None:
        """v6 (LLM_EOT_PARTIAL): think while listening. At the first silence that Flux's latest partial transcript
        covers, start the early reply on it, without waiting for Flux's EagerEndOfTurn."""
        if not (self._llm_eot_partial and self._flux_turn_open) or self._vad_speaking or self._local_ended:
            return
        if self._floor_pending or self._held is not None or self._bot_speaking:
            return
        ev = self._last_update
        if not ev or (self._turn_t0 is not None and self._t_vad_stopped < self._turn_t0):
            return
        # v6 smoke: a partial ends in "," ("Call Mom,"), which made the LLM answer <wait/> to a finished command
        text = self._full((ev.get("transcript") or "").strip()).rstrip(" ,;:")
        if not text or dangling(text) or self._llm_wait_t == self._t_vad_stopped:
            return
        if self._eager_pending and norm(text) == norm(self._eager_text):
            return
        covered = self._wall(ev.get("conn"), ev.get("audio_window_end"))
        if covered is None or covered < self._t_vad_stopped - self._vad_stop_secs - 0.05:
            return  # the partial does not reach the end of the speech yet; the next Update decides
        if self._eager_pending:
            await self._cancel_eager_end_of_turn()
        now = time.time()
        self._stats["partial_eager"] += 1
        self._eager_pending = True
        self._eager_event = ev
        self._eager_src = "partial"
        self._eager_text = text
        logger.info(f"{self}: silence after a covering partial transcript; early reply starts: [{text}]")
        METRICS.eager(now, None, text)
        self._gate_check(text)
        await self._push_eager_end_of_turn(text, user_id=self._user_id, result=ev)

    async def llm_verdict(self, kind: str, text: str) -> None:
        """v6 (LLM_EOT): the fast LLM's first output for a speculative reply. "wait" = the sentence is cut off: the
        early reply is withdrawn and Flux's end of turn is held a moment; "go" = finished: end the turn here."""
        if not self._llm_eot or self._smart_turn:
            return None
        if not self._eager_pending or norm(text) != norm(self._eager_text) or self._local_ended or not self._flux_turn_open:
            self._stats["llm_verdict_stale"] += 1
            if kind == "wait" and norm(text) == self._last_commit:
                # v6 smoke: Flux confirmed the turn before the <wait/> arrived, so the early reply that is released is
                # <wait/> itself (silence). The user is done: run the LLM again, without the <wait/> option.
                self._stats["llm_wait_rerun"] = self._stats.get("llm_wait_rerun", 0) + 1
                logger.info(f"{self}: <wait/> came after the turn was confirmed; asking the LLM again: [{text}]")
                return "rerun"
            return None
        if kind == "wait":
            self._stats["llm_verdict_wait"] += 1
            self._llm_wait_t = self._t_vad_stopped
            self._eager_pending = False
            self._eager_event = None
            logger.info(f"{self}: the LLM says the sentence is not finished (<wait/>): [{text}]")
            await self._cancel_eager_end_of_turn()
            return None
        if kind == "tool":
            # v6 smoke (t6): Pipecat withdraws a speculative tool call itself; Flux's end of turn then runs it
            self._stats["llm_verdict_tool"] = self._stats.get("llm_verdict_tool", 0) + 1
            return None
        self._stats["llm_verdict_go"] += 1
        if self._vad_speaking or self._t_vad_stopped < (self._turn_t0 or 0):
            return None
        if self._adaptive and self._statement_hold > 0 and is_statement(text, self.prev_reply()):
            # v7: "done" on a remark ("Mhmm. But... yeah.") cut in on fdbench Candor pauses; Flux and the hold decide
            self._stats["go_statement_ignored"] += 1
            logger.info(f"{self}: cut-off check says done, but this is a statement; Flux and the hold decide: [{text}]")
            return None
        self._st_complete_at = time.time()
        self._st_speech_end = self._t_vad_stopped - self._vad_stop_secs
        await self._try_local_end()
        return None

    # ------------------------------------------------------- v3: dangling-word hold
    #
    # v2 split "Turn on the ... kitchen light" into two turns: Flux ended the first at the pause, and Smart Turn
    # (which only hears the audio) agreed. A transcript that ends on "the", "to", "um" or a bare "Turn on" is held
    # for DANGLING_HOLD_SECS: if Flux opens a new turn meanwhile, the two transcripts become one request;
    # if not, the held turn is answered as it is, that much later.

    async def _hold(
        self, event: dict, transcript: str, now: float, t_server: float | None, secs: float | None = None, why: str = "",
        keep_eager: bool = False,
    ) -> None:
        secs = self._dangling_hold if secs is None else secs
        self._held = (event, transcript, t_server)
        self._held_deadline = now + secs
        if not why:
            self._stats["dangling_held"] += 1
        if not (keep_eager and self._eager_pending and norm(self._eager_text) == norm(transcript)):
            self._eager_pending = False
            await self._cancel_eager_end_of_turn()  # an early reply to half a sentence is no use
        reason = why or "a dangling word"
        logger.info(f"{self}: Flux end of turn on {reason}; holding up to {secs:.1f} s: [{transcript}]")
        if self._held_task:
            self._held_task.cancel()
        self._held_task = asyncio.create_task(self._release_held())

    async def _release_held(self) -> None:
        cap = time.time() + 3.0  # v6: never hold longer than this, whatever the VAD says
        while self._held is not None:
            wait = self._held_deadline - time.time()
            if wait <= 0:
                if self._vad_speaking and time.time() < cap:
                    # v6 smoke: the user is still talking but Flux has not opened the next turn yet (it took 0.9 s
                    # in s07); releasing now answered "Remind me to" on its own. Keep holding while the VAD hears speech.
                    await asyncio.sleep(0.1)
                    continue
                break
            await asyncio.sleep(min(wait, 0.1))
        held, self._held, self._held_task = self._held, None, None
        if held is None:
            return  # joined to the next turn
        event, transcript, t_server = held
        self._stats["dangling_released"] += 1
        self._eager_pending = False  # v7: a statement hold may have kept the early reply; the commit releases it
        logger.info(f"{self}: nothing followed the held turn; answering it: [{transcript}]")
        await self._commit_end_of_turn(event, transcript, time.time(), t_server)

    # ------------------------------------------------------- v4: unsure-pause hold
    #
    # v3's faster transcript let Flux end turns at mid-sentence pauses (fdbench pause TOR 33 -> 67 %). Smart Turn
    # judged those pauses "not finished" but was only allowed to end turns early, never to keep them open. Now a Flux
    # end of turn is held for UNSURE_HOLD_SECS when Smart Turn's p for the same silence is below UNSURE_HOLD_P and the
    # transcript has at least UNSURE_HOLD_MIN_WORDS words (v3 data: every low-p turn that really was finished was a
    # one-to-three-word reply such as "Sam." or "Set an alarm."). Resuming joins the two halves, as the dangling hold does.

    async def _await_smart_turn(self) -> bool:
        """Wait (bounded) for a Smart Turn check of the current silence still running; True if it ended the turn."""
        task = self._st_task
        if self._unsure_p <= 0 or task is None or task.done():
            return False
        self._stats["unsure_waited_st"] += 1
        try:
            await asyncio.wait_for(asyncio.shield(task), 1.0)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            pass
        return self._local_ended

    def _unsure(self, transcript: str) -> float | None:
        """p of Smart Turn's verdict on this silence if it says clearly not finished, else None."""
        if self._unsure_p <= 0 or self._st_last is None or self._vad_speaking:
            return None
        t_stop, p = self._st_last
        if t_stop != self._t_vad_stopped or p >= self._unsure_p:
            return None
        if len(_words(transcript)) < self._unsure_min_words:
            return None
        if transcript.rstrip().endswith("?"):
            # v5: a finished question; in v4's fdbench run all 3 wasted holds were questions, none of the 3 joins
            self._stats["unsure_skipped_q"] = self._stats.get("unsure_skipped_q", 0) + 1
            logger.info(f"{self}: Smart Turn p={p:.2f} but the transcript is a question; no hold: [{transcript}]")
            return None
        return p

    # ------------------------------------------------------- v2: Smart Turn (local end of turn)
    #
    # Flux ends a turn about 1 s after the last word (v1 median 1.05 s). On the Pi, Silero reports silence after
    # VAD_STOP_SECS (0.2 s); Smart Turn v3 (8 MB ONNX, one core) then judges from the audio alone whether the
    # sentence is finished. If it is, and Flux has a transcript that covers the speech up to that silence (an eager
    # end of turn, or an Update whose audio window reaches it), the turn ends here, without waiting for Flux.
    # Flux keeps running: if the user goes on talking the turn is reopened (before she speaks: the VAD; after:
    # Flux's TurnResumed or a longer final transcript), so an early end costs a restart, never lost words.

    async def _smart_turn_check(self, t_stop: float) -> None:
        start = (self._st_turn_start or t_stop - 8.0) - 0.2
        chunks = [pcm for pcm, t in self._st_audio if t >= start]
        pcm = b"".join(chunks)
        rate = self._st_rate or 16000
        if len(pcm) < rate * 2 * 0.4:
            return  # under 0.4 s of audio: leave it to Flux
        import numpy as np

        audio = np.frombuffer(pcm[len(pcm) % 2:], dtype=np.int16).astype(np.float32) / 32768.0
        t0 = time.perf_counter()
        try:
            res = await asyncio.to_thread(self._st_model._predict_endpoint, audio)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{self}: Smart Turn failed: {e!r}")
            return
        ms = (time.perf_counter() - t0) * 1000
        self._stats["st_runs"] += 1
        self._stats["st_ms_total"] += ms
        self._stats["st_ms_max"] = max(self._stats["st_ms_max"], ms)
        p = float(res.get("probability", 0.0))
        self._st_last = (t_stop, p)
        METRICS.smart_turn(time.time(), p, ms, len(audio) / rate)
        if self._vad_speaking or self._local_ended or not self._flux_turn_open or self._t_vad_stopped != t_stop:
            logger.debug(f"{self}: Smart Turn p={p:.2f} ({ms:.0f} ms) came too late; the turn moved on")
            return
        if p < self._st_threshold:
            logger.info(f"{self}: Smart Turn: not finished (p={p:.2f}, {ms:.0f} ms); waiting for Flux")
            return
        self._stats["st_complete"] += 1
        logger.info(f"{self}: Smart Turn: finished (p={p:.2f}, {ms:.0f} ms)")
        self._st_complete_at = time.time()
        self._st_speech_end = t_stop - self._vad_stop_secs
        await self._try_local_end()

    async def _try_local_end(self) -> None:
        if self._st_complete_at is None or self._local_ended or self._vad_speaking or not self._flux_turn_open:
            return
        event = None
        candidates = [self._eager_event] if self._eager_pending and self._eager_event is not None else []
        candidates.append(self._last_update)
        for c in candidates:
            # the transcript must have heard the audio up to the silence Smart Turn judged
            if c is not None and (c.get("transcript") or "").strip():
                covered = self._wall(c.get("conn"), c.get("audio_window_end"))
                if covered is not None and self._st_speech_end is not None and covered >= self._st_speech_end:
                    event = c
                    break
        if event is None:
            return  # no transcript reaches the end of the speech yet; the next Update or eager end decides
        transcript = self._full((event.get("transcript") or "").strip())
        if self._dangling_hold > 0 and dangling(transcript):
            # v3: "Turn on" can sound finished; leave it to Flux and the dangling-word hold
            self._st_complete_at = None
            self._stats["dangling_no_local"] += 1
            logger.info(f"{self}: Smart Turn said finished, but the transcript ends on a dangling word: [{transcript}]")
            return
        now = time.time()
        waited = (now - self._st_complete_at) * 1000
        self._local_ended = True
        self._local_transcript = transcript
        self._local_t = now
        self._st_complete_at = None
        self._st_turn_start = None
        self._stats["local_eot"] += 1
        by = "LLM" if self._llm_eot and not self._smart_turn else "Smart Turn"
        if by == "LLM":
            self._stats["llm_local_eot"] += 1
        source = "eager transcript" if event is self._eager_event else "update transcript"
        logger.info(f"{self}: {by} end of turn ({source}, waited {waited:.0f} ms for it; Flux not done yet): [{transcript}]")
        self._eager_pending = False
        self._user_is_speaking = False
        self._record_end_of_turn(dict(event, trigger="llm_eot" if by == "LLM" else "smart_turn"), transcript, now, None)
        self._clear_eager_end_of_turn()
        self._on_commit(transcript)
        await self.push_frame(
            TranscriptionFrame(transcript, self._user_id, time_now_iso8601(), None, result=event, finalized=True)
        )
        await self.broadcast_frame(ProposedUserStoppedSpeakingFrame)
        await self._call_event_handler("on_end_of_turn", transcript)

    async def _reopen_turn(self, why: str) -> None:
        """The user was not done after all: interrupt the reply; Flux's own end of turn then counts."""
        self._local_ended = False
        self._stats["local_eot_reopened"] += 1
        logger.info(f"{self}: Smart Turn end of turn undone ({why})")
        self._user_is_speaking = True
        METRICS.start_of_turn(time.time(), None)
        await self.broadcast_frame(ProposedUserStartedSpeakingFrame)

    async def _flux_end_after_local(self, event: dict, transcript: str, now: float, t_server: float | None) -> None:
        """Flux's EndOfTurn for a turn already ended locally: normally only a timing record."""
        self._stats["end_of_turn"] += 1
        self._local_ended = False
        self._flux_turn_open = False
        self._user_is_speaking = False
        self._eager_pending = False
        self._clear_eager_end_of_turn()
        saved = (now - self._local_t) * 1000
        self._stats["local_eot_saved_ms_total"] += saved
        transcript = self._full(transcript)
        self._prefix = ""
        METRICS.flux_after_local(now, saved, transcript)

        def words(s: str) -> list[str]:
            return "".join(c.lower() if c.isalnum() else " " for c in s).split()

        if len(words(transcript)) > len(words(self._local_transcript)):
            # Flux heard more than the local end of turn used: answer the whole sentence instead.
            self._stats["local_eot_corrected"] += 1
            logger.info(f"{self}: Flux end of turn {saved:.0f} ms later has more words; redoing the turn: [{transcript}]")
            METRICS.start_of_turn(now, t_server)
            await self.broadcast_frame(ProposedUserStartedSpeakingFrame)
            self._record_end_of_turn(event, transcript, now, t_server)
            self._gate_check(transcript)
            await self.push_frame(
                TranscriptionFrame(transcript, self._user_id, time_now_iso8601(), None, result=event, finalized=True)
            )
            await self.broadcast_frame(ProposedUserStoppedSpeakingFrame)
            await self._call_event_handler("on_end_of_turn", transcript)
        else:
            logger.info(f"{self}: Flux end of turn {saved:.0f} ms after the Smart Turn end (same words): [{transcript}]")

    def _record_end_of_turn(self, event: dict, transcript: str, now: float, t_server: float | None) -> None:
        words = [w for w in (event.get("words") or []) if isinstance(w, dict)]
        conn = event.get("conn")
        start = end = None
        warning = None
        last_word = None
        if words:
            last_word = words[-1].get("word")
            start = self._wall(conn, words[0].get("start"))
            end = self._wall(conn, words[-1].get("end"))
            if end is None:
                warning = f"last word time {words[-1].get('end')} s is outside the recorded audio"
            elif end > now + 0.05 or now - end > 15:
                warning = f"last word maps to {now - end:+.2f} s before end of turn; word times may be per turn"
                start = end = None
        else:
            warning = "Flux sent no word timings"
        METRICS.end_of_turn(
            now,
            t_server,
            transcript,
            start,
            end,
            "flux words" if end is not None else None,
            last_word,
            event.get("trigger"),
            warning,
        )
        if warning:
            logger.warning(f"{self}: speech-end timing: {warning}")

    # ----------------------------------------------------------------- helpers

    @property
    def stats(self) -> dict:
        s = dict(self._stats)
        s["push_ms_avg"] = round(s["push_ms_total"] / s["pushes"], 2) if s["pushes"] else None
        s["audio_total_s"] = round(s["audio_total_s"], 1)
        s["audio_sent_s"] = round(s["audio_sent_s"], 1)
        s["sent_pct"] = round(100 * s["audio_sent_s"] / s["audio_total_s"], 1) if s["audio_total_s"] else None
        s["st_ms_avg"] = round(s["st_ms_total"] / s["st_runs"], 1) if s["st_runs"] else None
        s["local_eot_saved_ms_avg"] = round(s["local_eot_saved_ms_total"] / s["local_eot"], 1) if s["local_eot"] else None
        return s
