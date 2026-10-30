#!/usr/bin/env python3
"""Yield Basis WBTC TRD monitor. Python 3.10+, standard library only."""
import argparse
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal, getcontext
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

getcontext().prec = 60
MARKET = '0x651D4b8168488FA163D85304662E8278d4c55BAa'
ASSET = '0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599'
PAGE = 'https://yieldbasis.com/analytics/market/' + MARKET
LEVELS = (Decimal('-1'), Decimal('-0.5'), Decimal('0'))
LOG = logging.getLogger('trd')


class RemoteError(Exception):
    pass


def post(url, payload):
    """Never expose URLs or remote response bodies: URLs can contain credentials."""
    req = Request(url, data=json.dumps(payload).encode(),
                  headers={'Content-Type': 'application/json', 'User-Agent': 'WBTC-TRD-Monitor/1.0'})
    try:
        with urlopen(req, timeout=25) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RemoteError(f'HTTP {exc.code}') from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise RemoteError('Network error or invalid JSON') from None


def trd_percent(redemption, fundamental):
    if fundamental <= 0 or redemption < 0:
        raise RemoteError('Invalid contract values')
    # WBTC: 8 decimals; LT pricePerShare: 18 decimals; quote: 1e18 shares.
    return (Decimal(redemption) * 10**10 / Decimal(fundamental) - 1) * 100


