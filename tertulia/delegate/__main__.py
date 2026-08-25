"""Command line entry point: ``tertulia-delegate`` / ``python -m tertulia.delegate``."""

from __future__ import annotations

import argparse
import logging
import signal
import sys

from .adapters import AdapterError, make_adapter
from .client import ConciergeClient, ConciergeError
from .config import ConfigError, load_config
from .daemon import DelegateDaemon


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _build(args: argparse.Namespace) -> DelegateDaemon:
    cfg = load_config(args.config)
    client = ConciergeClient(cfg.concierge_url, cfg.token())
    adapter = make_adapter(cfg.adapter, sandbox_dir=cfg.sandbox_dir)
    return DelegateDaemon(cfg, client, adapter)


def cmd_run(args: argparse.Namespace) -> int:
    daemon = _build(args)

    def _stop(*_: object) -> None:
        daemon.stop = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    daemon.run_forever()
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Verify config, token, concierge reachability and the adapter."""
    cfg = load_config(args.config)
    print(f"config:    {args.config}")
    print(f"agent:     {cfg.agent_name} (delegate of {cfg.owner_name})")
    print(f"profile:   {cfg.profile_path} ({'ok' if cfg.profile_path.exists() else 'MISSING'})")
    try:
        token = cfg.token()
        print("token:     ok")
    except ConfigError as exc:
        print(f"token:     {exc}")
        return 2
    client = ConciergeClient(cfg.concierge_url, token)
    try:
        # /v0/room authenticates without joining: hello (and the welcome
        # ritual it can trigger) only happens when the daemon starts.
        room = client.room()
        print(f"concierge: ok — room {room['name']!r}, language {room['language']}, "
              f"{len(room['delegates'])} delegate(s) joined")
    except ConciergeError as exc:
        print(f"concierge: FAILED ({exc})")
        return 2
    if args.skip_adapter:
        return 0
    try:
        adapter = make_adapter(cfg.adapter, sandbox_dir=cfg.sandbox_dir)
        result = adapter.complete(system_prompt="You are a connectivity check. Answer with the single word OK.", prompt="Ready?")
        print(f"adapter:   ok — {cfg.adapter.kind}/{cfg.adapter.model} answered {result.text!r}"
              + (f" ({result.cost_usd:.4f} USD)" if result.cost_usd is not None else ""))
    except AdapterError as exc:
        print(f"adapter:   FAILED ({exc})")
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tertulia-delegate", description="Tertulia delegate daemon.")
    parser.add_argument("-c", "--config", default="delegate.yaml", help="path to delegate.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="run the daemon").set_defaults(func=cmd_run)
    p = sub.add_parser("check", help="check config, token, concierge and adapter")
    p.add_argument("--skip-adapter", action="store_true", help="do not spend an LLM call")
    p.set_defaults(func=cmd_check)
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
