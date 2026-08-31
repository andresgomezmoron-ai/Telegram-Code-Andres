"""A very small Telegram Bot API client (stdlib only).

Long polling, so the bot needs no public IP, no domain, no certificate and no
inbound firewall rule — it only makes outbound HTTPS calls. That is what makes
it deployable on any cheap VPS in a couple of minutes.
"""

from __future__ import annotations

import json
import logging
import random
import socket
import time
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"


class TelegramError(RuntimeError):
    """A non-ok response from the Bot API."""

    def __init__(self, description: str, error_code: int | None = None, retry_after: int | None = None):
        super().__init__(description)
        self.description = description
        self.error_code = error_code
        self.retry_after = retry_after

    @property
    def is_parse_error(self) -> bool:
        """True when Telegram rejected our HTML, so we can resend plain text."""
        text = self.description.lower()
        return "can't parse entities" in text or "unsupported start tag" in text

    @property
    def is_not_modified(self) -> bool:
        return "message is not modified" in self.description.lower()


class TelegramClient:
    def __init__(self, token: str, *, timeout: float = 30.0, retries: int = 4):
        self._token = token
        self._timeout = timeout
        self._retries = retries

    # -- plumbing ---------------------------------------------------------

    def _call(self, method: str, payload: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        url = f"{API_ROOT}/bot{self._token}/{method}"
        body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        deadline = timeout if timeout is not None else self._timeout
        last: Exception | None = None

        for attempt in range(self._retries):
            request = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=deadline + 10) as response:
                    parsed = json.loads(response.read().decode("utf-8"))
                if parsed.get("ok"):
                    return parsed.get("result")
                raise _error_from(parsed)
            except urllib.error.HTTPError as exc:
                parsed = _safe_json(exc)
                error = _error_from(parsed) if parsed else TelegramError(str(exc), exc.code)
                if error.error_code == 429 or (error.error_code or 0) >= 500:
                    last = error
                    time.sleep(error.retry_after + 1 if error.retry_after else _backoff(attempt))
                    continue
                raise error from exc
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, json.JSONDecodeError) as exc:
                last = exc
                log.warning("Telegram %s falló (%s); reintento %d", method, exc, attempt + 1)
                time.sleep(_backoff(attempt))

        raise TelegramError(f"Telegram no responde tras {self._retries} intentos: {last}")

    # -- API surface ------------------------------------------------------

    def get_me(self) -> dict[str, Any]:
        return self._call("getMe")

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "edited_message"],
        }
        if offset is not None:
            payload["offset"] = offset
        return self._call("getUpdates", payload, timeout=timeout) or []

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> int:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
            payload["allow_sending_without_reply"] = True
        result = self._call("sendMessage", payload)
        return int(result["message_id"])

    def edit_message_text(
        self, chat_id: int, message_id: int, text: str, *, parse_mode: str | None = None
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            self._call("editMessageText", payload)
        except TelegramError as exc:
            if not exc.is_not_modified:
                raise

    def delete_message(self, chat_id: int, message_id: int) -> None:
        self._call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        try:
            self._call("sendChatAction", {"chat_id": chat_id, "action": action})
        except TelegramError as exc:  # cosmetic only, never worth failing a turn
            log.debug("sendChatAction falló: %s", exc)

    def set_my_commands(self, commands: list[tuple[str, str]]) -> None:
        payload = {"commands": [{"command": name, "description": desc} for name, desc in commands]}
        try:
            self._call("setMyCommands", payload)
        except TelegramError as exc:
            log.warning("No se pudo registrar el menú de comandos: %s", exc)

    def delete_webhook(self, *, drop_pending: bool = False) -> None:
        """Long polling and webhooks are mutually exclusive; make sure we win."""
        try:
            self._call("deleteWebhook", {"drop_pending_updates": drop_pending})
        except TelegramError as exc:
            log.warning("No se pudo borrar el webhook: %s", exc)


def _error_from(parsed: dict[str, Any]) -> TelegramError:
    parameters = parsed.get("parameters") or {}
    return TelegramError(
        str(parsed.get("description") or "error desconocido"),
        parsed.get("error_code"),
        parameters.get("retry_after"),
    )


def _safe_json(exc: urllib.error.HTTPError) -> dict[str, Any] | None:
    try:
        return json.loads(exc.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - the body is best effort
        return None


def _backoff(attempt: int) -> float:
    return min(2.0 ** attempt, 30.0) + random.uniform(0, 0.5)
