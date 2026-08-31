import threading
import time

from claudegram.app import _parse_command, _uptime
from claudegram.claude import ClaudeError, Reply
from tests.conftest import FakeClaude, FakeTelegram, message


def test_parse_command_handles_arguments_and_bot_suffix():
    assert _parse_command("/modelo@mi_bot haiku") == ("modelo", "haiku")
    assert _parse_command("/nuevo") == ("nuevo", "")
    assert _parse_command("hola") == ("", "")


def test_uptime_is_readable():
    assert _uptime(30) == "30 s"
    assert _uptime(3600 * 25) == "1 d 1 h"


def test_help_is_sent_as_html(make_bot):
    bot, telegram, _ = make_bot()
    bot._dispatch(message("/ayuda"))
    assert telegram.sent
    assert telegram.sent[0][2] == "HTML"
    assert "/nuevo" in telegram.texts[0]


def test_unauthorized_user_only_gets_their_id(make_bot):
    bot, telegram, claude = make_bot()
    bot._dispatch(message("hola", user_id=999, chat_id=999))
    bot._dispatch(message("hola otra vez", user_id=999, chat_id=999, update_id=2))
    assert len(telegram.sent) == 1  # the second one is inside the cooldown
    assert "999" in telegram.texts[0]
    assert claude.calls == []


def test_id_answers_even_to_strangers(make_bot):
    bot, telegram, _ = make_bot()
    bot._dispatch(message("/id", user_id=777, chat_id=777))
    assert "777" in telegram.texts[0]


def test_media_without_text_gets_a_hint(make_bot):
    bot, telegram, _ = make_bot()
    bot._dispatch(message(None, photo=[{"file_id": "x"}]))
    assert "Solo entiendo texto" in telegram.texts[0]


def test_service_message_is_ignored(make_bot):
    bot, telegram, _ = make_bot()
    bot._dispatch(message(None, new_chat_members=[{"id": 2}]))
    assert telegram.sent == []


def test_modelo_switches_and_persists(make_bot):
    bot, telegram, _ = make_bot()
    bot._dispatch(message("/modelo haiku"))
    assert bot.store.get(500).model == "claude-haiku-4-5"
    assert "claude-haiku-4-5" in telegram.texts[-1]
    bot._dispatch(message("/modelo", update_id=2))
    assert "/modelo opus" in telegram.texts[-1]
    bot._dispatch(message("/modelo inventado", update_id=3))
    assert "No conozco" in telegram.texts[-1]
    assert bot.store.get(500).model == "claude-haiku-4-5"


def test_esfuerzo_validates_and_respects_the_model(make_bot):
    bot, telegram, _ = make_bot()
    bot._dispatch(message("/esfuerzo max"))
    assert bot.store.get(500).effort == "max"
    bot._dispatch(message("/esfuerzo turbo", update_id=2))
    assert "Opciones" in telegram.texts[-1]
    assert bot.store.get(500).effort == "max"
    bot._dispatch(message("/modelo haiku", update_id=3))
    bot._dispatch(message("/esfuerzo low", update_id=4))
    assert "no admite" in telegram.texts[-1]


def test_breve_toggles_and_reaches_the_request(make_bot):
    bot, telegram, claude = make_bot()
    bot._dispatch(message("/breve"))
    assert bot.store.get(500).brief is True
    bot._answer(500, "hola")
    assert claude.calls[0]["brief"] is True
    bot._dispatch(message("/breve", update_id=2))
    assert bot.store.get(500).brief is False


def test_nuevo_clears_the_history(make_bot):
    bot, telegram, _ = make_bot()
    bot._answer(500, "primera pregunta")
    assert len(bot.store.get(500).messages) == 2
    bot._dispatch(message("/nuevo"))
    assert bot.store.get(500).messages == []
    assert "Conversación nueva" in telegram.texts[-1]


def test_unknown_command(make_bot):
    bot, telegram, _ = make_bot()
    bot._dispatch(message("/bailar"))
    assert "No conozco /bailar" in telegram.texts[-1]


def test_answer_sends_the_reply_and_keeps_the_history(make_bot):
    bot, telegram, claude = make_bot()
    bot._answer(500, "¿cuánto es 2+2?")
    assert telegram.texts == ["respuesta"]
    session = bot.store.get(500)
    assert [m["content"] for m in session.messages] == ["¿cuánto es 2+2?", "respuesta"]
    assert claude.calls[0]["messages"][-1]["content"] == "¿cuánto es 2+2?"


def test_usage_is_accumulated(make_bot):
    reply = Reply(text="ok", model="claude-opus-5",
                  usage={"input": 100, "output": 50, "cache_read": 10, "cache_write": 0})
    bot, telegram, _ = make_bot(claude=FakeClaude(reply=reply))
    bot._answer(500, "hola")
    bot._dispatch(message("/estado", update_id=9))
    assert bot.store.get(500).tokens["output"] == 50
    assert "Gasto aprox" in telegram.texts[-1]


def test_long_reply_is_split(make_bot):
    long_text = "\n\n".join(f"Párrafo {i} con texto suficiente." for i in range(300))
    bot, telegram, _ = make_bot(claude=FakeClaude(reply=Reply(text=long_text)))
    bot._answer(500, "escribe mucho")
    assert len(telegram.sent) > 1
    assert all(len(text) <= 4096 for text in telegram.texts)


def test_truncated_reply_warns(make_bot):
    reply = Reply(text="a medias", stop_reason="max_tokens")
    bot, telegram, _ = make_bot(claude=FakeClaude(reply=reply))
    bot._answer(500, "hola")
    assert "Cortado en el límite" in telegram.texts[-1]


