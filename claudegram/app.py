"""The bot itself: poll Telegram, talk to Claude, send the answer back.

Threading model — one poller plus one worker per chat:

* the poller only touches Telegram, so commands like /parar and /ping answer
  instantly even while a long response is being generated;
* each chat has its own worker thread and queue, so a slow answer in one chat
  never blocks another;
* messages that arrive together are merged into a single turn, which is what
  happens naturally when Telegram splits a long paste into several messages.
"""

from __future__ import annotations

import logging
import queue
import signal
import threading
import time
from typing import Any

from . import __version__
from .claude import ClaudeClient, ClaudeError, Reply
from .config import EFFORTS, MODELS, MODELS_BY_ID, Config, resolve_model
from .formatting import TELEGRAM_LIMIT, Chunk, render, split_plain, strip_tags, telegram_length
from .sessions import Session, SessionStore
from .telegram import TelegramClient, TelegramError

log = logging.getLogger(__name__)

COMMANDS: list[tuple[str, str]] = [
    ("nuevo", "Empieza una conversación desde cero"),
    ("breve", "Respuestas muy cortas (ahorra datos)"),
    ("modelo", "Cambia de modelo: opus, sonnet, haiku, fable"),
    ("esfuerzo", "Razonamiento: low, medium, high, xhigh, max"),
    ("parar", "Corta la respuesta que se está generando"),
    ("estado", "Modelo, historial y gasto aproximado"),
    ("ping", "Comprueba que el bot sigue vivo"),
    ("ayuda", "Qué sabe hacer este bot"),
    ("id", "Tu id de Telegram (para la lista blanca)"),
]

HELP = """\
Soy Claude, conectado a este chat. Escríbeme y ya está: me acuerdo de la \
conversación, así que puedes preguntar cosas encadenadas.

<b>Comandos</b>
/nuevo — olvida la conversación y empieza otra
/breve — respuestas de 60 palabras o menos (y volver a lo normal)
/modelo — sin nada, enseña las opciones; /modelo haiku cambia
/esfuerzo — low, medium, high, xhigh, max
/parar — corta la respuesta en curso
/estado — modelo, tamaño del historial y gasto aproximado
/ping — ¿sigues ahí?

<b>En el avión</b>
Si el wifi es de solo mensajería, esto sigue funcionando porque todo el trabajo \
pasa en el servidor: el móvil solo manda y recibe texto.
Si va muy justo: <code>/modelo haiku</code> y <code>/breve</code>.
"""

UNAUTHORIZED_COOLDOWN = 600.0  # seconds between "no autorizado" replies

MEDIA_KEYS = ("photo", "voice", "audio", "document", "video", "video_note", "sticker", "animation")


