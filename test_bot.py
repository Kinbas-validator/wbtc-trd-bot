import tempfile
import time
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch
from bot import State, Source, RemoteError, trd_percent, MARKET, ASSET


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.db'
        self.state = State(self.path, '123')
        self.sent = []

    def tearDown(self):
        self.state.db.close()
        self.temp.cleanup()

    def tick(self, value):
        self.state.observe(D(value), 123, int(time.time()))
        self.state.flush(self.sent.append)

    def test_wbtc_decimal_scaling(self):
        self.assertEqual(trd_percent(98940000, 10**18), D('-1.06'))
        self.assertEqual(trd_percent(10**8, 10**18), 0)
        self.assertEqual(trd_percent(100100000, 10**18), D('0.1'))

    def test_upward_crossings_once_even_after_restart(self):
        self.tick('-1.06')
        self.sent.clear()
        for value in ['-1.01', '-1', '-0.75', '-1.2', '-0.99']:
            self.tick(value)
        self.assertEqual(len(self.sent), 1)
        self.state.db.close()
        self.state = State(self.path, '123')
        self.tick('-1.5')
        self.tick('-0.8')
        self.assertEqual(len(self.sent), 1)
        self.tick('-0.5')
        self.assertEqual(len(self.sent), 2)

    def test_jump_over_all_thresholds(self):
        self.tick('-1.06')
        self.sent.clear()
        self.tick('0.02')
        self.assertEqual(len(self.sent), 3)
        self.assertIn('достиг или превысил 0%', self.sent[-1])

    def test_rounded_negative_is_not_zero(self):
        self.tick('-0.1')
        self.sent.clear()
        self.tick('-0.000001')
        self.assertEqual(self.sent, [])
        self.tick('0')
        self.assertEqual(len(self.sent), 1)

    def test_decrease_does_not_alert(self):
        self.tick('-1.06')
        self.sent.clear()
        self.tick('-2')
        self.assertEqual(self.sent, [])

    def test_failed_send_retained_across_restart(self):
        self.state.observe(D('-1.06'), 100, int(time.time()))
        def failure(body):
            raise RemoteError('test')
        with self.assertRaises(RemoteError):
            self.state.flush(failure)
        self.state.db.close()
        self.state = State(self.path, '123')
        self.state.flush(self.sent.append)
        self.state.flush(self.sent.append)
        self.assertEqual(len(self.sent), 1)

    def test_outage_and_recovery(self):
        self.tick('-1.06')
        self.sent.clear()
        self.state.failure(1000)
        self.state.failure(1299)
        self.state.flush(self.sent.append)
        self.assertEqual(len(self.sent), 0)
        self.state.failure(1300)
        self.state.failure(1600)
        self.state.flush(self.sent.append)
        self.assertEqual(len(self.sent), 1)
        self.tick('-0.4')
        self.assertEqual(len(self.sent), 4)  # outage, two crossings, recovery

    def test_initial_status_does_not_claim_historical_crossings(self):
        self.tick('-0.4')
        self.assertEqual(len(self.sent), 1)
        self.tick('-1.2')
        self.tick('-0.4')
        self.assertEqual(len(self.sent), 1)

    def test_chat_change_fails_closed(self):
        with self.assertRaises(ValueError):
            State(self.path, '456')

    def test_sample_reads_same_block_and_correct_contracts(self):
        source = Source('https://rpc.example')
        block = {'number': '0x64', 'hash': '0xabc', 'timestamp': hex(int(time.time()))}
        calls = []
        def rpc(method, params):
            calls.append((method, params))
            if method == 'eth_chainId':
                return '0x1'
            if method == 'eth_getBlockByNumber':
                return block
            self.assertEqual(params[1], '0x64')
            data = params[0]['data']
            values = {'0xd7062005': int(ASSET, 16), '0x313ce567': 8,
                      '0xf51dd485' + format(10**18, '064x'): 98940000,
                      '0x99530b06': 10**18}
            self.assertEqual(params[0]['to'], ASSET if data == '0x313ce567' else MARKET)
            return '0x' + format(values[data], '064x')
        with patch.object(source, 'rpc', side_effect=rpc):
            self.assertEqual(source.sample()[0], D('-1.06'))
        self.assertEqual(calls[-1], ('eth_getBlockByNumber', ['0x64', False]))

    def test_wrong_chain_and_stale_data_rejected(self):
        source = Source('https://rpc.example')
        with patch.object(source, 'rpc', return_value='0x89'):
            with self.assertRaises(RemoteError):
                source.sample()
        old_block = {'timestamp': hex(int(time.time()) - 400)}
        with patch.object(source, 'rpc', side_effect=['0x1', old_block]):
            with self.assertRaises(RemoteError):
                source.sample()


if __name__ == '__main__':
    unittest.main()
