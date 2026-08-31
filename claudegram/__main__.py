"""Entry point: `python -m claudegram [--setup|--check]`."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from . import __version__
from .app import Bot
from .claude import ClaudeClient, ClaudeError
from .config import Config, ConfigError, load_dotenv_file, update_env_file
from .telegram import TelegramClient, TelegramError

PAIRING_TIMEOUT = 300.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="claudegram",
        description="Habla con Claude desde Telegram, también a 10.000 metros.",
    )
    parser.add_argument("--setup", action="store_true",
                        help="Configuración guiada: token, clave de la API y tu id de Telegram")
    parser.add_argument("--check", action="store_true",
                        help="Comprueba el token, la clave y el modelo, y sale")
    parser.add_argument("--env-file", default=".env",
                        help="Archivo con las variables de entorno (por defecto: .env)")
    parser.add_argument("--version", action="version", version=f"claudegram {__version__}")
    args = parser.parse_args(argv)

    load_dotenv_file(args.env_file)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # The guided setup runs before the config is validated: its whole job is to
    # fill in what is still missing.
    if args.setup:
        logging.getLogger().setLevel(logging.ERROR)
        try:
            return _setup(args.env_file)
        except KeyboardInterrupt:
            print("\nCancelado. Puedes repetirlo con --setup cuando quieras.")
            return 130

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Configuración incorrecta: {exc}", file=sys.stderr)
        print("Prueba con: python -m claudegram --setup", file=sys.stderr)
        return 2

    logging.getLogger().setLevel(config.log_level)
    logging.getLogger("claudegram").setLevel(config.log_level)

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        logging.warning("No hay ANTHROPIC_API_KEY en el entorno; las respuestas fallarán.")

    if args.check:
        return _check(config)

    try:
        Bot(config).run()
    except KeyboardInterrupt:
        return 130
    return 0


# -- guided setup ---------------------------------------------------------


def _setup(env_file: str) -> int:
    print("Configuración de claudegram")
    print("===========================\n")

    token = _ensure_token(env_file)
    if token is None:
        return 1
    if not _ensure_api_key(env_file):
        return 1
    if not _ensure_owner(env_file, TelegramClient(token, timeout=35)):
        return 1

    print("\n✓ Todo listo. Arranca el bot con:")
    print("    sudo systemctl restart claudegram\n")
    return 0


def _ensure_token(env_file: str) -> str | None:
    """Keep asking for a bot token until Telegram accepts one."""
    for attempt in range(3):
        token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        if token and not token.startswith("123456789:"):
            try:
                me = TelegramClient(token, timeout=15).get_me()
            except TelegramError as exc:
                print(f"✗ Telegram rechaza ese token: {exc}\n")
            else:
                print(f"✓ Bot conectado: @{me.get('username')}")
                return token
        if attempt == 2:
            break
        print("Token del bot. Lo tienes en el mensaje de @BotFather,")
        print("es de la forma 8634000253:AAHv5-Hy...\n")
        answer = _ask("  Pégalo aquí: ")
        if not answer:
            print("✗ Sin token no hay bot.")
            return None
        update_env_file(env_file, {"TELEGRAM_BOT_TOKEN": answer})
        os.environ["TELEGRAM_BOT_TOKEN"] = answer
    print("✗ Demasiados intentos con el token.")
    return None


def _ensure_api_key(env_file: str) -> bool:
    """Same idea for the Anthropic key: validated against the real API."""
    for attempt in range(3):
        key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        if key and not key.endswith("..."):
            config = _draft_config()
            try:
                name = ClaudeClient(config).check(config.model)
            except ClaudeError as exc:
                print(f"✗ {exc}\n")
            else:
                print(f"✓ API de Claude: {config.model} disponible ({name})")
                return True
        if attempt == 2:
            break
        print("\nClave de la API de Anthropic (console.anthropic.com → API keys).")
        print("Empieza por sk-ant- y necesita saldo en Billing.\n")
        answer = _ask("  Pégala aquí: ")
        if not answer:
            print("✗ Sin clave no hay respuestas.")
            return False
        update_env_file(env_file, {"ANTHROPIC_API_KEY": answer})
        os.environ["ANTHROPIC_API_KEY"] = answer
    print("✗ Demasiados intentos con la clave.")
    return False


def _ensure_owner(env_file: str, telegram: TelegramClient) -> bool:
    """Learn the owner's Telegram id from the first message they send."""
    try:
        me = telegram.get_me()
    except TelegramError as exc:
        print(f"✗ No puedo hablar con Telegram: {exc}")
        return False

    print(f"\nÚltimo paso: abre https://t.me/{me.get('username')} en el móvil")
    print("y escríbele cualquier cosa (un «hola» vale).\n")
    print("Esperando tu mensaje… (Ctrl+C para cancelar)")

    offset: int | None = None
    deadline = time.time() + PAIRING_TIMEOUT
    while time.time() < deadline:
        try:
            updates = telegram.get_updates(offset, 20)
        except TelegramError as exc:
            if exc.error_code == 409:
                print("\n✗ El bot ya está en marcha y me quita los mensajes.")
                print("  Párralo y repite:  sudo systemctl stop claudegram")
                return False
            print(f"\n✗ Telegram: {exc}")
            return False

        for update in updates:
            offset = int(update["update_id"]) + 1
            message = update.get("message") or update.get("edited_message")
            sender = (message or {}).get("from") or {}
            user_id = sender.get("id")
            if not user_id:
                continue
            # Acknowledge, so the bot does not answer this message later on.
            try:
                telegram.get_updates(offset, 0)
            except TelegramError:
                pass
            update_env_file(env_file, {"TELEGRAM_ALLOWED_USER_IDS": str(user_id)})
            os.environ["TELEGRAM_ALLOWED_USER_IDS"] = str(user_id)
            who = sender.get("username") or sender.get("first_name") or user_id
            print(f"\n✓ Autorizado: {who} (id {user_id})")
            print("  A partir de ahora el bot solo te contesta a ti.")
            return True

    print("\n✗ No ha llegado ningún mensaje en 5 minutos.")
    return False


def _draft_config() -> Config:
    """A Config good enough to talk to the API while setup is still running."""
    env = dict(os.environ)
    env.setdefault("TELEGRAM_BOT_TOKEN", "pendiente")
    if not (env.get("TELEGRAM_ALLOWED_USER_IDS") or "").strip():
        env["TELEGRAM_ALLOWED_USER_IDS"] = "0"
    return Config.from_env(env)


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


# -- check ----------------------------------------------------------------


def _check(config: Config) -> int:
    ok = True

    try:
        me = TelegramClient(config.telegram_token, timeout=15).get_me()
        print(f"✓ Telegram: conectado como @{me.get('username')} (id {me.get('id')})")
    except TelegramError as exc:
        print(f"✗ Telegram: {exc}", file=sys.stderr)
        ok = False

    try:
        name = ClaudeClient(config).check(config.model)
        print(f"✓ Claude: {config.model} disponible ({name})")
    except ClaudeError as exc:
        print(f"✗ Claude: {exc}", file=sys.stderr)
        ok = False

    who = "cualquiera (¡ojo!)" if config.allow_everyone else ", ".join(
        str(i) for i in sorted(config.allowed_user_ids)
    )
    print(f"· Usuarios autorizados: {who}")
    print(f"· Esfuerzo: {config.effort} · max_tokens: {config.max_tokens}")
    print(f"· Estado en: {config.state_dir.resolve()}")
    print(f"· Respuestas en vivo (CLAUDEGRAM_STREAM_EDITS): {'sí' if config.stream_edits else 'no'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
