import pytest

from claudegram.claude import Reply
from claudegram.config import Config
from claudegram.telegram import TelegramError


class FakeTelegram:
    """Records what the bot would have sent to Telegram."""

    def __init__(self):
        self.sent: list[tuple[int, str, str | None]] = []
        self.edits: list[tuple[int, int, str, str | None]] = []
        self.commands: list[tuple[str, str]] = []
        self.deleted: list[tuple[int, int]] = []
        self.reject_html = False

    def get_me(self):
        return {"username": "bot_de_pruebas", "id": 1}

    def delete_webhook(self, *, drop_pending=False):
        pass

    def set_my_commands(self, commands):
        self.commands = commands

    def send_message(self, chat_id, text, *, parse_mode=None, reply_to_message_id=None):
        if self.reject_html and parse_mode == "HTML":
            raise TelegramError("Bad Request: can't parse entities: whatever", 400)
        self.sent.append((chat_id, text, parse_mode))
        return len(self.sent)

    def edit_message_text(self, chat_id, message_id, text, *, parse_mode=None):
        self.edits.append((chat_id, message_id, text, parse_mode))

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    def send_chat_action(self, chat_id, action="typing"):
        pass

    @property
    def texts(self) -> list[str]:
        return [text for _, text, _ in self.sent]


class FakeClaude:
    def __init__(self, reply: Reply | None = None, error: Exception | None = None):
        self.reply = reply if reply is not None else Reply(text="respuesta", model="claude-opus-5")
        self.error = error
        self.calls: list[dict] = []

    def ask(self, *, model, effort, brief, system, messages, on_delta=None, cancel=None):
        self.calls.append(
            {"model": model, "effort": effort, "brief": brief, "messages": list(messages)}
        )
        if self.error is not None:
            raise self.error
        if on_delta is not None:
            on_delta(self.reply.text)
        return self.reply


@pytest.fixture
def config(tmp_path):
    def build(**overrides):
        env = {
            "TELEGRAM_BOT_TOKEN": "token-de-prueba",
            "TELEGRAM_ALLOWED_USER_IDS": "500",
            "CLAUDEGRAM_STATE_DIR": str(tmp_path / "state"),
            "CLAUDEGRAM_POLL_TIMEOUT_SECONDS": "1",
        }
        env.update(overrides)
        return Config.from_env(env)

    return build


@pytest.fixture
def make_bot(config):
    from claudegram.app import Bot

    def build(claude: FakeClaude | None = None, telegram: FakeTelegram | None = None, **env):
        telegram = telegram or FakeTelegram()
        claude = claude or FakeClaude()
        bot = Bot(config(**env), telegram=telegram, claude=claude)
        return bot, telegram, claude

    return build


def message(text="hola", *, chat_id=500, user_id=500, update_id=1, **extra):
    payload = {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": chat_id},
            "from": {"id": user_id, "username": "andres"},
            "text": text,
            **extra,
        },
    }
    if text is None:
        payload["message"].pop("text")
    return payload
