"""Exercise real simulator logic and Flask routes without external services."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import server


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name, filename in [('SIMULATOR_STATE_FILE', 'simulator.json'), ('SETTINGS_FILE', 'settings.json'), ('POSITIONS_FILE', 'positions.json')]:
            patcher = patch.object(server, name, str(root / filename))
            patcher.start()
            self.addCleanup(patcher.stop)
        network = patch('requests.sessions.Session.request', side_effect=AssertionError('External network is disabled in tests'))
        network.start()
        self.addCleanup(network.stop)
        server.shared_state['simulator'] = None
        server.shared_state['price_cache'] = {}
        server.shared_state['active_positions'] = {}
        server.shared_state['trading_status'] = 'RUNNING'
        server.shared_state['settings'].update(simulation_enabled=False, simulation_start_balance=1000.0)
        self.client = server.app.test_client()

    def test_profitable_trade_updates_cash_positions_and_saved_history(self):
        sim = server.TradingSimulator(1000)
        sim.start()
        success, position = sim.buy('DEMO', 100, 15, 50000, 'Demo', 2)
        self.assertTrue(success)
        self.assertEqual(position['amount_tokens'], 50)
        self.assertEqual(sim.balance, 900)
        self.assertTrue(sim.sell('DEMO', 3, 'take-profit'))
        self.assertEqual(sim.balance, 1050)
        self.assertEqual(sim.open_positions, {})
        self.assertEqual(sim.completed_trades[0]['pnl_usd'], 50)
        saved = json.loads(Path(server.SIMULATOR_STATE_FILE).read_text())
        restored = server.TradingSimulator.load_from_state(saved)
        self.assertEqual(restored.balance, 1050)
        self.assertEqual(len(restored.completed_trades), 1)

    def test_paused_or_underfunded_purchase_does_not_change_cash(self):
        sim = server.TradingSimulator(50)
        self.assertFalse(sim.buy('DEMO', 10, 15, 50000, 'Demo', 1)[0])
        sim.start()
        self.assertFalse(sim.buy('DEMO', 100, 15, 50000, 'Demo', 1)[0])
        self.assertEqual(sim.balance, 50)
        self.assertEqual(sim.open_positions, {})

    def test_price_cache_rejects_stale_quote(self):
        server.shared_state['price_cache']['DEMO'] = {'price': 2.5, 'timestamp': 100}
        with patch.object(server.time, 'time', return_value=144):
            self.assertEqual(server.get_price_from_cache('DEMO'), 2.5)
        with patch.object(server.time, 'time', return_value=145):
            self.assertIsNone(server.get_price_from_cache('DEMO'))

    def test_api_settings_enable_simulation_and_commands_change_status(self):
        response = self.client.post('/settings', json={'simulation_enabled': True, 'simulation_start_balance': 500})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.post('/simulator/control/start').json['simulator_status'], 'RUNNING')
        self.assertEqual(self.client.get('/status').json['simulator_state']['balance'], 500)
        self.assertEqual(self.client.post('/control/pause').json['trading_status'], 'PAUSED')
        self.assertEqual(self.client.post('/control/invalid').status_code, 400)

    def test_simulator_commands_rejected_until_enabled(self):
        self.assertEqual(self.client.post('/simulator/control/start').status_code, 400)
        self.assertEqual(self.client.post('/simulator/control/restart').status_code, 400)


if __name__ == '__main__':
    unittest.main()
