"""Long-term memory for the fast model, kept off the voice path.

The conversation she answers from stays short (LLM_HISTORY_MESSAGES); this module is what
survives it. Three parts, all under data/memory/:

    note.md             what she knows about the household: facts, preferences, routines, open
                        threads. At most MEMORY_NOTE_MAX_CHARS. It is pasted into her system
                        prompt, so her reply time does not grow with how long she has been running.
    episodes/*.jsonl    the raw transcript of every conversation (one record per message), written
                        as it happens, so nothing is lost if the summary is poor or the Pi dies.
    episodes.jsonl      one line per closed episode: when, how many turns, a one-line summary.

An episode is a conversation. It closes on its own, never by her choice, when nothing has been
said for MEMORY_IDLE_SECS (the context is then reset to the system prompt: a fresh start, the
note carries what matters) or when it has grown past MEMORY_EPISODE_MESSAGES messages (the note
is refreshed; the context keeps its recent tail). Closing an episode runs a cheap model in the
background (Summarizer) that merges the transcript into the note; her next reply is not delayed,
and the new note reaches her prompt as soon as it is ready. If the summary call fails, the
episode stays marked unsummarized and is folded into the next one.

Older detail is reached on demand through the `recall` tool: a keyword search over the raw
transcripts, one tool call, only on the turns that need it ("what did I tell you last week
about the dentist?"). The persona is never rewritten: only the note changes.

Memory is per household. She cannot tell speakers apart, so the note describes the people as
they introduce themselves.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx
from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.services.llm_service import FunctionCallParams

RECALL_SCHEMA = FunctionSchema(
    name="recall",
    description=(
        "Search what was said in earlier conversations (previous days or hours), when the user refers to "
        "something that is not in your notes or in this conversation. Returns matching lines with their date."
    ),
    properties={
        "query": {"type": "string", "description": "A few keywords: names, places, things, not a full sentence."}
    },
    required=["query"],
)

_STOP = set(
    "the a an and or of to in on at for with is are was were be been do did does it its this that these those "
    "i you he she we they me my your his her our their what when where who how did say said tell told about "
    "have has had not no yes please can could would will just like".split()
)
_WORD = re.compile(r"[a-z0-9']+")


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if len(w) >= 3 and w not in _STOP}


def _when(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%a %d %b %Y, %I:%M %p").replace(" 0", " ").lstrip("0")


# ----------------------------------------------------------------------------- storage


class MemoryStore:
    def __init__(self, root: Path):
        self.root = root
        self.episodes_dir = root / "episodes"
        self.note_path = root / "note.md"
        self.index_path = root / "episodes.jsonl"
        self.episodes_dir.mkdir(parents=True, exist_ok=True)

    # note
    def note(self) -> str:
        try:
            return self.note_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""

    def save_note(self, text: str) -> None:
        tmp = self.note_path.with_suffix(".tmp")
        tmp.write_text(text.strip() + "\n", encoding="utf-8")
        tmp.replace(self.note_path)

    # episodes
    def new_episode(self) -> Path:
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        path = self.episodes_dir / f"{stamp}.jsonl"
        n = 1
        while path.exists():
            n += 1
            path = self.episodes_dir / f"{stamp}-{n}.jsonl"
        return path

    @staticmethod
    def append(path: Path, rec: dict[str, Any]) -> None:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    @staticmethod
    def read(path: Path) -> list[dict[str, Any]]:
        out = []
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except FileNotFoundError:
            pass
        return out

    # index
    def index(self) -> list[dict[str, Any]]:
        return self.read(self.index_path)

    def index_add(self, rec: dict[str, Any]) -> None:
        self.append(self.index_path, rec)

    def index_mark_summarized(self, files: list[str]) -> None:
        rows = self.index()
        for r in rows:
            if r.get("file") in files:
                r["summarized"] = True
        tmp = self.index_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        tmp.replace(self.index_path)

    def unsummarized(self) -> list[dict[str, Any]]:
        return [r for r in self.index() if not r.get("summarized")]

    # recall
    def episode_files(self) -> list[Path]:
        return sorted(self.episodes_dir.glob("*.jsonl"), reverse=True)

    def recall(self, query: str, *, limit: int = 6, max_files: int = 60) -> dict[str, Any]:
        q = _tokens(query)
        if not q:
            return {"matches": [], "episodes": []}
        hits: list[tuple[int, float, dict]] = []
        for path in self.episode_files()[:max_files]:
            recs = [r for r in self.read(path) if r.get("role") in ("user", "assistant") and r.get("content")]
            for i, r in enumerate(recs):
                score = len(q & _tokens(r["content"]))
                if score == 0:
                    continue
                follow = recs[i + 1]["content"] if i + 1 < len(recs) and recs[i + 1]["role"] != r["role"] else None
                hits.append((score, r["t"], {"when": _when(r["t"]), "who": r["role"], "text": r["content"][:300],
                                             **({"reply": follow[:300]} if follow else {})}))
        hits.sort(key=lambda h: (-h[0], -h[1]))
        episodes = []
        for row in self.index():
            if row.get("summary") and q & _tokens(row["summary"]):
                episodes.append({"when": _when(row["started"]), "summary": row["summary"]})
        return {"matches": [h[2] for h in hits[:limit]], "episodes": episodes[-4:]}


# -------------------------------------------------------------------------- summarizer

NOTE_INSTRUCTIONS = """You maintain the long-term memory note of {assistant}, a voice assistant in {user}'s home.
You get the current note and the transcript of the latest conversation. Rewrite the note so it holds
everything worth remembering in future conversations: facts about the people (names, preferences, routines,
places, relationships, health or work details they shared), plans and things they asked for, open threads
("wanted to know X, not answered yet"), and how they like to be spoken to.
Rules:
- Merge, do not append: update facts that changed, keep the rest, drop trivia and one-off requests that are
  over (an alarm already set, a question already answered, small talk).
