"""Per-chat conversation state, persisted to disk.

The conversation lives on the server, not on the phone: if the plane's wifi
drops, or the bot restarts mid-flight, the thread is still there. Each chat is
one small JSON file written atomically, which is plenty for a personal bot and
survives a `kill -9` without corrupting anything.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import EFFORTS, MODELS_BY_ID

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


@dataclass
class Session:
    chat_id: int
    model: str
    effort: str
    brief: bool = False
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    turns: int = 0
    tokens: dict[str, int] = field(
        default_factory=lambda: {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    )

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_assistant(self, text: str) -> None:
        self.messages.append({"role": "assistant", "content": text})

    def drop_last_user(self) -> None:
        """Undo the pending user turn when the request failed outright."""
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages.pop()

    def record_usage(self, usage: dict[str, int]) -> None:
        for key, value in usage.items():
            if key in self.tokens:
                self.tokens[key] += int(value or 0)
        self.turns += 1

    @property
    def size_chars(self) -> int:
        return sum(len(str(message.get("content", ""))) for message in self.messages)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": SCHEMA_VERSION,
            "chat_id": self.chat_id,
            "model": self.model,
            "effort": self.effort,
            "brief": self.brief,
            "messages": self.messages,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "turns": self.turns,
            "tokens": self.tokens,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any], *, default_model: str, default_effort: str) -> "Session":
        tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        tokens.update({k: int(v) for k, v in (data.get("tokens") or {}).items() if k in tokens})
        # A model or effort we no longer know about (hand-edited file, older
        # version) must not crash the chat: fall back to the configured default.
        model = str(data.get("model") or default_model)
        effort = str(data.get("effort") or default_effort)
        return cls(
            chat_id=int(data["chat_id"]),
            model=model if model in MODELS_BY_ID else default_model,
            effort=effort if effort in EFFORTS else default_effort,
            brief=bool(data.get("brief", False)),
            messages=[m for m in data.get("messages", []) if m.get("role") in ("user", "assistant")],
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
            turns=int(data.get("turns") or 0),
            tokens=tokens,
        )


class SessionStore:
    def __init__(self, state_dir: Path, *, default_model: str, default_effort: str,
                 max_messages: int, max_chars: int):
        self.state_dir = Path(state_dir)
        self.chats_dir = self.state_dir / "chats"
        self.archive_dir = self.state_dir / "archive"
        self.default_model = default_model
        self.default_effort = default_effort
        self.max_messages = max_messages
        self.max_chars = max_chars
        self._cache: dict[int, Session] = {}
        self._lock = threading.Lock()
        self.chats_dir.mkdir(parents=True, exist_ok=True)

    def get(self, chat_id: int) -> Session:
        with self._lock:
            session = self._cache.get(chat_id)
            if session is not None:
                return session
            session = self._load(chat_id) or Session(
                chat_id=chat_id, model=self.default_model, effort=self.default_effort
            )
            self._cache[chat_id] = session
            return session

    def save(self, session: Session) -> None:
        self.trim(session)
        session.updated_at = time.time()
        _write_json(self._path(chat_id=session.chat_id), session.to_json())

    def reset(self, chat_id: int) -> int:
        """Start a fresh conversation, keeping a copy of the old one on disk."""
        session = self.get(chat_id)
        archived = len(session.messages)
        if archived:
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(session.updated_at))
            _write_json(self.archive_dir / f"{chat_id}-{stamp}.json", session.to_json())
        session.messages = []
        session.turns = 0
        session.tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        session.created_at = time.time()
        self.save(session)
        return archived

    def trim(self, session: Session) -> None:
        """Keep the tail of the conversation within the configured limits."""
        messages = session.messages
        while len(messages) > self.max_messages or (
            len(messages) > 2 and session.size_chars > self.max_chars
        ):
            messages.pop(0)
        # The API requires the history to start with a user turn.
        while messages and messages[0]["role"] != "user":
            messages.pop(0)

    # -- update offset ----------------------------------------------------

    def load_offset(self) -> int | None:
        path = self.state_dir / "offset.json"
        try:
            return int(json.loads(path.read_text(encoding="utf-8"))["offset"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save_offset(self, offset: int) -> None:
        _write_json(self.state_dir / "offset.json", {"offset": int(offset)})

    # -- internals --------------------------------------------------------

    def _path(self, chat_id: int) -> Path:
        return self.chats_dir / f"{chat_id}.json"

    def _load(self, chat_id: int) -> Session | None:
        path = self._path(chat_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Session.from_json(
                data, default_model=self.default_model, default_effort=self.default_effort
            )
        except (OSError, ValueError, KeyError) as exc:
            log.warning("No se pudo leer %s (%s); empiezo conversación nueva", path, exc)
            return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
