# Claude por Telegram ✈️

Un bot de Telegram que habla con la API de Claude. Está pensado para un caso
concreto: **poder usar Claude desde el avión**, donde el wifi de a bordo suele
ser de solo mensajería, lentísimo y a veces de pago por megabyte.

Funciona porque todo el trabajo pasa en un servidor tuyo, en tierra:

```
  tu móvil  ──── texto ────►  Telegram  ────►  tu VPS (este bot)  ────►  API de Claude
   (avión)   ◄─── texto ────            ◄────                    ◄────
```

El móvil solo manda y recibe texto plano. Ni la app de Claude, ni claude.ai,
ni nada que necesite internet abierto: si Telegram entra, Claude entra.

## Qué sabe hacer

- **Conversación con memoria.** El historial vive en el servidor: si el wifi se
  corta o el bot se reinicia a mitad de vuelo, el hilo sigue ahí.
- **Respuestas troceadas** al límite de 4096 caracteres de Telegram, sin partir
  nunca un bloque de código por la mitad.
- **Código legible en el móvil**: el Markdown de Claude se convierte al HTML que
  entiende Telegram. Si algo no se puede convertir, se manda en texto plano en
  vez de perder la respuesta.
- **Cambio de modelo y de esfuerzo en caliente** (`/modelo haiku` cuando la
  conexión va fatal, `/esfuerzo max` cuando la pregunta es dura).
- **Modo breve** (`/breve`) para respuestas de 60 palabras: menos datos, menos
  espera.
- **Cortar una respuesta** a mitad con `/parar`.
- **Solo tú.** Lista blanca de ids de Telegram; a cualquier otro no le contesta.

## Lo que necesitas

