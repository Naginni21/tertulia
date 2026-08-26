"""``notify_command``: every inbox batch reaches the observer as JSON on stdin."""

import json
import time

from tertulia.delegate.config import AdapterConfig, BehaviourConfig, DelegateConfig
from tertulia.delegate.daemon import DelegateDaemon


def test_notify_command_receives_batch(tmp_path):
    out = tmp_path / "seen.json"
    cfg = DelegateConfig(
        concierge_url="http://127.0.0.1:1", agent_name="A", owner_name="O", personality="",
        profile_path=tmp_path / "profile.md", memory_dir=tmp_path / "memory",
        state_dir=tmp_path / "state", sandbox_dir=tmp_path / "sandbox",
        shared_dir=tmp_path / "shared", outbox_dir=tmp_path / "outbox", token_file=tmp_path / "token", owner_telegram_user_id=None,
        adapter=AdapterConfig(kind="scripted"), behaviour=BehaviourConfig(),
        base_dir=tmp_path,
        notify_command=f"cat > {out}",
    )
    daemon = DelegateDaemon(cfg, client=None, adapter=None)
    # An already-expired turn: _process_batch handles it without touching the client.
    events = [{"kind": "turn", "turn_id": 1, "round_id": 1, "deadline": 0.0}]
    daemon._process_batch(events)

    deadline = time.time() + 5
    seen = None
    while time.time() < deadline and seen is None:
        try:
            seen = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            time.sleep(0.05)
    assert seen == events
