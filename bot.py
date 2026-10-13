#!/usr/bin/env python3
"""Личный монитор дешёвых авиабилетов: Travelpayouts Data API -> Telegram.

Это API кэшированных цен. Багаж В НЁМ НЕ ПОДТВЕРЖДАЕТСЯ.
Не объявляйте ни один билет «с багажом» до проверки тарифа при покупке.
"""
from __future__ import annotations

import hashlib
import html
import logging
import os
import re
import signal
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / '.env')
LOG = logging.getLogger('egypt_flight_watch')
API = 'https://api.travelpayouts.com/aviasales/v3/prices_for_dates'
MOSCOW_CODES = {'MOW', 'SVO', 'DME', 'VKO', 'ZIA'}
AIRPORT_NAMES = {
    'MOW': 'Москва', 'SVO': 'Шереметьево', 'DME': 'Домодедово',
    'VKO': 'Внуково', 'ZIA': 'Жуковский', 'SSH': 'Шарм-эль-Шейх',
    'HRG': 'Хургада', 'CAI': 'Каир', 'SPX': 'Каир (Сфинкс)',
    'HBE': 'Александрия', 'RMF': 'Марса-Алам', 'LXR': 'Луксор',
    'ASW': 'Асуан', 'HMB': 'Сохаг', 'ATZ': 'Асьют',
    'DBB': 'Эль-Аламейн', 'TCP': 'Таба'
}
MONTHS_RU = ['', 'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
             'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря']


@dataclass(frozen=True)
class Rule:
    name: str
    origin: str
    destination: str
    start: date
    end: date


@dataclass(frozen=True)
class Deal:
    rule: Rule
    origin: str
    destination: str
    origin_airport: str
    destination_airport: str
    departure: datetime
    price: int
    transfers: int
    airline: str
    flight_number: str
    link: str

    @property
    def flight_key(self) -> str:
        # Стоимость не включаем: повторяем сообщение только при снижении цены.
        raw = ('|'.join(map(str, (
            self.rule.name, self.origin_airport, self.destination_airport,
            self.departure.isoformat(), self.transfers, self.airline,
            self.flight_number
        )))).encode('utf-8')
        return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    travelpayouts_token: str
    owner_telegram_user_id: int | None
    interval_seconds: int
    max_price: int
    max_alerts_per_check: int
    egypt_origins: tuple[str, ...]
    state_db: Path

    @classmethod
    def from_env(cls) -> Settings:
        tg = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
        tp = os.environ.get('TRAVELPAYOUTS_API_TOKEN', '').strip()
        if not re.fullmatch(r'\d+:[\w-]+', tg):
            raise ValueError('Задайте действующий TELEGRAM_BOT_TOKEN в файле .env')
        if not re.fullmatch(r'[a-zA-Z0-9_-]{16,128}', tp):
            raise ValueError('Задайте TRAVELPAYOUTS_API_TOKEN в файле .env')
        uid = os.environ.get('OWNER_TELEGRAM_USER_ID', '').strip()
        owner = int(uid) if uid else None
        origins = tuple(dict.fromkeys(
            c.strip().upper() for c in
            os.environ.get('EGYPT_ORIGINS', 'SSH').split(',')
            if c.strip()
        ))
        if not origins or any(not re.fullmatch(r'[A-Z]{3}', c) for c in origins):
            raise ValueError('EGYPT_ORIGINS должен содержать коды IATA (например, SSH,HRG)')
        minutes = int(os.environ.get('CHECK_INTERVAL_MINUTES', '15'))
        if not 1 <= minutes <= 1440:
            raise ValueError('CHECK_INTERVAL_MINUTES: допустимо от 1 до 1440')
        cap = int(os.environ.get('MAX_PRICE_RUB', '10000'))
        if cap <= 0:
            raise ValueError('MAX_PRICE_RUB должен быть положительным')
        max_alerts = int(os.environ.get('MAX_ALERTS_PER_CHECK', '10'))
        if not 1 <= max_alerts <= 30:
            raise ValueError('MAX_ALERTS_PER_CHECK: допустимо от 1 до 30')
        db = Path(os.environ.get('STATE_DB', 'state/flight_watch.sqlite3'))
        if not db.is_absolute():
            db = Path(__file__).resolve().parent / db
        return cls(tg, tp, owner, minutes * 60, cap, max_alerts, origins, db)


