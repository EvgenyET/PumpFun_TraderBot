"""Run the existing Flask API locally without market workers or external calls."""
import argparse
import os
from pathlib import Path
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=5000)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    previous = Path.cwd()
    with tempfile.TemporaryDirectory(prefix='pumpfun-demo-') as folder:
        try:
            os.chdir(folder)
            import server
            server.shared_state['settings'].update(simulation_enabled=True, simulation_start_balance=1000.0)
            server.shared_state['simulator'] = server.TradingSimulator(1000.0)
            print('Local demo: no market workers, no external requests; state is temporary.')
            server.app.run(host='127.0.0.1', port=args.port, debug=False, use_reloader=False)
        finally:
            os.chdir(previous)


if __name__ == '__main__':
    main()
