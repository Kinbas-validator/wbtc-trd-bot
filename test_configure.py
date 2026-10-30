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


if __name__ == '__main__':
    unittest.main()