def rules_for(settings: Settings) -> list[Rule]:
    rules = [Rule('туда', 'MOW', 'SSH', date(2026, 10, 16), date(2026, 10, 20))]
    # Обратный рейс только из Шарм-эль-Шейха, не из других городов Египта.
    rules.append(Rule('обратно', 'SSH', 'MOW', date(2026, 10, 29), date(2026, 11, 1)))
    return rules


def months_in_period(first: date, last: date) -> list[str]:
    out = []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        out.append(f'{year:04d}-{month:02d}')
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def parse_departure(raw: object, origin: str) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        when = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if when.tzinfo:
            tz = 'Europe/Moscow' if origin in MOSCOW_CODES else 'Africa/Cairo'
            return when.astimezone(ZoneInfo(tz))
        return when
    except (ValueError, OverflowError):
        return None


def normalize_link(link: object, origin: str, destination: str, depart: date) -> str:
    # Кнопка всегда ведёт на Aviasales; внешние адреса из API не используем.
    search_fallback = f'https://www.aviasales.ru/search/{origin}{depart:%d%m}{destination}1'
    if not isinstance(link, str) or not link or link.startswith('//'):
        return search_fallback
    url = urljoin('https://www.aviasales.ru/', link)
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.hostname not in {'www.aviasales.ru', 'aviasales.ru'}:
        return search_fallback
    return url


def extract_records(payload: dict) -> list[dict]:
    data = payload.get('data', [])
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        return [r for r in data.values() if isinstance(r, dict)]
    return []


def parse_deals(payload: dict, rule: Rule, max_price: int, today: date) -> list[Deal]:
    result = []
    if payload.get('success') is not True:
        raise RuntimeError(f"Travelpayouts success=false ({str(payload.get('error'))[:100]})")
    for row in extract_records(payload):
        orig = str(row.get('origin') or rule.origin).upper()
        dest = str(row.get('destination') or rule.destination).upper()
        orig_air = str(row.get('origin_airport') or orig).upper()
        dest_air = str(row.get('destination_airport') or dest).upper()
        if rule.name == 'туда':
            if orig not in MOSCOW_CODES or dest != 'SSH':
                continue
        elif orig != rule.origin or dest not in MOSCOW_CODES:
            continue
        departure = parse_departure(row.get('departure_at'), orig)
        if departure is None or not (max(rule.start, today) <= departure.date() <= rule.end):
            continue  # Обязательно: Travelpayouts может вернуть ближайшую ЧУЖУЮ дату.
        if row.get('return_at'):
            continue  # Только билеты в одну сторону.
        try:
            raw_price = row.get('price')
            raw_transfers = row.get('transfers')
            if isinstance(raw_price, bool) or isinstance(raw_transfers, bool):
                continue
            price = int(raw_price)
            transfers = int(raw_transfers)
            if price <= 0 or price >= max_price or transfers < 0 or price != float(raw_price):
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        expire_str = row.get('expires_at')
        if expire_str:
            try:
                expires = datetime.fromisoformat(str(expire_str).replace('Z', '+00:00'))
                now = datetime.now(expires.tzinfo) if expires.tzinfo else datetime.now()
                if expires <= now:
                    continue
            except ValueError:
                continue  # Не будем слать просроченные или непонятные данные.
        link = normalize_link(row.get('link'), rule.origin, rule.destination, departure.date())
        result.append(Deal(rule, orig, dest, orig_air, dest_air, departure, price,
                           transfers, str(row.get('airline') or ''),
                           str(row.get('flight_number') or ''), link))
    return result


