# server.py

import os
import json
import time
import threading
import logging
import queue
from flask import Flask, request, jsonify
from collections import deque
import requests
import cloudscraper
import random
from datetime import datetime, timezone, timedelta

# --- КОНФИГУРАЦИЯ ---
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SETTINGS_FILE = os.path.join(DATA_DIR, "server_settings.json")
POSITIONS_FILE = os.path.join(DATA_DIR, "active_positions.json")
SIMULATOR_STATE_FILE = os.path.join(DATA_DIR, "simulator_state.json")
LOG_MAX_LEN = 200

# --- API ---
DEX_LATEST = "https://api.dexscreener.com/token-boosts/latest/v1"
DEX_PAIR_INFO_API = "https://api.dexscreener.com/latest/dex/pairs/solana/{addresses}"
DEX_PRICE_API = "https://api.dexscreener.com/latest/dex/tokens/{addresses}"

# --- Ключевые слова для разделения логов ---
TRADING_KEYWORDS = ["[ТРЕЙДЕР]", "Позиция", "прошел фильтры", "Обнаружена просадка", "Отклонен", "Таймаут", "Движок:",
                    "[СИМУЛЯТОР]"]

# --- ГЛОБАЛЬНОЕ СОСТОЯНИЕ И БЛОКИРОВКИ ---
shared_state = {
    "settings": {"min_fdv": 30000, "position_size_usd": 10.0, "drawdown_pct": 15.0, "stop_loss_pct": 20.0,
                 "take_profit_pct": 50.0, "wallet_private_key": 'YOUR_WALLET_PRIVATE_KEY', "simulation_enabled": False,
                 "simulation_start_balance": 1000.0},
    "trading_status": "RUNNING", "active_positions": {}, "trading_logs": deque(maxlen=LOG_MAX_LEN),
    "server_logs": deque(maxlen=LOG_MAX_LEN),
    "simulator": None, "price_cache": {}, "tokens_to_track": set(), "candidate_queue": queue.Queue()
}
GLOBAL_LOCK = threading.RLock()

# --- СИСТЕМА ЛОГИРОВАНИЯ ---
log_queue = queue.Queue()


class QueueLogHandler(logging.Handler):
    def emit(self, record): log_queue.put(self.format(record))


def log_processor():
    while True:
        message = log_queue.get();
        if not message.strip(): continue
        log_entry = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message.strip()}"
        print(log_entry, flush=True)
        is_trading_log = any(keyword in message for keyword in TRADING_KEYWORDS)
        with GLOBAL_LOCK:
            if is_trading_log:
                shared_state["trading_logs"].appendleft(log_entry)
            else:
                shared_state["server_logs"].appendleft(log_entry)
        log_queue.task_done()


formatter = logging.Formatter('%(levelname)s: %(message)s');
handler = QueueLogHandler();
handler.setFormatter(formatter)
root_logger = logging.getLogger();
root_logger.setLevel(logging.INFO);
root_logger.handlers.clear();
root_logger.addHandler(handler)
for logger_name in ["requests", "urllib3", "werkzeug"]: logging.getLogger(logger_name).setLevel(logging.WARNING)


# --- МЕНЕДЖЕР ЦЕН ---
class PriceManager(threading.Thread):
    def __init__(self, scraper, stop_event):
        super().__init__(daemon=True);
        self.scraper, self.stop_event = scraper, stop_event
        logging.info("Менеджер цен инициализирован.")

    def run(self):
        while not self.stop_event.is_set():
            try:
                with GLOBAL_LOCK:
                    tokens = list(shared_state["tokens_to_track"])
                if not tokens: time.sleep(5); continue
                for i in range(0, len(tokens), 30):
                    token_chunk = tokens[i:i + 30]
                    r = self.scraper.get(DEX_PRICE_API.format(addresses=",".join(token_chunk)), timeout=15)
                    if r.status_code == 200 and r.json().get("pairs"):
                        with GLOBAL_LOCK:
                            for pair in r.json()["pairs"]:
                                addr, price_str = pair.get("baseToken", {}).get("address"), pair.get("priceUsd")
                                if addr and price_str: shared_state["price_cache"][addr] = {"price": float(price_str),
                                                                                            "timestamp": time.time()}
                time.sleep(7)
            except Exception as e:
                logging.error(f"Ошибка в менеджере цен: {e}", exc_info=False);
                time.sleep(20)


