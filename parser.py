import html
import json
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone

import requests
from apify_client import ApifyClient

# Вся конфигурация (секреты, списки, лимиты, таймауты) — в config.py
from config import (
    ACTOR_ID,
    APIFY_TOKEN,
    BLOCK_PHRASES,
    CAPTION_LIMIT,
    CAPTION_SAFE,
    DOWNLOAD_TIMEOUT,
    FIRST_RUN_WINDOW_MIN,
    MAX_ITEMS,
    MAX_ITEMS_PER_TARGET,
    MAX_LOOKBACK_MIN,
    MAX_RETRIES,
    MEDIA_MIN_SIZE_PHOTO,
    MEDIA_MIN_SIZE_VIDEO,
    OVERLAP_MIN,
    PROXY_URL,
    QUERY_TYPE,
    RESOLVE_TCO_TIMEOUT,
    RETRY_DELAY_SEC,
    SENT_IDS_FILE,
    SKIP_REPLIES,
    SKIP_RETWEETS,
    STATE_FILE,
    SYNDICATION_TIMEOUT,
    TCO_RE,
    TG_BOT_TOKEN,
    TG_CHAT_ID,
    TG_MAX_UPLOAD_BYTES,
    TG_MEDIA_GROUP_LIMIT,
    TG_MEDIA_TIMEOUT,
    TG_TEXT_TIMEOUT,
    TRANSLATE_TO,
    TWITTER_HANDLES,
)

UTC = timezone.utc  # noqa: UP017


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


def pick_video_variants(variants):
    """
    Возвращает список URL видео-вариантов, отсортированный ПО УБЫВАНИЮ битрейта
    (только mp4, если есть; иначе любые с URL).
    Первый элемент — лучшее качество. Перебор идёт вниз, пока файл не влезет
    в лимит загрузки Telegram (TG_MAX_UPLOAD_BYTES).
    """
    if not variants:
        return []
    mp4s = [
        v for v in variants
        if isinstance(v, dict)
        and "video/mp4" in (v.get("content_type") or v.get("contentType") or "")
        and v.get("url")
    ]
    if mp4s:
        mp4s.sort(key=lambda v: v.get("bitrate") or 0, reverse=True)
        return [v["url"] for v in mp4s]
    # fallback: любые варианты с URL
    return [v["url"] for v in variants if isinstance(v, dict) and v.get("url")]


def pick_best_video_variant(variants):
    """Лучший (самый качественный) вариант видео — первый из pick_video_variants."""
    urls = pick_video_variants(variants)
    return urls[0] if urls else None


def _extract_media_entry(m):
    """
    (candidates, type, thumb) для ОДНОГО элемента media из данных твита.
    candidates — список URL-кандидатов для скачивания (для видео — все варианты
    от лучшего качества к худшему, чтобы подобрать влезающее в лимит Telegram).
    thumb — превью-картинка (крайний запасной вариант). Для фото thumb = сам URL.
    Возвращает (None, None, None), если прямых URL нет.
    """
    if not isinstance(m, dict):
        # Если медиа задано строкой и это не t.co — используем его
        if isinstance(m, str) and "t.co" not in m:
            return [m], "photo", None
        return None, None, None

    media_type = m.get("type", "photo")

    # Для видео — только прямые mp4 из вариантов. НИКОГДА не отдаём превью
    # с типом "video": sendVideo отвергнет JPEG.
    if media_type in ("video", "animated_gif"):
        video_info = m.get("video_info") or {}
        variants = video_info.get("variants") or m.get("variants") or []
        candidates = pick_video_variants(variants)
        if not candidates:
            single = m.get("video_url") or m.get("video")
            candidates = [single] if single and "t.co" not in single else []
        thumb = m.get("media_url_https") or m.get("media_url")
        if candidates:
            return candidates, "video", (thumb if thumb and "t.co" not in thumb else None)
        return None, None, None

    # Прямые URL картинки (pbs.twimg.com) — t.co не годится
    photo_url = (
        m.get("media_url_https")
        or m.get("media_url")
        or m.get("photo_url")
    )
    if photo_url and "t.co" not in photo_url:
        return [photo_url], "photo", photo_url

    return None, None, None


