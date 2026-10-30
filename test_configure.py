import stat
import tempfile
import unittest
from pathlib import Path
from configure import read_config, write_config


class ConfigTests(unittest.TestCase):
    def test_atomic_private_config_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.env'
            values = {'TELEGRAM_BOT_TOKEN': '123:example', 'TELEGRAM_CHAT_ID': '456',
                      'ETH_RPC_URL': 'https://rpc.example/?key=test&option=1'}
            write_config(path, values)
            self.assertEqual(read_config(path), values)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_bad_values_do_not_overwrite_existing_config(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.env'
            write_config(path, {'TELEGRAM_CHAT_ID': '456'})
            with self.assertRaises(ValueError):
                write_config(path, {'TELEGRAM_CHAT_ID': '456\nINJECTED=1'})
            self.assertEqual(read_config(path), {'TELEGRAM_CHAT_ID': '456'})


class TerminalTests(unittest.TestCase):
    def test_full_setup_with_piped_stdin_and_hidden_token(self):
        """Exercise real /dev/tty IO, as in curl | sudo bash, without network calls."""
        import errno
        import os
        import pty
        import select
        import signal
        import sys
        import time

        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'config.env'
            code = f'''
import sys
from pathlib import Path
from decimal import Decimal
sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})
import configure
configure.STATE_PATH = Path({str(Path(folder) / 'state.db')!r})
configure.Source.sample = lambda self: (Decimal('-1.06'), 123, 456)
def fake_send(token, chat, body):
    assert token == '123:test_secret_token'
    assert chat == '456'
    assert 'Тест установки' in body
configure.send = fake_send
sys.argv = ['configure.py', '--chat-id', '456', '--config', {str(config)!r}]
configure.main()
'''
            pid, master = pty.fork()
            if pid == 0:
                # stdin is an EOF pipe, not the terminal. /dev/tty must still work.
                read_end, write_end = os.pipe()
                os.close(write_end)
                os.dup2(read_end, 0)
                os.close(read_end)
                os.execv(sys.executable, [sys.executable, '-c', code])
            output = bytearray()
            deadline = time.monotonic() + 15
            replied_chat = replied_token = False
            try:
                while time.monotonic() < deadline:
                    if not select.select([master], [], [], 0.2)[0]:
                        continue
                    try:
                        chunk = os.read(master, 8192)
                    except OSError as exc:
                        if exc.errno == errno.EIO:
                            break
                        raise
                    if not chunk:
                        break
                    output.extend(chunk)
                    if not replied_chat and b'Telegram chat ID [456]: ' in output:
                        os.write(master, b'\n')
                        replied_chat = True
                    if not replied_token and 'Токен BotFather (скрытый ввод): '.encode() in output:
                        os.write(master, b'123:test_secret_token\n')
                        replied_token = True
                else:
                    self.fail('Setup dialog timed out')
                _, status = os.waitpid(pid, 0)
                pid = None
                rendered = output.decode('utf-8', errors='replace')
                self.assertEqual(os.waitstatus_to_exitcode(status), 0, rendered)
                self.assertTrue(replied_chat and replied_token, rendered)
                self.assertIn('Конфигурация сохранена', rendered)
                self.assertNotIn('123:test_secret_token', rendered)
                self.assertEqual(read_config(config)['TELEGRAM_CHAT_ID'], '456')
                self.assertEqual(read_config(config)['TELEGRAM_BOT_TOKEN'], '123:test_secret_token')
                self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
            finally:
                os.close(master)
                if pid is not None:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    os.waitpid(pid, 0)


if __name__ == '__main__':
    unittest.main()