def get_price_from_cache(token_address):
    with GLOBAL_LOCK:
        price_data = shared_state["price_cache"].get(token_address)
        if price_data and (time.time() - price_data["timestamp"]) < 45: return price_data["price"]
    return None


# --- КЛАСС СИМУЛЯТОРА ---
class TradingSimulator:
    def __init__(self, start_balance=1000.0):
        self.start_balance, self.balance, self.open_positions, self.completed_trades, self.status = float(
            start_balance), float(start_balance), {}, [], "STOPPED"
        self.log(f"Симулятор инициализирован. Баланс: ${start_balance:.2f}")

    @staticmethod
    def load_from_state(state):
        sim = TradingSimulator(state['start_balance']);
        sim.balance, sim.open_positions, sim.completed_trades, sim.status = state['balance'], state.get(
            'open_positions', {}), state.get('completed_trades', []), state.get('status', 'STOPPED')
        sim.log("Состояние симулятора восстановлено.");
        return sim

    def to_dict(self):
        return {'start_balance': self.start_balance, 'balance': self.balance, 'open_positions': self.open_positions,
                'completed_trades': self.completed_trades, 'status': self.status}

    def save(self):
        with open(SIMULATOR_STATE_FILE, 'w', encoding='utf-8') as f: json.dump(self.to_dict(), f, indent=2)

    def log(self, message):
        logging.info(f"[СИМУЛЯТОР] {message}")

    def set_status(self, status, message):
        self.status = status; self.log(message); self.save()

    def start(self):
        self.set_status("RUNNING", "Симуляция ЗАПУЩЕНА.")

    def pause(self):
        self.set_status("PAUSED", "Симуляция на ПАУЗЕ.")

    def resume(self):
        self.set_status("RUNNING", "Симуляция ВОЗОБНОВЛЕНА.")

    def stop(self):
        self.set_status("STOPPED", "Симуляция ОСТАНОВЛЕНА.")

    def buy(self, token_address, amount_usd, entry_drawdown_pct, fdv_at_discovery, token_name, buy_price):
        if self.status != "RUNNING": return False, f"Покупка отклонена, статус: {self.status}"
        if self.balance < amount_usd: self.log("ОШИБКА: Недостаточно средств."); return False, "Недостаточно средств"
        self.balance -= amount_usd
        token_amount = amount_usd / buy_price if buy_price > 0 else 0
        self.open_positions[token_address] = {"token_name": token_name, "token_ca": token_address,
                                              "amount_tokens": token_amount, "entry_value_usd": amount_usd,
                                              "entry_price": buy_price, "entry_time": time.time(),
                                              "entry_drawdown_pct": entry_drawdown_pct,
                                              "fdv_at_discovery": fdv_at_discovery}
        self.log(f"ПОКУПКА: {token_name} по цене {buy_price:.8f} на ${amount_usd:.2f}.");
        self.save()
        return True, {"token": token_address, "buy_price": buy_price, "amount_tokens": token_amount,
                      "entry_value_usd": amount_usd, "timestamp": time.time()}

    def sell(self, token_address, sell_price, exit_reason):
        if token_address not in self.open_positions: self.log(
            f"ОШИБКА: {token_address} нет в портфеле."); return False, "Позиция не найдена"
        position = self.open_positions.pop(token_address)
        sell_value = position["amount_tokens"] * sell_price;
        pnl_usd = sell_value - position['entry_value_usd'];
        pnl_pct = (pnl_usd / position['entry_value_usd'] * 100) if position['entry_value_usd'] > 0 else 0
        self.balance += sell_value
        trade_record = {**position, "exit_time": time.time(), "exit_price": sell_price, "pnl_usd": pnl_usd,
                        "pnl_pct": pnl_pct, "exit_reason": exit_reason, "status": "Closed"}
        self.completed_trades.append(trade_record)
        pnl_str = f"ПРИБЫЛЬ ${pnl_usd:.2f}" if pnl_usd >= 0 else f"УБЫТОК ${-pnl_usd:.2f}"
        self.log(f"ПРОДАЖА ({exit_reason}): {position['token_name']}. {pnl_str}. Баланс: ${self.balance:.2f}");
        self.save()
        return True

    def get_state(self):
        pnl = self.balance - self.start_balance;
        pnl_percent = (pnl / self.start_balance * 100) if self.start_balance > 0 else 0
        active_trades = []
        for token_ca, pos in self.open_positions.items():
            trade = {**pos, "status": "Open"}
            current_price = get_price_from_cache(token_ca)
            if current_price:
                current_value = pos["amount_tokens"] * current_price
                trade["pnl_usd"] = current_value - pos["entry_value_usd"]
                trade["pnl_pct"] = (trade["pnl_usd"] / pos["entry_value_usd"] * 100) if pos[
                                                                                            "entry_value_usd"] > 0 else 0
            active_trades.append(trade)
        all_trades = sorted(self.completed_trades + active_trades, key=lambda x: x.get('entry_time', 0), reverse=True)
        return {"status": self.status, "balance": round(self.balance, 2), "pnl": round(pnl, 2),
                "pnl_percent": round(pnl_percent, 2), "open_positions_count": len(self.open_positions),
                "all_trades": all_trades}


