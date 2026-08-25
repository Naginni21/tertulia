from tertulia.concierge.store import Store, hash_token


def test_delegate_tokens_are_hashed_and_unique():
    store = Store(":memory:")
    d1, t1 = store.create_delegate("Tomás", now=1.0)
    d2, t2 = store.create_delegate("Valentina", now=2.0)
    assert t1 != t2 and t1.startswith("tt_")
    assert store.delegate_by_token(t1).id == d1.id
    assert store.delegate_by_token("nope") is None
    # Only the hash is stored.
    row = store._conn.execute("SELECT token_hash FROM delegates WHERE id = ?", (d1.id,)).fetchone()
    assert row["token_hash"] == hash_token(t1) and t1 not in row["token_hash"]
    store.revoke(d2.id)
    assert store.delegate_by_token(t2) is None


def test_hello_marks_first_join_once():
    store = Store(":memory:")
    d, _ = store.create_delegate("Tomás", now=1.0)
    assert store.register_hello(d.id, "Brisa", now=5.0) is True
    assert store.register_hello(d.id, "Brisa", now=6.0) is False
    assert store.delegate(d.id).agent_name == "Brisa"
    assert store.delegates(joined_only=True)[0].joined_at == 5.0


def test_events_are_per_delegate_and_sequential():
    store = Store(":memory:")
    a, _ = store.create_delegate("A", now=1.0)
    b, _ = store.create_delegate("B", now=1.0)
    assert store.push_event(a.id, "room_message", {"x": 1}, at=1.0) == 1
    assert store.push_event(a.id, "turn", {"x": 2}, at=2.0) == 2
    assert store.push_event(b.id, "turn", {"x": 3}, at=3.0) == 1
    evs = store.events_after(a.id, 1)
    assert [e["seq"] for e in evs] == [2] and evs[0]["kind"] == "turn" and evs[0]["x"] == 2
    assert store.events_after(b.id, 0)[0]["x"] == 3


def test_spontaneous_count_and_tail():
    store = Store(":memory:")
    a, _ = store.create_delegate("A", now=1.0)
    b, _ = store.create_delegate("B", now=1.0)
    store.add_message(at=10, sender_kind="human", sender_name="Felipe", text="hola")
    store.add_message(at=11, sender_kind="delegate", sender_name="Brisa", delegate_id=a.id, text="hola!", turn_id=7)
    store.add_message(at=12, sender_kind="delegate", sender_name="Brisa", delegate_id=a.id, text="algo")
    store.add_message(at=13, sender_kind="concierge", sender_name="concierge", text="nota")
    store.add_message(at=14, sender_kind="delegate", sender_name="Cobre", delegate_id=b.id, text="otra")
    assert store.spontaneous_count(a.id, since=0) == 1  # the turn message does not count
    assert store.consecutive_delegate_tail() == 2       # Brisa + Cobre spontaneous, concierge transparent
    store.add_message(at=15, sender_kind="human", sender_name="Felipe", text="ya")
    assert store.consecutive_delegate_tail() == 0
    assert store.last_message_at(a.id) == 12
    assert [m.text for m in store.recent_messages(2)] == ["otra", "ya"]
