"""The room map refreshes after N spontaneous messages, not only on ritual close."""

from tertulia.delegate.adapters.base import Completion
from tertulia.delegate.config import AdapterConfig, BehaviourConfig, DelegateConfig
from tertulia.delegate.daemon import DelegateDaemon


class CannedAdapter:
    def complete(self, *, system_prompt, prompt, timeout=None, model=None):
        return Completion(text="# Mapa\n- Chasqui trae dato el 2-sept.", cost_usd=None, raw={})


class QuietClient:
    def transcript(self, limit=40, *, ritual_id=None):
        return []


def test_room_map_refreshes_after_n_messages(tmp_path):
    cfg = DelegateConfig(
        concierge_url="http://127.0.0.1:1", agent_name="A", owner_name="O", personality="",
        profile_path=tmp_path / "profile.md", memory_dir=tmp_path / "memory",
        state_dir=tmp_path / "state", sandbox_dir=tmp_path / "sandbox",
        shared_dir=tmp_path / "shared", outbox_dir=tmp_path / "outbox",
        token_file=tmp_path / "token", owner_telegram_user_id=None,
        adapter=AdapterConfig(kind="scripted"),
        behaviour=BehaviourConfig(map_update_every_messages=2), base_dir=tmp_path,
    )
    daemon = DelegateDaemon(cfg, client=QuietClient(), adapter=CannedAdapter())
    # Old messages: not reactable, but they still count toward the map refresh.
    ev = {"kind": "room_message", "at": 0.0,
          "message": {"sender_kind": "delegate", "sender_name": "X", "text": "hola", "at": 0.0}}
    daemon._process_batch([ev])
    assert not daemon.room_map_path.exists()
    daemon._process_batch([ev])
    assert daemon.room_map_path.read_text(encoding="utf-8").startswith("# Mapa")
    assert daemon._state["msgs_since_map"] == 0
