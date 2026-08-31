import os
import stat

import pytest

from claudegram import __main__ as cli
from claudegram.config import ConfigError, update_env_file
from claudegram.telegram import TelegramError
from tests.conftest import FakeTelegram


def test_update_env_replaces_appends_and_keeps_comments(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# el token\nTELEGRAM_BOT_TOKEN=viejo\n\n# otra cosa\nA=1\n", encoding="utf-8")
    os.chmod(env, 0o600)

    update_env_file(env, {"TELEGRAM_BOT_TOKEN": "nuevo", "ANTHROPIC_API_KEY": "sk-ant-x"})

    assert env.read_text(encoding="utf-8") == (
        "# el token\nTELEGRAM_BOT_TOKEN=nuevo\n\n# otra cosa\nA=1\nANTHROPIC_API_KEY=sk-ant-x\n"
    )
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_update_env_creates_the_file_private(tmp_path):
    env = tmp_path / ".env"
    update_env_file(env, {"A": "1"})
    assert env.read_text(encoding="utf-8") == "A=1\n"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_update_env_rejects_multiline_values(tmp_path):
    with pytest.raises(ConfigError):
        update_env_file(tmp_path / ".env", {"A": "uno\ndos"})


def test_pairing_learns_the_owner_id(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_ALLOWED_USER_IDS=123456789\n", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123456789")

    telegram = FakeTelegram()
    batches = [[{"update_id": 5, "message": {"from": {"id": 761234567, "username": "andres"},
                                             "chat": {"id": 761234567}, "text": "hola"}}]]
    acknowledged = []

    def get_updates(offset, timeout):
        acknowledged.append(offset)
        return batches.pop(0) if batches else []

    telegram.get_updates = get_updates

    assert cli._ensure_owner(env, telegram) is True
    assert "TELEGRAM_ALLOWED_USER_IDS=761234567" in env.read_text(encoding="utf-8")
    assert os.environ["TELEGRAM_ALLOWED_USER_IDS"] == "761234567"
    assert acknowledged[-1] == 6  # the pairing message is consumed, not answered later


def test_pairing_explains_a_running_bot(tmp_path, capsys):
    telegram = FakeTelegram()

    def get_updates(offset, timeout):
        raise TelegramError("Conflict: terminated by other getUpdates request", 409)

    telegram.get_updates = get_updates

    assert cli._ensure_owner(tmp_path / ".env", telegram) is False
    assert "systemctl stop claudegram" in capsys.readouterr().out


def test_pairing_gives_up_quietly(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "PAIRING_TIMEOUT", -1)
    telegram = FakeTelegram()
    telegram.get_updates = lambda offset, timeout: []
    assert cli._ensure_owner(tmp_path / ".env", telegram) is False
    assert "No ha llegado ningún mensaje" in capsys.readouterr().out


def test_existing_token_is_accepted_without_asking(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "8634000253:AAvalido")
    monkeypatch.setattr(cli, "TelegramClient", lambda token, timeout=None: FakeTelegram())
    monkeypatch.setattr("builtins.input", lambda _prompt: pytest.fail("no debería preguntar"))

    assert cli._ensure_token(tmp_path / ".env") == "8634000253:AAvalido"
    assert "@bot_de_pruebas" in capsys.readouterr().out


def test_placeholder_token_is_replaced_by_what_you_paste(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=123456789:AAxxxxx\n", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456789:AAxxxxx")
    monkeypatch.setattr(cli, "TelegramClient", lambda token, timeout=None: FakeTelegram())
    monkeypatch.setattr("builtins.input", lambda _prompt: "8634000253:AAreal")

    assert cli._ensure_token(env) == "8634000253:AAreal"
    assert "TELEGRAM_BOT_TOKEN=8634000253:AAreal" in env.read_text(encoding="utf-8")


def test_setup_gives_up_when_you_paste_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    assert cli._ensure_token(tmp_path / ".env") is None


def test_setup_runs_the_three_steps_in_order(tmp_path, monkeypatch, capsys):
    """token → clave → id, y solo entonces dice que arranques."""
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=8634000253:AAvalido\n", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "8634000253:AAvalido")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")

    steps = []
    monkeypatch.setattr(cli, "TelegramClient", lambda token, timeout=None: FakeTelegram())
    monkeypatch.setattr(cli, "_ensure_token", lambda f: steps.append("token") or "tok")
    monkeypatch.setattr(cli, "_ensure_api_key", lambda f: steps.append("clave") or True)
    monkeypatch.setattr(cli, "_ensure_owner", lambda f, t: steps.append("id") or True)

    assert cli._setup(env) == 0
    assert steps == ["token", "clave", "id"]
    assert "systemctl restart claudegram" in capsys.readouterr().out


def test_setup_stops_at_the_first_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_ensure_token", lambda f: None)
    monkeypatch.setattr(cli, "_ensure_api_key", lambda f: pytest.fail("no debería llegar aquí"))
    assert cli._setup(tmp_path / ".env") == 1
