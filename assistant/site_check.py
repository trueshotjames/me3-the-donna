"""Local checks on what a build or edit task wrote, before she says it is done (v2).

The v1 route test scored the website 2.5/5: it was saved under another name and the requested title was missing,
yet she said it was ready. These checks run on the Pi in milliseconds, with no model call:

    the files Hermes says it wrote exist (relative paths are resolved against HERMES_WORKDIR, Hermes's TERMINAL_CWD)
    an HTML page parses and has <html>, <head> and <body>
    names the user asked for ("Sunrise Bakery") appear on the page
    local links and images (href/src) point at files that exist

A failure goes back to Hermes once, in the same session, as a repair task (assistant/tasks.py).
"""

from __future__ import annotations

import html.parser
import re
from pathlib import Path

# Two or more capitalised words in a row ("Sunrise Bakery"), or anything in quotes: the names a page must show.
_NAME = re.compile(r"\b([A-Z][\w'&-]*(?:\s+[A-Z][\w'&-]*)+)\b")
_QUOTED = re.compile(r"[\"“]([^\"”]{2,60})[\"”]")
_SKIP_NAMES = {"Create A", "Make A", "Build A"}


class _Page(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: set[str] = set()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.tags.add(tag)
        for k, v in attrs:
            if k in ("href", "src") and v:
                self.links.append(v)


def wanted_names(task_text: str) -> list[str]:
    names = [m.group(1) for m in _NAME.finditer(task_text)] + [m.group(1) for m in _QUOTED.finditer(task_text)]
    return [n for n in dict.fromkeys(names) if n not in _SKIP_NAMES]


def _resolve(path: str, workdir: Path | None) -> Path | None:
    p = Path(path)
    if p.is_absolute():
        return p if p.exists() else None
    for base in ([workdir] if workdir else []) + [Path.cwd()]:
        if (base / p).exists():
            return base / p
    return None


def check(files: list[str], task_text: str, workdir: Path | None) -> list[str]:
    """Problems found, in plain words for Hermes ([] = all good)."""
    problems: list[str] = []
    found: list[Path] = []
    for f in files or []:
        p = _resolve(str(f), workdir)
        if p is None:
            problems.append(f"{f} does not exist")
        else:
            found.append(p)
    pages = [p for p in found if p.suffix.lower() in (".html", ".htm")]
    if not files:
        problems.append("you listed no files")
    elif not pages:
        problems.append("there is no .html page among the files")
    text_all = ""
    for page in pages:
        txt = page.read_text(encoding="utf-8", errors="replace")
        text_all += txt.lower()
        parser = _Page()
        try:
            parser.feed(txt)
            parser.close()
        except Exception as e:  # noqa: BLE001
            problems.append(f"{page.name} does not parse as HTML ({e})")
            continue
        missing = {"html", "head", "body"} - parser.tags
        if missing:
            problems.append(f"{page.name} has no {', '.join(sorted(missing))} element")
        for link in parser.links:
            if re.match(r"^(https?:|mailto:|tel:|data:|#|//|javascript:)", link):
                continue
            target = (page.parent / link.split("#")[0].split("?")[0])
            if link.split("#")[0] and not target.exists():
                problems.append(f"{page.name} links to {link}, which does not exist")
    if pages:
        for name in wanted_names(task_text):
            if name.lower() not in text_all:
                problems.append(f'the page does not show "{name}"')
    return problems
