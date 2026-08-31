"""Entry point: `python -m claudegram [--check]`."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from . import __version__
from .app import Bot
from .claude import ClaudeClient, ClaudeError
from .config import Config, ConfigError, load_dotenv_file
from .telegram import TelegramClient, TelegramError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="claudegram",
        description="Habla con Claude desde Telegram, también a 10.000 metros.",
    )
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

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"Configuración incorrecta: {exc}", file=sys.stderr)
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
