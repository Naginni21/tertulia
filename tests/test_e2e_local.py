"""End-to-end on localhost: real HTTP API, real daemons, scripted agents, fake
Telegram. Exercises join → welcome ritual (two rounds) → room map written →
human message → spontaneous reply with quota."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from tertulia.concierge.app import Concierge
from tertulia.concierge.rituals import load_rituals
from tertulia.concierge.server import ApiServer
from tertulia.concierge.store import Store
from tertulia.delegate.adapters.scripted import ScriptedAdapter
from tertulia.delegate.client import ConciergeClient
from tertulia.delegate.config import AdapterConfig, BehaviourConfig, DelegateConfig
from tertulia.delegate.daemon import DelegateDaemon
from tertulia.delegate.prompt import REPLY, SILENCE

from conftest import FakeTelegram, make_config


def wait_for(predicate, timeout=15.0, step=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def delegate_config(base: Path, url: str, agent: str, owner: str) -> DelegateConfig:
    base.mkdir(parents=True, exist_ok=True)
    (base / "profile.md").write_text(f"# Perfil de {owner}\n- Trabaja en cosas de {owner}.\n", encoding="utf-8")
    return DelegateConfig(
        concierge_url=url, agent_name=agent, owner_name=owner, personality="test",
        profile_path=base / "profile.md", memory_dir=base / "memory", state_dir=base / "state",
        sandbox_dir=base / "sandbox", token_file=base / "token", owner_telegram_user_id=None,
        adapter=AdapterConfig(kind="scripted"),
        behaviour=BehaviourConfig(react_max_age_seconds=600, batch_settle_seconds=0.2),
        base_dir=base,
    )


def test_welcome_ritual_and_spontaneous_reply(tmp_path, fake_tg: FakeTelegram):
    cfg = make_config(tmp_path)
    store = Store(cfg.db_path)
    app = Concierge(cfg, store, fake_tg, load_rituals(cfg.room.rituals_dir))
    app.start()
    server = ApiServer(app, "127.0.0.1", 0)
    server.start()
    url = f"http://127.0.0.1:{server.port}"

    _, token_a = store.create_delegate("Tomás")
    _, token_b = store.create_delegate("Valentina")

    def brisa_brain(system_prompt: str, prompt: str) -> str:
        assert "You are Brisa, the delegate of Tomás" in system_prompt
        if "## Task" in prompt:
            return "# Mapa de la sala\n- Cobre: delegado de Valentina."
        if "## Triage" in prompt:
            return REPLY  # cheap gate says yes; the voice model writes next
        if "## Decide" in prompt:
            return "Hola Felipe, aquí Brisa."
        if 'round "presentaciones"' in prompt:
            return "Soy Brisa, delegada de Tomás."
        return "Réplica de Brisa para Cobre."

    def cobre_brain(system_prompt: str, prompt: str) -> str:
        if "## Task" in prompt:
            return "# Mapa\n- Brisa: delegada de Tomás."
        if "## Triage" in prompt:
            return SILENCE  # gate says no: the voice model must never run
        if "## Decide" in prompt:
            raise AssertionError("voice model ran although triage said silence")
        return "Cobre presente." if 'round "presentaciones"' in prompt else "Réplica de Cobre."

    brisa = DelegateDaemon(delegate_config(tmp_path / "brisa", url, "Brisa", "Tomás"),
                           ConciergeClient(url, token_a), ScriptedAdapter(brisa_brain))
    cobre = DelegateDaemon(delegate_config(tmp_path / "cobre", url, "Cobre", "Valentina"),
                           ConciergeClient(url, token_b), ScriptedAdapter(cobre_brain))

    ticker_stop = threading.Event()

    def ticker():
        while not ticker_stop.is_set():
            app.tick()
            time.sleep(0.1)

    threading.Thread(target=app.poll_telegram_forever, daemon=True).start()
    threading.Thread(target=ticker, daemon=True).start()
    # Brisa joins first, then Cobre (join order matters for the ritual order).
    threading.Thread(target=brisa.run_forever, kwargs={"wait": 2}, daemon=True).start()
    assert wait_for(lambda: brisa.my_id is not None)
    threading.Thread(target=cobre.run_forever, kwargs={"wait": 2}, daemon=True).start()

    try:
        # Welcome ritual: open, 2 intros, 2 replies, close.
        assert wait_for(lambda: any("Cierre de la bienvenida" in t for t in fake_tg.texts), timeout=20), fake_tg.texts
        texts = fake_tg.texts
        assert "Se suma: Brisa (delegado de Tomás) y Cobre (delegado de Valentina)" in texts[0]
        assert "<b>🤖 Brisa (delegado de Tomás)</b>\nSoy Brisa, delegada de Tomás." in texts
        assert "<b>🤖 Cobre (delegado de Valentina)</b>\nCobre presente." in texts
        assert any("Réplica de Brisa" in t for t in texts) and any("Réplica de Cobre" in t for t in texts)
        intro_b, intro_c = texts.index(next(t for t in texts if "Soy Brisa" in t)), texts.index(next(t for t in texts if "Cobre presente" in t))
        assert intro_b < intro_c  # join order: Brisa joined first

        # Room maps written from the agents' output.
        assert wait_for(lambda: (tmp_path / "brisa" / "memory" / "room-map.md").exists())
        assert "Cobre" in (tmp_path / "brisa" / "memory" / "room-map.md").read_text()
        assert wait_for(lambda: (tmp_path / "cobre" / "memory" / "room-map.md").exists())

        # A human mentions Brisa: Brisa answers once, Cobre stays quiet.
        n_before = len(fake_tg.sent)
        fake_tg.queue_human(1, cfg.telegram.chat_id, 7, "Felipe", "Hola Brisa, ¿estás ahí?", time.time())
        assert wait_for(lambda: any("Hola Felipe, aquí Brisa." in t for t in fake_tg.texts), timeout=15)
        time.sleep(1.0)
        assert len(fake_tg.sent) == n_before + 1
        assert store.spontaneous_count(1, since=0) == 1

        # /status works for anyone; /welcome only for admins.
        fake_tg.queue_human(2, cfg.telegram.chat_id, 7, "Felipe", "/status", time.time())
        assert wait_for(lambda: any("Estado de la sala" in t for t in fake_tg.texts))
        fake_tg.queue_human(3, cfg.telegram.chat_id, 7, "Felipe", "/welcome", time.time())
        assert wait_for(lambda: any("Solo un administrador" in t for t in fake_tg.texts))
    finally:
        ticker_stop.set()
        brisa.stop = cobre.stop = True
        app.stop_event.set()
        server.stop()
        store.close()


def test_quota_and_turn_rules(tmp_path, fake_tg: FakeTelegram):
    cfg = make_config(tmp_path)
    store = Store(cfg.db_path)
    app = Concierge(cfg, store, fake_tg, {})  # no rituals: nothing auto-starts
    app.start()
    delegate, _ = store.create_delegate("Tomás")
    app.hello(delegate, "Brisa")
    delegate = store.delegate(delegate.id)

    for i in range(3):
        app.say(delegate, f"mensaje {i}", None)
    try:
        app.say(delegate, "uno más", None)
        assert False, "expected quota rejection"
    except Exception as exc:  # ApiError
        assert getattr(exc, "status", None) == 429 and getattr(exc, "code", None) == "daily_quota"

    try:
        app.say(delegate, "x", 999)
        assert False
    except Exception as exc:
        assert getattr(exc, "code", None) == "unknown_turn"
    store.close()
