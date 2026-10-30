#!/usr/bin/env python3
"""Interactive installation configuration; secret input always comes from /dev/tty."""
import argparse
import getpass
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from urllib.parse import urlsplit

from bot import Source, RemoteError, percent, send

DEFAULT_RPC = 'https://ethereum-rpc.publicnode.com'
STATE_PATH = Path('/var/lib/trd-bot/state.sqlite3')
STAGE = 'чтение конфигурации'


def read_config(path):
    if not path.exists():
        return {}
    result = {}
    for line in path.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            result[key.strip()] = value.strip()
    return result


def write_config(path, values):
    for key, value in values.items():
        if not re.fullmatch(r'[A-Z_]+', key) or any(ch.isspace() for ch in value):
            raise ValueError('Invalid configuration format')
    descriptor, temporary = tempfile.mkstemp(prefix='.trd-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as output:
            os.fchmod(output.fileno(), 0o600)
            for key, value in values.items():
                output.write(f'{key}={value}\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    global STAGE
    parser = argparse.ArgumentParser()
    parser.add_argument('--chat-id', default='')
    parser.add_argument('--config', default='/etc/trd-bot.env')
    args = parser.parse_args()
    path = Path(args.config)
    previous = read_config(path)
    STAGE = 'открытие SSH-терминала (нужен интерактивный SSH-сеанс)'
    # A terminal is non-seekable: buffered r+ attempts to create a seekable
    # BufferedRandom stream. Separate read/write handles also work with piped stdin.
    with open('/dev/tty', 'r', encoding='utf-8') as terminal_input, \
            open('/dev/tty', 'w', encoding='utf-8', buffering=1) as terminal:
        def ask(prompt, default=''):
            terminal.write(prompt)
            terminal.flush()
            value = terminal_input.readline()
            if value == '':
                raise ValueError('Interactive terminal required')
            return value.strip() or default

        STAGE = 'ввод Telegram chat ID'
        chat = args.chat_id or previous.get('TELEGRAM_CHAT_ID', '')
        chat = ask(f'Telegram chat ID [{chat}]: ', chat)
        if not re.fullmatch(r'-?\d+', chat):
            raise ValueError('Chat ID must be numeric')
        STAGE = 'проверка chat ID в сохранённом состоянии'
        state_path = STATE_PATH
        if state_path.exists():
            with sqlite3.connect(f'file:{state_path}?mode=ro', uri=True) as state:
                row = state.execute("SELECT value FROM kv WHERE key='chat_id'").fetchone()
                if row and row[0] != chat:
                    raise ValueError('Existing state belongs to another chat; keep its original chat ID')
        STAGE = 'ввод токена BotFather'
        token = previous.get('TELEGRAM_BOT_TOKEN', '')
        token = getpass.getpass('Токен BotFather (скрытый ввод' +
                                ('; Enter — оставить прежний' if token else '') + '): ',
                                stream=terminal).strip() or token
        if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', token):
            raise ValueError('Invalid Telegram token format')
        STAGE = 'проверка свежих данных Ethereum RPC'
        rpc = previous.get('ETH_RPC_URL', DEFAULT_RPC)
        for attempt in range(3):
            print('Проверяю свежие данные WBTC TRD через Ethereum RPC…', flush=True)
            try:
                value, block, _ = Source(rpc).sample()
                print(f'WBTC TRD: {percent(value)}; блок Ethereum: {block}', flush=True)
                break
            except (RemoteError, ValueError, TypeError, KeyError):
                if attempt == 2:
                    raise ValueError('RPC check failed; configuration was not changed') from None
                print('RPC недоступен или вернул некорректные данные. Можно повторить '
                      'проверку или указать свой HTTPS Ethereum mainnet RPC.')
                candidate = getpass.getpass('Новый RPC URL (скрытый ввод; Enter — повторить): ',
                                           stream=terminal).strip()
                if candidate:
                    rpc = candidate
            parts = urlsplit(rpc)
            if parts.scheme != 'https' or not parts.hostname or any(c.isspace() for c in rpc):
                raise ValueError('RPC URL must be a valid HTTPS URL without whitespace')
        STAGE = 'доставка в Telegram (проверьте токен, chat ID и кнопку Start)'
        print('Отправляю тест в Telegram. Перед этим у бота должна быть нажата кнопка Start.', flush=True)
        send(token, chat, '✅ Тест установки WBTC TRD: Telegram подключён.\n'
             f'Проверенное значение: {percent(value)}.\n'
             'После завершения установки мониторинг будет запущен автоматически.')
        STAGE = 'сохранение конфигурации на сервере'
        write_config(path, {'TELEGRAM_BOT_TOKEN': token, 'TELEGRAM_CHAT_ID': chat,
                            'ETH_RPC_URL': rpc, 'POLL_SECONDS': '60'})
        print('Конфигурация сохранена с правами 600. Токен в GitHub не отправляется.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RemoteError, sqlite3.Error) as exc:
        # Exception text can contain a credential-bearing URL. Only show the
        # operation and exception type, which are sufficient to locate the failure.
        print(f'Установка остановлена. Этап: {STAGE}. Тип ошибки: {type(exc).__name__}. '
              'Секретные данные не выводятся. Исправьте причину и повторите команду.')
        raise SystemExit(1)
