"""Background tasks delegated to Hermes, exposed to the fast model as two tools.

    delegate(task)        start a Hermes run and return at once with a task id; the fast
                          model keeps the floor ("On it, I'll tell you when I have it").
    task_status(task_id)  running | done | failed, with progress or the result.

When a run finishes, the result goes to the Announcer (assistant/announcer.py) as a "tell"
announcement: a message starting with "Background task finished" is appended to the conversation
and the fast model is run again so it speaks the result in its own words. The announcer holds it
until the user's current query has been answered and the conversation is quiet, so a result
never lands in the middle of another reply (the first laptop test: a result queued during an
alarm tool call was answered by repeating the alarm confirmation). If the user cuts the result
off, it is repeated once after his new query.
A "still working on it" update is spoken at most once, never before NUDGE_MIN_SECS (60 s), and only right after
Hermes reports a new step (within NUDGE_FRESH_SECS): it names the request and that step ("Still on the Lakers score;
I'm searching the web for Lakers score tonight now."). No fresh step, no update; it is dropped if the result arrives.
v8: delegate takes a "briefing" (one spoken sentence about this request) that replaces the generic acknowledgement,
and a short "topic" used by the update.

Records go to turns.jsonl through METRICS.task_*; UI events are documented in EVENTS.md
(task_started, task_progress, task_done, task_failed).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.services.llm_service import FunctionCallParams

from assistant.announcer import (  # noqa: F401  (ActivityTracker re-exported for older imports)
    PRIORITY_LOW,
    PRIORITY_RESULT,
    ActivityTracker,
    Announcement,
    Announcer,
)
from assistant.services.hermes_client import HermesClient, HermesError, RunEvent, run_task
from assistant.site_check import check as check_site

FIXES = True  # v6: V6_FIXES (set by main.py): plain edit-task text, site check names from the edit goal

# v2: each kind of task has its own time limit, step budget, spoken acknowledgement and update interval
KINDS: dict[str, dict[str, Any]] = {
    # v3: lookups took a median 27 s in the v2 route test (date-and-source rule): budget half a minute, one search
    # and one page read; the "still working" nudge at 20 s instead of 15 s
    "lookup": {"max_secs": 45, "nudge_secs": 60, "steps": 2, "minutes": "half a", "ack": "Let me check."},
    "research": {"max_secs": 120, "nudge_secs": 60, "steps": 10, "minutes": "two",
                 "ack": "On it. That will take a minute or two; I'll tell you when I have it."},
    "edit": {"max_secs": 120, "nudge_secs": 60, "steps": 8, "minutes": "two", "ack": "On it, I'll tell you when it's changed."},
    "build": {"max_secs": 300, "nudge_secs": 90, "steps": 25, "minutes": "five",
              "ack": "On it. That will take a few minutes; I'll tell you when it's ready."},
}

NUDGE_MIN_SECS = 60.0  # v8 (King): the still-running update never comes earlier than this
NUDGE_FRESH_SECS = 15.0  # v8 (King): ... and only right after a new Hermes step, never from an old one
BRIEFING_MAX_CHARS = 220

DELEGATE_SCHEMA = FunctionSchema(
    name="delegate",
    description="Start a background task (web, research, website, files). The result arrives later on its own.",
    properties={
        "task": {
            "type": "string",
            "description": "The request in one clear sentence, with the names, places and dates it needs.",
        },
        "kind": {
            "type": "string",
            "enum": list(KINDS),
            "description": "lookup: one current fact; research: several facts, a comparison or a plan; edit: change earlier work; build: make a site or file.",
        },
        "context": {
            "type": "string",
            "description": "Earlier details the task needs (names, colours), or empty.",
        },
        "briefing": {
            "type": "string",
            "description": "One short spoken sentence telling the user what you are about to do for this request and roughly how long, e.g. \"I'll look up tonight's Lakers score; give me about half a minute.\"",
        },
        "topic": {
            "type": "string",
            "description": "The request in two to five words, e.g. \"tonight's Lakers score\".",
        },
        "follows_task": {
            "type": "string",
            "description": "Id of the earlier task this continues, or empty.",
        },
    },
    required=["task", "kind"],
)

CANCEL_SCHEMA = FunctionSchema(
    name="cancel_task",
    description='Stop a running background task ("never mind", "cancel that", "stop the website").',
    properties={"task_id": {"type": "string", "description": "The task id, or 'latest'."}},
    required=["task_id"],
)

CHANGE_SCHEMA = FunctionSchema(
    name="change_task",
    description='Change a background task\'s work ("make it blue", "add a contact page").',
    properties={
        "task_id": {"type": "string", "description": "The task id, or 'latest'."},
        "new_goal": {"type": "string", "description": "The change, in one clear sentence."},
    },
    required=["task_id", "new_goal"],
)

TASK_STATUS_SCHEMA = FunctionSchema(
    name="task_status",
    description="State, progress and result of a background task.",
    properties={"task_id": {"type": "string", "description": "The id returned by delegate, or 'latest'."}},
    required=["task_id"],
)

RESULT_MESSAGE = (
    "Background task finished (this message is from the background assistant, not from the user). "
    "Tell the user the result now, in your own words, in at most two spoken "
    "sentences, then offer more detail if there is more. The task was: {task}\nThe result:\n{result}"
)
FAILED_MESSAGE = "Background task failed ({reason}); this message is from the background assistant, not the user. Tell the user in one sentence that you could not finish: {task}"
NUDGE_MESSAGE = "Background task update (not from the user): still running after {secs} seconds. Say so in a few words, once."
REPEAT_MESSAGE = (
    "Background task reminder (not from the user): you were interrupted before finishing the result of the "
    "task \"{task}\" (see the earlier \"Background task finished\" message). Give that result now, briefly."
)

# v2: added to every task, so Hermes knows its budget and ends with a result that can be spoken as is
BRIEF = """{task}
{context}"""

# v2: the run's instructions: who reads the answer, the budget for this kind of task, and the result line
GUIDE = """You are working for a voice assistant; the user hears your result read aloud.
Now: {now}. Kind of task: {kind}.
Time budget: about {minutes} minute(s) and {steps} tool steps. Plan for it and aim to finish early. If you are running
out, stop and return your best partial result and what is left.
Before your first tool call, write one sentence saying what you will do.
For a current fact (a price, a rate, a score, news), give its date and source, taken from the search result when it
shows them. For a lookup: one web search and at most one page read, then answer. A website's main page is index.html.
End your reply with one line of JSON and nothing after it, with "spoken" being at most two short plain sentences to be
read aloud (no lists, markdown, links or code):
{{"status": "done", "spoken": "...", "details": "...", "files": ["..."], "left_to_do": ""}}
status is done, partial or failed; files lists the paths you wrote (empty if none)."""

SPOKEN_SUFFIX = {"partial": " I couldn't finish all of it.", "failed": ""}

# v2: one repair run in the same Hermes session when the local check finds problems with a build or edit
REPAIR = """A check of the files you wrote found these problems:
{problems}
Fix them now (keep everything else), then end your reply again with the same one-line JSON result."""

HERMES_INSTRUCTIONS = (
    "You are answering through a voice assistant. Reply with the answer only, written to be read aloud: "
    "two or three plain sentences first, then at most a few more if the task needs them. No lists, no markdown, "
    "no links, no code. Numbers and times in words where natural."
)



@dataclass
class Task:
    id: str
    text: str
    kind: str = "research"
    session_id: str = ""
    parent: str | None = None  # the task this one changes or continues
    brief: str = ""  # v2: what Hermes is sent as the task (request plus context)
    guide: str = ""  # v2: the run's instructions (budget, result format)
    context: str = ""
    check: list[str] | None = None  # v2: problems the local site check found (after the repair run, if any)
    repaired: bool = False
    t_start: float = field(default_factory=time.time)
    t_end: float | None = None
    state: str = "running"  # running | done | failed | cancelled
    run_id: str | None = None
    progress: str = ""
    tools_used: list[str] = field(default_factory=list)
    result: str | None = None
    error: str | None = None
    t_delivered: float | None = None
    nudged: bool = False
    topic: str = ""  # v8: two to five words naming the request, for the still-running update
    t_progress: float | None = None  # v8: when Hermes last reported a step
    nudge_text: str = ""  # v8: the update that was spoken, if any
    structured: dict | None = None  # v2: the JSON result line, when Hermes wrote one
    check_text: str = ""  # v6: for an edit, the text whose names the site check looks for (the new goal only)

    def status(self) -> dict[str, Any]:
        d: dict[str, Any] = {"task_id": self.id, "state": self.state, "task": self.text, "kind": self.kind}
        if self.state == "running":
            d["progress"] = self.progress or "working"
            d["running_secs"] = round(time.time() - self.t_start)
        elif self.state == "done":
            d["result"] = self.result
            if self.structured:
                d["details"] = self.structured.get("details")
                d["files"] = self.structured.get("files")
                d["left_to_do"] = self.structured.get("left_to_do")
        elif self.state == "failed":
            d["error"] = self.error
        return d

    def record(self) -> dict[str, Any]:
        return {
            "task_id": self.id,
            "task": self.text,
            "kind": self.kind,
            "parent": self.parent,
            "structured": bool(self.structured),
            "check": self.check,
            "repaired": self.repaired,
            "state": self.state,
            "run_id": self.run_id,
            "tools_used": self.tools_used,
            "t_start": self.t_start,
            "t_end": self.t_end,
            "task_ms": round((self.t_end - self.t_start) * 1000, 1) if self.t_end else None,
            "deliver_ms": round((self.t_delivered - self.t_end) * 1000, 1) if (self.t_delivered and self.t_end) else None,
            "error": self.error,
        }


class TaskManager:
    def __init__(
        self,
        client: HermesClient,
        *,
        session_id: str | None = None,
        nudge_secs: float = 30.0,
        max_secs: float = 300.0,
        on_event=None,  # callable(kind: str, **fields) -> None; UI bus
        on_record=None,  # callable(record: dict) -> None; turns.jsonl
        workdir=None,  # v2: Hermes's working folder (TERMINAL_CWD), for the site check
    ):
        from pathlib import Path

        self._workdir = Path(workdir) if workdir else None
        self._client = client
        self._session_id = session_id or f"me3-{uuid.uuid4().hex[:12]}"
        self._nudge_secs = nudge_secs
        self._max_secs = max_secs
        self._emit = on_event or (lambda kind, **f: None)
        self._record = on_record or (lambda rec: None)
        self._announcer: Announcer | None = None  # speaks results at the right moment
        self._tasks: dict[str, Task] = {}
        self._runs: dict[str, asyncio.Task] = {}
        self._ids = itertools.count(1)

    @property
    def session_id(self) -> str:
        return self._session_id

    def attach(self, announcer: Announcer) -> None:
        self._announcer = announcer

    def register(self, llm) -> list[FunctionSchema]:
        """Register the two tools on the Pipecat LLM service; returns their schemas for the context."""
        llm.register_function("delegate", self._delegate_tool)
        llm.register_function("task_status", self._status_tool)
        llm.register_function("cancel_task", self._cancel_tool)
        llm.register_function("change_task", self._change_tool)
        return [DELEGATE_SCHEMA, TASK_STATUS_SCHEMA, CANCEL_SCHEMA, CHANGE_SCHEMA]

    @property
    def tasks(self) -> list[Task]:
        return list(self._tasks.values())

    def latest(self) -> Task | None:
        return next(reversed(self._tasks.values()), None)

    def note(self, limit: int = 3) -> str | None:
        """v3: the last few tasks in one line, for the per-request note. The v2 route test lost a website edit
        because history trimming dropped the turn that built it; this keeps id, kind, state and files in view."""
        recent = [t for t in self.tasks if t.state != "cancelled"][-limit:]
        if not recent:
            return None
        parts = []
        for t in recent:
            files = (t.structured or {}).get("files") or []
            files_s = f", files {', '.join(str(f) for f in files[:4])}" if files else ""
            text = t.text if len(t.text) <= 80 else t.text[:77] + "..."
            parts.append(f"{t.id} {t.kind} {t.state}: {text}{files_s}")
        return "tasks: " + " | ".join(parts)

    # ------------------------------------------------------------------ tools

    def _find(self, tid: str | None) -> Task | None:
        tid = (tid or "").strip()
        return self.latest() if tid in ("", "latest") else self._tasks.get(tid)

    async def _say_instead_of_llm(self, params: FunctionCallParams, result: dict, say: str) -> None:
        """v2: speak a fixed line for this tool result without a second LLM call (as fast_confirm.py does)."""
        from pipecat.frames.frames import (
            FunctionCallResultProperties,
            LLMFullResponseEndFrame,
            LLMFullResponseStartFrame,
            LLMTextFrame,
        )

        await params.result_callback(result, properties=FunctionCallResultProperties(run_llm=False))
        await params.llm.push_frame(LLMFullResponseStartFrame())
        await params.llm.push_frame(LLMTextFrame(say))
        await params.llm.push_frame(LLMFullResponseEndFrame())

    async def _delegate_tool(self, params: FunctionCallParams) -> None:
        args = params.arguments or {}
        text = str(args.get("task", "")).strip()
        if not text:
            await params.result_callback({"error": "task text is empty"})
            return
        kind = str(args.get("kind") or "research").strip().lower()
        kind = kind if kind in KINDS else "research"
        task = self.delegate(text, kind=kind, context=str(args.get("context") or "").strip(),
                             follows=str(args.get("follows_task") or "").strip() or None)
        task.topic = " ".join(str(args.get("topic") or "").split())[:60]
        ack = KINDS[kind]["ack"]
        if kind in ("build", "edit"):
            ack = f"Okay: {text.rstrip('.!?')}. " + ack.removeprefix("On it. ").removeprefix("On it, ")
            ack = ack[:1].upper() + ack[1:]
        briefing = " ".join(str(args.get("briefing") or "").split())
        if briefing and len(briefing) <= BRIEFING_MAX_CHARS:
            ack = briefing  # v8: a sentence about this request instead of the generic acknowledgement
        await self._say_instead_of_llm(params, {"task_id": task.id, "state": "running", "kind": kind, "spoken": ack}, ack)

    async def _status_tool(self, params: FunctionCallParams) -> None:
        tid = str((params.arguments or {}).get("task_id", "latest"))
        task = self._find(tid)
        if task is None:
            await params.result_callback({"error": f"no task {tid}"})
            return
        await params.result_callback(task.status())

    async def _cancel_tool(self, params: FunctionCallParams) -> None:
        task = self._find(str((params.arguments or {}).get("task_id", "latest")))
        if task is None or task.state != "running":
            await params.result_callback({"error": "no background task is running"})
            return
        await self.cancel(task.id)
        await self._say_instead_of_llm(params, {"task_id": task.id, "state": "cancelled"}, "Okay, I stopped it.")

    async def _change_tool(self, params: FunctionCallParams) -> None:
        args = params.arguments or {}
        old = self._find(str(args.get("task_id", "latest")))
        goal = str(args.get("new_goal", "")).strip()
        if old is None or not goal:
            await params.result_callback({"error": "no such task, or the change is empty"})
            return
        if old.state == "running":
            await self.cancel(old.id)
        kind = "edit" if old.kind in ("build", "edit") else old.kind
        if FIXES:
            # v6: the goal first, in plain words; the earlier work only as a pointer to the files to edit. v5's text
            # ("Change to the earlier task (...): goal") sent Hermes searching for 120 s (27 steps) in the route test.
            files = (old.structured or {}).get("files") or []
            where = f" in {', '.join(str(f) for f in files[:4])}" if files else ""
            text = (
                f"{goal.rstrip('.')}. This changes the work you did earlier{where} for: {old.text.rstrip('.')}. "
                "Edit that file in place (read it once, then patch it); do not search for other files or start over."
            )
        else:
            text = f"Change to the earlier task ({old.text}): {goal}"
        task = self.delegate(text, kind=kind, follows=old.id)
        task.check_text = goal
        await self._say_instead_of_llm(
            params, {"task_id": task.id, "state": "running", "kind": kind, "replaces": old.id}, "Okay, changing it."
        )

    # ------------------------------------------------------------------ runs

    def delegate(self, text: str, *, kind: str = "research", context: str = "", follows: str | None = None) -> Task:
        # v2: one Hermes session per task thread. A follow-up (editing the site it built) reuses its parent's
        # session so Hermes remembers the work; anything else starts clean instead of carrying every earlier task.
        parent = self._tasks.get(follows) if follows else None
        tid = f"t{next(self._ids)}"
        session = parent.session_id if parent is not None else f"{self._session_id}-{tid}"
        task = Task(id=tid, text=text, kind=kind, session_id=session, parent=parent.id if parent else None, context=context)
        task.brief = BRIEF.format(task=text, context=f"Context from the conversation: {context}" if context else "").strip()
        task.guide = GUIDE.format(
            now=time.strftime("%A, %B %d, %Y, %I:%M %p"), kind=kind, minutes=KINDS[kind]["minutes"], steps=KINDS[kind]["steps"]
        )
        self._tasks[task.id] = task
        self._runs[task.id] = asyncio.create_task(self._run(task), name=f"hermes-{task.id}")
        self._emit("task_started", id=task.id, task=text, task_kind=kind)
        logger.info(f"delegated to Hermes [{task.id}, {kind}, session {session}]: {text}")
        return task

    async def cancel(self, task_id: str | None = None) -> bool:
        task = self.latest() if task_id is None else self._tasks.get(task_id)
        if task is None or task.state != "running":
            return False
        run = self._runs.get(task.id)
        if run:
            run.cancel()
        if task.run_id:
            await self._client.stop_run(task.run_id)
        task.state, task.t_end = "cancelled", time.time()
        self._emit("task_failed", id=task.id, task=task.text, error="cancelled")
        self._record(task.record())
        return True

    async def close(self) -> None:
        for run in self._runs.values():
            run.cancel()
        await asyncio.gather(*self._runs.values(), return_exceptions=True)

    async def _run(self, task: Task) -> None:
        nudge = asyncio.create_task(self._nudge_later(task))
        try:
            _, output = await asyncio.wait_for(
                run_task(
                    self._client,
                    task.brief or task.text,
                    session_id=task.session_id or self._session_id,
                    instructions=task.guide or HERMES_INSTRUCTIONS,
                    on_event=lambda ev: self._on_event(task, ev),
                ),
                timeout=self._max_for(task),
            )
            prose, task.structured = split_result(output)
            if task.kind in ("build", "edit") and task.structured is not None:
                prose, output = await self._check_and_repair(task, prose, output)
            task.state, task.result, task.t_end = "done", prose or output, time.time()
            logger.info(f"Hermes [{task.id}] done in {task.t_end - task.t_start:.1f}s: {output[:120]!r}")
            self._emit("task_done", id=task.id, task=task.text, result=task.result)
            spoken = (task.structured or {}).get("spoken")
            if isinstance(spoken, str) and spoken.strip():
                # v2: Hermes's own two sentences go straight to the speaker (no second fast-model call);
                # they join the conversation history, so "tell me more" and follow-ups still work.
                from assistant.processors import speakable, unspeakable_removed

                status = str(task.structured.get("status") or "done").lower()
                say = speakable(unspeakable_removed(spoken)).strip() + SPOKEN_SUFFIX.get(status, "")
                self._deliver(task, say, mode="say")
            else:
                self._deliver(
                    task,
                    RESULT_MESSAGE.format(task=task.text, result=task.result),
                    retry_text=REPEAT_MESSAGE.format(task=task.text),
                )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            await self._fail(task, f"no answer after {int(self._max_for(task))} seconds")
        except HermesError as e:
            await self._fail(task, str(e))
        except Exception as e:  # noqa: BLE001
            logger.exception(f"Hermes [{task.id}] crashed")
            await self._fail(task, repr(e)[:120])
        finally:
            nudge.cancel()
            if self._announcer is None:
                self._record(task.record())  # otherwise recorded once the result has been spoken

    async def _check_and_repair(self, task: Task, prose: str, output: str) -> tuple[str, str]:
        """v2: check the files a build or edit wrote; on problems, one repair run in the same session."""
        names_from = f"{task.text} {task.context} {self._tasks[task.parent].text if task.parent in self._tasks else ''}"
        if FIXES:
            # v6: v5's route test checked for "Sunrise Bakery  Create" (two texts glued by a space read as one name),
            # and an edit that renames the site must not be checked for the old name
            if task.kind == "edit" and task.check_text:
                names_from = task.check_text
            else:
                names_from = " . ".join(t for t in (task.text, task.context) if t)
        problems = check_site(task.structured.get("files") or [], names_from, self._workdir)
        if problems and not task.repaired:
            task.repaired = True
            logger.info(f"Hermes [{task.id}] site check failed ({'; '.join(problems)}); one repair run")
            self._emit("task_progress", id=task.id, progress="fixing a few problems", tool="site_check")
            left = max(30.0, self._max_for(task) - (time.time() - task.t_start))
            try:
                _, output2 = await asyncio.wait_for(
                    run_task(
                        self._client,
                        REPAIR.format(problems="\n".join(f"- {p}" for p in problems)),
                        session_id=task.session_id,
                        instructions=task.guide or HERMES_INSTRUCTIONS,
                        on_event=lambda ev: self._on_event(task, ev),
                    ),
                    timeout=left,
                )
                prose2, structured2 = split_result(output2)
                if structured2 is not None:
                    prose, output, task.structured = prose2, output2, structured2
                    problems = check_site(structured2.get("files") or [], names_from, self._workdir)
            except (asyncio.TimeoutError, HermesError) as e:
                logger.warning(f"Hermes [{task.id}] repair run failed: {e!r}")
        task.check = problems
        if problems:
            logger.info(f"Hermes [{task.id}] site check still failing: {'; '.join(problems)}")
            task.structured["status"] = "partial"
            task.structured["left_to_do"] = "; ".join(problems)
        else:
            logger.info(f"Hermes [{task.id}] site check passed")
        return prose, output

    def _max_for(self, task: Task) -> float:
        return min(self._max_secs, KINDS.get(task.kind, KINDS["research"])["max_secs"])

    def _nudge_for(self, task: Task) -> float:
        return max(NUDGE_MIN_SECS, KINDS.get(task.kind, {}).get("nudge_secs", self._nudge_secs))

    async def _fail(self, task: Task, reason: str) -> None:
        task.state, task.error, task.t_end = "failed", reason, time.time()
        logger.warning(f"Hermes [{task.id}] failed: {reason}")
        self._emit("task_failed", id=task.id, task=task.text, error=reason)
        self._deliver(task, FAILED_MESSAGE.format(reason=reason, task=task.text))

    def _on_event(self, task: Task, ev: RunEvent) -> None:
        if ev.kind == "tool.started":
            name = str(ev.data.get("tool") or ev.data.get("name") or "tool")
            task.tools_used.append(name)
            task.progress = _progress_text(name, ev.data.get("preview"))
            task.t_progress = time.time()
            self._emit("task_progress", id=task.id, progress=task.progress, tool=name)
            self._maybe_nudge(task)
        elif ev.kind == "run.started" or (ev.kind == "run" and ev.data.get("run_id")):
            task.run_id = str(ev.data.get("run_id") or task.run_id)

    async def _nudge_later(self, task: Task) -> None:
        try:
            await asyncio.sleep(self._nudge_for(task))
        except asyncio.CancelledError:
            return
        self._maybe_nudge(task)  # a step reported just before the minimum; later steps call it themselves

    def _fresh(self, task: Task, within: float) -> bool:
        return task.t_progress is not None and time.time() - task.t_progress <= within

    def _maybe_nudge(self, task: Task) -> None:
        """v8: at most one update, not before the minimum, and only about a step Hermes has just reported."""
        if task.state != "running" or task.nudged or not task.progress:
            return
        if time.time() - task.t_start < self._nudge_for(task) or not self._fresh(task, NUDGE_FRESH_SECS):
            return
        task.nudged = True
        topic = task.topic or "it"
        task.nudge_text = f"Still on {topic}; I'm {task.progress} now."
        logger.info(f"Hermes [{task.id}] still-running update: {task.nudge_text}")
        self._deliver(
            task,
            task.nudge_text,
            mode="say",
            mark=False,
            priority=PRIORITY_LOW,
            ttl=NUDGE_FRESH_SECS,
            still_wanted=lambda: task.state == "running" and self._fresh(task, NUDGE_FRESH_SECS + 10),
        )

    def _deliver(self, task: Task, text: str, *, mark: bool = True, mode: str = "tell", **kw) -> None:
        if self._announcer is None:
            logger.warning(f"Hermes [{task.id}]: no announcer attached; result not spoken")
            return

        def done(ann: Announcement, outcome: str) -> None:
            if outcome.startswith("dropped"):
                logger.info(f"Hermes [{task.id}]: announcement {outcome}")
            if mark:
                if outcome == "spoken":
                    task.t_delivered = ann.t_spoken
                self._record(task.record())

        kw.setdefault("priority", PRIORITY_RESULT)
        speak = self._announcer.say if mode == "say" else self._announcer.tell
        speak(text, "hermes", on_done=done, meta={"task_id": task.id}, **kw)


def split_result(output: str) -> tuple[str, dict | None]:
    """v2: (prose, structured) from a Hermes reply that ends with the JSON result line asked for in BRIEF."""
    text = (output or "").rstrip()
    if not text.endswith("}"):
        return text, None
    start = text.rfind("{")
    while start != -1:
        try:
            data = json.loads(text[start:])
        except ValueError:
            start = text.rfind("{", 0, start)
            continue
        if isinstance(data, dict) and ("spoken" in data or "status" in data):
            spoken = str(data.get("spoken") or "").strip()
            if spoken in ("", "...") or (spoken.startswith("<") and spoken.endswith(">")) or "|" in str(data.get("status")):
                return text[:start].strip(), None  # the format example came back, not a result
            return text[:start].strip(), data
        return text, None
    return text, None


def _progress_text(tool: str, preview: Any = None) -> str:
    t = tool.lower()
    what = " ".join(str(preview or "").split())
    if "search" in t and what and "http" not in what and "/" not in what and len(what) <= 60:
        return f"searching the web for {what}"  # v8: the step in the request's own words
    if "search" in t or "web" in t or "fetch" in t or "browser" in t:
        return "searching the web"
    if "file" in t or "read" in t or "write" in t:
        return "working with files"
    if "mcp_me3" in t:
        return "using the reminders and alarms"
    if "terminal" in t or "shell" in t:
        return "running a command"
    return f"using {tool}"
