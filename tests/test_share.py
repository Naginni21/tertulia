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

    def say(self, text, *, turn_id=None):
        self.calls.append(("say", text, turn_id))

    def share(self, path, caption="", *, turn_id=None):
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
