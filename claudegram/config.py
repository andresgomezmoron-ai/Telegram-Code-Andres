"""Configuration loaded from the environment.

Everything the bot needs comes from env vars (or a .env file read by
`load_dotenv_file`), so the same code runs from systemd, Docker or a shell.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")


class ConfigError(RuntimeError):
    """Raised when the environment is missing or contradicts itself."""


@dataclass(frozen=True)
class ModelSpec:
    """What a model accepts, so we never send it a parameter it rejects."""

    id: str
    alias: str
    label: str
    # Adaptive thinking: Opus 5 / Sonnet 5 / Fable 5 accept {"type": "adaptive"}.
    # Haiku 4.5 predates it and would 400, so we send no thinking parameter.
    adaptive_thinking: bool
    # output_config.effort errors on Haiku 4.5.
    supports_effort: bool
    # {"role": "system"} entries inside messages[] (Opus 5 / Fable 5 only).
    supports_mid_conversation_system: bool
    # Server-side refusal fallbacks, beta server-side-fallback-2026-07-01.
    supports_refusal_fallbacks: bool
    # USD per million tokens, for the rough spend estimate in /estado.
    price_in: float
    price_out: float


MODELS: tuple[ModelSpec, ...] = (
    ModelSpec(
        id="claude-opus-5",
        alias="opus",
        label="Opus 5 — el mejor, algo más lento ($5/$25 por millón de tokens)",
        adaptive_thinking=True,
        supports_effort=True,
        supports_mid_conversation_system=True,
        supports_refusal_fallbacks=True,
        price_in=5.0,
        price_out=25.0,
    ),
    ModelSpec(
        id="claude-sonnet-5",
        alias="sonnet",
        label="Sonnet 5 — rápido y más barato ($2/$10)",
        adaptive_thinking=True,
        supports_effort=True,
        supports_mid_conversation_system=False,
        supports_refusal_fallbacks=False,
        price_in=2.0,
        price_out=10.0,
    ),
    ModelSpec(
        id="claude-haiku-4-5",
        alias="haiku",
        label="Haiku 4.5 — el más rápido y barato ($1/$5), sin razonamiento",
        adaptive_thinking=False,
        supports_effort=False,
        supports_mid_conversation_system=False,
        supports_refusal_fallbacks=False,
        price_in=1.0,
        price_out=5.0,
    ),
    ModelSpec(
        id="claude-fable-5",
        alias="fable",
        label="Fable 5 — el más capaz, el más caro ($10/$50)",
        adaptive_thinking=True,
        supports_effort=True,
        supports_mid_conversation_system=True,
        supports_refusal_fallbacks=True,
        price_in=10.0,
        price_out=50.0,
    ),
)

MODELS_BY_ID = {spec.id: spec for spec in MODELS}
MODELS_BY_ALIAS = {spec.alias: spec for spec in MODELS}


def resolve_model(name: str) -> ModelSpec | None:
    """Accept either a full model id or a short alias ('opus', 'haiku', ...)."""
    key = name.strip().lower()
    if key in MODELS_BY_ID:
        return MODELS_BY_ID[key]
    return MODELS_BY_ALIAS.get(key)


DEFAULT_SYSTEM_PROMPT = """\
Eres Claude, respondiendo por Telegram. Muchas veces te escriben desde un avión, \
con una conexión de solo mensajería: lenta, con cortes y de pago por megabyte.

Latency-sensitive; begin your visible answer immediately.

