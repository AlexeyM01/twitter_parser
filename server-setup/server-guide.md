# Полное руководство: от купленного сервера до постов Шэмса в вашем канале

Это пошаговый путь «нулевого» сервера до работающей связки. Времени — 30–40 минут.

```
Ваш сервер (VPS)                      GitHub Actions                Telegram
┌─────────────────────────┐          ┌───────────────┐          ┌──────────┐
│ RSSHub (Docker)         │  RSS ←── │ parser.py     │  пост →  │  канал   │
│ берёт посты X по кукам  │─────────→│ каждые 20 мин │─────────→│          │
│ auth_token, отдаёт ленту│   HTTP   │ state.json    │ Bot API  │  @вы     │
└─────────────────────────┘          └───────────────┘          └──────────┘
```

**Что где живёт:**
- **Сервер** — только RSSHub: всегда включён, по кукам вашего аккаунта X собирает ленту Шэмса и отдаёт её как RSS по адресу `http://IP:1200/...`
- **GitHub** — парсер: раз в 20 минут забирает ленту с сервера, переводит и постит в канал. Ничего не ставится, всё бесплатно.
- **Telegram** — бот от @BotFather, добавленный админом в канал.

**Из присланных вами кук нужен только `auth_token`** (строка из 32 символов после `auth_token=`; в вашем примере она идёт после `twid=...`). Остальные куки (`ct0`, `cf_clearance`, `guest_id`, …) не нужны: RSSHub сам получит всё остальное.

---

## 1. Подключение к серверу по SSH

Хостинг после покупки присылает: IP-адрес, пользователя (обычно `root`) и пароль (или SSH-ключ).

**Windows (10/11):** откройте PowerShell и введите:

```powershell
ssh root@IP_СЕРВЕРА
```

**macOS/Linux:** то же самое в Терминале. Если хостер выдал файл ключа `key.pem`:

```bash
chmod 600 key.pem
ssh -i key.pem root@IP_СЕРВЕРА
```

На вопрос `Are you sure you want to continue connecting?` — ответьте `yes`, затем введите пароль. Увидели приглашение `root@server:~#` — вы внутри.

## 2. Первичная настройка сервера

```bash
apt update && apt upgrade -y          # обновления
apt install -y curl git nano          # базовые утилиты
```

Внутрисистемный firewall (ufw):

```bash
ufw allow OpenSSH
ufw --force enable
ufw status
```

⚠️ **Важно про хостеров:** у многих (Timeweb, Selectel, Beget, Hetzner, DigitalOcean и т.д.) есть **вторая, панельная** защита — «Firewall» / «Security Group» в личном кабинете. Порт 1200 нужно будет открыть **и там** (раздел 4). Это самая частая причина «на сервере работает, из GitHub не достучаться».

## 3. Установка Docker

```bash
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
docker --version && docker compose version   # проверка: две версии — всё ок
```

## 4. Запуск RSSHub

**Способ А — одним скриптом (рекомендую).** Скрипт `setup.sh` лежит в папке `server-setup/` этого проекта. Загрузите его на сервер и запустите:

```bash
# на своём компьютере (из папки проекта):
scp server-setup/setup.sh root@IP_СЕРВЕРА:/root/

# на сервере:
bash /root/setup.sh
```

Скрипт спросит `auth_token` — вставьте его значение из ваших кук, дальше всё сделает сам: поставит Docker, создаст `/opt/rsshub`, запустит контейнер и проверит ленту. В конце напечатает строку `CUSTOM_RSS_URL` для GitHub.

**Способ Б — вручную.** Если хочется понимать каждую строчку:

```bash
mkdir -p /opt/rsshub && cd /opt/rsshub

# файл с секретом (только значение auth_token из ваших кук)
cat > .env <<'EOF'
TWITTER_AUTH_TOKEN=ВСТАВЬТЕ_СЮДА_ЗНАЧЕНИЕ_auth_token
EOF
chmod 600 .env

# файл запуска
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
      CACHE_EXPIRE: 600
EOF

docker compose up -d      # скачает образ и запустит (~1-2 минуты)
```

Откройте порт:

```bash
ufw allow 1200/tcp
```

…и не забудьте про панель хостера (Security Group) из раздела 2.

## 5. Проверка ленты

На сервере:

```bash
curl -s "http://localhost:1200/twitter/user/ShamsCharania" | head -c 400
```

✅ **Ожидаемо:** начинается с `<?xml version=...` или `<rss ...>` — лента работает.

| Если видите | Что это значит | Что делать |
| --- | --- | --- |
| `Twitter API is not configured` | .env не подхватился | `cd /opt/rsshub && docker compose down && docker compose up -d` |
| `401` / `Unauthorized` | auth_token неверный/просроченный | Перезайдите в x.com, возьмите свежий auth_token, обновите `.env`, затем `docker compose restart` |
| `connection refused` | контейнер не поднялся | `docker compose logs --tail=50` |

Проверка **снаружи** (с вашего компьютера): `curl -s "http://IP_СЕРВЕРА:1200/twitter/user/ShamsCharania" | head -c 200`. Не отвечает — ищите закрытый порт в панели хостера.

## 6. Подключаем сервер к парсеру (GitHub)