def get_all_media(item):
    """
    Извлекает ПРЯМЫЕ URL ВСЕХ медиа твита из данных Apify (без t.co).
    Возвращает список (candidates, type, thumb), где candidates — список URL
    от лучшего качества к худшему (для видео) или одиночный URL (для фото).
    """
    media_list = item.get("media") or item.get("mediaDetails") or []
    if not media_list or not isinstance(media_list, list):
        return []

    result = []
    for m in media_list:
        candidates, m_type, thumb = _extract_media_entry(m)
        if candidates:
            result.append((candidates, m_type, thumb))
    return result


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
    Возвращает список (url, type) для ВСЕХ медиа твита (может быть пустым).
    """
    url = f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&token=x&lang=ru"
    try:
        resp = request_with_retry("GET", url, timeout=SYNDICATION_TIMEOUT)
        if resp.status_code != 200:
            print(f"  ⚠️ Syndication API вернул {resp.status_code}")
            return []

        data = resp.json()
        details = data.get("mediaDetails") or data.get("media") or []
        if not details or not isinstance(details, list):
            return []

        result = []
        for m in details:
            m_candidates, m_type, m_thumb = _extract_media_entry(m)
            if m_candidates:
                result.append((m_candidates, m_type, m_thumb))
        return result
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ Ошибка syndication API: {e}")
        return []


def fetch_media_via_fxtwitter(tweet_id):
    """
    Четвёртый резерв: публичный API fxtwitter (api.fxtwitter.com).
    Стабильно отдаёт прямые ссылки на фото и видео, когда Apify вернул
    только t.co, а syndication не ответил / не дал медиа.
    Возвращает список (candidates, type, thumb).
    """
    url = f"https://api.fxtwitter.com/status/{tweet_id}"
    try:
        resp = request_with_retry("GET", url, timeout=SYNDICATION_TIMEOUT)
        if resp.status_code != 200:
            print(f"  ⚠️ fxtwitter API вернул {resp.status_code}")
            return []

        data = resp.json()
        tweet = data.get("tweet") or {}
        media = tweet.get("media") or {}

        result = []
        for m in (media.get("photos") or []):
            u = m.get("url")
            if u and "t.co" not in u:
                result.append(([u], "photo", u))
        for m in (media.get("videos") or []):
            u = m.get("url")
            if u and "t.co" not in u:
                thumb = m.get("thumbnail_url") or m.get("thumbnail")
                result.append(([u], "video", thumb if thumb and "t.co" not in thumb else None))
        return result
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ Ошибка fxtwitter API: {e}")
        return []


def resolve_tco_url(short_url):
    """
    Преобразует короткую ссылку t.co в реальный URL.
    t.co часто не отвечает на HEAD и редиректит через HTML/JS,
    поэтому используем GET со stream=True.
    """
    try:
        resp = request_with_retry("GET", short_url, stream=True, allow_redirects=True, timeout=RESOLVE_TCO_TIMEOUT)
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
        response = request_with_retry("GET", real_url, timeout=DOWNLOAD_TIMEOUT, stream=True)
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "")

        # Проверяем размер ДО скачивания тела: если файл больше лимита загрузки
        # Telegram (50 МБ) — не качаем зря, sendVideo всё равно вернёт 413
        declared_size = int(response.headers.get("Content-Length") or 0)
        if declared_size > TG_MAX_UPLOAD_BYTES:
            print(
                f"  ⚠️ Файл больше лимита Telegram: {declared_size / 1024 / 1024:.1f} МБ"
                f" > {TG_MAX_UPLOAD_BYTES / 1024 / 1024:.0f} МБ — пропуск (для видео будет превью)"
            )
            response.close()
            return None

        response.content  # скачиваем тело (stream=True)  # noqa: B018
        size = len(response.content)

        print(f"     Content-Type: {content_type} | Size: {size} bytes")

        # Если Content-Length не был отдан — проверяем фактический размер
        if size > TG_MAX_UPLOAD_BYTES:
            print(
                f"  ⚠️ Файл больше лимита Telegram: {size / 1024 / 1024:.1f} МБ — пропуск (для видео будет превью)"
            )
            response.close()
            return None

        # Защита от получения HTML вместо медиа
        if "text/html" in content_type:
            print("  ❌ Получен HTML вместо медиа (возможно, требуется авторизация)")
            return None

        # Определение расширения
        ext = get_extension_from_content_type(content_type)
        if not ext:
            ext = ".mp4" if media_type_hint == "video" else ".jpg"

        # Проверка минимального размера
        min_size = MEDIA_MIN_SIZE_PHOTO if media_type_hint == "photo" else MEDIA_MIN_SIZE_VIDEO
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

    # Обрезка подписи до лимита Telegram
    caption = text[:CAPTION_SAFE] + "..." if len(text) > CAPTION_LIMIT else text

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
                resp = request_with_retry("POST", endpoint, data=data, files=files, timeout=TG_MEDIA_TIMEOUT)

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
            resp = request_with_retry("POST", f"{base_url}/sendMessage", data=payload, timeout=TG_TEXT_TIMEOUT)
            if not resp.ok:
                print(f"  ❌ Telegram API: {resp.status_code} — {resp.text[:300]}")
                return False
            print("  ✅ Текст отправлен")
            return True
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ Ошибка отправки: {type(e).__name__}: {e}")
            return False


def resolve_all_media(item, tweet_id):
    """
    Возвращает список (candidates, type, thumb) для ВСЕХ медиа твита.
    Стратегия:
      1) прямые URL из данных Apify (pbs.twimg.com / video.twimg.com);
      2) если их нет — syndication API по tweet_id;
      3) если и там пусто — fxtwitter API (надёжный публичный резерв);
      4) и только потом превью-картинка твита как фото.
    """
    media = get_all_media(item)
    if media:
        return media

    print("  🔁 Прямых URL нет, пробуем syndication API по tweet_id...")
    synd_media = fetch_media_via_syndication(tweet_id)
    if synd_media:
        print(f"  ✅ Найдено медиа через syndication: {len(synd_media)} шт.")
        return synd_media

    print("  🔁 Syndication пуст, пробуем fxtwitter API...")
    fx_media = fetch_media_via_fxtwitter(tweet_id)
    if fx_media:
        print(f"  ✅ Найдено медиа через fxtwitter: {len(fx_media)} шт.")
        return fx_media

    # Ничто не дало медиа — отправим хотя бы превью картинкой
    thumb = get_thumbnail(item)
    if thumb:
        print("  🖼️ Медиа не найдено — отправляем превью твита как фото")
        return [([thumb], "photo", thumb)]

    return []


def download_media_list(media_entries):
    """
    Скачивает список медиа (до TG_MEDIA_GROUP_LIMIT — лимит альбома Telegram).
    media_entries — список (candidates, type, thumb), где для видео candidates
    перебираются от лучшего качества к худшему, пока файл не влезет
    в лимит загрузки Telegram (TG_MAX_UPLOAD_BYTES).
    Если ни один вариант не влез — скачивается превью-картинка кадра.
    Возвращает список (path, type) успешно скачанных файлов.
    """
    downloaded = []
    for candidates, media_type, thumb in media_entries[:TG_MEDIA_GROUP_LIMIT]:
        path = None
        for candidate_url in candidates:
            path = download_media(candidate_url, media_type)
            if path:
                break
            # Вариант не прошёл (например, слишком большой) — пробуем качество ниже

        if path:
            downloaded.append((path, media_type or "photo"))
            continue

        # Ни один вариант не влез в лимит — отправляем превью-кадр как фото
        if thumb and thumb not in candidates:
            print("  🖼️ Ни один вариант видео не влез в лимит — отправляем превью-кадр как фото")
            thumb_path = download_media(thumb, "photo")
            if thumb_path:
                downloaded.append((thumb_path, "photo"))
    return downloaded


def send_media_group(caption, media_files):
    """
    Отправляет несколько медиа ОДНИМ альбомом (sendMediaGroup, до 10 файлов).
    media_files — список (path, type). Подпись вешается на первый файл.
    При ошибке альбома — fallback: отправка файлов по одному.
    Возвращает True, если что-то успешно отправлено.
    """
    base_url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"
    n = len(media_files)
    print(f"  📤 Отправка альбома из {n} медиа...")

    # Собираем JSON-описание альбома: подпись — только на первом элементе
    media_json = []
    for i, (_, m_type) in enumerate(media_files):
        entry = {
            "type": "video" if m_type == "video" else "photo",
            "media": f"attach://file{i}",
        }
        if i == 0:
            entry["caption"] = caption[:CAPTION_SAFE]
            entry["parse_mode"] = "HTML"
        media_json.append(entry)

    # Файлы передаются как multipart: file0, file1, ...
    files = {}
    handles = []
    for i, (path, _) in enumerate(media_files):
        fh = open(path, "rb")  # noqa: SIM115
        handles.append(fh)
        files[f"file{i}"] = (os.path.basename(path), fh)

    try:
        resp = request_with_retry(
            "POST",
            f"{base_url}/sendMediaGroup",
            data={"chat_id": TG_CHAT_ID, "media": json.dumps(media_json)},
            files=files,
            timeout=TG_MEDIA_TIMEOUT,
        )
        if resp.ok:
            print("  ✅ Альбом успешно отправлен!")
            # Успех — подчищаем временные файлы
            for path, _ in media_files:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            return True
        print(f"  ❌ Telegram API ошибка (sendMediaGroup): {resp.status_code} — {resp.text[:300]}")
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ Ошибка отправки альбома: {type(e).__name__}: {e}")
    finally:
        for fh in handles:
            fh.close()

    # Fallback: альбом не прошёл — отправляем по одному, чтобы пост не потерялся
    print("  🔁 Fallback: отправка медиа по одному...")
    ok_any = False
    for i, (path, m_type) in enumerate(media_files):
        cap = caption if i == 0 else "📎 Дополнительное медиа из того же поста"
        if send_telegram_message(cap, path, m_type):
            ok_any = True
    return ok_any


def run_parser():
    client = ApifyClient(APIFY_TOKEN)
    sent_ids = load_sent_ids()

    # Окно выборки считается от последнего УСПЕШНОГО запуска (см. compute_since_time)
    run_input = {
        "twitterHandles": TWITTER_HANDLES,
        "translateTo": TRANSLATE_TO,
        "maxItems": MAX_ITEMS,
        "queryType": QUERY_TYPE,
        "maxItemsPerTarget": MAX_ITEMS_PER_TARGET,
        "sinceTime": compute_since_time(),
    }

    print("🚀 Запускаем скрапер в Apify...")
    run = client.actor(ACTOR_ID).call(run_input=run_input)

    # Совместимость версий apify-client:
    #   новые (>=1.7) возвращают объект Run -> run.default_dataset_id
    #   старые возвращают dict        -> run["defaultDatasetId"]
    if isinstance(run, dict):
        dataset_id = run.get("defaultDatasetId") or run.get("default_dataset_id")
    else:
        dataset_id = run.default_dataset_id
    dataset_items = client.dataset(dataset_id).list_items().items
    print(f"📦 Получено {len(dataset_items)} элементов\n")

    # Выборка прошла успешно — фиксируем время этого запуска как опорное.
    # Если упадёт ниже (сеть/Telegram), твиты уже записаны в sent_ids и не задублируются,
    # а следующий запуск перекроет окно на OVERLAP_MIN минут назад.
    save_last_run_ts()

    # Счётчики запуска — попадут в итоговую строку лога
    stats = {
        "total": 0,          # всего элементов из выборки
        "duplicates": 0,     # пропущены как уже отправленные
        "new_tweets": 0,     # новых (не в sent_ids)
        "filtered": 0,       # отфильтрованы по стоп-фразам
        "replies": 0,        # пропущены как ответы (реплаи)
        "retweets": 0,       # пропущены как репосты (ретвиты)
        "sent_album": 0,     # отправлено альбомом (2+ медиа)
        "sent_single": 0,    # отправлено с одиночным медиа
        "sent_text": 0,      # отправлено только текстом
        "failed": 0,         # НЕ отправлены (повтор при следующем запуске)
        "errors": 0,         # упали с исключением при обработке
    }

    for item in reversed(dataset_items):
        stats["total"] += 1
        try:
            process_tweet(item, sent_ids, stats)
        except Exception as e:  # noqa: BLE001
            # Один проблемный твит не должен останавливать весь запуск
            stats["errors"] += 1
            print(f"❌ Ошибка обработки твита {item.get('id')}: {type(e).__name__}: {e}\n")
            continue

    # Итоговая сводка запуска — всегда печатается последней, легко ищется по "📊"
    print(
        "\n📊 Итог запуска:"
        f" получено {stats['total']}"
        f" | дублей пропущено {stats['duplicates']}"
        f" | новых {stats['new_tweets']}"
        f" | отфильтровано {stats['filtered']}"
        f" | реплаев пропущено {stats['replies']}"
        f" | ретвитов пропущено {stats['retweets']}"
        f" | отправлено {stats['sent_album'] + stats['sent_single'] + stats['sent_text']}"
        f" (альбомов {stats['sent_album']}, с медиа {stats['sent_single']}, текстом {stats['sent_text']})"
        f" | ошибок отправки {stats['failed']}"
        f" | исключений {stats['errors']}"
    )


def _inc(stats, key):
    """Безопасное увеличение счётчика (если stats не передан — ничего не делает)."""
    if stats is not None:
        stats[key] += 1


def is_reply(item, text):
    """Определяет, является ли твит ответом (реплаем) другому пользователю.

    Проверяем в двух источниках:
    1. Поля, которые отдаёт скрапер (набор зависит от актора):
       isReply, inReplyToId, inReplyToStatusId, inReplyToUsername, inReplyToUserId.
    2. Резервный признак: реплай на X почти всегда начинается с @упоминания
       (как у @ClutchNumbers: "@hoopswithavery You don't understand...").
    """
    for field in ("isReply", "inReplyToId", "inReplyToStatusId",
                  "inReplyToUsername", "inReplyToUserId"):
        if item.get(field):
            return True
    first = (text or "").lstrip()
    return first.startswith("@")


def is_retweet(item, text):
    """Определяет, является ли твит репостом (ретвитом) чужого поста.

    Признаки:
    1. Поля от скрапера (набор зависит от актора): isRetweet, retweetedStatusId,
       retweeted_status_id, isNativeRetweet.
    2. Классический маркер: текст начинается с "RT @user:" — как у
       @HaterReport: "RT @currypistonn: LSU lost 5 games...".
    """
    for field in ("isRetweet", "retweetedStatusId", "retweeted_status_id",
                  "isNativeRetweet"):
        if item.get(field):
            return True
    first = (text or "").lstrip()
    return first.startswith("RT @")


def process_tweet(item, sent_ids, stats=None):
    author = item.get("author", {}).get("name", "Unknown") if isinstance(item.get("author"), dict) else "Unknown"
    text = item.get("text")
    tweet_id = str(item.get("id"))

    if not tweet_id:
        return
    if tweet_id in sent_ids:
        _inc(stats, "duplicates")
        return

    _inc(stats, "new_tweets")
    raw_text = text or ""

    # Фильтр репостов: чужие посты, перепощенные monitored-аккаунтом, не публикуются
    if SKIP_RETWEETS and is_retweet(item, raw_text):
        print(f"⏭ Твит {tweet_id} — ретвит чужого поста, пропущен")
        save_sent_id(tweet_id)   # чтобы не проверять его при каждом запуске
        _inc(stats, "retweets")
        return

    # Фильтр реплаев: ответы другим пользователям не публикуются
    if SKIP_REPLIES and is_reply(item, raw_text):
        print(f"⏭ Твит {tweet_id} — ответ (реплай), пропущен")
        save_sent_id(tweet_id)   # чтобы не проверять его при каждом запуске
        _inc(stats, "replies")
        return

    # Фильтр по стоп-фразам: твит пропускается, ID помечается как обработанный
    if any(phrase.lower() in raw_text.lower() for phrase in BLOCK_PHRASES):
        print(f"⏭ Твит {tweet_id} отфильтрован по стоп-фразе")
        save_sent_id(tweet_id)   # чтобы не проверять его при каждом запуске
        _inc(stats, "filtered")
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
    media_list = resolve_all_media(item, tweet_id)

    media_files = []
    if media_list:
        print(f"🖼️  Медиа: {len(media_list)} шт. ({', '.join(t for _, t, _ in media_list)})")
        media_files = download_media_list(media_list)
        if not media_files:
            print("📝 Ни одно медиа не скачалось — отправим текстом")
    else:
        print("📝 Твит без доступного медиа")

    if len(media_files) >= 2:
        # Несколько медиа — одним альбомом
        sent_ok = send_media_group(formatted_post, media_files)
        sent_kind = "sent_album"
    else:
        # Одиночное медиа (или его отсутствие) — как раньше
        media_path = media_files[0][0] if media_files else None
        media_type = media_files[0][1] if media_files else "photo"
        sent_ok = send_telegram_message(formatted_post, media_path, media_type)
        sent_kind = "sent_single" if media_path else "sent_text"

    if sent_ok:
        # ID сохраняем ТОЛЬКО после успешной отправки.
        # Если упало (сеть/прокси/Telegram) — не сохраняем, и следующий запуск
        # (окно перекрывается на OVERLAP_MIN) повторит попытку.
        save_sent_id(tweet_id)
        _inc(stats, sent_kind)
        print(f"✅ Обработан твит {tweet_id}\n")
    else:
        _inc(stats, "failed")
        print(f"⏳ Твит {tweet_id} НЕ отправлен — повторим при следующем запуске\n")

    time.sleep(2)


if __name__ == "__main__":
    run_parser()