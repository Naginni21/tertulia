"""The briefing: the owner's note for a ritual is the delegate's material, then it is archived."""

from tertulia.delegate.adapters.base import Completion
from tertulia.delegate.config import AdapterConfig, BehaviourConfig, DelegateConfig
from tertulia.delegate.daemon import FOR_OWNER_FILE, DelegateDaemon


class CannedAdapter:
    def __init__(self):
        self.prompts = []

    def complete(self, *, system_prompt, prompt, timeout=None, model=None):
        self.prompts.append(prompt)
        return Completion(text="hola sala", cost_usd=None, raw={})


class TurnStubClient:
    def __init__(self):
        self.said = []

    def transcript(self, limit=40, *, ritual_id=None):
        return []

    def say(self, text, *, turn_id=None, origin_owner=False):
        self.said.append((text, turn_id))


def _daemon(tmp_path):
    cfg = DelegateConfig(
        concierge_url="http://127.0.0.1:1", agent_name="Faro", owner_name="Felipe", personality="",
        profile_path=tmp_path / "profile.md", memory_dir=tmp_path / "memory",
        state_dir=tmp_path / "state", sandbox_dir=tmp_path / "sandbox",
        shared_dir=tmp_path / "shared", outbox_dir=tmp_path / "outbox",
        token_file=tmp_path / "token", owner_telegram_user_id=None,
        adapter=AdapterConfig(kind="scripted"), behaviour=BehaviourConfig(), base_dir=tmp_path,
    )
    return DelegateDaemon(cfg, client=TurnStubClient(), adapter=CannedAdapter(), clock=lambda: 1_000.0)


TURN = {"turn_id": 1, "ritual_id": 5, "ritual": "weekly", "round_id": "semana", "instruction": "cuenta", "deadline": 1_300.0}


def test_turn_speaks_from_the_briefing_and_close_archives_it(tmp_path):
    (tmp_path / "briefing").mkdir()
    (tmp_path / "briefing" / "weekly.md").write_text("Esta semana Felipe arregló el daemon.", encoding="utf-8")
    daemon = _daemon(tmp_path)
    daemon._handle_turn(TURN, 1_000.0)

    prompt = daemon.adapter.prompts[0]
    assert "<briefing>" in prompt and "Esta semana Felipe arregló el daemon." in prompt and "Briefing from Felipe" in prompt
    assert daemon.client.said == [("hola sala", 1)]

    daemon._handle_ritual_closed({"ritual_id": 5, "ritual": "weekly", "after_close": []})
    assert not (tmp_path / "briefing" / "weekly.md").exists()
    assert len(list((tmp_path / "briefing" / "sent").iterdir())) == 1


def test_turn_without_briefing_has_no_briefing_block(tmp_path):
    daemon = _daemon(tmp_path)
    daemon._handle_turn(TURN, 1_000.0)
    assert "<briefing>" not in daemon.adapter.prompts[0]


def test_ritual_soon_reaches_the_owner(tmp_path):
    daemon = _daemon(tmp_path)
    daemon._process_batch([{"kind": "ritual_soon", "seq": 1, "ritual": "weekly", "name": "Semanal", "at": 1_800_000_000.0}])
    filed = (tmp_path / FOR_OWNER_FILE).read_text(encoding="utf-8")
    assert "briefing/weekly.md" in filed and "Semanal" in filed
