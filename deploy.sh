#!/usr/bin/env bash
# =============================================================
# Установка NBA-парсера на сервер (Ubuntu 20.04/22.04/24.04, Debian 11/12)
#
# ВСЕ ЧУВСТВИТЕЛЬНЫЕ ДАННЫЕ БЕРУТСЯ ИЗ ФАЙЛА .env РЯДОМ С ЭТИМ СКРИПТОМ.
#   cp .env.example .env && nano .env   (заполни ключи ДО запуска установки)
#
# Запуск: sudo bash deploy.sh
# =============================================================
set -euo pipefail

APP_DIR="/opt/nba-parser"
ENV_FILE="/etc/nba-parser.env"          # куда встанет .env на сервере
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_ENV="$SCRIPT_DIR/.env"

# Ключи, которые обязаны присутствовать в .env
REQUIRED_KEYS=(APIFY_TOKEN ACTOR_ID TG_BOT_TOKEN TG_CHAT_ID)

echo "=== [1/5] Проверка окружения ==="
if ! command -v python3 >/dev/null; then
    echo "python3 не найден — устанавливаю..."
    apt-get update -qq && apt-get install -y -qq python3 python3-venv python3-pip
fi
if ! command -v systemctl >/dev/null; then
    echo "ОШИБКА: systemd не найден — скрипт рассчитан на Ubuntu/Debian." >&2
    exit 1
fi

# -------------------------------------------------------------
# Валидация локального .env со всеми секретами
# -------------------------------------------------------------
validate_env() {
    local file="$1"

    echo "=== [2/5] Проверка секретов в $(basename "$file") ==="

    if [ ! -f "$file" ]; then
        echo "ОШИБКА: файл .env не найден рядом с deploy.sh ($file)." >&2
        echo "Сделай так:" >&2
        echo "  cp .env.example .env" >&2
        echo "  nano .env            # заполни 4 ключа" >&2
        exit 1
    fi

    # Разбираем .env в под-оболочке, чтобы не подхватить мусор в текущий процесс
    local values
    values="$(set -a; source "$file" 2>/dev/null || true; set +a;
              printf '%s\n' "$APIFY_TOKEN|$ACTOR_ID|$TG_BOT_TOKEN|$TG_CHAT_ID" 2>/dev/null || true)"

    local i=0 missing=0
    local value
    for key in "${REQUIRED_KEYS[@]}"; do
        value="$(printf '%s' "$values" | cut -d'|' -f$((i + 1)))"
        if [ -z "$value" ]; then
            echo "  ✗ $key — пусто или отсутствует" >&2
            missing=1
        elif [[ "$value" == вставь* ]]; then
            echo "  ✗ $key — остался шаблонный placeholder из .env.example" >&2
            missing=1
        else
            echo "  ✓ $key — заполнен"
        fi
        i=$((i + 1))
    done

    if [ "$missing" -ne 0 ]; then
        echo "ОШИБКА: .env заполнен не полностью — исправь и запусти deploy.sh заново." >&2
        exit 1
    fi

    # Мягкое предупреждение для явно кривых значений (не блокируем)
    value="$(printf '%s' "$values" | cut -d'|' -f3)"
    if [[ "$value" != *:* ]]; then
        echo "  ⚠ TG_BOT_TOKEN обычно содержит двоеточие (123456:AA...) — проверь значение" >&2
    fi
}

# -------------------------------------------------------------
validate_env "$LOCAL_ENV"

echo "=== [3/5] Копирование файлов в $APP_DIR ==="
mkdir -p "$APP_DIR"
cp "$SCRIPT_DIR/parser.py" "$APP_DIR/parser.py"

echo "=== [4/5] Установка зависимостей ==="
if [ ! -d "$APP_DIR/venv" ]; then
    python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --upgrade pip -q
"$APP_DIR/venv/bin/pip" install -q requests apify-client

echo "=== [5/5] Установка секретов и systemd-юнитов ==="
# Секреты: из локального .env на сервер, права только для root
install -m 600 -o root -g root "$LOCAL_ENV" "$ENV_FILE"
echo "  Секреты установлены: $ENV_FILE (chmod 600, владелец root)"

cp "$SCRIPT_DIR/nba-parser.service" /etc/systemd/system/
cp "$SCRIPT_DIR/nba-parser.timer"   /etc/systemd/system/
systemctl daemon-reload

cat <<'DONE'

=============================================================
Готово! Ключи уже установлены из .env. Осталось 2 шага:

1) Тестовый запуск:         systemctl start nba-parser.service
   Смотреть лог:            journalctl -u nba-parser -f
2) Включить расписание:     systemctl enable --now nba-parser.timer

Полезные команды:
   Статус таймера:          systemctl list-timers | grep nba
   Ручной запуск:           systemctl start nba-parser.service
   Логи за сегодня:         journalctl -u nba-parser --since today

Обновить ключи: отредактируй локальный .env и снова запусти
   sudo bash deploy.sh  (секреты переустановятся из него)
=============================================================
DONE