# --- УТИЛИТЫ СОХРАНЕНИЯ/ЗАГРУЗКИ ---
def save_state():
    with GLOBAL_LOCK:
        with open(SETTINGS_FILE, 'w') as f: json.dump(shared_state['settings'], f, indent=2)
        with open(POSITIONS_FILE, 'w') as f: json.dump(shared_state['active_positions'], f, indent=2)


def load_state():
    with GLOBAL_LOCK:
        try:
            with open(SETTINGS_FILE, 'r') as f:
                shared_state['settings'].update(json.load(f)); logging.info("Настройки загружены.")
        except (FileNotFoundError, json.JSONDecodeError):
            logging.info("Файл настроек не найден.")
        try:
            with open(POSITIONS_FILE, 'r') as f:
                shared_state['active_positions'].update(json.load(f)); logging.info(
                    f"Загружено {len(shared_state['active_positions'])} поз.")
        except (FileNotFoundError, json.JSONDecodeError):
            logging.info("Файл позиций не найден.")


# --- МОДУЛЬ ТОРГОВЛИ ---
class SolanaTrader:
    def buy(self, token_address, amount_usd, private_key, entry_drawdown_pct, fdv_at_discovery, token_name, buy_price):
        with GLOBAL_LOCK:
            if shared_state['settings']['simulation_enabled']:
                if shared_state['simulator']: return shared_state['simulator'].buy(token_address, amount_usd,
                                                                                   entry_drawdown_pct, fdv_at_discovery,
                                                                                   token_name, buy_price)
                return False, "Симулятор не инициализирован"
        logging.info(f"[ТРЕЙДЕР] Покупка {token_address} на ${amount_usd}")
        token_amount = amount_usd / buy_price if buy_price > 0 else 0
        return True, {"token": token_address, "buy_price": buy_price, "amount_tokens": token_amount,
                      "entry_value_usd": amount_usd, "timestamp": time.time()}

    def sell(self, token_address, amount_tokens, private_key, sell_price, exit_reason):
        with GLOBAL_LOCK:
            if shared_state['settings']['simulation_enabled']:
                if shared_state['simulator']: return shared_state['simulator'].sell(token_address, sell_price,
                                                                                    exit_reason)
                return False, "Симулятор не инициализирован"
        logging.info(f"[ТРЕЙДЕР] Продажа {amount_tokens} {token_address}");
        return True


