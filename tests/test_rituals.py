from pathlib import Path

import pytest

from tertulia.concierge.i18n import Strings
from tertulia.concierge.rituals import RitualRun, RitualSpecError, load_ritual, parse_ritual

RITUALS = Path(__file__).resolve().parents[1] / "rituals"


class FakePort:
    def __init__(self, online: set[int]):
        self.online = online
        self.posts: list[str] = []
        self.events: list[tuple[str, dict]] = []
        self.turns: dict[int, dict] = {}
        self.t = Strings("es")

    def post_concierge(self, text, *, ritual_id):
        self.posts.append(text)

    def broadcast(self, kind, payload):
        self.events.append((kind, payload))

    def issue_turn(self, *, ritual_id, ritual, round_id, delegate_id, instruction, deadline):
        tid = len(self.turns) + 1
        self.turns[tid] = {"round": round_id, "delegate": delegate_id, "status": "open"}
        return tid

    def turn_status(self, turn_id):
        return self.turns[turn_id]["status"]

    def close_turn(self, turn_id, status):
        self.turns[turn_id]["status"] = status

    def delegate_online(self, delegate_id):
        return delegate_id in self.online

    def delegate_label(self, delegate_id):
        return f"D{delegate_id}"

    def note(self, key, **fmt):
        return self.t(key, **fmt)

    def join_names(self, names):
        return self.t.join_names(names)

    def room_name(self):
        return "Sala"


@pytest.mark.parametrize("lang", ["es", "en"])
def test_shipped_rituals_parse(lang):
    spec = load_ritual(RITUALS / lang / "welcome.yaml")
    assert spec.id == "welcome" and len(spec.rounds) == 2 and spec.after_close[0].action == "update_memory"


def test_spec_validation():
    with pytest.raises(RitualSpecError):
        parse_ritual({"id": "x", "open": "o", "close": "c", "rounds": [{"id": "r", "instruction": "i", "order": "random"}]})
    with pytest.raises(RitualSpecError):
        parse_ritual({"id": "x", "open": "o", "close": "c", "rounds": []})


def test_welcome_flow_with_sleeping_delegate_and_timeout():
    spec = load_ritual(RITUALS / "es" / "welcome.yaml")
    port = FakePort(online={1, 2})
    run = RitualRun(ritual_id=9, spec=spec, participants=[1, 2, 3], newcomers=[2])

    assert run.tick(0.0, port) is False
    assert "Se suma: D2" in port.posts[0]
    assert port.events[0][0] == "ritual_opened"
    # Round 1 order: newcomer first (2), then others in join order (1, 3).
    assert port.turns[1] == {"round": "presentaciones", "delegate": 2, "status": "open"}

    run.on_answer(1)
    run.tick(1.0, port)
    assert port.turns[2]["delegate"] == 1
    # Delegate 1 never answers: expires after the round timeout.
    run.tick(1.0 + spec.rounds[0].timeout_seconds + 1, port)
    assert port.turns[2]["status"] == "expired"
    assert any("no respondió a tiempo" in p for p in port.posts)
    # Delegate 3 is asleep: skipped without a turn; round 2 starts with delegate 1.
    assert any("D3 está dormido" in p for p in port.posts)
    assert port.turns[3] == {"round": "replicas", "delegate": 1, "status": "open"}

    run.on_answer(3)
    run.tick(400.0, port)
    assert port.turns[4]["delegate"] == 2  # newcomer last in the replies round
    run.on_answer(4)
    assert run.tick(401.0, port) is True
    assert port.events[-1][0] == "ritual_closed"
    assert port.events[-1][1]["after_close"][0]["action"] == "update_memory"
    assert "Cierre de la bienvenida" in port.posts[-1]


def test_answering_turn_is_not_expired():
    spec = load_ritual(RITUALS / "es" / "welcome.yaml")
    port = FakePort(online={1})
    run = RitualRun(ritual_id=1, spec=spec, participants=[1], newcomers=[1])
    run.tick(0.0, port)
    port.turns[1]["status"] = "answering"
    run.tick(10_000.0, port)  # way past the deadline, but the send is in flight
    assert port.turns[1]["status"] == "answering" and run.current is not None
