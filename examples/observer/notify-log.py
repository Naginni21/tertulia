#!/usr/bin/env python3
"""Minimal observer: append every inbox batch to a JSONL your main agent can read.

Wire it in ``delegate.yaml``::

    notify_command: python3 ../examples/observer/notify-log.py

The daemon pipes each batch of room events as JSON on stdin (fire-and-forget,
cwd = your delegate folder), so this writes ``room-log.jsonl`` next to your
delegate. From there, your main agent closes the loop however you like — tail
the file, or replace this script with one that pings your own agent directly.
The loop back INTO the room is the outbox: your agent writes a note in
``outbox/`` and your delegate acts on it.
"""
import json
import sys
import time
from pathlib import Path

LOG = Path("room-log.jsonl")


def main() -> None:
    try:
        events = json.load(sys.stdin)
    except ValueError:
        return
    if not events:
        return
    with LOG.open("a", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps({"received_at": time.time(), **event}, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
