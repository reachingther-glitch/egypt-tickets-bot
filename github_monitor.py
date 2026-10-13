#!/usr/bin/env python3
"""Один проход мониторинга для GitHub Actions.

Не запускает Telegram polling и не хранит Chat ID в публичном репозитории.
Состояние отправленных предложений — только хеши рейсов/минимальная цена.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bot import (MOSCOW_CODES, Deal, Rule, Settings, Telegram, Travelpayouts,
                 deal_message, months_in_period, parse_deals, rules_for)
from hotel_alerts import credentials_enabled, poll_mailbox, load_watchlist

LOG = logging.getLogger('egypt_gh_monitor')
STATE_FILE = Path(__file__).resolve().parent / 'state' / 'sent_prices.json'


def get_chat_id() -> int:
    raw = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
    if not raw or not raw.lstrip('-').isdigit():
        raise ValueError('Нужен GitHub Secret TELEGRAM_CHAT_ID (числовой ID вашего Telegram-чата)')
    return int(raw)


def load_history(path: Path = STATE_FILE) -> dict[str, int]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('version') != 1:
        raise ValueError('Неподдерживаемый формат state/sent_prices.json')
    offers = data.get('best_prices')
    if not isinstance(offers, dict):
        raise ValueError('Неверный формат best_prices в state/sent_prices.json')
    clean: dict[str, int] = {}
    for key, price in offers.items():
        if not isinstance(key, str) or not isinstance(price, int) or isinstance(price, bool):
            raise ValueError('Повреждена история уведомлений')
        clean[key] = price
    return clean


def save_history(offers: dict[str, int], path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {'version': 1, 'best_prices': offers}
    # Атомарное сохранение: состояние не теряется при прерывании записи.
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix='.sent-', suffix='.tmp', delete=False) as handle:
        tmp = Path(handle.name)
        json.dump(data, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write('\n')
    tmp.replace(path)


def query_jobs(cfg: Settings, today_by_origin: dict[str, date]) -> list[tuple[Rule, str, bool, date, int]]:
    jobs = []
    for rule in rules_for(cfg):
        today = today_by_origin['MOW'] if rule.origin in MOSCOW_CODES else today_by_origin['EG']
        if today > rule.end:
            continue
        for month in months_in_period(max(today, rule.start), rule.end):
            for direct in (True, False):
                jobs.append((rule, month, direct, today, cfg.max_price))
    return jobs


def fetch_one(token: str, task: tuple[Rule, str, bool, date, int]) -> list[Deal]:
    rule, month, direct, today, max_price = task
    # Собственная Session в потоке: не делим requests.Session между потоками.
    api = Travelpayouts(token)
    try:
        payload = api.fetch(rule, month, direct)
        deals = parse_deals(payload, rule, max_price, today)
        # Учитываем, что API иногда может вернуть запись с пересадкой на direct=true.
        return [d for d in deals if not direct or d.transfers == 0]
    finally:
        api.session.close()


def scan(cfg: Settings, *, api_fetch=fetch_one,
         today_by_origin: dict[str, date] | None = None,
         max_workers: int = 5) -> tuple[list[Deal], int, int]:
    if today_by_origin is None:
        today_by_origin = {
            'MOW': datetime.now(ZoneInfo('Europe/Moscow')).date(),
            'EG': datetime.now(ZoneInfo('Africa/Cairo')).date(),
        }
    jobs = query_jobs(cfg, today_by_origin)
    if not jobs:
        return [], 0, 0
    offers: dict[str, Deal] = {}
    successes = errors = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_job = {pool.submit(api_fetch, cfg.travelpayouts_token, job): job for job in jobs}
        for future in as_completed(future_to_job):
            rule, month, direct, _today, _max_price = future_to_job[future]
            try:
                found = future.result()
                successes += 1
                for deal in found:
                    if deal.price >= cfg.max_price:
                        continue
                    old = offers.get(deal.flight_key)
                    if old is None or deal.price < old.price:
                        offers[deal.flight_key] = deal
            except Exception as exc:
                errors += 1
                # Не печатаем запросы/ответы/токены, даже в исключениях.
                LOG.warning('Ошибка Travelpayouts: %s → %s, %s direct=%s (%s)',
                            rule.origin, rule.destination, month, direct,
                            type(exc).__name__)
    if errors and not successes:
        raise RuntimeError('Ни один запрос Travelpayouts не прошёл. Проверьте токен и доступность API.')
    ordered = sorted(offers.values(), key=lambda d: (d.transfers != 0, d.price, d.departure, d.origin))
    return ordered, successes, errors


def check_once(cfg: Settings, tg: Telegram, chat_id: int,
               *, history_path: Path = STATE_FILE, api_fetch=fetch_one,
               today_by_origin: dict[str, date] | None = None) -> dict[str, int]:
    history = load_history(history_path)
    offers, successes, errors = scan(cfg, api_fetch=api_fetch, today_by_origin=today_by_origin)
    sent = 0
    for deal in offers:
        if sent >= cfg.max_alerts_per_check:
            break
        best = history.get(deal.flight_key)
        if best is not None and deal.price >= best:
            continue
        tg.send(chat_id, deal_message(deal))
        history[deal.flight_key] = deal.price
        # После КАЖДОЙ успешной отправки сохраняем файл для последующего git-коммита.
        save_history(history, history_path)
        sent += 1
    LOG.info('Запросов: %s. Ошибок: %s. Подходящих вариантов: %s. Новых уведомлений: %s.',
             successes, errors, len(offers), sent)
    return {'requests': successes, 'errors': errors, 'matched': len(offers), 'sent': sent}


def test_telegram(tg: Telegram, chat_id: int) -> None:
    tg.send(chat_id,
            '✅ <b>Тест успешен! GitHub подключён.</b>\n\n'
            '⏱ Плановая проверка: примерно каждые 5 минут (GitHub может задерживать запуски).\n'
            '✈️ Москва → Шарм-эль-Шейх: 16–20 октября 2026.\n'
            '✈️ Шарм-эль-Шейх → Москва: 29 октября – 1 ноября 2026.\n'
            '🏨 OnlineTours: отели 4–5★, отзывы ≥ 8.0, AI/UAI.\n'
            '💰 Сообщение о цене строго ниже 10 000 ₽.\n'
            '🧳 Зарегистрированный багаж Travelpayouts Data API не подтверждает.\n\n'
            'Это ТЕСТ связи с Telegram, а не найденный билет.')


def main() -> int:
    parser = argparse.ArgumentParser(description='Монитор авиабилетов Travelpayouts для GitHub Actions')
    parser.add_argument('--mode', choices=('check', 'test', 'flights', 'hotels'), default='check')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    cfg = Settings.from_env()
    chat_id = get_chat_id()
    tg = Telegram(cfg.telegram_token)
    if args.mode == 'test':
        test_telegram(tg, chat_id)
        LOG.info('Тестовое сообщение отправлено.')
        return 0
    # Два независимых механизма: авиабилеты по API Travelpayouts; отельные
    # уведомления только из писем OnlineTours и лишь после настройки почты.
    errors = []
    if args.mode in ('check', 'flights'):
        try:
            check_once(cfg, tg, chat_id)
        except Exception as exc:
            LOG.error('Авиабилеты: %s', type(exc).__name__)
            errors.append('Не удалось проверить авиабилеты')
    if args.mode in ('check', 'hotels'):
        if not credentials_enabled():
            if args.mode == 'hotels':
                errors.append('Мониторинг отелей не подключён: HOTEL_EMAIL_ENABLED=1')
            else:
                LOG.info('Отели: email-подписки OnlineTours ещё не подключены — пропуск.')
        elif datetime.now(ZoneInfo('Africa/Cairo')).date() > date(2026, 11, 1):
            LOG.info('Период отелей 16.10–01.11.2026 закончился; пропуск.')
        else:
            try:
                stat = poll_mailbox(lambda message: tg.send(chat_id, message))
                LOG.info('Отели: %s', stat)
            except Exception as exc:
                LOG.error('Отели: %s', type(exc).__name__)
                errors.append('Не удалось проверить почту OnlineTours')
    if errors:
        raise RuntimeError('; '.join(errors))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
