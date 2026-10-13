#!/usr/bin/env python3
"""Пересылка достоверно опознанных писем OnlineTours о снижении цены в Telegram.

Не выполняет скрейпинг сайта и не считает цену пакетного тура ценой номера.
OnlineTours сам определяет снижение стоимости по созданной пользователем подписке.
GitHub Actions проверяет почту и пересылает совпавшие оповещения.
"""
from __future__ import annotations

import hashlib
import html
import imaplib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode, urlsplit

LOG = logging.getLogger('onlinetours_email_watch')
HOME = Path(__file__).resolve().parent
HOTEL_WATCHLIST = HOME / 'hotel_watchlist.json'
STATE_PATH = HOME / 'state' / 'hotel_email_ids.json'
ALLOWED_SENDERS = ('onlinetours.ru', 'onlinetours.travel')
DROP_RE = re.compile(r'(цена\s+сниз|снижени[ея]\s+цен|цен[аы]\s+упал|подешевел|стало\s+дешевле|'
                     r'цена\s+стала\s+ниже|снижен[аы]\s+цен|price\s+drop)', re.IGNORECASE)
CONFIRM_RE = re.compile(r'(подтвердите\s+подписку|вы\s+подписалис|подписка\s+оформлена|'
                        r'подписат[ься]\s+на\s+снижение|confirm\s+your\s+subscription)', re.IGNORECASE)


class TextFromHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('p', 'div', 'li', 'br', 'tr'):
            self.parts.append(' ')

    def handle_endtag(self, tag):
        if tag in ('script', 'style') and self.hidden:
            self.hidden -= 1
        if tag in ('p', 'div', 'li', 'tr'):
            self.parts.append(' ')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def norm(text: str) -> str:
    text = html.unescape(text).casefold().replace('ё', 'е')
    text = re.sub(r'[^\w\s]', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def _header(value: object) -> str:
    if value is None:
        return ''
    try:
        return str(make_header(decode_header(str(value))))
    except Exception:
        return str(value)


def _mail_text(msg: EmailMessage) -> str:
    plain: list[str] = []
    rendered: list[str] = []
    pieces = msg.walk() if msg.is_multipart() else [msg]
    for item in pieces:
        if item.get_content_disposition() == 'attachment':
            continue
        content_type = item.get_content_type()
        if content_type not in ('text/plain', 'text/html'):
            continue
        try:
            data = item.get_content()
        except (UnicodeError, LookupError, ValueError):
            continue
        if not isinstance(data, str):
            continue
        if content_type == 'text/plain':
            plain.append(data)
        else:
            parser = TextFromHTML()
            parser.feed(data)
            rendered.append(' '.join(parser.parts))
    return '\n'.join(plain or rendered)[:60000]


@dataclass(frozen=True)
class Hotel:
    name: str
    stars: int
    online_tours_rating: float
    all_inclusive_available: bool
    url: str
    aliases: tuple[str, ...]

    @property
    def slug(self) -> str:
        return hashlib.sha256(self.url.encode('utf-8')).hexdigest()[:16]


def load_watchlist(path: Path = HOTEL_WATCHLIST) -> list[Hotel]:
    data = json.loads(path.read_text(encoding='utf-8'))
    hotels: list[Hotel] = []
    if not isinstance(data, list):
        raise ValueError('hotel_watchlist.json должен быть списком')
    for item in data:
        h = Hotel(item['name'], int(item['stars']), float(item['online_tours_rating']),
                  item['all_inclusive_available'] is True, item['url'],
                  tuple(item.get('aliases') or [item['name']]))
        domain = urlsplit(h.url)
        if (h.stars not in (4, 5) or h.online_tours_rating < 8 or
                not h.all_inclusive_available or domain.scheme != 'https' or
                domain.hostname != 'www.onlinetours.ru' or
                not domain.path.startswith('/oteli/egypt/')):
            raise ValueError(f'Недопустимый отель в списке: {h.name}')
        if not any(len(norm(alias)) >= 8 for alias in h.aliases):
            raise ValueError(f'Недостаточно длинное название отеля: {h.name}')
        hotels.append(h)
    return hotels


def match_hotels(subject: str, body: str, hotels: list[Hotel]) -> list[Hotel]:
    # Никаких промо-рассылок: нужно явное сообщение о снижении цены, а не
    # приглашение подписаться; конкретный отель должен совпасть с allowlist.
    if not DROP_RE.search(subject + ' ' + body[:1500]):
        return []
    if CONFIRM_RE.search(subject + ' ' + body[:450]):
        return []
    normalized = norm(subject + ' ' + body)
    selected = [h for h in hotels if any(norm(alias) in normalized for alias in h.aliases)]
    # Общее рекламное письмо со списком отелей не трактуем как снижение цены
    # каждого из них. На оповещение принимаем только один конкретный отель.
    return selected if len(selected) == 1 else []


def trusted_sender(message: EmailMessage) -> bool:
    sender = parseaddr(_header(message.get('From')))[1].casefold()
    if '@' not in sender:
        return False
    domain = sender.rsplit('@', 1)[1]
    return any(domain == allowed or domain.endswith('.' + allowed) for allowed in ALLOWED_SENDERS)


def get_email_date(msg: EmailMessage) -> datetime | None:
    raw = msg.get('Date')
    if not raw:
        return None
    try:
        d = parsedate_to_datetime(str(raw))
        if d.tzinfo is None:
            return None
        return d.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def parse_notification(raw: bytes, hotels: list[Hotel], now: datetime | None = None,
                       allowed_age_days: int = 3) -> tuple[list[Hotel], str] | None:
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception:
        return None
    if not trusted_sender(msg):
        return None
    sent = get_email_date(msg)
    now = now or datetime.now(timezone.utc)
    if sent is None or sent < now - timedelta(days=allowed_age_days) or sent > now + timedelta(hours=1):
        return None
    subject = _header(msg.get('Subject'))
    body = _mail_text(msg)
    hotels_found = match_hotels(subject, body, hotels)
    if not hotels_found:
        return None
    # В общедоступной истории храним только SHA-256, не адрес или email.
    message_id = _header(msg.get('Message-ID'))
    fingerprint = hashlib.sha256(raw).hexdigest() if not message_id else hashlib.sha256(
        (message_id + '|' + hashlib.sha256(raw).hexdigest()).encode('utf-8')).hexdigest()
    return hotels_found, fingerprint


def load_sent(path: Path = STATE_PATH) -> set[str]:
    if not path.exists():
        return set()
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('hashes'), list):
        raise ValueError('История отельных уведомлений повреждена')
    return set(s for s in data['hashes'] if isinstance(s, str) and re.fullmatch(r'[a-f0-9]{64}', s))