<tone_preference>
Responde en el idioma en el que te escriban (por defecto, español).
Ve al grano: la primera frase ya contiene la respuesta. Sin preámbulos, sin \
repetir la pregunta y sin ofrecer ayuda adicional al final.
Párrafos cortos. Listas solo si aportan. Nada de tablas anchas: no se leen en un \
móvil.
Extensión proporcional a la pregunta: una duda breve se responde en dos frases.
En código, manda solo lo que hace falta, en bloques con ``` y el lenguaje.
</tone_preference>

No tienes acceso a internet, ni a archivos, ni a herramientas: respondes de \
memoria. Si algo depende de datos posteriores a tu entrenamiento o de consultar \
una fuente, dilo en una línea en vez de inventarlo.
"""

BRIEF_INSTRUCTION = (
    "Modo breve activado: responde en 60 palabras o menos, sin listas ni código "
    "salvo que sea imprescindible."
)


@dataclass(frozen=True)
class Config:
    telegram_token: str
    allowed_user_ids: frozenset[int]
    allow_everyone: bool
    model: str
    effort: str
    max_tokens: int
    state_dir: Path
    history_max_messages: int
    history_max_chars: int
    system_prompt: str
    stream_edits: bool
    edit_interval: float
    poll_timeout: int
    request_timeout: float
    refusal_fallbacks: bool
    drop_pending_on_start: bool
    log_level: str

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Config":
        env = os.environ if environ is None else environ

        token = _clean(env.get("TELEGRAM_BOT_TOKEN"))
        if not token:
            raise ConfigError(
                "Falta TELEGRAM_BOT_TOKEN. Créalo con @BotFather en Telegram y "
                "ponlo en el archivo .env."
            )

        raw_ids = _clean(env.get("TELEGRAM_ALLOWED_USER_IDS"))
        if not raw_ids:
            raise ConfigError(
                "Falta TELEGRAM_ALLOWED_USER_IDS. Escríbele /id a tu bot para ver "
                "tu id numérico, o pon '*' para permitir a cualquiera (no "
                "recomendado: quien encuentre el bot gastaría tu saldo de la API)."
            )
        allow_everyone = raw_ids.strip() == "*"
        allowed: frozenset[int] = frozenset()
        if not allow_everyone:
            try:
                allowed = frozenset(
                    int(part) for part in raw_ids.replace(";", ",").split(",") if part.strip()
                )
            except ValueError as exc:
                raise ConfigError(
                    "TELEGRAM_ALLOWED_USER_IDS debe ser una lista de números "
                    f"separados por comas (recibido: {raw_ids!r})."
                ) from exc
            if not allowed:
                raise ConfigError("TELEGRAM_ALLOWED_USER_IDS está vacío.")

        model_name = _clean(env.get("CLAUDEGRAM_MODEL")) or "claude-opus-5"
        spec = resolve_model(model_name)
        if spec is None:
            raise ConfigError(
                f"CLAUDEGRAM_MODEL={model_name!r} no está en la lista conocida: "
                + ", ".join(m.id for m in MODELS)
            )

        effort = (_clean(env.get("CLAUDEGRAM_EFFORT")) or "medium").lower()
        if effort not in EFFORTS:
            raise ConfigError(
                f"CLAUDEGRAM_EFFORT={effort!r} no es válido. Opciones: {', '.join(EFFORTS)}."
            )

        system_prompt = _clean(env.get("CLAUDEGRAM_SYSTEM_PROMPT")) or DEFAULT_SYSTEM_PROMPT

        return cls(
            telegram_token=token,
            allowed_user_ids=allowed,
            allow_everyone=allow_everyone,
            model=spec.id,
            effort=effort,
            max_tokens=_env_int(env, "CLAUDEGRAM_MAX_TOKENS", 8000, minimum=256),
            state_dir=Path(_clean(env.get("CLAUDEGRAM_STATE_DIR")) or "./state").expanduser(),
            history_max_messages=_env_int(env, "CLAUDEGRAM_HISTORY_MAX_MESSAGES", 60, minimum=2),
            history_max_chars=_env_int(env, "CLAUDEGRAM_HISTORY_MAX_CHARS", 400_000, minimum=2000),
            system_prompt=system_prompt,
            stream_edits=_env_bool(env, "CLAUDEGRAM_STREAM_EDITS", False),
            edit_interval=_env_float(env, "CLAUDEGRAM_EDIT_INTERVAL_SECONDS", 4.0, minimum=1.0),
            poll_timeout=_env_int(env, "CLAUDEGRAM_POLL_TIMEOUT_SECONDS", 30, minimum=1),
            request_timeout=_env_float(env, "CLAUDEGRAM_TIMEOUT_SECONDS", 600.0, minimum=10.0),
            refusal_fallbacks=_env_bool(env, "CLAUDEGRAM_REFUSAL_FALLBACKS", True),
            drop_pending_on_start=_env_bool(env, "CLAUDEGRAM_DROP_PENDING_ON_START", False),
            log_level=(_clean(env.get("CLAUDEGRAM_LOG_LEVEL")) or "INFO").upper(),
        )

    @property
    def model_spec(self) -> ModelSpec:
        return MODELS_BY_ID[self.model]

    def is_allowed(self, user_id: int) -> bool:
        return self.allow_everyone or user_id in self.allowed_user_ids


def load_dotenv_file(path: str | os.PathLike[str] = ".env") -> int:
    """Load KEY=VALUE lines into os.environ without overwriting what is set.

    Deliberately tiny: no python-dotenv dependency, no interpolation, no export
    keyword. Returns how many variables were set.
    """
    file = Path(path)
    if not file.is_file():
        return 0
    loaded = 0
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _env_bool(env: dict[str, str], name: str, default: bool) -> bool:
    raw = _clean(env.get(name)).lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "y", "on", "si", "sí"):
        return True
    if raw in ("0", "false", "no", "n", "off"):
        return False
    raise ConfigError(f"{name}={raw!r} no es un booleano (usa true/false).")


def _env_int(env: dict[str, str], name: str, default: int, *, minimum: int) -> int:
    raw = _clean(env.get(name))
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} debe ser un número entero.") from exc
    if value < minimum:
        raise ConfigError(f"{name}={value} es demasiado bajo (mínimo {minimum}).")
    return value


def _env_float(env: dict[str, str], name: str, default: float, *, minimum: float) -> float:
    raw = _clean(env.get(name))
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} debe ser un número.") from exc
    if value < minimum:
        raise ConfigError(f"{name}={value} es demasiado bajo (mínimo {minimum}).")
    return value
