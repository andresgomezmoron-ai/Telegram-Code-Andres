#!/usr/bin/env bash
# Instala el bot como servicio systemd en un VPS Debian/Ubuntu (AlphaVPS, Hetzner,
# Contabo, lo que sea). Es idempotente: puedes volver a ejecutarlo para actualizar.
#
#   git clone https://github.com/andresgomezmoron-ai/Telegram-Code-Andres.git
#   sudo bash Telegram-Code-Andres/deploy/install-vps.sh
#
# Para instalar una rama concreta:  BRANCH=mi-rama sudo -E bash deploy/install-vps.sh
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/andresgomezmoron-ai/Telegram-Code-Andres.git}"
BRANCH="${BRANCH:-}"   # vacío = la rama por defecto del repositorio
DEST="${DEST:-/opt/claudegram}"
SERVICE_USER="${SERVICE_USER:-claudegram}"

if [[ $EUID -ne 0 ]]; then
  echo "Ejecútalo con sudo." >&2
  exit 1
fi

echo "==> Paquetes del sistema"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git ca-certificates

echo "==> Usuario de servicio: ${SERVICE_USER}"
id -u "${SERVICE_USER}" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "${SERVICE_USER}"

echo "==> Código en ${DEST}"
if [[ -d "${DEST}/.git" ]]; then
  git -C "${DEST}" fetch --quiet origin
  TARGET="${BRANCH:-$(git -C "${DEST}" rev-parse --abbrev-ref HEAD)}"
  git -C "${DEST}" checkout --quiet "${TARGET}"
  git -C "${DEST}" reset --hard --quiet "origin/${TARGET}"
elif [[ -f "$(dirname "$0")/../claudegram/__main__.py" ]]; then
  mkdir -p "${DEST}"
  tar -C "$(dirname "$0")/.." --exclude=./.venv --exclude=./.git --exclude=./state -cf - . \
    | tar -C "${DEST}" -xf -
elif [[ -n "${BRANCH}" ]]; then
  git clone --quiet --branch "${BRANCH}" "${REPO_URL}" "${DEST}"
else
  git clone --quiet "${REPO_URL}" "${DEST}"
fi

echo "==> Entorno virtual y dependencias"
[[ -d "${DEST}/.venv" ]] || python3 -m venv "${DEST}/.venv"
"${DEST}/.venv/bin/pip" install --quiet --upgrade pip
"${DEST}/.venv/bin/pip" install --quiet -r "${DEST}/requirements.txt"

mkdir -p "${DEST}/state"
if [[ ! -f "${DEST}/.env" ]]; then
  cp "${DEST}/.env.example" "${DEST}/.env"
  sed -i 's|^CLAUDEGRAM_STATE_DIR=.*|CLAUDEGRAM_STATE_DIR='"${DEST}"'/state|' "${DEST}/.env"
  echo "==> He creado ${DEST}/.env — EDÍTALO antes de arrancar."
  NEEDS_EDIT=1
fi
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${DEST}"
chmod 600 "${DEST}/.env"

echo "==> Servicio systemd"
sed "s|/opt/claudegram|${DEST}|g; s|User=claudegram|User=${SERVICE_USER}|; s|Group=claudegram|Group=${SERVICE_USER}|" \
  "${DEST}/deploy/claudegram.service" > /etc/systemd/system/claudegram.service
systemctl daemon-reload
systemctl enable --quiet claudegram

if [[ "${NEEDS_EDIT:-0}" == "1" ]]; then
  cat <<TXT

Casi está. Ahora:

  1. sudo nano ${DEST}/.env
     Pon TELEGRAM_BOT_TOKEN y ANTHROPIC_API_KEY. Deja de momento
     TELEGRAM_ALLOWED_USER_IDS como está: lo rellenas en el paso 4.

  2. sudo -u ${SERVICE_USER} ${DEST}/.venv/bin/python -m claudegram --check
     Comprueba token, clave y modelo sin gastar tokens.

  3. sudo systemctl start claudegram

  4. Escríbele /id a tu bot en Telegram: te dirá tu número. Ponlo en
     TELEGRAM_ALLOWED_USER_IDS y: sudo systemctl restart claudegram

  5. journalctl -u claudegram -f   # ver los logs

TXT
else
  systemctl restart claudegram
  echo
  echo "Actualizado y reiniciado. Logs: journalctl -u claudegram -f"
fi