def test_cancelled_reply_keeps_the_partial_text(make_bot):
    reply = Reply(text="iba por aquí", cancelled=True)
    bot, telegram, _ = make_bot(claude=FakeClaude(reply=reply))
    bot._answer(500, "hola")
    assert "iba por aquí" in telegram.texts[0]
    assert "parado" in telegram.texts[-1]
    assert bot.store.get(500).messages[-1]["content"] == "iba por aquí"


def test_refusal_is_reported_and_the_turn_is_dropped(make_bot):
    reply = Reply(text="", stop_reason="refusal", refusal="cyber")
    bot, telegram, _ = make_bot(claude=FakeClaude(reply=reply))
    bot._answer(500, "algo raro")
    assert "declinado" in telegram.texts[-1]
    assert bot.store.get(500).messages == []


def test_api_error_drops_the_user_turn(make_bot):
    bot, telegram, _ = make_bot(claude=FakeClaude(error=ClaudeError("La API está caída")))
    bot._answer(500, "hola")
    assert "La API está caída" in telegram.texts[-1]
    assert bot.store.get(500).messages == []


def test_html_rejection_falls_back_to_plain_text(make_bot):
    telegram = FakeTelegram()
    telegram.reject_html = True
    bot, _, _ = make_bot(telegram=telegram, claude=FakeClaude(reply=Reply(text="`código`")))
    bot._answer(500, "hola")
    assert telegram.sent[-1][2] is None
    assert "código" in telegram.texts[-1]


def test_parar_cancels_the_running_turn(make_bot):
    bot, telegram, _ = make_bot()
    bot._cancel_for(500)
    bot._dispatch(message("/parar"))
    assert bot._cancels[500].is_set()
    assert "paro" in telegram.texts[-1]
    bot._dispatch(message("/parar", update_id=2))
    assert "No estaba escribiendo" in telegram.texts[-1]


def test_worker_merges_messages_that_arrive_together(make_bot):
    release = threading.Event()

    class SlowClaude(FakeClaude):
        def ask(self, **kwargs):
            release.wait(2.0)
            return super().ask(**kwargs)

    claude = SlowClaude()
    bot, telegram, _ = make_bot(claude=claude)
    bot._enqueue(500, "primera parte")
    bot._enqueue(500, "segunda parte")
    bot._enqueue(500, "tercera parte")
    release.set()

    deadline = time.time() + 5
    while len(claude.calls) < 1 and time.time() < deadline:
        time.sleep(0.02)
    time.sleep(0.3)
    bot._shutdown()

    joined = [call["messages"][-1]["content"] for call in claude.calls]
    assert "segunda parte" in "\n".join(joined)
    assert "tercera parte" in "\n".join(joined)


def test_stream_edits_updates_one_message(make_bot):
    bot, telegram, _ = make_bot(CLAUDEGRAM_STREAM_EDITS="true", CLAUDEGRAM_EDIT_INTERVAL_SECONDS="1")
    bot._answer(500, "hola")
    # First a plain placeholder, then the final HTML edit on the same message.
    assert telegram.sent[0][2] is None
    assert telegram.edits
    assert telegram.edits[-1][3] == "HTML"


def test_run_loop_processes_a_batch_and_persists_the_offset(make_bot):
    bot, telegram, claude = make_bot()
    seen: list = []

    def get_updates(offset, timeout):
        seen.append(offset)
        if len(seen) == 1:
            return [message("hola", update_id=7)]
        bot._stop.set()
        return []

    telegram.get_updates = get_updates
    bot.run()

    assert seen[0] is None          # nothing stored yet
    assert bot.store.load_offset() == 8
    assert len(claude.calls) == 1
    assert telegram.commands       # the command menu was registered


def test_run_survives_a_telegram_outage(make_bot):
    from claudegram.telegram import TelegramError

    bot, telegram, _ = make_bot()
    attempts: list = []

    def get_updates(offset, timeout):
        attempts.append(offset)
        if len(attempts) == 1:
            raise TelegramError("Bad Gateway", 502)
        bot._stop.set()
        return []

    telegram.get_updates = get_updates
    bot._stop.wait = lambda _timeout: None  # no real sleep in tests
    bot.run()

    assert len(attempts) == 2


def test_live_placeholder_is_deleted_when_nothing_useful_arrives(make_bot):
    """A refusal after some text has streamed leaves a placeholder to clean up."""

    class RefusesMidStream(FakeClaude):
        def ask(self, **kwargs):
            kwargs["on_delta"]("empezaba a escribir…")
            return Reply(text="", stop_reason="refusal", refusal="cyber")

    bot, telegram, _ = make_bot(claude=RefusesMidStream(), CLAUDEGRAM_STREAM_EDITS="true",
                                CLAUDEGRAM_EDIT_INTERVAL_SECONDS="1")
    bot._answer(500, "algo")
    assert telegram.deleted == [(500, 1)]
    assert "declinado" in telegram.texts[-1]


def test_session_with_an_unknown_model_falls_back(make_bot, tmp_path):
    import json

    bot, telegram, _ = make_bot()
    session = bot.store.get(500)
    bot.store.save(session)
    path = bot.store.chats_dir / "500.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["model"] = "claude-de-hace-diez-anos"
    data["effort"] = "extremo"
    path.write_text(json.dumps(data), encoding="utf-8")

    bot.store._cache.clear()
    reloaded = bot.store.get(500)
    assert reloaded.model == "claude-opus-5"
    assert reloaded.effort == "medium"
    bot._dispatch(message("/estado"))  # must not raise
