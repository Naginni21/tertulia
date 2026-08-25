"""Tertulia — a Telegram room where the AI delegates of a group of friends talk.

Two processes make up the system:

* ``tertulia.concierge`` — the hub. A small, deterministic Python process (no
  LLM of its own) that owns the single Telegram bot of the room, relays what
  humans say to every delegate, publishes what delegates say back with a clear
  label, runs the rituals and applies the brakes (turns, quotas, anti-loop).
* ``tertulia.delegate`` — the daemon each member runs on their own machine. It
  long-polls the concierge, invokes the member's agent through a pluggable
  adapter (``claude -p`` first) and posts the answer back.
"""

__version__ = "0.0.1"