# --- МОДУЛИ БОТА ---
class PositionManager(threading.Thread):
    def __init__(self, position_data, stop_event):
        super().__init__(daemon=True);
        self.position, self.stop_event = position_data, stop_event;
        self.token_addr = position_data['token']

    def run(self):
        with GLOBAL_LOCK:
            shared_state["tokens_to_track"].add(self.token_addr)
        logging.info(f"Позиция {self.token_addr}: Мониторинг запущен.")
        with GLOBAL_LOCK:
            sl_pct, tp_pct, pk = shared_state['settings']['stop_loss_pct'], shared_state['settings']['take_profit_pct'], \
                                 shared_state['settings']['wallet_private_key']
        sl_price, tp_price = self.position['buy_price'] * (1 - sl_pct / 100), self.position['buy_price'] * (
                    1 + tp_pct / 100)
        while not self.stop_event.is_set():
            try:
                current_price = get_price_from_cache(self.token_addr)
                if current_price is None: time.sleep(1); continue
                if current_price <= sl_price:
                    logging.info(f"Позиция {self.token_addr}: Сработал Stop-Loss! Цена: {current_price}");
                    SolanaTrader().sell(self.token_addr, self.position['amount_tokens'], pk, sell_price=current_price,
                                        exit_reason="Stop-Loss");
                    break
                if current_price >= tp_price:
                    logging.info(f"Позиция {self.token_addr}: Сработал Take-Profit! Цена: {current_price}");
                    SolanaTrader().sell(self.token_addr, self.position['amount_tokens'], pk, sell_price=current_price,
                                        exit_reason="Take-Profit");
                    break
                time.sleep(1)
            except Exception as e:
                logging.error(f"Позиция {self.token_addr}: Ошибка: {e}", exc_info=False); time.sleep(5)
        with GLOBAL_LOCK:
            shared_state["tokens_to_track"].discard(self.token_addr)
            if self.token_addr in shared_state['active_positions']: del shared_state['active_positions'][
                self.token_addr]
        logging.info(f"Позиция {self.token_addr}: Мониторинг завершен.");
        save_state()


class TradingEngine:
    def __init__(self):
        pass

    def _start_drawdown_tracking(self, token_data):
        threading.Thread(target=self._process_drawdown, args=(token_data,), daemon=True).start()

    def _process_drawdown(self, token_data):
        addr, fdv, token_name, boost = token_data['address'], token_data['fdv'], token_data['name'], token_data[
            'boost_amount']
        with GLOBAL_LOCK:
            shared_state["tokens_to_track"].add(addr)
        logging.info(f"Движок: {addr} прошел фильтры (FDV: ${fdv:,.0f}) (буст {boost}). Начинаю отслеживание.")
        peak_price, start_time = 0, time.time()
        while time.time() - start_time < 3600:
            with GLOBAL_LOCK:
                if shared_state['trading_status'] != 'RUNNING': break
                settings = shared_state['settings'].copy()
            try:
                current_price = get_price_from_cache(addr)
                if current_price is None: time.sleep(1); continue
                if peak_price == 0: peak_price = current_price
                if current_price > peak_price: peak_price = current_price
                drawdown = (peak_price - current_price) / peak_price * 100 if peak_price > 0 else 0
                if drawdown >= settings['drawdown_pct']:
                    buy_price_real = get_price_from_cache(addr) or current_price
                    logging.info(f"Движок: Просадка {drawdown:.2f}% для {addr}. Вхожу по цене {buy_price_real:.8f}.")
                    trader = SolanaTrader()
                    success, details = trader.buy(addr, settings['position_size_usd'], settings['wallet_private_key'],
                                                  entry_drawdown_pct=drawdown, fdv_at_discovery=fdv,
                                                  token_name=token_name, buy_price=buy_price_real)
                    if success:
                        with GLOBAL_LOCK: shared_state['active_positions'][addr] = details
                        save_state();
                        PositionManager(details, threading.Event()).start()
                    break
                time.sleep(1)
            except Exception as e:
                logging.error(f"Движок: Ошибка отслеживания {addr}: {e}", exc_info=False); time.sleep(5)
        with GLOBAL_LOCK:
            shared_state["tokens_to_track"].discard(addr)
        if time.time() - start_time >= 3600: logging.info(f"Движок: Таймаут отслеживания для {addr}.")


