import json

from claudegram.sessions import Session, SessionStore


def make_store(tmp_path, **kwargs):
    options = dict(default_model="claude-opus-5", default_effort="medium",
                   max_messages=6, max_chars=10_000)
    options.update(kwargs)
    return SessionStore(tmp_path, **options)


def test_new_session_uses_defaults(tmp_path):
    session = make_store(tmp_path).get(42)
    assert session.chat_id == 42
    assert session.model == "claude-opus-5"
    assert session.messages == []


def test_session_round_trips_through_disk(tmp_path):
    store = make_store(tmp_path)
    session = store.get(7)
    session.add_user("hola")
    session.add_assistant("qué tal")
    session.record_usage({"input": 10, "output": 20, "cache_read": 5, "cache_write": 0})
    store.save(session)

    reloaded = make_store(tmp_path).get(7)
    assert reloaded.messages == [
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": "qué tal"},
    ]
    assert reloaded.tokens["output"] == 20
    assert reloaded.turns == 1


def test_trim_drops_oldest_and_keeps_a_user_first(tmp_path):
    store = make_store(tmp_path, max_messages=4)
    session = store.get(1)
    for i in range(5):
        session.add_user(f"p{i}")
        session.add_assistant(f"r{i}")
    store.trim(session)
    assert len(session.messages) <= 4
    assert session.messages[0]["role"] == "user"


def test_trim_respects_the_char_budget(tmp_path):
    store = make_store(tmp_path, max_messages=100, max_chars=500)
    session = store.get(1)
    for i in range(20):
        session.add_user("x" * 200)
        session.add_assistant("y" * 200)
    store.trim(session)
    assert session.size_chars <= 500 or len(session.messages) <= 2
    assert session.messages[0]["role"] == "user"


def test_reset_archives_the_old_conversation(tmp_path):
    store = make_store(tmp_path)
    session = store.get(3)
    session.add_user("algo")
    store.save(session)

    assert store.reset(3) == 1
    assert store.get(3).messages == []
    archives = list((tmp_path / "archive").glob("3-*.json"))
    assert len(archives) == 1
    assert json.loads(archives[0].read_text(encoding="utf-8"))["messages"][0]["content"] == "algo"


def test_offset_is_persisted(tmp_path):
    store = make_store(tmp_path)
    assert store.load_offset() is None
    store.save_offset(99)
    assert make_store(tmp_path).load_offset() == 99


def test_corrupt_file_starts_a_fresh_session(tmp_path):
    store = make_store(tmp_path)
    store.save(store.get(5))
    (tmp_path / "chats" / "5.json").write_text("{no es json", encoding="utf-8")
    assert make_store(tmp_path).get(5).messages == []


def test_drop_last_user_only_removes_a_trailing_user_turn(tmp_path):
    session = Session(chat_id=1, model="m", effort="medium")
    session.add_user("a")
    session.drop_last_user()
    assert session.messages == []
    session.add_user("a")
    session.add_assistant("b")
    session.drop_last_user()
    assert len(session.messages) == 2
