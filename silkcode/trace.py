"""A machine-readable record of a headless run.

An external harness - Terminal-Bench, SWE-bench, a CI job comparing two
models - needs three things from an agent it drives: a structured trace of
what happened, the final answer separated from the streaming noise, and an
exit code it can branch on without parsing anything. This module is the
first of those; the CLI's one-shot mode (`silkcode -p --trace ...`) wires up
all three.

The trace is JSONL - one event per line, flushed as written, so a run that
is killed still leaves the events that happened. Each line carries `ts`
(seconds since the run started), `kind`, and the event's data; the final
`done` line aggregates tokens and wall time, which is what a harness sums
when it compares harnesses rather than models.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

# Streamed model text arrives a few characters at a time; a trace with one
# line per token fragment is unreadable and enormous. Fragments are folded
# into one `text` event per uninterrupted stretch.


class TraceWriter:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("w", encoding="utf-8")
        self._started = time.monotonic()
        self._text: list[str] = []

    def _write(self, kind: str, data: dict) -> None:
        record = {"ts": round(time.monotonic() - self._started, 3), "kind": kind, **data}
        self._file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._file.flush()

    def _flush_text(self) -> None:
        if self._text:
            self._write("text", {"text": "".join(self._text)})
            self._text = []

    def event(self, kind: str, data) -> None:
        """An agent on_event callback: tee this into the agent's handler."""
        if kind == "text":
            self._text.append(str(data))
            return
        self._flush_text()
        if isinstance(data, dict):
            self._write(kind, dict(data))
        else:
            self._write(kind, {"data": data})

    def done(self, *, status: str, prompt_tokens: int, completion_tokens: int,
             detail: str = "") -> None:
        self._flush_text()
        self._write("done", {
            "status": status,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "seconds": round(time.monotonic() - self._started, 3),
            **({"detail": detail} if detail else {}),
        })
        self._file.close()
