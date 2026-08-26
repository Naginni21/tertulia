from tertulia.concierge.limits import Limits, SpontaneousSnapshot, check_spontaneous, check_text


def snap(**kw):
    base = dict(sent_last_24h=0, seconds_since_last_own=None, consecutive_delegate_tail=0, ritual_running=False)
    base.update(kw)
    return SpontaneousSnapshot(**base)


def test_spontaneous_allowed_by_default():
    assert check_spontaneous(Limits(), snap()) is None


def test_daily_quota():
    assert check_spontaneous(Limits(spontaneous_per_24h=3), snap(sent_last_24h=3)) == "daily_quota"
    assert check_spontaneous(Limits(spontaneous_per_24h=3), snap(sent_last_24h=2)) is None


def test_min_gap():
    assert check_spontaneous(Limits(min_gap_seconds=30), snap(seconds_since_last_own=10)) == "too_soon"
    assert check_spontaneous(Limits(min_gap_seconds=30), snap(seconds_since_last_own=31)) is None


def test_anti_ping_pong():
    assert check_spontaneous(Limits(max_consecutive_delegate_messages=4), snap(consecutive_delegate_tail=4)) == "waiting_for_humans"


def test_ritual_blocks_spontaneous():
    assert check_spontaneous(Limits(), snap(ritual_running=True)) == "ritual_running"


def test_text_checks():
    assert check_text(Limits(max_message_chars=10), "") == "empty"
    assert check_text(Limits(max_message_chars=10), "x" * 11) == "too_long"
    assert check_text(Limits(max_message_chars=10), "hola") is None


def test_zero_disables_the_consecutive_brake():
    from tertulia.concierge.limits import Limits, SpontaneousSnapshot, check_spontaneous
    snap = SpontaneousSnapshot(sent_last_24h=0, seconds_since_last_own=None,
                               consecutive_delegate_tail=50, ritual_running=False)
    assert check_spontaneous(Limits(max_consecutive_delegate_messages=0), snap) is None
