#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(id -un)"
RUN_HOME="$(getent passwd "${RUN_USER}" | cut -d: -f6)"
CONFIG_DIR="${XDG_CONFIG_HOME:-${RUN_HOME}/.config}/wsl-daily-news"
ENV_FILE="${CONFIG_DIR}/news_mail.env"
CODEX_BIN_DEFAULT="$(command -v codex || true)"

if [[ -z "${RUN_HOME}" ]]; then
  echo "Cannot resolve home directory for user: ${RUN_USER}" >&2
  exit 1
fi

if [[ -z "${CODEX_BIN_DEFAULT}" ]]; then
  CODEX_BIN_DEFAULT="codex"
fi

mkdir -p "${CONFIG_DIR}" "${ROOT_DIR}/logs" "${ROOT_DIR}/output"
chmod 700 "${CONFIG_DIR}"

if [[ ! -f "${ENV_FILE}" ]]; then
  cat > "${ENV_FILE}" <<'EOF'
SMTP_HOST=smtp.gmail.com
SMTP_PORT=465
SMTP_SENDER=your_gmail@gmail.com
SMTP_RECIPIENT=your_mailbox@example.com
SMTP_APP_PASSWORD="replace_with_gmail_app_password"
MAIL_SUBJECT_PREFIX="[WSL Daily News]"
CODEX_BIN=__CODEX_BIN__
CODEX_TIMEOUT_SECONDS=900
CODEX_MAX_ATTEMPTS=3
CODEX_RETRY_DELAY_SECONDS=20
SEND_FAILURE_EMAIL=1
EOF
  sed -i "s#__CODEX_BIN__#${CODEX_BIN_DEFAULT//\\/\\\\}#g" "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
fi

TMP_FILES=()
cleanup() {
  rm -f "${TMP_FILES[@]:-}"
}
trap cleanup EXIT

install_unit() {
  local src="$1"
  local dest_name="$2"
  local tmp_file
  tmp_file="$(mktemp)"
  TMP_FILES+=("${tmp_file}")
  sed \
    -e "s#__WORKDIR__#${ROOT_DIR}#g" \
    -e "s#__RUN_USER__#${RUN_USER}#g" \
    -e "s#__RUN_HOME__#${RUN_HOME}#g" \
    -e "s#__ENV_FILE__#${ENV_FILE}#g" \
    "${src}" > "${tmp_file}"
  wsl.exe -d Ubuntu -u root -- bash -lc "install -m 0644 '${tmp_file}' '/etc/systemd/system/${dest_name}'"
}

install_unit "${ROOT_DIR}/systemd/wsl-daily-news-morning.service" "wsl-daily-news-morning.service"
install_unit "${ROOT_DIR}/systemd/wsl-daily-news-morning.timer" "wsl-daily-news-morning.timer"
install_unit "${ROOT_DIR}/systemd/wsl-daily-news-evening.service" "wsl-daily-news-evening.service"
install_unit "${ROOT_DIR}/systemd/wsl-daily-news-evening.timer" "wsl-daily-news-evening.timer"
wsl.exe -d Ubuntu -u root -- systemctl daemon-reload
wsl.exe -d Ubuntu -u root -- systemctl enable --now wsl-daily-news-morning.timer
wsl.exe -d Ubuntu -u root -- systemctl enable --now wsl-daily-news-evening.timer

echo "Installed:"
echo "  env: ${ENV_FILE}"
echo "  morning service: /etc/systemd/system/wsl-daily-news-morning.service"
echo "  morning timer: /etc/systemd/system/wsl-daily-news-morning.timer"
echo "  evening service: /etc/systemd/system/wsl-daily-news-evening.service"
echo "  evening timer: /etc/systemd/system/wsl-daily-news-evening.timer"
echo
echo "Check next run:"
echo "  systemctl list-timers 'wsl-daily-news*' --all"
