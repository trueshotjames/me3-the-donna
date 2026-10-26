"""Settings-panel operations the UI can ask for over the WebSocket (assistant/EVENTS.md).

- ``summarize {last}``: median/p90 of the per-turn timings in data/turns.jsonl
  (same numbers as ``python -m benchmark.summarize``).
- ``vad_calibrate {secs}``: calibrate the voice detector without stopping the assistant.
  The microphone stays with the pipeline; this taps the running Silero VAD's frame history
  (assistant/vad.py) for ``secs`` of silence and ``secs`` of speech, and mutes the STT for
  the duration so the talking part does not become a conversation turn. Same formula as
  ``python -m benchmark.vad_calibrate``.
- ``vad_apply {confidence, min_volume}``: apply thresholds to the running VAD and save them
  to .env.
- ``config``: current settings for the panel.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any

from loguru import logger
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import STTMuteFrame

from assistant.config import ROOT, Config
from benchmark.summarize import load as load_turns
from benchmark.summarize import summarize
from benchmark.vad_calibrate import suggest


class SettingsOps:
    def __init__(self, cfg: Config, bus, vad, worker=None):
        self.cfg = cfg
        self.bus = bus
        self.vad = vad  # RecordingSileroVAD
        self.worker = worker
        self._calibrating: asyncio.Task | None = None

    def attach(self, worker) -> None:
        self.worker = worker

    # ------------------------------------------------------------------ config

    def config(self) -> dict[str, Any]:
        p = self.vad.params if self.vad else None
        return {
            "model": self.cfg.llm_model,
            "reasoning_effort": self.cfg.llm_reasoning_effort or None,
            "history_messages": self.cfg.llm_history_messages,
            "vad_confidence": p.confidence if p else self.cfg.vad_confidence,
            "vad_min_volume": p.min_volume if p else self.cfg.vad_min_volume,
            "vad_start_secs": self.cfg.vad_start_secs,
            "vad_stop_secs": self.cfg.vad_stop_secs,
            "turn_start": self.cfg.turn_start,
            "eager_end_of_turn": self.cfg.eager_end_of_turn,
            "tts_voice": self.cfg.tts_voice,
            "turns_log": str(self.cfg.turns_log),
            "calibrating": self._calibrating is not None and not self._calibrating.done(),
        }

    async def handle(self, msg: dict[str, Any]) -> bool:
        """Serve a UI command; returns False when the command is not one of ours."""
        kind = msg.get("type")
        if kind == "config":
            self.bus.emit("config", **self.config())
        elif kind == "summarize":
            await self.summarize(int(msg.get("last") or 0) or None)
        elif kind == "vad_calibrate":
            self.start_calibration(float(msg.get("secs") or 10.0))
        elif kind == "vad_calibrate_cancel":
            if self._calibrating and not self._calibrating.done():
                self._calibrating.cancel()
        elif kind == "vad_apply":
            await self.apply_vad(float(msg["confidence"]), float(msg["min_volume"]))
        else:
            return False
        return True

    # --------------------------------------------------------------- summarize

    async def summarize(self, last: int | None) -> None:
        path = Path(self.cfg.turns_log)
        if not path.exists():
            self.bus.emit("summary", error=f"no turns recorded yet ({path.name})", rows=[], turns=0)
            return
        recs = await asyncio.to_thread(load_turns, path, last, None)
        if not recs:
            self.bus.emit("summary", error="no turns recorded yet", rows=[], turns=0)
            return
        self.bus.emit("summary", last=last, **summarize(recs))

    # --------------------------------------------------------------- calibrate

    def start_calibration(self, secs: float) -> None:
        if self._calibrating and not self._calibrating.done():
            self.bus.emit("calibrate", phase="error", message="calibration already running")
            return
        if self.vad is None or not hasattr(self.vad, "frames_between"):
            self.bus.emit("calibrate", phase="error", message="the running VAD keeps no frame history")
            return
        secs = max(3.0, min(secs, 30.0))
        self._calibrating = asyncio.create_task(self._calibrate(secs), name="vad-calibrate")

    async def _mute(self, mute: bool) -> None:
        if self.worker is not None:
            await self.worker.queue_frame(STTMuteFrame(mute=mute))

    async def _calibrate(self, secs: float) -> None:
        emit = self.bus.emit
        try:
            await self._mute(True)
            emit("muted", muted=True)
            emit("calibrate", phase="quiet", secs=secs, message=f"Stay quiet for {secs:.0f} seconds")
            t0 = time.time()
            await asyncio.sleep(secs)
            t1 = time.time()
            quiet = [(c, v) for _, c, v in self.vad.frames_between(t0, t1)]
            emit("calibrate", phase="talk", secs=secs, message=f"Now talk normally for {secs:.0f} seconds")
            t2 = time.time()
            await asyncio.sleep(secs)
            t3 = time.time()
            talk = [(c, v) for _, c, v in self.vad.frames_between(t2, t3)]
            result = suggest(quiet, talk)
            result["current"] = {"confidence": self.vad.params.confidence, "min_volume": self.vad.params.min_volume}
            logger.info(
                f"VAD calibration: suggested confidence {result['confidence']} / min volume {result['min_volume']}"
                f" ({result['speech_pass_pct']}% of speech frames pass, {result['quiet_pass_pct']}% of quiet frames)"
            )
            emit("calibrate", phase="done", result=result)
        except asyncio.CancelledError:
            emit("calibrate", phase="cancelled", message="calibration cancelled")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"VAD calibration failed: {e!r}")
            emit("calibrate", phase="error", message=str(e))
        finally:
            await self._mute(False)
            emit("muted", muted=False)

    async def apply_vad(self, confidence: float, min_volume: float) -> None:
        confidence = max(0.05, min(confidence, 0.95))
        min_volume = max(0.0, min(min_volume, 0.95))
        if self.vad is not None:
            old = self.vad.params
            self.vad.set_params(
                VADParams(confidence=confidence, start_secs=old.start_secs, stop_secs=old.stop_secs, min_volume=min_volume)
            )
        self.cfg.vad_confidence, self.cfg.vad_min_volume = confidence, min_volume
        saved = _update_env({"VAD_CONFIDENCE": str(confidence), "VAD_MIN_VOLUME": str(min_volume)})
        logger.info(f"VAD thresholds applied: confidence {confidence}, min volume {min_volume} (.env {'updated' if saved else 'NOT updated'})")
        self.bus.emit("config", **self.config())
        self.bus.emit("info", message=f"VAD thresholds applied: confidence {confidence}, loudness {min_volume}" + ("" if saved else " (could not write .env)"))


def _update_env(values: dict[str, str], path: Path = ROOT / ".env") -> bool:
    """Set KEY=value lines in .env, keeping comments and order; appends keys that are missing."""
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        return False
    lines = text.splitlines()
    done = set()
    for i, line in enumerate(lines):
        m = re.match(r"^\s*([A-Z0-9_]+)\s*=(.*)$", line)
        if m and m.group(1) in values:
            rest = m.group(2)
            comment = ""
            if "#" in rest:
                comment = "  #" + rest.split("#", 1)[1]
            lines[i] = f"{m.group(1)}={values[m.group(1)]}{comment}"
            done.add(m.group(1))
    for k, v in values.items():
        if k not in done:
            lines.append(f"{k}={v}")
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True
    except OSError:
        return False
