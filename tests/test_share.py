"""Sharing files from the pre-approved catalogue, end to end and at the daemon gate."""

from pathlib import Path

from tertulia.concierge.app import Concierge
from tertulia.concierge.server import ApiServer
from tertulia.concierge.store import Store
from tertulia.delegate.client import ConciergeClient
from tertulia.delegate.config import AdapterConfig, BehaviourConfig, DelegateConfig
from tertulia.delegate.daemon import DelegateDaemon

from conftest import FakeTelegram, make_config


def test_share_reaches_telegram_and_transcript(tmp_path, fake_tg: FakeTelegram):
    cfg = make_config(tmp_path)
    store = Store(cfg.db_path)
    app = Concierge(cfg, store, fake_tg, {})
    _, token = store.create_delegate("Ana")
    server = ApiServer(app, "127.0.0.1", 0)
    server.start()
    try:
        client = ConciergeClient(f"http://127.0.0.1:{server.port}", token)
        client.hello("Bot")
        f = tmp_path / "kit.zip"
        f.write_bytes(b"PK\x03\x04 fake zip")
        client.share(f, "toma, el kit completo")
        assert fake_tg.documents, "sendDocument never reached Telegram"
        doc = fake_tg.documents[0]
        assert doc["filename"] == "kit.zip"
        assert doc["content"] == b"PK\x03\x04 fake zip"
        assert "Bot" in (doc["caption"] or "")
        texts = [m["text"] for m in client.transcript(10)]
        assert any("kit.zip" in t and "toma, el kit completo" in t for t in texts)
    finally:
        server.stop()


class ShareStubClient:
    def __init__(self):
        self.calls = []

    def say(self, text, *, turn_id=None, origin_owner=False):
        self.calls.append(("say", text, turn_id))

    def share(self, path, caption="", *, turn_id=None, origin_owner=False):
        self.calls.append(("share", Path(path).name, caption, turn_id))


def test_daemon_share_directive_respects_catalogue(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "ads-kit.zip").write_bytes(b"x")
    cfg = DelegateConfig(
        concierge_url="http://127.0.0.1:1", agent_name="A", owner_name="O", personality="",
        profile_path=tmp_path / "profile.md", memory_dir=tmp_path / "memory",
        state_dir=tmp_path / "state", sandbox_dir=tmp_path / "sandbox",
        shared_dir=shared, outbox_dir=tmp_path / "outbox", token_file=tmp_path / "token", owner_telegram_user_id=None,
        adapter=AdapterConfig(kind="scripted"), behaviour=BehaviourConfig(), base_dir=tmp_path,
    )
    daemon = DelegateDaemon(cfg, client=ShareStubClient(), adapter=None)
    daemon._say_or_share("[SHARE ads-kit.zip] ahí va el paquete")
    daemon._say_or_share("[SHARE secreto.env] toma")

    calls = daemon.client.calls
    assert calls[0] == ("share", "ads-kit.zip", "ahí va el paquete", None)
    # A file outside the catalogue never leaves the machine: text only.
    assert calls[1][0] == "say" and "toma" in calls[1][1]


def test_ask_directive_files_question_and_still_speaks(tmp_path):
    cfg = DelegateConfig(
        concierge_url="http://127.0.0.1:1", agent_name="A", owner_name="O", personality="",
        profile_path=tmp_path / "profile.md", memory_dir=tmp_path / "memory",
        state_dir=tmp_path / "state", sandbox_dir=tmp_path / "sandbox",
        shared_dir=tmp_path / "shared", outbox_dir=tmp_path / "outbox",
        token_file=tmp_path / "token", owner_telegram_user_id=None,
        adapter=AdapterConfig(kind="scripted"), behaviour=BehaviourConfig(), base_dir=tmp_path,
    )
    daemon = DelegateDaemon(cfg, client=ShareStubClient(), adapter=None)
    daemon._say_or_share("[ASK] Chasqui pregunta el umbral exacto; no me consta, lo pregunto.")

    assert daemon.client.calls[0][0] == "say"
    assert "no me consta" in daemon.client.calls[0][1]
    filed = (tmp_path / "for-owner.md").read_text(encoding="utf-8")
    assert "umbral exacto" in filed and filed.startswith("- [")


def test_owner_origin_bypasses_quota_at_the_concierge(tmp_path, fake_tg: FakeTelegram):
    cfg = make_config(tmp_path)
    store = Store(cfg.db_path)
    app = Concierge(cfg, store, fake_tg, {})
    delegate, _ = store.create_delegate("Tomás")
    app.hello(delegate, "Brisa")
    delegate = store.delegate(delegate.id)
    for i in range(3):
        app.say(delegate, f"mensaje {i}", None)
    # Quota exhausted for the delegate's own speech, but the owner's is free:
    app.say(delegate, "nota del dueño", None, origin_owner=True)
    # ...and it does not eat the delegate's future quota either.
    assert store.spontaneous_count(delegate.id, since=0) == 3
