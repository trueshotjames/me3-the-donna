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
A "still working on it" nudge is spoken once after NUDGE_SECS if the user has not asked, and is
dropped if the result arrives first.

Records go to turns.jsonl through METRICS.task_*; UI events are documented in EVENTS.md
(task_started, task_progress, task_done, task_failed).
"""

from __future__ import annotations

import asyncio
import itertools
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

DELEGATE_SCHEMA = FunctionSchema(
    name="delegate",
    description=(
        "Hand a task that needs research, several steps, current information from the web, or files "
        "to the background assistant. Returns at once with a task id; the result arrives later on its own. "
        "Never use it for the time, reminders, alarms or small talk."
    ),
    properties={
        "task": {
            "type": "string",
            "description": "The user's request restated in one clear sentence, with any names, places or dates it needs.",
        }
    },
    required=["task"],
)

TASK_STATUS_SCHEMA = FunctionSchema(
    name="task_status",
    description="Check a background task the user asked about. Returns its state and, when done, the result.",
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

HERMES_INSTRUCTIONS = (
    "You are answering through a voice assistant. Reply with the answer only, written to be read aloud: "
    "two or three plain sentences first, then at most a few more if the task needs them. No lists, no markdown, "
    "no links, no code. Numbers and times in words where natural."
)



@dataclass
class Task:
    id: str
    text: str
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

    def status(self) -> dict[str, Any]:
        d: dict[str, Any] = {"task_id": self.id, "state": self.state, "task": self.text}
        if self.state == "running":
            d["progress"] = self.progress or "working"
            d["running_secs"] = round(time.time() - self.t_start)
        elif self.state == "done":
            d["result"] = self.result
        elif self.state == "failed":
            d["error"] = self.error
        return d

    def record(self) -> dict[str, Any]:
        return {
            "task_id": self.id,
            "task": self.text,
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
    ):
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
        return [DELEGATE_SCHEMA, TASK_STATUS_SCHEMA]

    @property
    def tasks(self) -> list[Task]:
        return list(self._tasks.values())

    def latest(self) -> Task | None:
        return next(reversed(self._tasks.values()), None)

    # ------------------------------------------------------------------ tools

    async def _delegate_tool(self, params: FunctionCallParams) -> None:
        text = str((params.arguments or {}).get("task", "")).strip()
        if not text:
            await params.result_callback({"error": "task text is empty"})
            return
        task = self.delegate(text)
        await params.result_callback(
            {"task_id": task.id, "state": "running", "note": "Started. Acknowledge briefly; the result will arrive on its own."}
        )

    async def _status_tool(self, params: FunctionCallParams) -> None:
        tid = str((params.arguments or {}).get("task_id", "latest")).strip()
        task = self.latest() if tid in ("", "latest") else self._tasks.get(tid)
        if task is None:
            await params.result_callback({"error": f"no task {tid}"})
            return
        await params.result_callback(task.status())

    # ------------------------------------------------------------------ runs

    def delegate(self, text: str) -> Task:
        task = Task(id=f"t{next(self._ids)}", text=text)
        self._tasks[task.id] = task
        self._runs[task.id] = asyncio.create_task(self._run(task), name=f"hermes-{task.id}")
        self._emit("task_started", id=task.id, task=text)
        logger.info(f"delegated to Hermes [{task.id}]: {text}")
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
                    task.text,
                    session_id=self._session_id,
                    instructions=HERMES_INSTRUCTIONS,
                    on_event=lambda ev: self._on_event(task, ev),
                ),
                timeout=self._max_secs,
            )
            task.state, task.result, task.t_end = "done", output, time.time()
            logger.info(f"Hermes [{task.id}] done in {task.t_end - task.t_start:.1f}s: {output[:120]!r}")
            self._emit("task_done", id=task.id, task=task.text, result=output)
            self._deliver(
                task,
                RESULT_MESSAGE.format(task=task.text, result=output),
                retry_text=REPEAT_MESSAGE.format(task=task.text),
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            await self._fail(task, f"no answer after {int(self._max_secs)} seconds")
        except HermesError as e:
            await self._fail(task, str(e))
        except Exception as e:  # noqa: BLE001
            logger.exception(f"Hermes [{task.id}] crashed")
            await self._fail(task, repr(e)[:120])
        finally:
            nudge.cancel()
            if self._announcer is None:
                self._record(task.record())  # otherwise recorded once the result has been spoken

    async def _fail(self, task: Task, reason: str) -> None:
        task.state, task.error, task.t_end = "failed", reason, time.time()
        logger.warning(f"Hermes [{task.id}] failed: {reason}")
        self._emit("task_failed", id=task.id, task=task.text, error=reason)
        self._deliver(task, FAILED_MESSAGE.format(reason=reason, task=task.text))

    def _on_event(self, task: Task, ev: RunEvent) -> None:
        if ev.kind == "tool.started":
            name = str(ev.data.get("tool") or ev.data.get("name") or "tool")
            task.tools_used.append(name)
            task.progress = _progress_text(name)
            self._emit("task_progress", id=task.id, progress=task.progress, tool=name)
        elif ev.kind == "run.started" or (ev.kind == "run" and ev.data.get("run_id")):
            task.run_id = str(ev.data.get("run_id") or task.run_id)

    async def _nudge_later(self, task: Task) -> None:
        try:
            await asyncio.sleep(self._nudge_secs)
        except asyncio.CancelledError:
            return
        if task.state == "running" and not task.nudged:
            task.nudged = True
            self._deliver(
                task,
                NUDGE_MESSAGE.format(secs=int(self._nudge_secs)),
                mark=False,
                priority=PRIORITY_LOW,
                ttl=60.0,
                still_wanted=lambda: task.state == "running",
            )

    def _deliver(self, task: Task, text: str, *, mark: bool = True, **kw) -> None:
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
        self._announcer.tell(text, "hermes", on_done=done, meta={"task_id": task.id}, **kw)


def _progress_text(tool: str) -> str:
    t = tool.lower()
    if "search" in t or "web" in t or "fetch" in t or "browser" in t:
        return "searching the web"
    if "file" in t or "read" in t or "write" in t:
        return "working with files"
    if "mcp_me3" in t:
        return "using the reminders and alarms"
    if "terminal" in t or "shell" in t:
        return "running a command"
    return f"using {tool}"