1. Un **bot de Telegram**: habla con [@BotFather](https://t.me/BotFather),
   `/newbot`, y te da un token.
2. Una **clave de la API de Anthropic**: <https://console.anthropic.com> →
   *API keys*. Ojo: esto se paga por tokens, no va con la suscripción de
   claude.ai.
3. Un **servidor encendido mientras vuelas**. Cualquier VPS de 1 GB vale y va
   sobrado — AlphaVPS, Hetzner, Contabo, un Raspberry Pi en casa… El bot usa
   *long polling*, así que **no necesita IP pública, ni dominio, ni certificado,
   ni abrir un solo puerto**: solo salidas HTTPS.

## Instalación en el VPS (Debian/Ubuntu)

Entra por SSH a tu servidor y:

```bash
git clone https://github.com/andresgomezmoron-ai/Telegram-Code-Andres.git
sudo bash Telegram-Code-Andres/deploy/install-vps.sh
```

Instala en `/opt/claudegram`, crea un usuario de sistema sin shell, monta el
entorno virtual y deja el servicio de systemd listo. Después:

```bash
sudo nano /opt/claudegram/.env          # token, tu id y la clave de la API
sudo -u claudegram /opt/claudegram/.venv/bin/python -m claudegram --check
sudo systemctl start claudegram
journalctl -u claudegram -f             # ver los logs
```

`--check` comprueba las tres cosas que suelen fallar (token de Telegram, clave
de Anthropic, modelo disponible) **sin gastar tokens**. Úsalo siempre antes de
un viaje.

<details>
<summary>Paso a paso, si prefieres hacerlo a mano</summary>

```bash
sudo apt update && sudo apt install -y python3-venv git
sudo git clone https://github.com/andresgomezmoron-ai/Telegram-Code-Andres.git /opt/claudegram
cd /opt/claudegram
sudo python3 -m venv .venv
sudo .venv/bin/pip install -r requirements.txt
sudo cp .env.example .env && sudo chmod 600 .env
sudo nano .env
sudo cp deploy/claudegram.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now claudegram
```
</details>

### ¿Tu id de Telegram? (huevo y gallina, en dos pasos)

El bot no arranca sin lista blanca, pero tú todavía no sabes tu número. Se
resuelve así:

1. Deja `TELEGRAM_ALLOWED_USER_IDS` con el valor de ejemplo y arranca el bot.
2. Escríbele `/id` por Telegram: ese comando **contesta a cualquiera**, justo
   para esto, y te da tu número.
3. Pon ese número en `.env` y `sudo systemctl restart claudegram`.

Desde ese momento, a cualquier otro le dice que el bot es privado y no gasta ni
un token de tu API.

### Con Docker

```bash
cp .env.example .env && nano .env
docker compose up -d
docker compose logs -f
```

## Comandos

| Comando | Para qué |
|---|---|
| `/nuevo` | Empieza una conversación desde cero (la anterior se archiva en disco) |
| `/breve` | Respuestas de 60 palabras o menos. Se vuelve a pulsar para desactivar |
| `/modelo` | Sin argumento, enseña las opciones. `/modelo haiku`, `/modelo opus`… |
| `/esfuerzo` | `low`, `medium`, `high`, `xhigh`, `max` |
| `/parar` | Corta la respuesta que se está generando |
| `/estado` | Modelo, tamaño del historial, tokens y gasto aproximado |
| `/ping` | ¿Sigues vivo? Útil para saber si el problema es el avión o el bot |
| `/ayuda` | Resumen de todo esto |
| `/id` | Tu id de Telegram |

Los comandos se registran en el menú de Telegram, así que en el móvil te salen
autocompletados: escribes `/` y eliges.

## Antes del vuelo (30 segundos)

- [ ] `journalctl -u claudegram -n 20` o `/ping` desde el móvil: el bot está vivo.
- [ ] Comprueba el **saldo de la API** en la consola de Anthropic. Un bot que no
      contesta a 10.000 metros por una tarjeta caducada da mucha rabia.
- [ ] Mira si el plan gratuito de mensajería de tu aerolínea **incluye Telegram**.
      Algunas solo dejan pasar WhatsApp e iMessage; en ese caso necesitarás el
      plan de internet completo (y entonces esto también funciona, y gastando
      muchos menos datos que la web de Claude).
- [ ] En Telegram: **Ajustes → Datos y almacenamiento → descarga automática
      desactivada**. Da igual para el bot, pero evita que el chat de tu grupo
      familiar se coma la cuota.
- [ ] Si vas a preguntar sobre algún código, mándatelo **antes de despegar**:
      pegar 500 líneas por un wifi de avión es sufrir.

## En el aire

- Si va lento: `/modelo haiku` y `/breve`. Haiku responde en un par de segundos
  y gasta cinco veces menos.
- Si la pregunta es de verdad difícil: `/modelo opus` y `/esfuerzo xhigh`, y ten
  paciencia — puede tardar un minuto largo. El indicador de «escribiendo…» te
  dice que sigue trabajando.
- Si el mensaje se queda a medias, no lo repitas a lo loco: `/estado` te dice si
  el turno se llegó a guardar.
- `CLAUDEGRAM_STREAM_EDITS=false` (el valor por defecto) manda **un solo mensaje** con la
  respuesta acabada. Ponerlo a `true` es más bonito —ves el texto escribirse—
  pero son varias idas y venidas por la conexión del avión.

## Configuración

Todo se controla con variables de entorno (ver [`.env.example`](.env.example)):

| Variable | Por defecto | Qué hace |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Token de @BotFather (obligatorio) |
| `TELEGRAM_ALLOWED_USER_IDS` | — | Ids que pueden usarlo, separados por comas (obligatorio) |
| `ANTHROPIC_API_KEY` | — | Clave de la API (obligatorio) |
| `CLAUDEGRAM_MODEL` | `claude-opus-5` | Modelo inicial |
| `CLAUDEGRAM_EFFORT` | `medium` | Esfuerzo de razonamiento inicial |
| `CLAUDEGRAM_MAX_TOKENS` | `8000` | Tope por respuesta (incluye el razonamiento) |
| `CLAUDEGRAM_STATE_DIR` | `./state` | Dónde se guardan las conversaciones |
| `CLAUDEGRAM_HISTORY_MAX_MESSAGES` | `60` | Mensajes que se reenvían como contexto |
| `CLAUDEGRAM_HISTORY_MAX_CHARS` | `400000` | Tope de caracteres del historial |
| `CLAUDEGRAM_STREAM_EDITS` | `false` | Ir editando el mensaje mientras se genera |
| `CLAUDEGRAM_EDIT_INTERVAL_SECONDS` | `4` | Cada cuánto se edita, si lo anterior está activo |
| `CLAUDEGRAM_TIMEOUT_SECONDS` | `600` | Tope de espera de una respuesta de la API |
| `CLAUDEGRAM_POLL_TIMEOUT_SECONDS` | `30` | Duración de cada long poll |
| `CLAUDEGRAM_REFUSAL_FALLBACKS` | `true` | Si Claude declina, la API reintenta en otro modelo |
| `CLAUDEGRAM_DROP_PENDING_ON_START` | `false` | Ignorar los mensajes recibidos mientras estaba parado |
| `CLAUDEGRAM_SYSTEM_PROMPT` | (ver `config.py`) | Instrucciones del sistema |
| `CLAUDEGRAM_LOG_LEVEL` | `INFO` | `DEBUG` para ver cada petición |

## Cuánto cuesta

Se paga por tokens. Una conversación normal de vuelo —30 o 40 mensajes— sale
por **céntimos**, no euros: `/estado` te da el acumulado de la conversación
actual. Tres cosas ayudan:

- El historial se cachea (*prompt caching*), así que reenviar la conversación en
  cada turno cuesta ~10 veces menos que la primera vez.
- `/modelo haiku` es 5 veces más barato que Opus para preguntas fáciles.
- `/breve` recorta la parte cara, que es la salida.

## Seguridad

- **Lista blanca obligatoria.** Sin `TELEGRAM_ALLOWED_USER_IDS` el bot no
  arranca. A los desconocidos les contesta una vez cada diez minutos y nada más.
- **No abre ningún puerto.** Solo hace peticiones salientes.
- El servicio de systemd corre como usuario sin shell, con `ProtectSystem=strict`
  y permiso de escritura únicamente en `state/`.
- `.env` está en el `.gitignore` y el instalador lo deja en modo `600`.
- Las conversaciones se guardan **en claro** en `state/` (es tu servidor). Si eso
  te preocupa, cifra el disco o borra la carpeta de vez en cuando.

## Desarrollo

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest          # 66 tests, sin tocar la red
.venv/bin/python -m claudegram --check
```

Cuatro módulos, sin marcos de trabajo por medio:

| Archivo | Responsabilidad |
|---|---|
| `claudegram/telegram.py` | Cliente del Bot API con solo la librería estándar |
| `claudegram/claude.py` | Llamadas a la API de Claude (streaming, errores, cancelación) |
| `claudegram/formatting.py` | Markdown → HTML de Telegram y troceado de mensajes |
| `claudegram/sessions.py` | Historial por chat, en JSON y con escritura atómica |
| `claudegram/app.py` | Bucle de polling, comandos y un hilo por conversación |
| `claudegram/config.py` | Variables de entorno y qué acepta cada modelo |

## Limitaciones

- **Solo texto.** Ni fotos, ni audios, ni documentos. En el aire da igual: los
  planes de mensajería suelen bloquear justo eso.
- **Sin herramientas**: Claude no navega, ni ejecuta código, ni lee tus
  archivos. Responde de memoria. Era el objetivo: pocas piezas y ningún riesgo.
- Un mensaje muy largo se manda troceado, y Telegram puede reordenar los trozos
  si la conexión es muy mala (van con un cuarto de segundo de separación para
  que no pase).