class CandidateProcessor(threading.Thread):
    def __init__(self, scraper, stop_event, trading_engine):
        super().__init__(daemon=True);
        self.scraper, self.stop_event, self.engine = scraper, stop_event, trading_engine
        self.processed_candidates = set()
        logging.info("Обработчик кандидатов инициализирован.")

    def run(self):
        while not self.stop_event.is_set():
            try:
                candidates_to_check = []
                while not shared_state["candidate_queue"].empty():
                    candidate_data = shared_state["candidate_queue"].get()
                    if candidate_data['address'] not in self.processed_candidates:
                        candidates_to_check.append(candidate_data);
                        self.processed_candidates.add(candidate_data['address'])
                if not candidates_to_check: time.sleep(5); continue
                with GLOBAL_LOCK:
                    min_fdv = shared_state["settings"]["min_fdv"]
                token_addresses = [c['address'] for c in candidates_to_check]
                for i in range(0, len(token_addresses), 30):
                    chunk_addrs = token_addresses[i:i + 30]
                    url = "https://api.dexscreener.com/latest/dex/tokens/" + ",".join(chunk_addrs)
                    r = self.scraper.get(url, timeout=15)
                    if r.status_code != 200: continue
                    data = r.json()
                    if not data or not data.get("pairs"): continue
                    pairs_by_address = {p.get("baseToken", {}).get("address"): p for p in data["pairs"] if p}
                    for candidate in candidates_to_check:
                        addr = candidate['address']
                        pair = pairs_by_address.get(addr)
                        if not pair: continue
                        if not (addr and addr.endswith("pump")):
                            if addr: logging.info(f"Движок: Отклонен {addr} (не 'pump' токен).")
                            continue
                        fdv = pair.get("fdv", 0)
                        if fdv >= min_fdv:
                            self.engine._start_drawdown_tracking(
                                {"address": addr, "fdv": fdv, "name": pair.get("baseToken", {}).get("name", "Unknown"),
                                 "boost_amount": candidate['boost_amount']})
                        else:
                            logging.info(f"Движок: Отклонен {addr} (FDV ${fdv:,.0f} < ${min_fdv:,.0f}).")
                time.sleep(5)
            except Exception as e:
                logging.error(f"Ошибка в обработчике кандидатов: {e}", exc_info=False); time.sleep(20)


class DexScreenerMonitor(threading.Thread):
    def __init__(self, stop_event, scraper):
        super().__init__(daemon=True);
        self.stop_event, self.scraper = stop_event, scraper
        self.seen_tokens, self.is_first_run = set(), True

    def run(self):
        logging.info("Монитор Dexscreener запущен.")
        while not self.stop_event.is_set():
            try:
                with GLOBAL_LOCK:
                    if shared_state['trading_status'] != 'RUNNING': time.sleep(5); continue
                r = self.scraper.get(DEX_LATEST, timeout=20);
                r.raise_for_status()
                json_response = r.json()
                boosts = json_response.get("boosts", []) if isinstance(json_response,
                                                                       dict) else json_response if isinstance(
                    json_response, list) else []
                if self.is_first_run:
                    if isinstance(boosts, list): self.seen_tokens.update(
                        t.get("tokenAddress") for t in boosts if isinstance(t, dict))
                    self.is_first_run = False;
                    logging.info(f"Монитор: База из {len(self.seen_tokens)} токенов установлена.");
                    time.sleep(10);
                    continue
                if isinstance(boosts, list):
                    for token_data in boosts:
                        if not isinstance(token_data, dict): continue
                        addr = token_data.get("tokenAddress")
                        if addr and addr not in self.seen_tokens:
                            self.seen_tokens.add(addr);
                            boost_amount = token_data.get('amount', 0)
                            logging.info(f"Монитор: Новый буст ({boost_amount}) для {addr}");
                            shared_state["candidate_queue"].put({'address': addr, 'boost_amount': boost_amount})
                time.sleep(10)
            except Exception as e:
                logging.error(f"Ошибка в мониторе: {e}", exc_info=False); time.sleep(30)


# --- FLASK API ---
app = Flask(__name__)


@app.route('/settings', methods=['POST'])
def handle_settings():
    with GLOBAL_LOCK:
        new_settings = request.json;
        shared_state['settings'].update(new_settings)
        sim_enabled = shared_state['settings'].get('simulation_enabled', False)
        if sim_enabled and not shared_state.get('simulator'):
            start_balance = shared_state['settings'].get('simulation_start_balance', 1000.0)
            shared_state['simulator'] = TradingSimulator(start_balance)
        elif not sim_enabled and shared_state.get('simulator'):
            shared_state['simulator'] = None
        save_state();
        logging.info("Настройки обновлены.")
    return jsonify({"status": "ok"})


