"""A delegate that cannot reach the concierge for long tells its owner once — and again when it is back.

(4-sep-2026: Faro retried every minute for eleven hours behind a web filter
and nobody knew until the log was read the next day.)
"""

from tertulia.delegate.client import ConciergeError
from tertulia.delegate.config import AdapterConfig, BehaviourConfig, DelegateConfig
from tertulia.delegate.daemon import FOR_OWNER_FILE, DelegateDaemon


class FlakyHello:
    def __init__(self, failures: int):
        self.failures = failures

    def hello(self, name):
        if self.failures:
            self.failures -= 1
            raise ConciergeError(403, "http_error")
        return {"delegate": {"id": 3, "owner_name": "O"}, "room": {"name": "T", "language": "es", "delegates": []}}


class FakeClock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _daemon(tmp_path, failures, **behaviour):
    cfg = DelegateConfig(
        concierge_url="http://127.0.0.1:1", agent_name="Faro", owner_name="O", personality="",
        profile_path=tmp_path / "profile.md", memory_dir=tmp_path / "memory",
        state_dir=tmp_path / "state", sandbox_dir=tmp_path / "sandbox",
        shared_dir=tmp_path / "shared", outbox_dir=tmp_path / "outbox",
        token_file=tmp_path / "token", owner_telegram_user_id=None,
        adapter=AdapterConfig(kind="scripted"), behaviour=BehaviourConfig(**behaviour), base_dir=tmp_path,
    )
    clock = FakeClock()
    daemon = DelegateDaemon(cfg, client=FlakyHello(failures), adapter=None, clock=clock, sleep=clock.sleep)
    daemon.seen = []
    daemon._notify = daemon.seen.extend  # the observer, captured
    return daemon


def test_long_outage_is_reported_once_and_so_is_the_return(tmp_path):
    # Backoff 2, 4, 8, 16, 32, then 60 s: forty failures span well over 15 min.
    daemon = _daemon(tmp_path, failures=40, offline_alert_seconds=900)
    daemon.connect()

    filed = (tmp_path / FOR_OWNER_FILE).read_text(encoding="utf-8")
    assert filed.count("unable to reach the concierge") == 1
    assert "403 http_error" in filed
    assert "back in the room" in filed
    assert [e["kind"] for e in daemon.seen] == ["delegate_offline", "delegate_back"]
    assert daemon.my_id == 3


def test_short_blip_stays_quiet(tmp_path):
    daemon = _daemon(tmp_path, failures=3, offline_alert_seconds=900)
    daemon.connect()

    assert not (tmp_path / FOR_OWNER_FILE).exists()
    assert daemon.seen == []


def test_zero_disables_the_alert(tmp_path):
    daemon = _daemon(tmp_path, failures=40, offline_alert_seconds=0)
    daemon.connect()

    assert not (tmp_path / FOR_OWNER_FILE).exists()
    assert daemon.seen == []
