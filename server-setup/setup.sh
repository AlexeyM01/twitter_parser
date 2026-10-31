#!/usr/bin/env bash
# ============================================================================
# Установка RSSHub для парсера "Shams Charania -> Telegram"
# Запускать на свежем сервере Ubuntu/Debian от root:  bash setup.sh
# Скрипт: ставит Docker (если нет), создаёт /opt/rsshub, запускает контейнер,
#         проверяет ленту и подсказывает следующие шаги.
# ============================================================================
set -euo pipefail

RSSHUB_DIR="/opt/rsshub"
PORT="1200"

echo "=============================================================="
echo " RSSHub для парсера Shams -> Telegram"
echo "=============================================================="

# --- 0. Проверки -------------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите от root: sudo bash setup.sh"; exit 1
fi

# --- 1. auth_token -------------------------------------------------------------
echo
echo "Нужен auth_token из кук залогиненного x.com (это единственное, что"
echo "нужно из куки; берётся в браузере: DevTools -> Application -> Cookies"
echo "-> https://x.com -> auth_token; это строка из 32 символов)."
echo
read -r -p "Вставьте auth_token: " AUTH_TOKEN
if ! echo "$AUTH_TOKEN" | grep -qE '^[0-9a-f]{32}$'; then
  echo "ВНИМАНИЕ: значение не похоже на auth_token (32 hex-символа)."
  read -r -p "Всё равно продолжить? [y/N]: " yn
  [ "${yn:-n}" = "y" ] || exit 1
fi

# --- 2. Docker -----------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  echo "[*] Устанавливаю Docker..."
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
else
  echo "[*] Docker уже установлен: $(docker --version)"
fi
docker compose version >/dev/null 2>&1 || { echo "Docker Compose не найден"; exit 1; }

# --- 3. Файлы ------------------------------------------------------------------
mkdir -p "$RSSHUB_DIR"
cd "$RSSHUB_DIR"

umask 077
cat > .env <<EOF
# auth_token из кук x.com (можно несколько через запятую)
TWITTER_AUTH_TOKEN=$AUTH_TOKEN
EOF

cat > docker-compose.yml <<'EOF'
services:
  rsshub:
    image: diygod/rsshub:latest
    container_name: rsshub
    restart: always
    ports:
      - "1200:1200"
    env_file:
      - .env
    environment:
      NODE_ENV: production
      CACHE_EXPIRE: 600          # кэш ленты, сек
      CACHE_CONTENT_EXPIRE: 3600 # кэш контента, сек
EOF
echo "[*] Файлы созданы в $RSSHUB_DIR (.env только для root)"

# --- 4. Firewall -----------------------------------------------------------------
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow ${PORT}/tcp >/dev/null && echo "[*] ufw: открыт порт ${PORT}/tcp"
fi
echo "ВАЖНО: если у хостера есть панельный firewall (Security Group),"
echo "откройте порт ${PORT}/tcp и там."

# --- 5. Запуск --------------------------------------------------------------------
echo "[*] Скачиваю образ и запускаю (первый раз ~1-2 минуты)..."
docker compose up -d

echo "[*] Проверяю ленту (до 60 секунд)..."
URL="http://127.0.0.1:${PORT}/twitter/user/ShamsCharania"
for i in $(seq 1 12); do
  sleep 5
  BODY=$(curl -fsS -m 15 "$URL" 2>/dev/null || true)
  if echo "$BODY" | grep -q "<rss"; then
    echo
    echo "=============================================================="
    echo " ГОТОВО! RSS-лента работает."
    echo
    echo " Вставьте в GitHub репозиторий (Settings -> Secrets and"
    echo " variables -> Actions -> Variables):"
    echo
    echo "   CUSTOM_RSS_URL = http://IP_СЕРВЕРА:${PORT}/twitter/user/ShamsCharania"
    echo
    echo " (замените IP_СЕРВЕРА на публичный IP этого сервера)"
    echo "=============================================================="
    exit 0
  fi
  if echo "$BODY" | grep -q "Twitter API is not configured"; then
    echo "Ошибка: переменная TWITTER_AUTH_TOKEN не подхватилась."; exit 1
  fi
done
echo
echo "Контейнер запущен, но лента пока не отвечает."
echo "Посмотрите логи: docker compose -f $RSSHUB_DIR/docker-compose.yml logs -f"
echo "Частая причина: 401 - auth_token неверный/просроченный (см. server-guide.md, раздел 10)."
exit 1