class Bot:
    def __init__(self, config: Config, telegram: TelegramClient | None = None,
                 claude: ClaudeClient | None = None):
        self.config = config
        self.telegram = telegram or TelegramClient(config.telegram_token, timeout=config.poll_timeout)
        self.claude = claude or ClaudeClient(config)
        self.store = SessionStore(
            config.state_dir,
            default_model=config.model,
            default_effort=config.effort,
            max_messages=config.history_max_messages,
            max_chars=config.history_max_chars,
        )
        self._stop = threading.Event()
        self._queues: dict[int, queue.Queue] = {}
        self._workers: dict[int, threading.Thread] = {}
        self._cancels: dict[int, threading.Event] = {}
        self._chat_locks: dict[int, threading.RLock] = {}
        self._registry = threading.Lock()
        self._warned: dict[int, float] = {}
        self._started_at = time.time()

    # -- lifecycle --------------------------------------------------------

    def run(self) -> None:
        me = self.telegram.get_me()
        log.info("Conectado como @%s (id %s)", me.get("username"), me.get("id"))
        self.telegram.delete_webhook(drop_pending=self.config.drop_pending_on_start)
        self.telegram.set_my_commands(COMMANDS)

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self._on_signal)
            except ValueError:  # not the main thread (tests)
                pass

        offset = None if self.config.drop_pending_on_start else self.store.load_offset()
        log.info(
            "Escuchando (modelo %s, esfuerzo %s, usuarios %s)",
            self.config.model,
            self.config.effort,
            "cualquiera" if self.config.allow_everyone else sorted(self.config.allowed_user_ids),
        )

        while not self._stop.is_set():
            try:
                updates = self.telegram.get_updates(offset, self.config.poll_timeout)
            except TelegramError as exc:
                log.error("getUpdates: %s", exc)
                self._stop.wait(5.0)
                continue
            for update in updates:
                offset = int(update["update_id"]) + 1
                try:
                    self._dispatch(update)
                except Exception:  # noqa: BLE001 - one bad update must not kill the bot
                    log.exception("Error procesando el update %s", update.get("update_id"))
            if updates and offset is not None:
                self.store.save_offset(offset)

        self._shutdown()

    def _on_signal(self, signum: int, _frame: Any) -> None:
        if self._stop.is_set():  # second Ctrl-C: leave now
            raise KeyboardInterrupt
        log.info("Señal %s recibida, cerrando…", signum)
        self._stop.set()

    def _shutdown(self) -> None:
        with self._registry:
            workers = list(self._workers.items())
            for chat_id, _ in workers:
                self._cancels.setdefault(chat_id, threading.Event()).set()
                self._queues[chat_id].put(None)
        for _, worker in workers:
            worker.join(timeout=10.0)
        log.info("Adiós.")

    # -- update handling --------------------------------------------------

    def _dispatch(self, update: dict[str, Any]) -> None:
        message = update.get("message") or update.get("edited_message")
        if not message:
            return
        chat_id = int(message["chat"]["id"])
        sender = message.get("from") or {}
        user_id = int(sender.get("id", 0))
        text = (message.get("text") or message.get("caption") or "").strip()
        command, argument = _parse_command(text)

        if not self.config.is_allowed(user_id):
            self._handle_stranger(chat_id, user_id, sender, command)
            return

        if not text:
            if any(key in message for key in MEDIA_KEYS):
                self._say(chat_id, "Solo entiendo texto: ni audios, ni fotos, ni archivos.")
            return

        if command:
            self._handle_command(chat_id, user_id, command, argument)
            return

        self._enqueue(chat_id, text)

    def _handle_stranger(self, chat_id: int, user_id: int, sender: dict[str, Any], command: str) -> None:
        log.warning("Mensaje de un usuario no autorizado: id=%s (%s)", user_id, sender.get("username"))
        if command == "id":
            self._say(chat_id, f"Tu id de Telegram es <code>{user_id}</code>.")
            return
        last = self._warned.get(user_id, 0.0)
        if time.time() - last > UNAUTHORIZED_COOLDOWN:
            self._warned[user_id] = time.time()
            self._say(chat_id, f"Este bot es privado. Tu id es <code>{user_id}</code>.")

    def _handle_command(self, chat_id: int, user_id: int, command: str, argument: str) -> None:
        if command == "parar":
            event = self._cancels.get(chat_id)
            if event is not None and not event.is_set():
                event.set()
                self._say(chat_id, "Vale, paro.")
            else:
                self._say(chat_id, "No estaba escribiendo nada.")
            return

        if command == "ping":
            self._say(chat_id, f"pong · llevo {_uptime(time.time() - self._started_at)} en marcha")
            return

        if command in ("ayuda", "help", "start"):
            self._say(chat_id, HELP)
            return

        if command == "id":
            self._say(chat_id, f"Tu id de Telegram es <code>{user_id}</code>.")
            return

        with self._lock_for(chat_id):
            session = self.store.get(chat_id)

            if command == "nuevo":
                archived = self.store.reset(chat_id)
                self._say(
                    chat_id,
                    "Conversación nueva."
                    + (f" (he archivado los {archived} mensajes anteriores)" if archived else ""),
                )
                return

            if command == "breve":
                session.brief = not session.brief
                self.store.save(session)
                self._say(
                    chat_id,
                    "Modo breve <b>activado</b>: respuestas de 60 palabras o menos."
                    if session.brief
                    else "Modo breve <b>desactivado</b>.",
                )
                return

            if command == "modelo":
                if not argument:
                    self._say(chat_id, _model_menu(session.model))
                    return
                spec = resolve_model(argument)
                if spec is None:
                    self._say(chat_id, f"No conozco «{argument}».\n\n" + _model_menu(session.model))
                    return
                session.model = spec.id
                self.store.save(session)
                note = "" if spec.supports_effort else "\nEste modelo no usa /esfuerzo."
                self._say(chat_id, f"Ahora uso <b>{spec.id}</b>.{note}")
                return

            if command == "esfuerzo":
                spec = MODELS_BY_ID[session.model]
                if not spec.supports_effort:
                    self._say(chat_id, f"{session.model} no admite niveles de esfuerzo.")
                    return
                if argument.lower() not in EFFORTS:
                    self._say(
                        chat_id,
                        f"Esfuerzo actual: <b>{session.effort}</b>.\nOpciones: "
                        + ", ".join(EFFORTS)
                        + "\nMás esfuerzo = mejores respuestas, más lentas y más caras.",
                    )
                    return
                session.effort = argument.lower()
                self.store.save(session)
                self._say(chat_id, f"Esfuerzo: <b>{session.effort}</b>.")
                return

            if command == "estado":
                self._say(chat_id, _status(session, self._started_at))
                return

        self._say(chat_id, f"No conozco /{command}. Prueba /ayuda.")

    # -- worker plumbing --------------------------------------------------

    def _lock_for(self, chat_id: int) -> threading.RLock:
        with self._registry:
            return self._chat_locks.setdefault(chat_id, threading.RLock())

    def _cancel_for(self, chat_id: int) -> threading.Event:
        with self._registry:
            return self._cancels.setdefault(chat_id, threading.Event())

    def _enqueue(self, chat_id: int, text: str) -> None:
        with self._registry:
            self._chat_locks.setdefault(chat_id, threading.RLock())
            self._cancels.setdefault(chat_id, threading.Event())
            work = self._queues.get(chat_id)
            if work is None:
                work = queue.Queue()
                self._queues[chat_id] = work
                worker = threading.Thread(
                    target=self._worker, args=(chat_id,), name=f"chat-{chat_id}", daemon=True
                )
                self._workers[chat_id] = worker
                worker.start()
        work.put(text)

    def _worker(self, chat_id: int) -> None:
        work = self._queues[chat_id]
        while True:
            item = work.get()
            if item is None:
                return
            batch = [item]
            # Telegram splits long pastes into several messages; treat whatever
            # is already queued as one turn.
            while True:
                try:
                    nxt = work.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    return
                batch.append(nxt)
            try:
                self._answer(chat_id, "\n".join(batch))
            except Exception:  # noqa: BLE001
                log.exception("Fallo respondiendo en el chat %s", chat_id)
                self._say(chat_id, "Me he encontrado un error inesperado. Inténtalo otra vez.")

    # -- the actual turn --------------------------------------------------

    def _answer(self, chat_id: int, prompt: str) -> None:
        cancel = self._cancel_for(chat_id)
        cancel.clear()
        lock = self._lock_for(chat_id)

        with lock:
            session = self.store.get(chat_id)
            session.add_user(prompt)
            self.store.save(session)
            model, effort, brief = session.model, session.effort, session.brief
            history = list(session.messages)

        live = _LiveMessage(self.telegram, chat_id, self.config.edit_interval) if self.config.stream_edits else None
        started = time.monotonic()

        with _Typing(self.telegram, chat_id):
            try:
                reply = self.claude.ask(
                    model=model,
                    effort=effort,
                    brief=brief,
                    system=self.config.system_prompt,
                    messages=history,
                    on_delta=live.update if live else None,
                    cancel=cancel,
                )
            except ClaudeError as exc:
                with lock:
                    session = self.store.get(chat_id)
                    session.drop_last_user()
                    self.store.save(session)
                if live:
                    live.discard()
                self._say(chat_id, f"⚠️ {exc}\n\nTu mensaje no se ha guardado; puedes repetirlo.")
                return

        elapsed = time.monotonic() - started
        log.info(
            "chat=%s modelo=%s %.1fs tokens_in=%s tokens_out=%s cache=%s",
            chat_id, model, elapsed, reply.usage.get("input"), reply.usage.get("output"),
            reply.usage.get("cache_read"),
        )
        self._deliver(chat_id, session_lock=lock, reply=reply, live=live)

    def _deliver(self, chat_id: int, *, session_lock: threading.RLock, reply: Reply,
                 live: "_LiveMessage | None") -> None:
        if reply.refusal:
            with session_lock:
                session = self.store.get(chat_id)
                session.drop_last_user()
                self.store.save(session)
            if live:
                live.discard()
            self._say(chat_id, f"Claude ha declinado responder a eso ({reply.refusal}).")
            return

        text = reply.text.strip()
        if not text:
            if live:
                live.discard()
            note = "He parado sin escribir nada." if reply.cancelled else "La respuesta ha llegado vacía."
            self._say(chat_id, note)
            return

        with session_lock:
            session = self.store.get(chat_id)
            session.add_assistant(text)
            if reply.usage:
                session.record_usage(reply.usage)
            self.store.save(session)

        chunks = render(text)
        if reply.cancelled:
            chunks.append(Chunk(html="<i>— parado a mitad —</i>", plain="— parado a mitad —"))
        elif reply.truncated:
            note = (
                f"⚠️ Cortado en el límite de {self.config.max_tokens} tokens. "
                "Dime «sigue» y continúo."
            )
            chunks.append(Chunk(html=note, plain=note))

        first = 0
        if live is not None and live.message_id is not None and chunks:
            live.finish(chunks[0])
            first = 1
        for chunk in chunks[first:]:
            self._send_chunk(chat_id, chunk)
            time.sleep(0.25)  # keep the order and stay clear of flood limits

    def _send_chunk(self, chat_id: int, chunk: Chunk) -> None:
        try:
            self.telegram.send_message(chat_id, chunk.html, parse_mode="HTML")
        except TelegramError as exc:
            if not exc.is_parse_error:
                log.error("No he podido enviar un trozo al chat %s: %s", chat_id, exc)
                return
            log.warning("Telegram ha rechazado el HTML (%s); mando texto plano", exc)
            try:
                self.telegram.send_message(chat_id, chunk.plain)
            except TelegramError as inner:
                log.error("Tampoco ha entrado en texto plano: %s", inner)

    def _say(self, chat_id: int, html: str) -> None:
        """Send one of our own messages, which is already Telegram HTML."""
        for piece in split_plain(html, TELEGRAM_LIMIT):
            try:
                self.telegram.send_message(chat_id, piece, parse_mode="HTML")
            except TelegramError as exc:
                if exc.is_parse_error:
                    self.telegram.send_message(chat_id, strip_tags(piece))
                else:
                    log.error("sendMessage: %s", exc)