class Travelpayouts:
    def __init__(self, token: str, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers.update({
            'X-Access-Token': token,
            'Accept-Encoding': 'gzip, deflate',
            'User-Agent': 'PrivateFlightWatch/1.0 (Travelpayouts Data API)',
        })

    def fetch(self, rule: Rule, month: str, direct: bool) -> dict:
        params = {
            'origin': rule.origin, 'destination': rule.destination,
            'departure_at': month, 'one_way': 'true',
            'direct': 'true' if direct else 'false',
            'currency': 'rub', 'market': 'ru',
            'sorting': 'price', 'limit': 1000, 'page': 1,
        }
        # API-ключ НЕ вставляем в URL (не попадает в URL, историю и логи).
        response = self.session.get(API, params=params, timeout=(8, 25))
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError('Travelpayouts: JSON-ответ не является объектом')
        return payload


class State:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        self.db.execute('''CREATE TABLE IF NOT EXISTS offers (
            id TEXT PRIMARY KEY, best_price INTEGER NOT NULL,
            alerted_at TEXT NOT NULL)''')
        self.db.commit()

    def get(self, key: str, default: str = '') -> str:
        row = self.db.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
        return row[0] if row else default

    def put(self, key: str, value: str) -> None:
        self.db.execute('INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)', (key, value))
        self.db.commit()

    def need_alert(self, deal: Deal) -> bool:
        row = self.db.execute('SELECT best_price FROM offers WHERE id=?',
                              (deal.flight_key,)).fetchone()
        return row is None or deal.price < row[0]

    def mark_alerted(self, deal: Deal) -> None:
        self.db.execute('''INSERT INTO offers (id,best_price,alerted_at) VALUES (?,?,?)
                           ON CONFLICT(id) DO UPDATE SET
                           best_price=MIN(best_price,excluded.best_price),
                           alerted_at=excluded.alerted_at''',
                        (deal.flight_key, deal.price, datetime.now().isoformat()))
        self.db.commit()


def escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def deal_message(deal: Deal) -> str:
    route = ('Москва → Шарм-эль-Шейх' if deal.rule.name == 'туда' else
             'Шарм-эль-Шейх → Москва')
    date_part = f'{deal.departure.day} {MONTHS_RU[deal.departure.month]}'
    route_type = '✅ Прямой рейс' if deal.transfers == 0 else f'🔁 Пересадок: {deal.transfers}'
    airport_line = f'{deal.origin_airport} → {deal.destination_airport}'
    flight_info = (f'\n✈️ Рейс: {escape(deal.airline)} {escape(deal.flight_number)}'
                   if deal.airline and deal.flight_number else '')
    return (f'✈️ <b>Берём? Дешёвый вариант</b>\n\n'
            f'<b>{escape(route)}</b> (в одну сторону)\n'
            f'📅 {date_part} — <b>{deal.price:,} ₽/чел.</b>\n'
            f'🛫 {escape(airport_line)}\n'
            f'{route_type}{flight_info}\n'
            f'🧳 <b>Багаж НЕ подтверждён</b> — проверьте сдаваемый багаж '
            f'в тарифе перед оплатой.\n\n'
            f'🔗 <a href="{escape(deal.link)}">Проверить билет, багаж и итоговую цену</a>\n'
            f'⚠️ Цена из кеша Travelpayouts: наличие и цена могут измениться. '
            f'Стоимость багажа может увеличить итог свыше 10 000 ₽.')


class Telegram:
    def __init__(self, token: str, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.endpoint = f'https://api.telegram.org/bot{token}/'

    def call(self, method: str, payload: dict, timeout: int = 35) -> dict:
        # Не логируем URL запроса: в нём содержится Telegram-токен.
        try:
            r = self.session.post(self.endpoint + method, json=payload, timeout=timeout)
            r.raise_for_status()
            body = r.json()
        except requests.RequestException as exc:
            raise RuntimeError(f'Ошибка запроса Telegram ({type(exc).__name__})') from None
        if not body.get('ok'):
            raise RuntimeError(f'Telegram {method}: {str(body.get("description", "error"))[:160]}')
        return body

    def send(self, chat_id: int, message: str) -> None:
        self.call('sendMessage', {
            'chat_id': chat_id, 'text': message, 'parse_mode': 'HTML',
            'disable_web_page_preview': True,
        }, timeout=15)

    def updates(self, offset: int, timeout: int = 20) -> list[dict]:
        payload = {'offset': offset, 'timeout': timeout, 'allowed_updates': ['message']}
        return self.call('getUpdates', payload, timeout=timeout + 10).get('result', [])


class Watcher:
    def __init__(self, cfg: Settings, api: Travelpayouts, telegram: Telegram, state: State):
        self.cfg, self.api, self.telegram, self.state = cfg, api, telegram, state
        self.running = True

    def enabled(self) -> bool:
        return self.state.get('enabled') == '1' and bool(self.state.get('chat_id'))

    def owner(self, msg: dict) -> bool:
        uid = (msg.get('from') or {}).get('id')
        cid = (msg.get('chat') or {}).get('id')
        if not isinstance(uid, int) or not isinstance(cid, int):
            return False
        # Личный бот: только private chat, опциональная привязка user ID.
        if (msg.get('chat') or {}).get('type') != 'private':
            return False
        if self.cfg.owner_telegram_user_id is not None and uid != self.cfg.owner_telegram_user_id:
            return False
        existing = self.state.get('user_id')
        return not existing or existing == str(uid)

    def handle(self, msg: dict) -> bool:
        txt = str(msg.get('text') or '').strip()
        cmd = txt.split()[0].split('@')[0].lower() if txt else ''
        cid = (msg.get('chat') or {}).get('id')
        uid = (msg.get('from') or {}).get('id')
        if not self.owner(msg):
            if isinstance(cid, int) and cmd.startswith('/'):
                self.telegram.send(cid, '🔒 Доступ к этому личному боту ограничен.')
            return False
        if cmd == '/start':
            if not self.state.get('user_id'):
                self.state.put('user_id', str(uid))
                self.state.put('chat_id', str(cid))
            self.state.put('enabled', '1')
            self.telegram.send(cid,
                f'✅ Мониторинг включён. Travelpayouts проверяется каждые {self.cfg.interval_seconds // 60} минут.\n'
                '✈️ Москва → Шарм: 16–20 октября 2026.\n'
                '✈️ Шарм-эль-Шейх → Москва: 29 октября – 1 ноября 2026.\n'
                '💰 Сигнал на цену строго ниже 10 000 ₽.\n'
                '🧳 Важно: API не даёт данных о багаже. '
                'Присылаю кандидатов с обязательной пометкой «багаж не подтверждён».\n'
                'Команды: /check, /status, /test, /pause, /resume, /help')
            return True
        if cmd == '/test':
            self.telegram.send(cid,
                '🧪 <b>ПРИМЕР уведомления — не реальная цена</b>\n\n'
                '✈️ Берём?\nМосква → Шарм-эль-Шейх\n'
                '📅 16 октября — <b>7 900 ₽/чел.</b>\n'
                '✅ Прямой рейс\n'
                '🧳 Багаж НЕ подтверждён.\n'
                'Цена вымышленная; это только проверка Telegram.')
            return False
        if cmd == '/check':
            self.telegram.send(cid, '🔎 Проверяю цены в кеше Travelpayouts…')
            self.check(manual=True)
            return False
        if cmd == '/pause' or cmd == '/stop':
            self.state.put('enabled', '0')
            self.telegram.send(cid, '⏸ Мониторинг приостановлен. Возобновить: /resume')
            return False
        if cmd == '/resume':
            self.state.put('enabled', '1')
            self.telegram.send(cid, '▶️ Мониторинг возобновлён.')
            return True
        if cmd == '/status':
            active = 'работает' if self.enabled() else 'приостановлен'
            last = self.state.get('last_check', 'ещё не выполнялась')
            self.telegram.send(cid,
                f'📊 Мониторинг: {active}.\n'
                f'Периодичность: {self.cfg.interval_seconds // 60} мин.\n'
                f'Порог: строго менее {self.cfg.max_price:,} ₽.\n'
                f'Египетские аэропорты: {", ".join(self.cfg.egypt_origins)}\n'
                f'Последняя проверка: {last}\n'
                '🧳 Багаж: подлежит проверке по ссылке.')
            return False
        if cmd == '/help':
            self.telegram.send(cid,
                '/start — включить\n/check — проверить сейчас\n'
                '/status — состояние\n/test — тестовое сообщение\n'
                '/pause — пауза\n/resume — возобновить\n'
                'Бот присылает только кандидатов до 10 000 ₽. '
                'Наличие зарегистрированного багажа нужно проверить вручную.')
            return False
        if cmd.startswith('/'):
            self.telegram.send(cid, 'Неизвестная команда. Доступно: /help')
        return False

    def check(self, manual: bool = False, today: date | None = None) -> int:
        if not self.enabled():
            return 0
        collected: dict[str, Deal] = {}
        completed = errors = 0
        for rule in rules_for(self.cfg):
            local_today = today or datetime.now(ZoneInfo(
                'Europe/Moscow' if rule.origin in MOSCOW_CODES else 'Africa/Cairo')).date()
            if local_today > rule.end:
                continue
            for month in months_in_period(max(local_today, rule.start), rule.end):
                for direct in (True, False):
                    try:
                        data = self.api.fetch(rule, month, direct)
                        completed += 1
                        for deal in parse_deals(data, rule, self.cfg.max_price, local_today):
                            # Не путать фильтр direct=true с API, который его проигнорировал.
                            if direct and deal.transfers != 0:
                                continue
                            previous = collected.get(deal.flight_key)
                            if previous is None or deal.price < previous.price:
                                collected[deal.flight_key] = deal
                    except Exception as exc:
                        errors += 1
                        # Чувствительные данные/URL исключены из лога.
                        LOG.warning('Не удалось проверить %s %s %s direct=%s (%s)',
                                    rule.origin, rule.destination, month, direct,
                                    type(exc).__name__)
                    time.sleep(0.08)
        if not completed and errors:
            if manual:
                self.telegram.send(int(self.state.get('chat_id')),
                                   '❌ Travelpayouts не ответил. Проверьте API-ключ и интернет.')
            return 0
        deals = [d for d in collected.values() if self.state.need_alert(d)]
        deals.sort(key=lambda d: (d.transfers != 0, d.price, d.departure, d.origin_airport))
        sent = 0
        cid = int(self.state.get('chat_id'))
        for deal in deals[:self.cfg.max_alerts_per_check]:
            try:
                self.telegram.send(cid, deal_message(deal))
                self.state.mark_alerted(deal)
                sent += 1
            except Exception as exc:
                LOG.warning('Не удалось отправить уведомление (%s)', type(exc).__name__)
                break  # Не помечаем как отправленное: попробуем в следующем цикле.
            time.sleep(0.1)
        self.state.put('last_check', datetime.now(ZoneInfo('Europe/Moscow')).strftime('%d.%m.%Y %H:%M МСК'))
        if manual:
            self.telegram.send(cid,
                f'✅ Проверка завершена. Запросов: {completed}. Ошибок: {errors}.\n'
                f'Кандидатов ниже {self.cfg.max_price:,} ₽ в кеше: {len(collected)}.\n'
                f'Новых уведомлений: {sent}.\n'
                'Багаж в Data API не указан — проверяйте тариф на сайте.')
        LOG.info('Проверка: запросов %s, ошибок %s, предложений %s, уведомлений %s',
                 completed, errors, len(collected), sent)
        return sent

    def run(self) -> None:
        offset = int(self.state.get('tg_offset', '0'))
        next_check = time.monotonic() if self.enabled() else float('inf')
        LOG.info('Бот запущен. Напишите /start в Telegram.')
        while self.running:
            if self.enabled() and time.monotonic() >= next_check:
                self.check()
                next_check = time.monotonic() + self.cfg.interval_seconds
            timeout = 15 if not self.enabled() else max(1, min(15, int(next_check - time.monotonic())))
            try:
                messages = self.telegram.updates(offset, timeout=timeout)
                for update in messages:
                    offset = max(offset, int(update['update_id']) + 1)
                    self.state.put('tg_offset', str(offset))
                    if 'message' in update and self.handle(update['message']):
                        next_check = time.monotonic()
                if not self.enabled():
                    next_check = float('inf')
            except Exception as exc:
                LOG.warning('Telegram: временная ошибка %s; повтор через 10 с',
                            type(exc).__name__)
                time.sleep(10)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    cfg = Settings.from_env()
    state = State(cfg.state_db)
    telegram = Telegram(cfg.telegram_token)
    api = Travelpayouts(cfg.travelpayouts_token)
    bot = Watcher(cfg, api, telegram, state)
    signal.signal(signal.SIGINT, lambda *_: setattr(bot, 'running', False))
    if hasattr(signal, 'SIGTERM'):
        signal.signal(signal.SIGTERM, lambda *_: setattr(bot, 'running', False))
    try:
        bot.run()
    finally:
        state.db.close()
        LOG.info('Бот остановлен.')


if __name__ == '__main__':
    main()
