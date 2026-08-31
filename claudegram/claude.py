"""The Claude side of the bridge.

Streaming is always on: it keeps a long answer from tripping the HTTP timeout,
lets `/parar` interrupt a runaway response, and (optionally) lets us edit the
Telegram message as the text arrives.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import anthropic

from .config import BRIEF_INSTRUCTION, MODELS_BY_ID, Config

log = logging.getLogger(__name__)

# Server-side refusal fallbacks: if a safety classifier declines the request,
# the API re-runs it on a suitable model instead of returning nothing.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeError(RuntimeError):
    """An error already phrased for the person reading it in Telegram."""


@dataclass
class Reply:
    text: str = ""
    model: str = ""
    stop_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    cancelled: bool = False
    refusal: str | None = None

    @property
    def truncated(self) -> bool:
        return self.stop_reason == "max_tokens"


class ClaudeClient:
    def __init__(self, config: Config, client: Any | None = None):
        self._config = config
        self._client = client or anthropic.Anthropic(
            timeout=config.request_timeout,
            max_retries=2,
        )
        self._fallbacks_enabled = config.refusal_fallbacks

    def check(self, model: str) -> str:
        """Validate credentials and the model id without spending tokens."""
        try:
            info = self._client.models.retrieve(model)
        except anthropic.APIError as exc:
            raise ClaudeError(_describe(exc, model)) from exc
        except TypeError as exc:
            raise ClaudeError(_missing_credentials(exc)) from exc
        return getattr(info, "display_name", None) or model

    def ask(
        self,
        *,
        model: str,
        effort: str,
        brief: bool,
        system: str,
        messages: list[dict[str, Any]],
        on_delta: Callable[[str], None] | None = None,
        cancel: Any | None = None,
    ) -> Reply:
        """Stream one assistant turn. Never raises for ordinary API failures."""
        for attempt in (1, 2):
            kwargs = self._build_kwargs(
                model=model, effort=effort, brief=brief, system=system, messages=messages
            )
            try:
                return self._stream(kwargs, on_delta=on_delta, cancel=cancel)
            except anthropic.BadRequestError as exc:
                message = str(getattr(exc, "message", exc))
                retryable = attempt == 1 and self._fallbacks_enabled and _is_beta_problem(message)
                if not retryable:
                    raise ClaudeError(_describe(exc, model)) from exc
                # The account or model does not have the fallback beta; drop it
                # for the rest of the process and try again.
                log.warning("Desactivo los fallbacks por refusal: %s", message)
                self._fallbacks_enabled = False
            except anthropic.APIError as exc:
                raise ClaudeError(_describe(exc, model)) from exc
            except TypeError as exc:
                # The SDK raises this (not an APIError) when no credentials
                # resolve at all — the classic "I forgot the .env" on a VPS.
                raise ClaudeError(_missing_credentials(exc)) from exc
        raise ClaudeError("No he podido completar la petición a la API de Claude.")

    # -- internals --------------------------------------------------------

    def _build_kwargs(
        self, *, model: str, effort: str, brief: bool, system: str, messages: list[dict[str, Any]]
    ) -> dict[str, Any]:
        spec = MODELS_BY_ID[model]
        history: list[dict[str, Any]] = list(messages)

        if brief:
            if spec.supports_mid_conversation_system and history and history[-1]["role"] == "user":
                # Mid-conversation system message: carries operator authority and,
                # unlike editing the system prompt, does not invalidate the cache.
                history.append({"role": "system", "content": BRIEF_INSTRUCTION})
            else:
                system = f"{system}\n\n{BRIEF_INSTRUCTION}"

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": self._config.max_tokens,
            "system": system,
            "messages": history,
            # Auto-cache the longest stable prefix: the conversation so far.
            "cache_control": {"type": "ephemeral"},
        }
        if spec.adaptive_thinking:
            kwargs["thinking"] = {"type": "adaptive"}
        if spec.supports_effort:
            kwargs["output_config"] = {"effort": effort}
        if self._fallbacks_enabled and spec.supports_refusal_fallbacks:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        return kwargs

    def _stream(
        self,
        kwargs: dict[str, Any],
        *,
        on_delta: Callable[[str], None] | None,
        cancel: Any | None,
    ) -> Reply:
        resource = self._client.beta.messages if "betas" in kwargs else self._client.messages
        parts: list[str] = []

        with resource.stream(**kwargs) as stream:
            for event in stream:
                if cancel is not None and cancel.is_set():
                    return Reply(text="".join(parts), model=kwargs["model"], cancelled=True)
                if event.type == "content_block_delta" and event.delta.type == "text_delta":
                    parts.append(event.delta.text)
                    if on_delta is not None:
                        on_delta("".join(parts))
            final = stream.get_final_message()

        return _reply_from(final)


def _reply_from(final: Any) -> Reply:
    reply = Reply(
        text=_text_of(final.content),
        model=getattr(final, "model", ""),
        stop_reason=getattr(final, "stop_reason", None),
        usage=_usage_of(getattr(final, "usage", None)),
    )
    if reply.stop_reason == "refusal":
        details = getattr(final, "stop_details", None)
        reason = getattr(details, "explanation", None) or getattr(details, "category", None)
        reply.refusal = str(reason) if reason else "sin detalle"
    return reply


def _text_of(content: Iterable[Any]) -> str:
    return "".join(block.text for block in content if getattr(block, "type", None) == "text").strip()


def _usage_of(usage: Any) -> dict[str, int]:
    if usage is None:
        return {}
    return {
        "input": int(getattr(usage, "input_tokens", 0) or 0),
        "output": int(getattr(usage, "output_tokens", 0) or 0),
        "cache_read": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
        "cache_write": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
    }


def _missing_credentials(exc: TypeError) -> str:
    if "authentication" in str(exc).lower():
        return (
            "El servidor no tiene credenciales de Anthropic: falta "
            "ANTHROPIC_API_KEY en el .env (y reiniciar el servicio)."
        )
    raise exc


def _is_beta_problem(message: str) -> bool:
    lowered = message.lower()
    return "fallback" in lowered or "beta" in lowered


def _describe(exc: Exception, model: str) -> str:
    """Translate an SDK exception into something useful on a phone."""
    if isinstance(exc, anthropic.AuthenticationError):
        return "La clave ANTHROPIC_API_KEY no es válida o ha caducado."
    if isinstance(exc, anthropic.PermissionDeniedError):
        return f"Tu clave de API no tiene acceso a {model}."
    if isinstance(exc, anthropic.NotFoundError):
        return f"El modelo {model} no existe para esta clave de API."
    if isinstance(exc, anthropic.RateLimitError):
        retry = _retry_after(exc)
        extra = f" Reinténtalo en {retry} s." if retry else " Espera un momento."
        return "Has alcanzado el límite de peticiones de la API." + extra
    if isinstance(exc, anthropic.BadRequestError):
        return f"La API ha rechazado la petición: {getattr(exc, 'message', exc)}"
    if isinstance(exc, anthropic.APITimeoutError):
        return "La API de Claude ha tardado demasiado. Prueba otra vez o usa /modelo haiku."
    if isinstance(exc, anthropic.APIConnectionError):
        return "No he podido conectar con la API de Claude desde el servidor."
    if isinstance(exc, anthropic.APIStatusError):
        code = getattr(exc, "status_code", "?")
        if isinstance(code, int) and code >= 500:
            return f"La API de Claude está caída o saturada (error {code}). Reinténtalo."
        return f"Error {code} de la API de Claude: {getattr(exc, 'message', exc)}"
    return f"Error inesperado hablando con la API de Claude: {exc}"


def _retry_after(exc: Exception) -> str | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        return headers.get("retry-after")
    except Exception:  # noqa: BLE001
        return None
