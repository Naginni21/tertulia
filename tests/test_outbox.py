"""Owner notes: the outbox is how the owner (or their main agent) speaks through the delegate."""

from tertulia.delegate.adapters.base import Completion
from tertulia.delegate.config import AdapterConfig, BehaviourConfig, DelegateConfig
from tertulia.delegate.daemon import DelegateDaemon
from tertulia.delegate.prompt import SILENCE


class CannedAdapter:
    def __init__(self, text):
        self.text = text
        self.prompts = []

    def complete(self, *, system_prompt, prompt, timeout=None, model=None):
        self.prompts.append(prompt)
        return Completion(text=self.text, cost_usd=None, raw={})


class RoomStubClient:
    def __init__(self, remaining=3, ritual=None):
        self.remaining = remaining
        self.ritual = ritual
        self.room_calls = 0
        self.said = []

    def room(self):
        self.room_calls += 1
        return {"name": "T", "language": "es", "delegates": [], "ritual": self.ritual,
                "limits": {"max_message_chars": 2000},
                "you": {"spontaneous_remaining_24h": self.remaining}}

    def transcript(self, limit=40, *, ritual_id=None):
        return []

    def say(self, text, *, turn_id=None, origin_owner=False):
        self.said.append((text, origin_owner))


def _cfg(tmp_path):
    return DelegateConfig(
        concierge_url="http://127.0.0.1:1", agent_name="A", owner_name="O", personality="",
        profile_path=tmp_path / "profile.md", memory_dir=tmp_path / "memory",
        state_dir=tmp_path / "state", sandbox_dir=tmp_path / "sandbox",
        shared_dir=tmp_path / "shared", outbox_dir=tmp_path / "outbox",
        token_file=tmp_path / "token", owner_telegram_user_id=None,
        adapter=AdapterConfig(kind="scripted"), behaviour=BehaviourConfig(), base_dir=tmp_path,
    )


def test_owner_note_becomes_a_room_message(tmp_path):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    (outbox / "nota.md").write_text("cuéntale a la sala lo del bug", encoding="utf-8")
    daemon = DelegateDaemon(_cfg(tmp_path), client=RoomStubClient(), adapter=CannedAdapter("aviso: el bug era nuestro"))
    daemon._process_outbox()

    assert daemon.client.said == [("aviso: el bug era nuestro", True)]  # origin_owner
    assert daemon.adapter.prompts and "cuéntale a la sala lo del bug" in daemon.adapter.prompts[0]
    assert not (outbox / "nota.md").exists()
    assert len(list((outbox / "sent").iterdir())) == 1


def test_owner_note_ignores_the_spontaneous_quota(tmp_path):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    (outbox / "nota.md").write_text("di algo", encoding="utf-8")
    daemon = DelegateDaemon(_cfg(tmp_path), client=RoomStubClient(remaining=0), adapter=CannedAdapter("x"))
    daemon._process_outbox()

    # The owner speaking has no quota; the note goes out even at 0 remaining.
    assert daemon.client.said == [("x", True)]
    assert not (outbox / "nota.md").exists()


def test_ritual_defers_notes_without_reaching_the_model(tmp_path):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    (outbox / "nota.md").write_text("di algo", encoding="utf-8")
    daemon = DelegateDaemon(
        _cfg(tmp_path), client=RoomStubClient(ritual={"id": 1, "name": "welcome"}), adapter=CannedAdapter("x")
    )
    daemon._process_outbox()
    daemon._process_outbox()  # within the backoff window

    assert daemon.adapter.prompts == []          # composing an unpostable note costs nothing
    assert daemon.client.room_calls == 1         # the backoff even skips polling
    assert (outbox / "nota.md").exists()
    # A human speaking lifts the backoff at once.
    daemon._process_batch([{"kind": "room_message", "at": 0,
                            "message": {"sender_kind": "human", "sender_name": "F", "text": "hola", "at": 0}}])
    assert daemon._outbox_backoff_until == 0.0


def test_silence_archives_the_note_without_speaking(tmp_path):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    (outbox / "nota.md").write_text("solo para tu contexto, no digas nada", encoding="utf-8")
    daemon = DelegateDaemon(_cfg(tmp_path), client=RoomStubClient(), adapter=CannedAdapter(SILENCE))
    daemon._process_outbox()

    assert daemon.client.said == []
    assert not (outbox / "nota.md").exists()
    assert len(list((outbox / "sent").iterdir())) == 1
