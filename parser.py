#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shams Charania (x.com/ShamsCharania) -> Telegram-канал.

Как это работает:
  1. Берёт RSS-ленту профиля через RSSHub (список инстансов с fallback)
     или через CUSTOM_RSS_URL (свой RSSHub / любой совместимый RSS).
  2. Находит новые посты (state.json), переводит на русский, скачивает картинки.
  3. Публикует в канал через Telethon (MTProto, сессия вашего аккаунта).
  4. Обновляет state.json (GitHub Actions коммитит его обратно в репозиторий).

Запуск:
    python parser.py              # рабочий режим
    python parser.py --dry-run    # только показать, что было бы отправлено

Все настройки — через переменные окружения (см. .env.example).
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import feedparser
import requests

log = logging.getLogger("shams2tg")

# ----------------------------------------------------------------------------
# Конфигурация
# ----------------------------------------------------------------------------


def _load_dotenv(path: str = ".env") -> None:
    """Мини-загрузчик .env без внешних зависимостей."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

# Публичные RSSHub-инстансы. Роут /twitter/user у многих отключён ("Twitter API
# is not configured") — поэтому список можно переопределить через RSSHUB_INSTANCES.
DEFAULT_INSTANCES = ",".join(
    [
        "https://rsshub.app",
        "https://rsshub.rssforever.com",
        "https://rsshub.pseudoyu.com",
        "https://rsshub.ktachibana.party",
        "https://rsshub.moeyy.cn",
    ]
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    target: str = os.getenv("TARGET_USER", "ShamsCharania")
    rsshub_instances: list = field(
        default_factory=lambda: [
            i.strip().rstrip("/")
            for i in os.getenv("RSSHUB_INSTANCES", DEFAULT_INSTANCES).split(",")
            if i.strip()
        ]
    )
    custom_rss_url: str = os.getenv("CUSTOM_RSS_URL", "").strip()
    test_feed: str = os.getenv("TEST_FEED", "").strip()

    api_id: int = _env_int("TELEGRAM_API_ID", 0)
    api_hash: str = os.getenv("TELEGRAM_API_HASH", "").strip()
    session: str = os.getenv("TELEGRAM_SESSION", "").strip()
    channel: str = os.getenv("TELEGRAM_CHANNEL", "").strip()

    state_file: str = os.getenv("STATE_FILE", "state.json")
    max_posts_per_run: int = _env_int("MAX_POSTS_PER_RUN", 5)
    max_age_hours: int = _env_int("MAX_AGE_HOURS", 24)
    skip_retweets: bool = _env_bool("SKIP_RETWEETS", True)

    translator: str = os.getenv("TRANSLATOR", "google_free").strip().lower()
    deepl_api_key: str = os.getenv("DEEPL_API_KEY", "").strip()

    dry_run: bool = "--dry-run" in sys.argv


CFG = Config()

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
TELEGRAM_LIMIT = 4096   # лимит одного сообщения Telegram
CAPTION_LIMIT = 1024    # лимит подписи к медиа-альбому

# ----------------------------------------------------------------------------
# Посты
# ----------------------------------------------------------------------------


@dataclass
class Post:
    pid: str
    url: str
    text: str
    published: datetime
    images: list = field(default_factory=list)
    is_retweet: bool = False


TAG_RE = re.compile(r"<[^>]+>")
STATUS_RE = re.compile(r"/status(?:es)?/(\d+)")
IMG_URL_RE = re.compile(r"https?://pbs\.twimg\.com/media/[^\s\"'<>]+")
EXT_RE = re.compile(r"\.(jpe?g|png|webp)(?:\?|$)", re.I)


def _clean_html(raw: str) -> str:
    text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", raw or "")
    text = TAG_RE.sub("", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def entry_to_post(entry) -> Optional[Post]:
    """RSS-запись -> Post. Возвращает None, если запись не похожа на твит."""
    link = getattr(entry, "link", "") or ""
    m = STATUS_RE.search(link)
    if not m:
        return None

    content = ""
    if getattr(entry, "content", None):
        content = entry.content[0].get("value", "") or ""
    content = content or getattr(entry, "summary", "") or getattr(entry, "description", "") or ""

    text = _clean_html(content)
    if not text:
        text = _clean_html(getattr(entry, "title", ""))  # RSSHub дублирует текст в title
    if not text:
        return None

    published = datetime.now(timezone.utc)
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        try:
            published = datetime.fromtimestamp(time.mktime(parsed), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass

    # Картинки: media:content / media:thumbnail / <img> в HTML-описании.
    images: list = []
    media_items = list(entry.get("media_content") or []) + list(entry.get("media_thumbnail") or [])
    for media in media_items:
        url = (media.get("url") or "").strip()
        if not url:
            continue
        mtype = (media.get("type") or media.get("medium") or "").lower()
        if mtype.startswith("image") or (not mtype and EXT_RE.search(url)):
            images.append(url)
    # <img src="..."> в HTML: ищем по необработанному HTML (теги убираются
    # _clean_html, поэтому именно здесь), предварительно сняв &amp;
    images += IMG_URL_RE.findall(html.unescape(content))
    seen: set = set()
    images = [i for i in images if not (i in seen or seen.add(i))][:4]  # max 4 фото

    title = getattr(entry, "title", "") or ""
    is_rt = text.startswith("RT @") or title.startswith("RT @")

    return Post(pid=m.group(1), url=link, text=text, published=published,
                images=images, is_retweet=is_rt)


# ----------------------------------------------------------------------------
# Источники (RSS)
# ----------------------------------------------------------------------------


def _fetch_url(url: str, timeout: int = 25) -> Optional[bytes]:
    try:
        r = requests.get(url, headers=HTTP_HEADERS, timeout=timeout)
        if r.status_code != 200:
            log.warning("HTTP %s от %s", r.status_code, url)
            return None
        return r.content
    except requests.RequestException as exc:
        log.warning("Ошибка запроса %s: %s", url, exc)
        return None


def fetch_feed() -> tuple:
    """Пробует источники по очереди. Возвращает (посты, url источника)."""
    sources: list = []
    if CFG.custom_rss_url:
        sources.append(CFG.custom_rss_url)
    sources += [f"{inst}/twitter/user/{CFG.target}" for inst in CFG.rsshub_instances]

    for url in sources:
        raw = _fetch_url(url)
        if raw is None:
            continue
        feed = feedparser.parse(raw)
        entries = feed.get("entries") or []
        if not entries:
            log.warning("Источник %s отдал ленту без записей, пробую следующий", url)
            continue
        posts = [p for p in (entry_to_post(e) for e in entries) if p]
        if posts:
            log.info("Получено %d записей из %s", len(posts), url)
            return posts, url
        log.warning("Источник %s отдал %d записей, но без валидных твитов", url, len(entries))
    return [], ""


def fetch_feed_from_file(path: str) -> tuple:
    """Чтение локального RSS-файла (для тестов, TEST_FEED=...)."""
    raw = Path(path).read_bytes()
    feed = feedparser.parse(raw)
    posts = [p for p in (entry_to_post(e) for e in feed.entries) if p]
    return posts, f"test:{path}"


# ----------------------------------------------------------------------------
# Состояние
# ----------------------------------------------------------------------------


def load_state() -> dict:
    p = Path(CFG.state_file)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.warning("state.json повреждён, начинаю заново")
    return {"posted_ids": [], "last_run": None}


def save_state(state: dict) -> None:
    Path(CFG.state_file).write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


# ----------------------------------------------------------------------------
# Перевод
# ----------------------------------------------------------------------------

URL_TOKEN_FMT = "URLOK{i}ENDOK"


class Translator:
    """Подключаемые бэкенды: deepl / google_free / off."""

    def __init__(self, backend: str, deepl_key: str):
        self.backend = backend
        self.deepl_key = deepl_key
        self.cache: dict = {}
        self._gt = None

    def _google(self, text: str) -> Optional[str]:
        if self._gt is None:
            try:
                from deep_translator import GoogleTranslator

                self._gt = GoogleTranslator(source="auto", target="ru")
            except Exception as exc:
                log.error("deep-translator недоступен: %s", exc)
                return None
        for delay in (1, 5, 12):
            try:
                return self._gt.translate(text)
            except Exception as exc:
                log.warning("Google-переводчик: %s, повтор через %dс",
                            type(exc).__name__, delay)
                time.sleep(delay)
        return None

    def _deepl(self, text: str) -> Optional[str]:
        host = (
            "https://api-free.deepl.com"
            if self.deepl_key.endswith(":fx")
            else "https://api.deepl.com"
        )
        try:
            r = requests.post(
                f"{host}/v2/translate",
                data={"auth_key": self.deepl_key, "text": text, "target_lang": "RU"},
                timeout=30,
            )
            if r.status_code == 200:
                return r.json()["translations"][0]["text"]
            log.warning("DeepL HTTP %s: %s", r.status_code, r.text[:200])
        except Exception as exc:
            log.warning("DeepL: %s", exc)
        return None

    def translate(self, text: str) -> Optional[str]:
        """Вернёт перевод или None (тогда публикуем оригинал)."""
        if self.backend == "off" or not text.strip():
            return None
        if text in self.cache:
            return self.cache[text]

        # Защищаем ссылки от переводчика токенами
        urls = re.findall(r"https?://\S+", text)
        protected = text
        for i, u in enumerate(urls):
            protected = protected.replace(u, URL_TOKEN_FMT.format(i=i))

        result = self._deepl(protected) if self.backend == "deepl" else self._google(protected)

        if result:
            for i, u in enumerate(urls):
                result = result.replace(URL_TOKEN_FMT.format(i=i), u)
            result = result.strip()
            self.cache[text] = result
        return result


# ----------------------------------------------------------------------------
# Форматирование сообщения
# ----------------------------------------------------------------------------


def build_message(post: Post, translated: Optional[str]) -> str:
    header = "🏀 <b>Shams Charania</b> · ESPN, NBA-инсайдер"

    body = (translated or post.text).strip()
    if len(body) > 2600:
        body = body[:2600].rstrip() + "…"

    parts = [header, "", html.escape(body)]

    # Раздел «Оригинал» нужен, только если реально есть перевод
    if translated and translated.strip() != post.text.strip():
        orig = post.text.strip()
        if len(orig) > 800:
            orig = orig[:800].rstrip() + "…"
        parts += ["", f"<i>Оригинал:</i> {html.escape(orig)}"]

    ts = post.published.strftime("%d.%m.%Y %H:%M UTC")
    parts += ["", f"🕒 {ts} · <a href=\"{html.escape(post.url, quote=True)}\">x.com</a>"]

    msg = "\n".join(parts).strip()
    if len(msg) > TELEGRAM_LIMIT:
        msg = msg[: TELEGRAM_LIMIT - 1] + "…"
    return msg


# ----------------------------------------------------------------------------
# Медиа
# ----------------------------------------------------------------------------


def download_images(urls: list, tmpdir: Path) -> list:
    files: list = []
    for i, url in enumerate(urls):
        try:
            r = requests.get(url, headers=HTTP_HEADERS, timeout=30)
            if r.status_code != 200:
                log.warning("Картинка %s: HTTP %s", url, r.status_code)
                continue
            data = r.content
            if not data or len(data) > 10 * 1024 * 1024:
                log.warning("Картинка %s пропущена (пустая или > 10 МБ)", url)
                continue
            ext = ".jpg"
            m = EXT_RE.search(url)
            if m:
                ext = {"jpeg": ".jpg", "jpg": ".jpg", "png": ".png", "webp": ".webp"}[
                    m.group(1).lower()
                ]
            path = tmpdir / f"img_{i}{ext}"
            path.write_bytes(data)
            files.append(path)
            log.info("Скачана картинка %s (%d КБ)", url, len(data) // 1024)
        except Exception as exc:
            log.warning("Картинка %s: %s", url, exc)
    return files


# ----------------------------------------------------------------------------
# Telegram (Telethon)
# ----------------------------------------------------------------------------


async def _send_with_retry(client, channel, msg: str, images: list) -> None:
    from telethon import errors

    for attempt in range(3):
        try:
            if images:
                caption = msg if len(msg) <= CAPTION_LIMIT else None
                sent = await client.send_file(
                    channel, [str(p) for p in images],
                    caption=caption, parse_mode="html",
                )
                if caption is None:  # длинный текст — отдельным сообщением ответом на альбом
                    first = sent[0] if isinstance(sent, list) else sent
                    await client.send_message(channel, msg, parse_mode="html",
                                              reply_to=first.id)
            else:
                await client.send_message(channel, msg, parse_mode="html")
            return
        except errors.FloodWaitError as exc:
            wait = exc.seconds + 5
            log.warning("FloodWait от Telegram: сплю %dс (попытка %d)", wait, attempt + 1)
            await asyncio.sleep(wait)
        except (errors.ChannelPrivateError, errors.ChatWriteForbiddenError,
                errors.UserBannedInChannelError) as exc:
            log.error("Нет прав на отправку в канал: %s", exc)
            raise
    raise RuntimeError("Не удалось отправить сообщение после 3 попыток")


async def send_to_telegram(outbox: list) -> None:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    entity = int(CFG.channel) if re.fullmatch(r"-?\d+", CFG.channel) else CFG.channel
    async with TelegramClient(StringSession(CFG.session), CFG.api_id, CFG.api_hash) as client:
        me = await client.get_me()
        log.info("Telegram: вошли как @%s (id %s)", getattr(me, "username", None), me.id)
        channel = await client.get_entity(entity)
        log.info("Канал: %s", getattr(channel, "title", entity))

        for msg, images in outbox:
            await _send_with_retry(client, channel, msg, images)
            log.info("Опубликовано (%d фото): %.60s…", len(images), msg.replace("\n", " "))
            await asyncio.sleep(3)  # антиспам-пауза между постами


# ----------------------------------------------------------------------------
# Основной цикл
# ----------------------------------------------------------------------------


def print_outbox(outbox: list) -> None:
    for msg, images in outbox:
        print("\n" + "=" * 64)
        print(msg)
        if images:
            print(f"[картинки: {', '.join(p.name for p in images)}]")


async def main() -> int:
    logging.basicConfig(
        level=logging.DEBUG if CFG.dry_run else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # --- 1. Получаем ленту -------------------------------------------------
    if CFG.test_feed:
        posts, source = fetch_feed_from_file(CFG.test_feed)
    else:
        posts, source = fetch_feed()

    if not posts:
        log.error("Не удалось получить ленту ни из одного источника. "
                  "Проверьте RSSHUB_INSTANCES / CUSTOM_RSS_URL (см. README, раздел 6).")
        return 1

    now = datetime.now(timezone.utc)
    fresh = [p for p in posts if now - p.published <= timedelta(hours=CFG.max_age_hours)]
    if CFG.skip_retweets:
        fresh = [p for p in fresh if not p.is_retweet]
    log.info("Свежих постов (<= %dч, без ретвитов): %d из %d",
             CFG.max_age_hours, len(fresh), len(posts))

    # --- 2. Dry-run: только показать --------------------------------------
    if CFG.dry_run or CFG.test_feed:
        tr = Translator(CFG.translator, CFG.deepl_api_key)
        outbox = []
        for post in sorted(fresh, key=lambda p: p.published, reverse=True)[: CFG.max_posts_per_run]:
            translated = tr.translate(post.text)
            outbox.append((build_message(post, translated), []))
            if post.images:
                log.info("Твит %s: найдено %d картинок: %s",
                         post.pid, len(post.images), ", ".join(post.images))
        print_outbox(outbox)
        log.info("DRY-RUN завершён: показано %d сообщений, state не изменён", len(outbox))
        return 0

    # --- 3. Рабочий режим: state и отбор новых -----------------------------
    state = load_state()
    posted_ids = set(state.get("posted_ids") or [])

    if not posted_ids:
        # Первый запуск: тихо запоминаем текущие посты, чтобы не завалить
        # канал старыми твитами. Захотите увидеть последние — очистите state.json.
        state["posted_ids"] = [p.pid for p in posts][-500:]
        state["last_run"] = now.isoformat()
        save_state(state)
        log.info("Первый запуск: запомнил %d постов без публикации", len(posts))
        return 0

    new = sorted((p for p in fresh if p.pid not in posted_ids),
                 key=lambda p: p.published)
    if not new:
        log.info("Новых постов нет (в ленте %d)", len(posts))
        state["last_run"] = now.isoformat()
        save_state(state)
        return 0

    if len(new) > CFG.max_posts_per_run:
        log.warning("Новых постов %d — публикую %d самых свежих (остальные пропущены)",
                    len(new), CFG.max_posts_per_run)
        new = new[-CFG.max_posts_per_run:]

    # --- 4. Перевод, медиа, отправка ----------------------------------------
    if not (CFG.api_id and CFG.api_hash and CFG.session and CFG.channel):
        log.error("Не заданы TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_SESSION / "
                  "TELEGRAM_CHANNEL. Заполните их (см. .env.example).")
        return 2

    tr = Translator(CFG.translator, CFG.deepl_api_key)
    tmpdir = Path(tempfile.mkdtemp(prefix="shams2tg_"))
    outbox: list = []
    for post in new:
        translated = tr.translate(post.text)
        images = download_images(post.images, tmpdir) if post.images else []
        outbox.append((build_message(post, translated), images))

    await send_to_telegram(outbox)

    # --- 5. Фиксируем отправленное ------------------------------------------
    posted_ids |= {p.pid for p in new}
    state["posted_ids"] = sorted(posted_ids)[-500:]
    state["last_run"] = now.isoformat()
    save_state(state)
    log.info("Готово: опубликовано %d, всего в истории %d id",
             len(new), len(state["posted_ids"]))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
