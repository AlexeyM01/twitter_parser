import html
import json
import os
import re
import tempfile
import time
from datetime import UTC, datetime, timedelta

import requests
from apify_client import ApifyClient

# === ФИЛЬТРАЦИЯ ===
# Стоп-фразы: твиты с ними не публикуются (сравнение без учёта регистра)
BLOCK_PHRASES = [
    "Tap to watch:",
    "Join us in wishing",
    # добавляйте свои фразы через запятую, например:
]

# Регулярка для вырезания ссылок t.co из текста твита
TCO_RE = re.compile(r"https?://t\.co/\w+", re.IGNORECASE)

# === КОНФИГУРАЦИЯ ===
APIFY_TOKEN: str = os.getenv("APIFY_TOKEN", "").strip()
ACTOR_ID: str = os.getenv("ACTOR_ID", "").strip()
TG_BOT_TOKEN: str = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID: str = os.getenv("TG_CHAT_ID", "").strip()

# Прокси: если задан — весь исходящий трафик идёт через него;
# если НЕ задан — системные прокси (реестр Windows / HTTP_PROXY) полностью игнорируются.
# Формат: http://127.0.0.1:10809 или socks5://127.0.0.1:1080
PROXY_URL: str = os.getenv("PROXY_URL", "").strip()

# Ретраи и таймауты
MAX_RETRIES = 3          # попыток на запрос (Telegram, syndication, скачивание медиа)
RETRY_DELAY_SEC = 5      # пауза между попытками

SENT_IDS_FILE = "sent_tweets.txt"
STATE_FILE = "parser_state.json"  # хранит время последнего успешного запуска

# === Настройки расписания (согласованы с systemd-таймером каждые 10 минут) ===
RUN_INTERVAL_MIN = 10       # период запуска парсера
OVERLAP_MIN = 2             # перекрытие окна — защита от твитов, опубликованных на границе запусков
MAX_LOOKBACK_MIN = 360      # потолок «догоняющего» окна после простоя (6 часов), чтобы не спамить старьем
FIRST_RUN_WINDOW_MIN = 15   # окно самого первого запуска (пока нет файла состояния)

# Глобальная сессия для сохранения cookies и заголовков между запросами
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "image/*,video/*,*/*",
    "Referer": "https://x.com/",
})

# --- Управление прокси ---
if PROXY_URL:
    # Явный прокси из .env — используем его для всех запросов
    SESSION.proxies.update({"http": PROXY_URL, "https": PROXY_URL})
    print(f"🌐 Исходящий трафик через прокси: {PROXY_URL}")
else:
    # Ничего не задано — игнорируем системные прокси-настройки.
    # Это спасает VPS от случайного подхвата окружения и делает поведение детерминированным.
    SESSION.trust_env = False
    print("🌐 Работаем без прокси (системные прокси-настройки игнорируются)")


def request_with_retry(method, url, retries=MAX_RETRIES, delay=RETRY_DELAY_SEC, **kwargs):
    """Обёртка с ретраями поверх сессии. Возвращает response или выбрасывает исключение."""
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            resp = SESSION.request(method, url, **kwargs)
            return resp
        except requests.exceptions.RequestException as e:
            last_exc = e
            kind = "ProxyError — проверь прокси/VPN" if isinstance(e, requests.exceptions.ProxyError) else type(e).__name__
            print(f"  ⚠ Попытка {attempt}/{retries} не удалась ({kind})")
            if attempt < retries:
                time.sleep(delay)
    raise last_exc

# Маппинг MIME-типов в расширения файлов
MIME_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
}


def load_sent_ids():
    try:
        with open(SENT_IDS_FILE, "r", encoding="utf-8") as f:
            return set(f.read().splitlines())
    except FileNotFoundError:
        return set()