def save_sent(hashes: set[str], path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Верхний предел: поток пользовательских писем ограничивается временем действия мониторинга.
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix='.hotels-', suffix='.tmp', delete=False) as f:
        p = Path(f.name)
        json.dump({'version': 1, 'hashes': sorted(hashes)}, f, indent=2)
        f.write('\n')
    p.replace(path)


def make_hotel_link(hotel: Hotel, arrival_from: str = '2026-10-16',
                    arrival_to: str = '2026-10-20',
                    departure_from: str = '2026-10-29',
                    departure_to: str = '2026-11-01',
                    adults: int = 2) -> str:
    """Ссылка на поиск *проживания*, с гибкими датами, как у авиабилетов.

    Сайт может предложить сочетания за пределами выбранного окна выезда,
    поэтому точные даты конкретного тарифа пользователь проверяет перед покупкой.
    """
    from datetime import date
    shortest = (date.fromisoformat(departure_from) - date.fromisoformat(arrival_to)).days
    longest = (date.fromisoformat(departure_to) - date.fromisoformat(arrival_from)).days
    if not 1 <= shortest <= longest <= 30:
        raise ValueError('Неподдерживаемые сроки проживания')
    params = {'adults': adults, 'kids': 0, 'start_from': arrival_from,
              'start_to': arrival_to, 'duration_from': shortest,
              'duration_to': longest, 'ticket_strategy': 'exclude'}
    return hotel.url + '?' + urlencode(params)