- Never list alarms or reminders; the tools keep those.
- Plain text, short lines, grouped under a few headings. At most {max_chars} characters.
- Do not invent anything. Keep dates when they matter ("since Oct 2026").
Output exactly: the new note, then a line containing only ---, then one or two sentences that summarize this
conversation for an index (who said what, in the past tense)."""


class Summarizer:
    """One chat completion against an OpenAI-compatible endpoint (the same OpenRouter key by default)."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        *,
        reasoning_effort: str = "",
        extra_body: dict | None = None,
        timeout: float = 90.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.model = model
        self._reasoning = reasoning_effort
        self._extra = extra_body or {}
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=timeout,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def update_note(
        self, old_note: str, transcript: str, *, user_name: str, assistant_name: str, max_chars: int
    ) -> tuple[str, str]:
        """Returns (new note, one-line episode summary)."""
        body: dict[str, Any] = {
            "model": self.model,
            "temperature": 0.2,
            "max_tokens": max(600, max_chars // 2),
            "messages": [
                {"role": "system", "content": NOTE_INSTRUCTIONS.format(assistant=assistant_name, user=user_name, max_chars=max_chars)},
                {"role": "user", "content": f"CURRENT NOTE:\n{old_note or '(empty)'}\n\nTRANSCRIPT:\n{transcript}"},
            ],
            **self._extra,
        }
        if self._reasoning:
            body["reasoning_effort"] = self._reasoning
        r = await self._client.post("/chat/completions", json=body)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        text = (r.json()["choices"][0]["message"]["content"] or "").strip()
        note, _, summary = text.partition("\n---")
        note = note.strip()
        summary = " ".join(summary.strip().split())[:400]
        if len(note) > max_chars:
            cut = note[:max_chars]
            note = cut[: cut.rfind("\n")] if "\n" in cut else cut
        return note, summary


# ------------------------------------------------------------------------------ memory


@dataclass
class Episode:
    path: Path
    started: float = field(default_factory=time.time)
    last: float = field(default_factory=time.time)
    records: list[dict[str, Any]] = field(default_factory=list)
    seen: dict[tuple, int] = field(default_factory=dict)  # message fingerprint -> times recorded

    @property
    def turns(self) -> int:
        return sum(1 for r in self.records if r["role"] == "user")


class Memory:
    def __init__(
        self,
        store: MemoryStore,
        summarizer: Summarizer | None,
        *,
        user_name: str,
        assistant_name: str,
        idle_secs: float = 1800.0,
        episode_messages: int = 40,
        note_max_chars: int = 4000,
        on_note: Callable[[str], None] | None = None,  # new note ready: put it in the prompt
        on_event=None,  # UI bus
    ):
        self.store = store
        self._summarizer = summarizer
        self._user, self._assistant = user_name, assistant_name
        self._idle_secs = idle_secs
        self._episode_messages = episode_messages
        self._max_chars = note_max_chars
        self._on_note = on_note or (lambda note: None)
        self._emit = on_event or (lambda kind, **f: None)
        self._context = None
        self._activity = None
        self._episode = Episode(store.new_episode())
        self._runner: asyncio.Task | None = None
        self._closing: asyncio.Task | None = None
        self._summaries: set[asyncio.Task] = set()

    # ----------------------------------------------------------------- wiring

    def attach(self, context, activity=None) -> None:
        self._context = context
        self._activity = activity

    def register(self, llm) -> list[FunctionSchema]:
        llm.register_function("recall", self._recall_tool)
        return [RECALL_SCHEMA]

    @property
    def note(self) -> str:
        return self.store.note()

    @property
    def episode(self) -> Episode:
        return self._episode

    async def start(self) -> None:
        if self._runner is None:
            self._runner = asyncio.create_task(self._idle_loop(), name="memory-idle")

    async def stop(self, *, wait_secs: float = 20.0) -> None:
        if self._runner:
            self._runner.cancel()
            self._runner = None
        self.observe_context()
        if self._episode.records:
            self.close_episode("shutdown", reset_context=False)
        pending = {t for t in self._summaries if not t.done()}
        if pending:
            logger.info(f"memory: waiting up to {wait_secs:.0f} s for the note to update")
            await asyncio.wait(pending, timeout=wait_secs)
        if self._summarizer:
            await self._summarizer.close()

    # --------------------------------------------------------------- recording

    def observe(self, messages: list[Any]) -> None:
        """Record messages not seen before (called before every LLM call, before the history trim).

        Messages are matched by content, not identity: Pipecat rebuilds message dicts between calls
        (the first laptop run recorded every user line again on each call). A message is new when
        the context holds it more times than it has been recorded; an exact repeat that falls out of
        the trimmed history before it is said again is missed, which memory can afford.
        """
        ep = self._episode
        new = 0
        in_context: dict[tuple, int] = {}
        for m in messages:
            if not isinstance(m, dict) or m.get("role") == "system":
                continue
            fp = _fingerprint(m)
            in_context[fp] = in_context.get(fp, 0) + 1
            if in_context[fp] <= ep.seen.get(fp, 0):
                continue
            ep.seen[fp] = in_context[fp]
            rec = _record(m)
            if rec is None:
                continue
            ep.records.append(rec)
            ep.last = rec["t"]
            new += 1
            try:
                self.store.append(ep.path, rec)
            except OSError as e:
                logger.warning(f"memory: could not write {ep.path.name}: {e}")
        if new and len(ep.records) >= self._episode_messages:
            self.close_episode("long", reset_context=False)

    def observe_context(self) -> None:
        if self._context is not None:
            try:
                msgs = self._context.get_messages() if hasattr(self._context, "get_messages") else self._context.messages
                self.observe(list(msgs))
            except Exception as e:  # noqa: BLE001
                logger.debug(f"memory: could not read the context: {e!r}")

    # ------------------------------------------------------------------ episodes

    def close_episode(self, reason: str, *, reset_context: bool) -> None:
        """Ends the current episode now; the note is updated in the background."""
        ep = self._episode
        if not ep.records:
            return
        self._episode = Episode(self.store.new_episode())
        if reset_context:
            self._reset_context()
        row = {
            "file": ep.path.name,
            "started": ep.started,
            "ended": time.time(),
            "turns": ep.turns,
            "messages": len(ep.records),
            "reason": reason,
            "summary": "",
            "summarized": False,
        }
        self.store.index_add(row)
        logger.info(f"memory: episode {ep.path.name} closed ({reason}, {ep.turns} turns)")
        self._emit("memory", state="episode_closed", file=ep.path.name, reason=reason, turns=ep.turns)
        if self._summarizer is None:
            return
        task = asyncio.create_task(self._summarize(), name="memory-summarize")
        self._summaries.add(task)
        task.add_done_callback(self._summaries.discard)

    def _reset_context(self) -> None:
        ctx = self._context
        if ctx is None:
            return
        if self._activity is not None and self._activity.busy:
            logger.debug("memory: context not reset (conversation busy)")
            return
        try:
            msgs = list(ctx.get_messages() if hasattr(ctx, "get_messages") else ctx.messages)
            system = [m for m in msgs if isinstance(m, dict) and m.get("role") == "system"][:1]
            ctx.set_messages(system)
            logger.info("memory: conversation history reset (new episode)")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"memory: could not reset the context: {e!r}")

    async def _summarize(self) -> None:
        pending = self.store.unsummarized()
        if not pending:
            return
        files = [r["file"] for r in pending]
        transcript = "\n\n".join(
            f"[conversation of {_when(r['started'])}]\n" + _transcript(self.store.read(self.store.episodes_dir / r["file"]), self._assistant)
            for r in pending
        )
        if len(transcript) > 60000:
            transcript = transcript[-60000:]
        old = self.store.note()
        t0 = time.monotonic()
        try:
            note, summary = await self._summarizer.update_note(
                old, transcript, user_name=self._user, assistant_name=self._assistant, max_chars=self._max_chars
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"memory: note update failed ({e!r}); episode kept for the next try")
            self._emit("memory", state="update_failed", error=repr(e)[:120])
            return
        if note:
            self.store.save_note(note)
        rows = self.store.index()
        for r in rows:
            if r["file"] in files:
                r["summarized"] = True
                if r["file"] == files[-1]:
                    r["summary"] = summary
        tmp = self.store.index_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        tmp.replace(self.store.index_path)
        logger.info(f"memory: note updated ({len(note)} chars, {time.monotonic() - t0:.1f} s, {len(files)} episode(s))")
        self._emit("memory", state="note_updated", chars=len(note), episodes=len(files))
        try:
            self._on_note(note)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"memory: prompt update failed: {e!r}")

    async def _idle_loop(self) -> None:
        while True:
            await asyncio.sleep(15)
            try:
                ep = self._episode
                if ep.records and time.time() - ep.last >= self._idle_secs:
                    if self._activity is not None and self._activity.busy:
                        continue
                    self.observe_context()
                    self.close_episode("idle", reset_context=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"memory idle check failed: {e!r}")

    # ---------------------------------------------------------------------- tool

    async def _recall_tool(self, params: FunctionCallParams) -> None:
        query = str((params.arguments or {}).get("query", "")).strip()
        if not query:
            await params.result_callback({"error": "query is empty"})
            return
        self.observe_context()
        found = await asyncio.get_running_loop().run_in_executor(None, self.store.recall, query)
        if not found["matches"] and not found["episodes"]:
            found["note"] = "nothing recorded about that; say so and ask the user"
        await params.result_callback(found)