def save_sent_id(tweet_id):
    with open(SENT_IDS_FILE, "a", encoding="utf-8") as f:
        f.write(f"{tweet_id}\n")

    # Периодическая обрезка файла, чтобы он рос бесконечно (~1 МБ ≈ 100 тыс. ID)
    try:
        if os.path.getsize(SENT_IDS_FILE) > 1_000_000:
            with open(SENT_IDS_FILE, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
            with open(SENT_IDS_FILE, "w", encoding="utf-8") as f:
                f.write("\n".join(lines[-20_000:]) + "\n")
    except OSError:
        pass


def load_last_run_ts():
    """Возвращает время последнего УСПЕШНОГО запуска (epoch) или None."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return float(json.load(f).get("last_success_ts"))
    except (FileNotFoundError, ValueError, KeyError, OSError):
        return None


def save_last_run_ts(ts=None):
    """Фиксирует успешный запуск. Вызывается ТОЛЬКО после успешной выборки из Apify."""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"last_success_ts": ts if ts is not None else time.time()}, f)


def compute_since_time():
    """
    Вычисляет sinceTime для текущего запуска.

    Логика:
      - первый запуск (нет состояния): окно FIRST_RUN_WINDOW_MIN минут;
      - обычный запуск: от последнего успешного запуска минус OVERLAP_MIN перекрытия;
      - после долгого простоя: не дальше MAX_LOOKBACK_MIN минут назад,
        чтобы одним махом не залить в канал сотни старых постов
        (лимит maxItems всё равно срежет лишнее).
    """
    now = datetime.now(UTC)
    last_ts = load_last_run_ts()

    if last_ts is None:
        since = now - timedelta(minutes=FIRST_RUN_WINDOW_MIN)
    else:
        since = datetime.fromtimestamp(last_ts, tz=UTC) - timedelta(minutes=OVERLAP_MIN)
        floor = now - timedelta(minutes=MAX_LOOKBACK_MIN)
        since = max(since, floor)

    print(f"⏱ Окно выборки: с {since.strftime('%Y-%m-%dT%H:%M:%SZ')} (текущее UTC: {now.strftime('%H:%M:%S')})")
    return since.strftime("%Y-%m-%dT%H:%M:%SZ")


def get_extension_from_content_type(content_type):
    """Определяет расширение файла по заголовку Content-Type"""
    if not content_type:
        return None
    mime = content_type.split(";")[0].strip().lower()
    return MIME_TO_EXT.get(mime)


def pick_best_video_variant(variants):
    """Выбирает вариант видео с максимальным битрейтом (предпочтительно mp4)"""
    if not variants:
        return None
    mp4s = [
        v for v in variants
        if isinstance(v, dict)
        and "video/mp4" in (v.get("content_type") or v.get("contentType") or "")
        and v.get("bitrate")
    ]
    if mp4s:
        return max(mp4s, key=lambda v: v["bitrate"]).get("url")
    # fallback: любой вариант с URL
    for v in variants:
        if isinstance(v, dict) and v.get("url"):
            return v["url"]
    return None


def get_media_info(item):
    """
    Извлекает ПРЯМОЙ URL медиа из данных твита (без t.co).
    Возвращает (url, type) или (None, None), если прямого URL нет.
    """
    media_list = item.get("media") or item.get("mediaDetails") or []
    if not media_list or not isinstance(media_list, list):
        return None, None

    first_media = media_list[0]
    if not isinstance(first_media, dict):
        # Если медиа задано строкой и это не t.co — используем его
        if isinstance(first_media, str) and "t.co" not in first_media:
            return first_media, "photo"
        return None, None

    media_type = first_media.get("type", "photo")

    # 1. Для видео сначала пробуем варианты из video_info — там прямые ссылки на .mp4
    if media_type in ("video", "animated_gif"):
        video_info = first_media.get("video_info") or {}
        variants = video_info.get("variants") or first_media.get("variants") or []
        video_url = pick_best_video_variant(variants)
        if not video_url:
            video_url = first_media.get("video_url") or first_media.get("video")
        if video_url and "t.co" not in video_url:
            return video_url, "video"
        # прямого видео-URL нет — возвращаем None, чтобы resolve_media
        # попробовал syndication (там часто есть настоящие mp4).
        # НИКОГДА не отдаём превью с типом "video": sendVideo отвергнет JPEG.
        return None, None

    # 2. Прямые URL картинки (pbs.twimg.com) — t.co не годится
    photo_url = (
        first_media.get("media_url_https")
        or first_media.get("media_url")
        or first_media.get("photo_url")
    )
    if photo_url and "t.co" not in photo_url and media_type not in ("video", "animated_gif"):
        return photo_url, "photo"

    # Прямого URL не нашли — сигнал вызывающему коду попробовать syndication API
    return None, None


def get_thumbnail(item):
    """Превью-картинка твита — крайний запасной вариант, если и Apify, и
    syndication не дали ни mp4, ни фото. Отправим её как фото, а не текстом."""
    media_list = item.get("media") or item.get("mediaDetails") or []
    if media_list and isinstance(media_list[0], dict):
        url = (media_list[0].get("media_url_https")
               or media_list[0].get("media_url"))
        if url and "t.co" not in url:
            return url
    return None


def fetch_media_via_syndication(tweet_id):
    """
    Резервный способ: публичный syndication API X.
    Возвращает JSON твита с mediaDetails (media_url_https + video_info.variants),
    даже если Apify отдал только t.co-ссылки.
    """
    url = f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&token=x&lang=ru"
    try:
        resp = request_with_retry("GET", url, timeout=20)
        if resp.status_code != 200:
            print(f"  ⚠️ Syndication API вернул {resp.status_code}")
            return None, None

        data = resp.json()
        details = data.get("mediaDetails") or data.get("media") or []
        if not details or not isinstance(details, list):
            return None, None

        m = details[0]
        m_type = m.get("type", "photo")

        if m_type in ("video", "animated_gif"):
            variants = (m.get("video_info") or {}).get("variants") or []
            video_url = pick_best_video_variant(variants)
            if video_url:
                return video_url, "video"

        photo_url = m.get("media_url_https")
        if photo_url:
            return photo_url, "photo"

        return None, None
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ Ошибка syndication API: {e}")
        return None, None


def resolve_tco_url(short_url):
    """
    Преобразует короткую ссылку t.co в реальный URL.
    t.co часто не отвечает на HEAD и редиректит через HTML/JS,
    поэтому используем GET со stream=True.
    """
    try:
        resp = request_with_retry("GET", short_url, stream=True, allow_redirects=True, timeout=15)
        real_url = str(resp.url)
        resp.close()  # не скачиваем тело ответа

        # Остались на t.co / попали на login / попали на страницу твита — не удалось
        if "t.co" in real_url or "login" in real_url:
            print(f"  ⚠️ Не удалось разрешить ссылку {short_url}")
            return None
        return real_url
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ Ошибка разрешения ссылки: {e}")
        return None


def download_media(url, media_type_hint):
    """Скачивает медиафайл по прямому URL (pbs.twimg.com / video.twimg.com)."""
    real_url = url

    # 1. Разрешаем короткую ссылку, если всё-таки попался t.co
    if "t.co" in url:
        print("  🔗 Разрешаем t.co ссылку...")
        resolved = resolve_tco_url(url)
        if resolved:
            real_url = resolved
            print(f"  ✅ Реальный URL: {real_url[:80]}...")
        else:
            print("  ❌ Не удалось получить реальный URL, пропускаем медиа")
            return None

    # 2. Запрашиваем фото в максимальном качестве
    if "pbs.twimg.com/media/" in real_url and "name=" not in real_url and "?" not in real_url:
        real_url = real_url + "?name=large"

    try:
        print("  📥 Загрузка файла...")
        response = request_with_retry("GET", real_url, timeout=30)
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "")
        size = len(response.content)

        print(f"     Content-Type: {content_type} | Size: {size} bytes")

        # Защита от получения HTML вместо медиа
        if "text/html" in content_type:
            print("  ❌ Получен HTML вместо медиа (возможно, требуется авторизация)")
            return None

        # Определение расширения
        ext = get_extension_from_content_type(content_type)
        if not ext:
            ext = ".mp4" if media_type_hint == "video" else ".jpg"

        # Проверка минимального размера
        min_size = 1024 if media_type_hint == "photo" else 10240
        if size < min_size:
            print(f"  ❌ Файл слишком мал ({size} байт), возможно поврежден")
            return None

        # Сохранение во временный файл
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=ext)  # noqa: SIM115
        temp_file.write(response.content)
        temp_file.close()

        print(f"  ✅ Сохранено: {temp_file.name}")
        return temp_file.name

    except requests.exceptions.HTTPError as e:
        print(f"  ❌ HTTP ошибка: {e}")
        return None
    except requests.exceptions.Timeout:
        print("  ❌ Таймаут загрузки")
        return None
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ Ошибка загрузки: {type(e).__name__}: {e}")
        return None


def send_telegram_message(text, media_path=None, media_type="photo"):
    """
    Отправляет пост в Telegram. Возвращает True при успехе, False при ошибке.
    При ошибке отправки медиа делает fallback на текст.
    """
    base_url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"

    # Обрезка подписи до лимита Telegram (1024 символа)
    caption = text[:1020] + "..." if len(text) > 1024 else text

    if media_path and os.path.exists(media_path):
        file_size = os.path.getsize(media_path)
        print(f"  📤 Отправка медиа ({file_size} байт, тип={media_type})...")

        if media_type == "video":
            endpoint = f"{base_url}/sendVideo"
            file_key = "video"
        else:
            endpoint = f"{base_url}/sendPhoto"
            file_key = "photo"

        try:
            with open(media_path, "rb") as media_file:
                files = {file_key: media_file}
                data = {
                    "chat_id": TG_CHAT_ID,
                    "caption": caption,
                    "parse_mode": "HTML",
                }
                resp = request_with_retry("POST", endpoint, data=data, files=files, timeout=90)

            if resp.ok:
                print("  ✅ Медиа успешно отправлено!")
                return True

            print(f"  ❌ Telegram API ошибка: {resp.status_code} — {resp.text[:300]}")
            # fallback: отправим текстом, чтобы пост не потерялся
            return send_telegram_message(text, None)
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ Ошибка отправки медиа: {type(e).__name__}: {e}")
            return send_telegram_message(text, None)
        finally:
            # Удаляем временный файл после попытки отправки
            try:
                os.unlink(media_path)
                print("  🗑️ Временный файл удалён")
            except OSError:
                pass
    else:
        # Отправка только текста
        payload = {
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
        try:
            resp = request_with_retry("POST", f"{base_url}/sendMessage", data=payload, timeout=60)
            if not resp.ok:
                print(f"  ❌ Telegram API: {resp.status_code} — {resp.text[:300]}")
                return False
            print("  ✅ Текст отправлен")
            return True
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ Ошибка отправки: {type(e).__name__}: {e}")
            return False


def resolve_media(item, tweet_id):
    """
    Возвращает (media_path_ready_url, media_type).
    Стратегия:
      1) прямой URL из данных Apify (pbs.twimg.com / video.twimg.com);
      2) если там только t.co или ничего — syndication API по tweet_id.
    """
    media_url, media_type = get_media_info(item)

    if media_url:
        return media_url, media_type

    print("  🔁 Прямого URL нет, пробуем syndication API по tweet_id...")
    synd_url, synd_type = fetch_media_via_syndication(tweet_id)
    if synd_url:
        print(f"  ✅ Найден медиа URL через syndication ({synd_type})")
        return synd_url, synd_type

    # Ни Apify, ни syndication не дали медиа — отправим хотя бы превью картинкой
    thumb = get_thumbnail(item)
    if thumb:
        print("  🖼️ Медиа не найдено — отправляем превью твита как фото")
        return thumb, "photo"

    return None, None


def run_parser():
    client = ApifyClient(APIFY_TOKEN)
    sent_ids = load_sent_ids()

    # Окно выборки считается от последнего УСПЕШНОГО запуска (см. compute_since_time)
    run_input = {
        "twitterHandles": ["NBA", "ShamsCharania", "TheDunkCentral"],
        "translateTo": "ru",
        "maxItems": 50,
        "queryType": "Latest",
        "maxItemsPerTarget": 5,
        "sinceTime": compute_since_time(),
    }

    print("🚀 Запускаем скрапер в Apify...")
    run = client.actor(ACTOR_ID).call(run_input=run_input)

    dataset_items = client.dataset(run.default_dataset_id).list_items().items
    print(f"📦 Получено {len(dataset_items)} элементов\n")

    # Выборка прошла успешно — фиксируем время этого запуска как опорное.
    # Если упадёт ниже (сеть/Telegram), твиты уже записаны в sent_ids и не задублируются,
    # а следующий запуск перекроет окно на OVERLAP_MIN минут назад.
    save_last_run_ts()

    for item in reversed(dataset_items):
        try:
            process_tweet(item, sent_ids)
        except Exception as e:  # noqa: BLE001
            # Один проблемный твит не должен останавливать весь запуск
            print(f"❌ Ошибка обработки твита {item.get('id')}: {type(e).__name__}: {e}\n")
            continue


def process_tweet(item, sent_ids):
    author = item.get("author", {}).get("name", "Unknown") if isinstance(item.get("author"), dict) else "Unknown"
    text = item.get("text")
    tweet_id = str(item.get("id"))

    if not tweet_id or tweet_id in sent_ids:
        return

    raw_text = text or ""

    # Фильтр по стоп-фразам: твит пропускается, ID помечается как обработанный
    if any(phrase.lower() in raw_text.lower() for phrase in BLOCK_PHRASES):
        print(f"⏭ Твит {tweet_id} отфильтрован по стоп-фразе")
        save_sent_id(tweet_id)   # чтобы не проверять его при каждом запуске
        return

    tweet_url = item.get("url") or f"https://x.com/{author}/status/{tweet_id}"

    # Вырезаем ссылки t.co и подчищаем лишние пробелы
    clean_text = TCO_RE.sub("", raw_text).strip()
    clean_text = re.sub(r" {2,}", " ", clean_text)
    safe_text = html.escape(clean_text)

    formatted_post = (
        f"🐦 <b>Новый пост от @{html.escape(author)}</b>\n\n"
        f"{safe_text}\n\n"
        f"🔗 <a href='{tweet_url}'>Открыть оригинал в X</a>"
    )

    print(f"\n=== Твит {tweet_id} ===")
    media_url, media_type = resolve_media(item, tweet_id)

    media_path = None
    if media_url:
        print(f"🖼️  Медиа: {media_type}")
        media_path = download_media(media_url, media_type)
    else:
        print("📝 Твит без доступного медиа")

    sent_ok = send_telegram_message(formatted_post, media_path, media_type or "photo")

    if sent_ok:
        # ID сохраняем ТОЛЬКО после успешной отправки.
        # Если упало (сеть/прокси/Telegram) — не сохраняем, и следующий запуск
        # (окно перекрывается на OVERLAP_MIN) повторит попытку.
        save_sent_id(tweet_id)
        print(f"✅ Обработан твит {tweet_id}\n")
    else:
        print(f"⏳ Твит {tweet_id} НЕ отправлен — повторим при следующем запуске\n")

    time.sleep(2)


if __name__ == "__main__":
    run_parser()