class Source:
    def __init__(self, url):
        if not url.startswith('https://'):
            raise ValueError('ETH_RPC_URL must use HTTPS')
        self.url = url

    def rpc(self, method, params):
        data = post(self.url, {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
        if not isinstance(data, dict) or data.get('error') or data.get('result') is None:
            raise RemoteError('Ethereum RPC error: ' + method)
        return data['result']

    def call(self, address, calldata, block):
        result = self.rpc('eth_call', [{'to': address, 'data': calldata}, block])
        if not isinstance(result, str) or len(result) != 66 or not result.startswith('0x'):
            raise RemoteError('Invalid contract response')
        try:
            return int(result, 16)
        except ValueError:
            raise RemoteError('Invalid contract encoding') from None

    def sample(self):
        if int(self.rpc('eth_chainId', []), 16) != 1:
            raise RemoteError('RPC is not Ethereum mainnet')
        block = self.rpc('eth_getBlockByNumber', ['latest', False])
        stamp = int(block['timestamp'], 16)
        if not -60 <= time.time() - stamp <= 180:
            raise RemoteError('Ethereum block is stale or in the future')
        tag = block['number']
        if self.call(MARKET, '0xd7062005', tag) != int(ASSET, 16):
            raise RemoteError('Market asset mismatch')
        if self.call(ASSET, '0x313ce567', tag) != 8:
            raise RemoteError('WBTC decimals mismatch')
        redemption = self.call(MARKET, '0xf51dd485' + format(10**18, '064x'), tag)
        fundamental = self.call(MARKET, '0x99530b06', tag)
        # Check that the numbered block did not change while reading its values.
        check = self.rpc('eth_getBlockByNumber', [tag, False])
        if not -60 <= time.time() - stamp <= 180:
            raise RemoteError('Ethereum block became stale during read')
        if check['hash'] != block['hash']:
            raise RemoteError('Block reorganized; retry on next poll')
        value = trd_percent(redemption, fundamental)
        if not Decimal('-100') <= value <= Decimal('100'):
            raise RemoteError('TRD outside validation bounds')
        return value, int(tag, 16), stamp


def percent(value):
    return f'{value:.6f}%'.replace('.', ',')


def timestamp(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


class State:
    def __init__(self, path, chat_id):
        self.db = sqlite3.connect(path)
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS fired (level TEXT PRIMARY KEY);
          CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY, body TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0);
        ''')
        owner = self.get('chat_id')
        if owner is not None and owner != chat_id:
            self.db.close()
            raise ValueError('State belongs to another chat; use a separate STATE_FILE')
        with self.db:
            self.set('chat_id', chat_id)

    def get(self, key):
        row = self.db.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    def set(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO kv VALUES (?,?)', (key, str(value)))

    def queue(self, body):
        self.db.execute('INSERT INTO outbox(body) VALUES (?)', (body,))

    def observe(self, value, block, stamp):
        previous = self.get('previous')
        with self.db:
            if previous is None:
                self.queue('Мониторинг WBTC Market TRD включён.\n'
                           f'Текущее значение: {percent(value)}\n'
                           'Пороги: −1,0%, −0,5%, 0%. Каждый порог — один раз.\n'
                           f'Блок: {block}; {timestamp(stamp)}\n{PAGE}')
                # Already reached thresholds are reported in the initial status;
                # only future upward crossings produce threshold alerts.
                for level in LEVELS:
                    if value >= level:
                        self.db.execute('INSERT OR IGNORE INTO fired VALUES (?)', (str(level),))
            else:
                old = Decimal(previous)
                for level in LEVELS:
                    fired = self.db.execute('SELECT 1 FROM fired WHERE level=?', (str(level),)).fetchone()
                    if not fired and old < level <= value:
                        title = ('✅ Market TRD достиг или превысил 0%.' if level == 0 else
                                 f'📈 Market TRD пересёк вверх уровень {percent(level)}.')
                        self.queue(title + f'\nWBTC: {percent(old)} → {percent(value)}'
                                   f'\nБлок: {block}; {timestamp(stamp)}\n{PAGE}')
                        self.db.execute('INSERT INTO fired VALUES (?)', (str(level),))
            self.set('previous', value)
            self.set('block', block)
            self.set('block_timestamp', stamp)
            self.set('last_success', int(time.time()))
            if self.get('outage_notified') == '1':
                self.queue('✅ Получение данных WBTC TRD восстановлено.\n'
                           f'Текущее значение: {percent(value)}')
            self.set('outage_since', '')
            self.set('outage_notified', '0')

    def failure(self, now):
        with self.db:
            since = self.get('outage_since')
            if not since:
                self.set('outage_since', now)
            elif now - int(since) >= 300 and self.get('outage_notified') != '1':
                self.queue('⚠️ Более 5 минут нет свежих данных WBTC TRD. '
                           'Проверки продолжаются; уведомления о порогах временно недоступны.')
                self.set('outage_notified', '1')

    def flush(self, sender):
        for row_id, body in self.db.execute('SELECT id,body FROM outbox WHERE sent=0 ORDER BY id').fetchall():
            sender(body)
            with self.db:
                self.db.execute('UPDATE outbox SET sent=1 WHERE id=?', (row_id,))


def send(token, chat_id, body):
    result = post('https://api.telegram.org/bot' + token + '/sendMessage',
                  {'chat_id': chat_id, 'text': body, 'disable_web_page_preview': True})
    if not isinstance(result, dict) or not result.get('ok'):
        raise RemoteError('Telegram rejected message')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Read TRD once; no Telegram or state changes')
    parser.add_argument('--test-telegram', action='store_true', help='Send a labeled test to configured chat')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    os.umask(0o077)
    if args.check:
        value, block, stamp = Source(os.environ['ETH_RPC_URL']).sample()
        print(json.dumps({'market': MARKET, 'trd_percent': str(value), 'block': block,
                          'block_time': timestamp(stamp)}, ensure_ascii=False))
        return
    token = os.environ['TELEGRAM_BOT_TOKEN']
    chat_id = os.environ['TELEGRAM_CHAT_ID']
    if not chat_id.lstrip('-').isdigit():
        raise ValueError('TELEGRAM_CHAT_ID must be numeric')
    if args.test_telegram:
        send(token, chat_id, 'Тест уведомлений WBTC TRD. Это проверка доставки, не рыночный сигнал.')
        print('Telegram test delivered')
        return
    interval = int(os.environ.get('POLL_SECONDS', '60'))
    if not 15 <= interval <= 3600:
        raise ValueError('POLL_SECONDS must be between 15 and 3600')
    source = Source(os.environ['ETH_RPC_URL'])
    path = Path(os.environ.get('STATE_FILE', './data/state.sqlite3'))
    path.parent.mkdir(parents=True, exist_ok=True)
    # Hold for the lifetime of this process; prevents two workers sharing state.
    lock = open(str(path) + '.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    state = State(path, chat_id)
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while running:
        start = time.monotonic()
        try:
            value, block, stamp = source.sample()
            state.observe(value, block, stamp)
            LOG.info('TRD=%s block=%s', percent(value), block)
        except (RemoteError, KeyError, TypeError, ValueError) as exc:
            LOG.warning('Source unavailable (%s)', type(exc).__name__)
            state.failure(int(time.time()))
        try:
            state.flush(lambda body: send(token, chat_id, body))
        except RemoteError:
            LOG.warning('Telegram delivery failed; queued messages retained')
        while running and time.monotonic() - start < interval:
            time.sleep(0.5)
    state.db.close()
    lock.close()


if __name__ == '__main__':
    try:
        main()
    except (RemoteError, KeyError, ValueError, OSError):
        LOG.error('Startup/check failed. Verify configuration, network access and state permissions.')
        raise SystemExit(1)