class _Typing:
    """Keep the 'typing…' bubble alive while Claude thinks."""

    def __init__(self, telegram: TelegramClient, chat_id: int, interval: float = 4.0):
        self._telegram = telegram
        self._chat_id = chat_id
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "_Typing":
        self._thread = threading.Thread(target=self._loop, name="typing", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._telegram.send_chat_action(self._chat_id, "typing")
            self._stop.wait(self._interval)


class _LiveMessage:
    """Optional: show the answer as it is written, by editing one message.

    Off by default. Every edit is another round trip over the plane's link, so
    it is nicer to watch but costs more data than a single final message.
    """

    def __init__(self, telegram: TelegramClient, chat_id: int, interval: float):
        self._telegram = telegram
        self._chat_id = chat_id
        self._interval = interval
        self._last = 0.0
        self._shown = ""
        self.message_id: int | None = None

    def update(self, text: str) -> None:
        now = time.monotonic()
        if now - self._last < self._interval or not text.strip():
            return
        self._last = now
        preview = text if telegram_length(text) <= 3900 else text[:3900] + "…"
        if preview == self._shown:
            return
        self._shown = preview
        try:
            if self.message_id is None:
                self.message_id = self._telegram.send_message(self._chat_id, preview)
            else:
                self._telegram.edit_message_text(self._chat_id, self.message_id, preview)
        except TelegramError as exc:
            log.debug("No he podido actualizar el mensaje en vivo: %s", exc)

    def finish(self, chunk: Chunk) -> None:
        if self.message_id is None:
            return
        try:
            self._telegram.edit_message_text(
                self._chat_id, self.message_id, chunk.html, parse_mode="HTML"
            )
        except TelegramError as exc:
            if exc.is_parse_error:
                self._telegram.edit_message_text(self._chat_id, self.message_id, chunk.plain)
            else:
                log.error("No he podido cerrar el mensaje en vivo: %s", exc)

    def discard(self) -> None:
        """Nothing useful came out: take the placeholder off the chat."""
        if self.message_id is None:
            return
        try:
            self._telegram.delete_message(self._chat_id, self.message_id)
        except TelegramError:
            try:
                self._telegram.edit_message_text(self._chat_id, self.message_id, "…")
            except TelegramError:
                pass


def _parse_command(text: str) -> tuple[str, str]:
    if not text.startswith("/"):
        return "", ""
    head, _, rest = text.partition(" ")
    command = head[1:].split("@", 1)[0].lower()
    return command, rest.strip()


def _model_menu(current: str) -> str:
    lines = ["<b>Modelos</b> (ahora: <code>%s</code>)" % current]
    for spec in MODELS:
        mark = "▸" if spec.id == current else "·"
        lines.append(f"{mark} <code>/modelo {spec.alias}</code> — {spec.label}")
    return "\n".join(lines)


def _status(session: Session, started_at: float) -> str:
    spec = MODELS_BY_ID[session.model]
    tokens = session.tokens
    cost = (
        tokens["input"] * spec.price_in
        + tokens["cache_write"] * spec.price_in * 1.25
        + tokens["cache_read"] * spec.price_in * 0.1
        + tokens["output"] * spec.price_out
    ) / 1_000_000
    return "\n".join(
        [
            f"<b>Modelo</b> {session.model}",
            f"<b>Esfuerzo</b> {session.effort if spec.supports_effort else '—'}"
            + ("  ·  <b>modo breve</b>" if session.brief else ""),
            f"<b>Historial</b> {len(session.messages)} mensajes "
            f"({format(session.size_chars, ',').replace(',', '.')} caracteres)",
            f"<b>Turnos</b> {session.turns}",
            f"<b>Tokens</b> {tokens['input']} entrada · {tokens['output']} salida · "
            f"{tokens['cache_read']} de caché",
            f"<b>Gasto aprox.</b> ${cost:.4f} en esta conversación",
            f"<b>Bot</b> v{__version__}, en marcha desde hace {_uptime(time.time() - started_at)}",
        ]
    )


def _uptime(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes} min"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h"