def hotel_message(h: Hotel) -> str:
    u = html.escape(make_hotel_link(h), quote=True)
    return (f'🏨 <b>OnlineTours: пришло письмо о снижении цены</b>\n\n'
            f'<b>{html.escape(h.name)}</b> — {h.stars}★\n'
            f'⭐ Рейтинг OnlineTours: {h.online_tours_rating:.1f}/10 '
            f'(при составлении списка)\n'
            f'🍽 В отеле встречаются тарифы «всё включено».\n'
            f'📆 Заезд: 16–20 октября, выезд: 29 октября – 1 ноября.\n\n'
            f'⚠️ <b>Это сигнал из письма, не подтверждённая цена номера!</b> '
            f'Проверь в открывшемся поиске точные даты, питание, количество гостей '
            f'и вариант <b>«Проживание», без перелёта</b>.\n'
            f'🔗 <a href="{u}">Посмотреть отель и цену на OnlineTours</a>')


def credentials_enabled() -> bool:
    return os.environ.get('HOTEL_EMAIL_ENABLED', '').strip().lower() in ('1', 'true', 'yes')


def poll_mailbox(send: Callable[[str], None], *,
                 imap_factory=None,
                 history_path: Path = STATE_PATH,
                 watchlist_path: Path = HOTEL_WATCHLIST,
                 now: datetime | None = None) -> dict[str, int]:
    """Читает доверенные письма без удаления / перемещения, пересылает новые.

    Требует отдельного email приложения (например, Gmail + пароль приложения),
    который задаётся только GitHub Secrets. Никакого пароля в коде.
    """
    if not credentials_enabled():
        return {'checked': 0, 'matched': 0, 'sent': 0, 'enabled': 0}
    username = os.environ.get('HOTEL_IMAP_USER', '').strip()
    password = os.environ.get('HOTEL_IMAP_PASSWORD', '')
    host = os.environ.get('HOTEL_IMAP_HOST', 'imap.gmail.com').strip()
    if not username or not password:
        raise ValueError('HOTEL_EMAIL_ENABLED=1: нужны secrets HOTEL_IMAP_USER и HOTEL_IMAP_PASSWORD')
    if not re.fullmatch(r'[a-zA-Z0-9.-]+', host):
        raise ValueError('Недопустимый HOTEL_IMAP_HOST')
    hotels = load_watchlist(watchlist_path)
    history = load_sent(history_path)
    now = now or datetime.now(timezone.utc)
    checked = matched = sent = 0
    if imap_factory is None:
        imap_factory = imaplib.IMAP4_SSL
    imap = imap_factory(host)
    try:
        imap.login(username, password)
        status, _ = imap.select('INBOX', readonly=True)
        if status != 'OK':
            raise RuntimeError('IMAP: не удалось выбрать INBOX')
        # Даты выборки по UTC, полезна небольшая страховка против часовых поясов.
        since = (now - timedelta(days=3)).strftime('%d-%b-%Y')
        status, data = imap.uid('SEARCH', None, 'SINCE', since, 'FROM', '"onlinetours"')
        if status != 'OK':
            raise RuntimeError('IMAP: поиск писем завершился ошибкой')
        # Не обрабатываем больше 120 новых/последних писем за цикл.
        for uid in (data[0].split() if data and data[0] else [])[-120:]:
            status, responses = imap.uid('FETCH', uid, '(RFC822)')
            if status != 'OK':
                continue
            payload = next((x[1] for x in responses if isinstance(x, tuple) and
                            len(x) > 1 and isinstance(x[1], bytes)), None)
            if not payload:
                continue
            checked += 1
            found = parse_notification(payload, hotels, now)
            if found is None:
                continue
            choices, digest = found
            for hotel in choices:
                identifier = hashlib.sha256((digest + '|' + hotel.slug).encode('ascii')).hexdigest()
                if identifier in history:
                    continue
                matched += 1
                send(hotel_message(hotel))
                history.add(identifier)
                save_sent(history, history_path)  # не теряем состояние уже отправленных уведомлений
                sent += 1
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    LOG.info('OnlineTours email: проверено писем %d; совпадений %d; отправлено %d', checked, matched, sent)
    return {'checked': checked, 'matched': matched, 'sent': sent, 'enabled': 1}