# -------------------------------------------------------------------------- helpers


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):  # multimodal parts
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict)).strip()
    return ""


def _fingerprint(m: dict[str, Any]) -> tuple:
    calls = tuple(
        (c.get("id", ""), c.get("function", {}).get("name", "")) for c in (m.get("tool_calls") or []) if isinstance(c, dict)
    )
    return (m.get("role"), _text(m.get("content")), m.get("tool_call_id", ""), calls)


def _record(m: dict[str, Any]) -> dict[str, Any] | None:
    role = m.get("role")
    now = time.time()
    if role == "user":
        text = _text(m.get("content"))
        if not text:
            return None
        if text.startswith("Background task"):
            return {"t": now, "role": "background", "content": text[:600]}
        return {"t": now, "role": "user", "content": text}
    if role == "assistant":
        text = _text(m.get("content"))
        calls = [c.get("function", {}).get("name", "") for c in (m.get("tool_calls") or []) if isinstance(c, dict)]
        if text.strip() == "<pass/>" or (not text and not calls):
            return None
        rec: dict[str, Any] = {"t": now, "role": "assistant", "content": text}
        if calls:
            rec["tool_calls"] = calls
        return rec
    if role == "tool":
        return {"t": now, "role": "tool", "name": m.get("name", ""), "content": _text(m.get("content"))[:300]}
    return None


def _transcript(records: list[dict[str, Any]], assistant_name: str) -> str:
    lines = []
    for r in records:
        role = r.get("role")
        if role == "user":
            lines.append(f"User: {r['content']}")
        elif role == "assistant":
            calls = r.get("tool_calls")
            if r.get("content"):
                lines.append(f"{assistant_name}: {r['content']}")
            elif calls:
                lines.append(f"[{assistant_name} called {', '.join(calls)}]")
        elif role == "tool":
            lines.append(f"[tool {r.get('name') or ''} -> {r['content'][:160]}]")
        elif role == "background":
            lines.append(f"[background assistant: {r['content'][:300]}]")
    return "\n".join(lines)