@app.route('/status', methods=['GET'])
def get_status():
    with GLOBAL_LOCK:
        trading_logs, server_logs = list(shared_state['trading_logs']), list(shared_state['server_logs'])
        positions = shared_state['active_positions']
        status = shared_state['trading_status']
        simulator_state = shared_state['simulator'].get_state() if shared_state.get('simulator') else None
    return jsonify({"trading_logs": trading_logs, "server_logs": server_logs, "active_positions": positions,
                    "trading_status": status, "simulator_state": simulator_state})


@app.route('/control/<command>', methods=['POST'])
def handle_main_control(command):
    status_map, msg_map = {"pause": "PAUSED", "resume": "RUNNING", "stop": "STOPPED"}, {"pause": "ПАУЗУ",
                                                                                        "resume": "ВОЗОБНОВЛЕНА",
                                                                                        "stop": "ОСТАНОВЛЕНА"}
    if command in status_map:
        with GLOBAL_LOCK: shared_state['trading_status'] = status_map[command]
        logging.info(f"Торговля: {msg_map[command]}.");
        return jsonify({"status": "ok", "trading_status": status_map[command]})
    return jsonify({"status": "error", "message": "Unknown command"}), 400


@app.route('/simulator/control/<command>', methods=['POST'])
def handle_simulator_control(command):
    with GLOBAL_LOCK:
        if command == 'restart':
            if not shared_state['settings'].get('simulation_enabled'): return jsonify(
                {"status": "error", "message": "Sim disabled"}), 400
            start_balance = shared_state['settings'].get('simulation_start_balance', 1000.0)
            shared_state['simulator'] = TradingSimulator(start_balance);
            shared_state['simulator'].save()
            logging.info("Симулятор перезапущен.");
            return jsonify({"status": "ok", "simulator_status": shared_state['simulator'].status})
        sim = shared_state.get('simulator')
        if not sim: return jsonify({"status": "error", "message": "Sim not enabled"}), 400
        actions = {"start": sim.start, "pause": sim.pause, "resume": sim.resume, "stop": sim.stop}
        if command in actions: actions[command](); return jsonify({"status": "ok", "simulator_status": sim.status})
    return jsonify({"status": "error", "message": "Unknown command"}), 400


# --- ГЛАВНАЯ ФУНКЦИЯ ---
if __name__ == '__main__':
    log_thread = threading.Thread(target=log_processor, daemon=True);
    log_thread.start()
    os.makedirs(DATA_DIR, exist_ok=True);
    load_state()
    with GLOBAL_LOCK:
        if shared_state['settings'].get('simulation_enabled', False):
            try:
                with open(SIMULATOR_STATE_FILE, 'r', encoding='utf-8') as f:
                    sim_state = json.load(f)
                    shared_state['simulator'] = TradingSimulator.load_from_state(sim_state)
                    shared_state['active_positions'] = {}
                    for token_ca, pos_data in sim_state.get('open_positions', {}).items():
                        shared_state['active_positions'][token_ca] = {
                            "token": token_ca, "buy_price": pos_data['entry_price'],
                            "amount_tokens": pos_data['amount_tokens'],
                            "entry_value_usd": pos_data['entry_value_usd'], "timestamp": pos_data['entry_time']
                        }
                    logging.info(
                        f"Синхронизировано {len(shared_state['active_positions'])} открытых позиций из симулятора.")
            except (FileNotFoundError, json.JSONDecodeError):
                logging.warning("Файл симулятора не найден. Создание нового.")
                shared_state['simulator'] = TradingSimulator(
                    shared_state['settings'].get('simulation_start_balance', 1000.0))

    scraper = cloudscraper.create_scraper()
    stop_event = threading.Event()
    trading_engine = TradingEngine()
    candidate_processor = CandidateProcessor(scraper, stop_event, trading_engine)
    price_manager = PriceManager(scraper, stop_event)
    monitor = DexScreenerMonitor(stop_event, scraper)

    candidate_processor.start();
    price_manager.start();
    monitor.start()

    with GLOBAL_LOCK:
        for addr, pos_data in shared_state['active_positions'].items():
            PositionManager(pos_data, threading.Event()).start()

    logging.info("Сервер запущен. API доступен на порту 5000.")
    app.run(host='0.0.0.0', port=5000, threaded=True)