1. Откройте ваш репозиторий → **Settings → Secrets and variables → Actions**.
2. Вкладка **Variables** → **New repository variable**:
   - Name: `CUSTOM_RSS_URL`
   - Secret: `http://IP_СЕРВЕРА:1200/twitter/user/ShamsCharania`
3. Убедитесь, что во вкладке **Secrets** лежат: `TELEGRAM_BOT_TOKEN` (от @BotFather), `TELEGRAM_CHANNEL` (`@имя_канала`), и что бот добавлен **администратором** канала с правом «Публикация сообщений».

Ничего в код менять не нужно: парсер при каждом запуске сначала пробует `CUSTOM_RSS_URL`.

## 7. (Опционально, но рекомендуется) Пароль на ленту

Сейчас ленту может забрать кто угодно, зная IP, — и «сжечь» лимиты вашего аккаунта X. Проще всего закрыться basic auth через Caddy:

```bash
# 1) придумайте пароль (только латиница и цифры) и получите хэш:
docker run --rm -it caddy:2 caddy hash-password
# скопируйте выведенную строку $2a$14$...

# 2) создайте конфиг Caddy:
cat > /opt/rsshub/Caddyfile <<'EOF'
:1201 {
    basic_auth {
        rss ХЭШ_ИЗ_ПУНКТА_1
    }
    reverse_proxy rsshub:1200
}
EOF
```

```bash
# 3) раскомментируйте сервис caddy в /opt/rsshub/docker-compose.yml
#    (готовый блок уже там, в конце файла) и примените:
cd /opt/rsshub && docker compose up -d
ufw allow 1201/tcp && ufw delete allow 1200/tcp   # наружу теперь только 1201
```

4) Обновите Variable в GitHub:
   `CUSTOM_RSS_URL` = `http://rss:ВАШ_ПАРОЛЬ@IP_СЕРВЕРА:1201/twitter/user/ShamsCharania`

Пароль хранится только в секретах GitHub; по HTTP передаётся сама лента, а не токен. Если пароль со спецсимволами — возьмите только буквы/цифры, чтобы не кодировать его в URL.

## 8. Первый запуск и проверка всей цепочки

GitHub → **Actions** → «Shams → Telegram» → **Run workflow** → дождитесь зелёной галочки.

Лог первого запуска: `Первый запуск: запомнил N постов без публикации` — **это норма**, чтобы канал не завалили старые твиты. `state.json` появится в репозитории — не удаляйте его (это память о том, что уже опубликовано).

Дальше всё работает само: каждые 20 минут Actions проверяет ленту; новый твит Шэмса → в канал через ~1–20 минут (перевод, картинки, ссылка).

## 9. Обслуживание сервера

| Что | Команда |
| --- | --- |
| Логи RSSHub (что происходит) | `docker compose -f /opt/rsshub/docker-compose.yml logs -f --tail=100` |
| Статус контейнеров | `docker compose -f /opt/rsshub/docker-compose.yml ps` |
| Перезапуск | `cd /opt/rsshub && docker compose restart` |
| Обновить RSSHub (раз в месяц) | `cd /opt/rsshub && docker compose pull && docker compose up -d` |
| Обновить сам сервер | `apt update && apt upgrade -y && reboot` (контейнеры поднимутся сами, `restart: always`) |

## 10. Частые ошибки

| Симптом | Причина | Лечение |
| --- | --- | --- |
| Лента отвечает на сервере, но Actions пишет «не удалось получить ленту» | Закрыт порт в панели хостера | Откройте 1200 (или 1201) в Security Group |
| `401` в логах RSSHub | auth_token протух (выход из аккаунта, смена пароля, бан) | Свежий auth_token → `nano /opt/rsshub/.env` → `docker compose restart` |
| `Twitter API is not configured` | .env не подхватился | `docker compose down && docker compose up -d` |
| Actions: `Unauthorized` у бота | Неверный токен бота | Перевыпустите у @BotFather (`/token`), обновите секрет |
| Actions: `bot is not a member of the channel` | Бот не админ канала | Шаг 6, пункт 3 |
| В канале дубли | Удалили state.json в репо | Не удалять; если удалили — дождитесь «первого запуска» (тихая инициализация) |
| Твиты перестали приходить | Всё сломалось? Проверьте по цепочке: Actions → лог запуска → сервер (`curl localhost:1200/...`) → токен | — |

## 11. Гигиена и безопасность

- `auth_token` — это **доступ к вашему аккаунту X**: не публикуйте его в чатах/скриншотах/репозитории. Единственное правильное место — файл `/opt/rsshub/.env` на сервере (права 600, только root).
- Куки, вставленные в переписку, лучше «перезайти»: после настройки выйдите из x.com и зайдите снова — старый токен умрёт. Затем обновите `/opt/rsshub/.env` и `docker compose restart`.
- SSH: по желанию — вход только по ключу (`PasswordAuthentication no` в `/etc/ssh/sshd_config`) и `ufw limit OpenSSH`.

---

**Коротко весь путь:** SSH → `apt update` → Docker → `bash setup.sh` (вставить auth_token) → проверка `curl` → Variable `CUSTOM_RSS_URL` в GitHub → Run workflow → готово.
