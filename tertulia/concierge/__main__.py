"""Command line entry point: ``tertulia-concierge`` / ``python -m tertulia.concierge``."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time

from .app import Concierge
from .config import ConfigError, load_config
from .rituals import load_rituals
from .server import ApiServer
from .store import Store
from .telegram import TelegramClient, TelegramError


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    rituals = load_rituals(cfg.room.rituals_dir)
    if "welcome" not in rituals:
        print(f"warning: no 'welcome' ritual found in {cfg.room.rituals_dir}", file=sys.stderr)
    store = Store(cfg.db_path)
    tg = TelegramClient(cfg.telegram.bot_token())
    try:
        me = tg.get_me()
        logging.getLogger("tertulia").info("telegram bot: @%s", me.get("username"))
    except TelegramError as exc:
        print(f"error: Telegram rejected the bot token: {exc}", file=sys.stderr)
        return 2

    app = Concierge(cfg, store, tg, rituals)
    app.start()
    server = ApiServer(app, cfg.server.host, cfg.server.port)
    server.start()
    poller = threading.Thread(target=app.poll_telegram_forever, name="telegram", daemon=True)
    poller.start()

    def _stop(*_: object) -> None:
        app.stop_event.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        while not app.stop_event.is_set():
            app.tick()
            app.stop_event.wait(0.5)
    finally:
        server.stop()
        store.close()
        logging.getLogger("tertulia").info("bye")
    return 0


def cmd_invite(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    store = Store(cfg.db_path)
    delegate, token = store.create_delegate(args.owner, args.owner_id)
    store.close()
    if args.quiet:
        print(token)
        return 0
    print(f"Delegate slot #{delegate.id} created for {args.owner}.")
    print("Give them this token (shown once, only the hash is stored):\n")
    print(f"    {token}\n")
    print("They paste it into their delegate's `token` file (see README).")
    return 0


def cmd_delegates(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    store = Store(cfg.db_path)
    rows = store.delegates()
    store.close()
    if not rows:
        print("no delegates yet — create one with `invite`")
        return 0
    now = time.time()
    for d in rows:
        seen = f"{int(now - d.last_seen_at)}s ago" if d.last_seen_at else "never"
        print(f"#{d.id:<3} owner={d.owner_name!r:<16} agent={d.agent_name!r:<16} joined={'yes' if d.joined_at else 'no':<3} last_seen={seen}")
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    store = Store(cfg.db_path)
    store.revoke(args.id)
    store.close()
    print(f"delegate #{args.id} revoked")
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    """Print chat and user IDs seen in recent updates, to fill the config.

    Deliberately does not load the config: this is how you discover the
    ``chat_id`` the config needs. Only ``TERTULIA_BOT_TOKEN`` is required."""
    token = os.environ.get("TERTULIA_BOT_TOKEN", "").strip()
    if not token:
        print("set TERTULIA_BOT_TOKEN first", file=sys.stderr)
        return 2
    tg = TelegramClient(token)
    me = tg.get_me()
    print(f"bot: @{me.get('username')} (id {me.get('id')})")
    print("Send a message in the group now; listening for 60s...")
    deadline = time.time() + 60
    offset = None
    seen: set[tuple[int, int]] = set()
    while time.time() < deadline:
        for upd in tg.get_updates(offset, timeout=10):
            offset = upd["update_id"] + 1
            msg = upd.get("message") or {}
            chat, user = msg.get("chat") or {}, msg.get("from") or {}
            key = (chat.get("id", 0), user.get("id", 0))
            if key in seen:
                continue
            seen.add(key)
            print(f"chat_id={chat.get('id')} ({chat.get('title') or chat.get('type')})  "
                  f"user_id={user.get('id')} ({user.get('first_name')})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tertulia-concierge", description="Tertulia concierge (room hub).")
    parser.add_argument("-c", "--config", default="concierge.yaml", help="path to concierge.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="run the concierge").set_defaults(func=cmd_run)
    p = sub.add_parser("invite", help="create a delegate slot and print its token")
    p.add_argument("--owner", required=True, help="the human's name as shown in the room")
    p.add_argument("--owner-id", type=int, default=None, help="the human's Telegram user ID (optional in v0)")
    p.add_argument("--quiet", action="store_true", help="print only the token")
    p.set_defaults(func=cmd_invite)
    sub.add_parser("delegates", help="list delegates").set_defaults(func=cmd_delegates)
    p = sub.add_parser("revoke", help="revoke a delegate's token")
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_revoke)
    sub.add_parser("whoami", help="discover chat/user IDs from recent updates").set_defaults(func=cmd_whoami)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
