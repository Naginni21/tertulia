"""``state.json`` is written after every adapter call, not only after an inbox batch.

(27-aug-2026: an owner-note call ran outside any batch, and the file trailed
the log by one call and 0.25 USD until the next batch happened to land.)
"""

import json

from tertulia.delegate.adapters.base import Completion
from tertulia.delegate.config import AdapterConfig, BehaviourConfig, DelegateConfig
from tertulia.delegate.daemon import DelegateDaemon


class CannedAdapter:
    def complete(self, *, system_prompt, prompt, timeout=None, model=None):
        return Completion(text="x", cost_usd=0.25, raw={})


class RoomStubClient:
    def __init__(self):
        self.said = []

    def room(self):
        return {"name": "T", "language": "es", "delegates": [], "ritual": None,
                "limits": {"max_message_chars": 2000}, "you": {"spontaneous_remaining_24h": 3}}

    def transcript(self, limit=40, *, ritual_id=None):
        return []

    def say(self, text, *, turn_id=None, origin_owner=False):
        self.said.append(text)


def test_outbox_call_persists_cost_and_calls(tmp_path):
    cfg = DelegateConfig(
        concierge_url="http://127.0.0.1:1", agent_name="A", owner_name="O", personality="",
        profile_path=tmp_path / "profile.md", memory_dir=tmp_path / "memory",
        state_dir=tmp_path / "state", sandbox_dir=tmp_path / "sandbox",
        shared_dir=tmp_path / "shared", outbox_dir=tmp_path / "outbox",
        token_file=tmp_path / "token", owner_telegram_user_id=None,
        adapter=AdapterConfig(kind="scripted"), behaviour=BehaviourConfig(), base_dir=tmp_path,
    )
    (tmp_path / "outbox").mkdir()
    (tmp_path / "outbox" / "nota.md").write_text("di algo", encoding="utf-8")
    daemon = DelegateDaemon(cfg, client=RoomStubClient(), adapter=CannedAdapter())
    daemon._process_outbox()

    state = json.loads((tmp_path / "state" / "state.json").read_text(encoding="utf-8"))
    assert state["calls"] == 1
    assert state["total_cost_usd"] == 0.25
