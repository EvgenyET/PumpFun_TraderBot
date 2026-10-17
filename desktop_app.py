# desktop_app.py

import sys
import os
import json
import requests
import paramiko
import threading
import base64
import time
import webbrowser
from datetime import datetime, timezone, timedelta
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QFormLayout,
    QLineEdit, QDoubleSpinBox, QPushButton, QTextEdit, QMessageBox, QGroupBox, QHBoxLayout,
    QLabel, QTabWidget, QCheckBox, QDialog, QTableWidget, QTableWidgetItem, QHeaderView,
    QMenu, QProgressDialog
)
from PyQt5.QtCore import QTimer, QObject, pyqtSignal, Qt, QRectF
from PyQt5.QtGui import QPainter, QBrush, QPen
import numpy as np
import pyqtgraph as pg

# --- КОНФИГУРАЦИЯ ---
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SETTINGS_FILE = os.path.join(DATA_DIR, "desktop_settings.json")
MOSCOW_TZ = timezone(timedelta(hours=3))
# API из вашего примера
GECKO_BASE_URL = "https://api.geckoterminal.com/api/v2"
GECKO_TOKEN_POOLS_API = f"{GECKO_BASE_URL}/networks/solana/tokens/{{token_address}}/pools"
GECKO_OHLCV_API = f"{GECKO_BASE_URL}/networks/solana/pools/{{pool_address}}/ohlcv/minute?aggregate=5&limit=1000"


class WorkerSignals(QObject):
    finished = pyqtSignal(object)
    error = pyqtSignal(str)
    message = pyqtSignal(str, str)


# --- НОВЫЙ КЛАСС: ОКНО С ГРАФИКОМ ---
class ChartDialog(QDialog):
    data_ready_signal = pyqtSignal(object)

    def __init__(self, trade_data, parent=None):
        super().__init__(parent)
        self.trade = trade_data
        self.setWindowTitle(f"График: {self.trade.get('token_name', 'N/A')}")
        self.setGeometry(250, 250, 1000, 600)

        layout = QVBoxLayout(self)
        self.plot_widget = pg.PlotWidget()
        layout.addWidget(self.plot_widget)

        self.progress_dialog = QProgressDialog("Загрузка данных графика...", None, 0, 0, self)
        self.progress_dialog.setWindowModality(Qt.WindowModal)
        self.progress_dialog.show()

        self.data_ready_signal.connect(self.plot_chart)
        self.fetch_chart_data()

    def fetch_chart_data(self):
        def worker():
            token_ca = self.trade.get('token_ca')
            retries = 5
            backoff_base = 1.5

            # 1. Получаем ПУЛЫ для токена
            pool_address = None
            for i in range(retries):
                try:
                    url = GECKO_TOKEN_POOLS_API.format(token_address=token_ca)
                    r = requests.get(url, timeout=10)
                    r.raise_for_status()
                    pools = r.json().get("data", [])
                    if pools:
                        # Сортируем пулы по объему за 24ч, чтобы найти самый ликвидный
                        pools.sort(key=lambda p: float(p.get("attributes", {}).get("volume_usd", {}).get("h24", 0)),
                                   reverse=True)
                        pool_address = pools[0].get("id").replace("solana_", "")
                        break
                    time.sleep(backoff_base ** i)
                except Exception as e:
                    if i == retries - 1:
                        self.data_ready_signal.emit({"error": f"Не удалось найти пулы: {e}"});
                        return
                    time.sleep(backoff_base ** i)

            if not pool_address:
                self.data_ready_signal.emit({"error": "Не найдено ни одного активного пула для токена."});
                return

            # 2. Получаем данные OHLCV для найденного пула
            for i in range(retries):
                try:
                    url = GECKO_OHLCV_API.format(pool_address=pool_address)
                    r_ohlcv = requests.get(url, timeout=15)
                    r_ohlcv.raise_for_status()
                    ohlcv_list = r_ohlcv.json().get("data", {}).get("attributes", {}).get("ohlcv_list", [])
                    self.data_ready_signal.emit({"ohlcv": ohlcv_list})
                    return
                except Exception as e:
                    if i == retries - 1:
                        self.data_ready_signal.emit({"error": f"Не удалось загрузить историю цен: {e}"});
                        return
                    time.sleep(backoff_base ** i)

        threading.Thread(target=worker, daemon=True).start()

    def plot_chart(self, data):
        self.progress_dialog.close()
        error = data.get("error")
        if error: QMessageBox.warning(self, "Ошибка", error); self.close(); return
        ohlcv_list = data.get("ohlcv")
        if not ohlcv_list: QMessageBox.warning(self, "Ошибка",
                                               "API не вернул данные истории цен."); self.close(); return

        # --- ИСПРАВЛЕНИЕ: Гарантируем хронологический порядок ---
        ohlcv_list.sort(key=lambda x: x[0])

        # Подготовка данных
        times = [d[0] for d in ohlcv_list]
        prices = [d[4] for d in ohlcv_list]  # Используем цену закрытия (close)

        # Настройка осей
        time_ticks = [(i, datetime.fromtimestamp(ts).strftime('%H:%M')) for i, ts in enumerate(times) if
                      i % (len(times) // 10 or 1) == 0]
        self.plot_widget.getAxis('bottom').setTicks([time_ticks])
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)

        # Рисуем линейный график цены
        self.plot_widget.plot(range(len(prices)), prices, pen=pg.mkPen('c', width=2))

        # Рисуем точки входа/выхода
        entry_time, exit_time = self.trade.get('entry_time'), self.trade.get('exit_time')
        if entry_time:
            entry_x = min(range(len(times)), key=lambda i: abs(times[i] - entry_time))
            entry_price = self.trade.get('entry_price')
            entry_arrow = pg.ArrowItem(pos=(entry_x, entry_price), angle=-90, pen=None, brush='g', tipAngle=60,
                                       headLen=15)
            self.plot_widget.addItem(entry_arrow)
        if exit_time:
            exit_x = min(range(len(times)), key=lambda i: abs(times[i] - exit_time))
            exit_price = self.trade.get('exit_price')
            exit_arrow = pg.ArrowItem(pos=(exit_x, exit_price), angle=90, pen=None, brush='r', tipAngle=60, headLen=15)
            self.plot_widget.addItem(exit_arrow)

        # --- ИСПРАВЛЕНИЕ: Принудительное обновление диапазона осей ---
        self.plot_widget.autoRange()


# --- ОКНО ДЛЯ ТАБЛИЦЫ СДЕЛОК ---
class TradesDialog(QDialog):
    def __init__(self, trades_data, parent=None):
        super().__init__(parent)
        self.setWindowTitle("История сделок симулятора");
        self.setGeometry(200, 200, 1200, 600)
        self.trades_data = trades_data
        layout = QVBoxLayout(self);
        self.table = QTableWidget();
        layout.addWidget(self.table)
        self.headers = ["Статус", "Название", "CA", "FDV", "Вход (МСК)", "Выход (МСК)", "Просадка (%)", "Цена входа",
                        "Цена выхода", "P/L ($)", "P/L (%)", "Причина"]
        self.table.setColumnCount(len(self.headers));
        self.table.setHorizontalHeaderLabels(self.headers)
        self.populate_table(self.trades_data)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers);
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch);
        self.table.resizeColumnsToContents()
        self.table.setSortingEnabled(True);
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_context_menu)
        self.table.doubleClicked.connect(self.handle_double_click)

    def get_trade_data_from_row(self, row):
        token_ca_item = self.table.item(row, 2)
        if not token_ca_item: return None
        token_ca = token_ca_item.text()
        return next((trade for trade in self.trades_data if trade.get('token_ca') == token_ca), None)

    def handle_double_click(self, index):
        trade_data = self.get_trade_data_from_row(index.row())
        if trade_data: ChartDialog(trade_data, self).exec_()

    def show_context_menu(self, pos):
        row = self.table.indexAt(pos).row()
        if row < 0: return
        trade_data = self.get_trade_data_from_row(row)
        if not trade_data or not trade_data.get('token_ca'): return
        token_ca = trade_data.get('token_ca')
        menu = QMenu()
        copy_ca_action = menu.addAction("Скопировать CA");
        open_browser_action = menu.addAction("Открыть в браузере");
        copy_link_action = menu.addAction("Скопировать ссылку")
        action = menu.exec_(self.table.mapToGlobal(pos))
        url = f"https://dexscreener.com/solana/{token_ca}";
        clipboard = QApplication.clipboard()
        if action == copy_ca_action:
            clipboard.setText(token_ca)
        elif action == open_browser_action:
            webbrowser.open(url)
        elif action == copy_link_action:
            clipboard.setText(url)

    def populate_table(self, trades_data):
        self.table.setRowCount(len(trades_data))
        for row, trade in enumerate(trades_data):
            self.set_item(row, 0, trade.get("status", "-"), is_status=True);
            self.set_item(row, 1, trade.get("token_name", "-"));
            self.set_item(row, 2, trade.get("token_ca", ""))
            self.set_item(row, 3, f"${trade.get('fdv_at_discovery', 0):,.0f}");
            self.set_item(row, 4, self.format_time(trade.get("entry_time")));
            self.set_item(row, 5, self.format_time(trade.get("exit_time")))
            self.set_item(row, 6, f"{trade.get('entry_drawdown_pct', 0):.2f}%");
            self.set_item(row, 7, f"{trade.get('entry_price', 0):.8f}")
            self.set_item(row, 8, f"{trade.get('exit_price', 0):.8f}" if "exit_price" in trade else "-")
            pnl_usd, pnl_pct = trade.get("pnl_usd"), trade.get("pnl_pct")
            self.set_item(row, 9, f"{pnl_usd:+.2f}" if pnl_usd is not None else "-", is_pnl=True)
            self.set_item(row, 10, f"{pnl_pct:+.2f}%" if pnl_pct is not None else "-", is_pnl=True)
            self.set_item(row, 11, trade.get("exit_reason", "-"))

    def set_item(self, row, col, text, is_pnl=False, is_status=False):
        item = QTableWidgetItem(str(text));
        item.setTextAlignment(Qt.AlignCenter)
        if is_pnl and text != "-":
            value = float(text.replace('$', '').replace('%', ''))
            if value > 0:
                item.setForeground(Qt.green)
            elif value < 0:
                item.setForeground(Qt.red)
        if is_status:
            if text == "Open":
                item.setForeground(Qt.green)
            else:
                item.setForeground(Qt.lightGray)
        self.table.setItem(row, col, item)

    def format_time(self, timestamp):
        if not timestamp: return "-"
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(MOSCOW_TZ).strftime('%Y-%m-%d %H:%M:%S')


# --- ГЛАВНОЕ ОКНО ---
class TradingBotGUI(QMainWindow):
    def __init__(self):
        super().__init__();
        self.setWindowTitle("Панель управления торговым ботом");
        self.setGeometry(100, 100, 800, 800)
        self.all_trades_data = []
        self.central_widget = QWidget();
        self.setCentralWidget(self.central_widget)
        self.layout = QVBoxLayout(self.central_widget)
        self._setup_ui();
        self.load_settings()
        self.status_timer = QTimer(self);
        self.status_timer.timeout.connect(self.get_status);
        self.get_status();
        self.status_timer.start(5000)

    def _setup_ui(self):
        ssh_group = QGroupBox("Управление сервером (SSH)")
        ssh_layout = QFormLayout()
        self.ssh_host_input = QLineEdit("YOUR_SERVER_HOST");
        self.ssh_user_input = QLineEdit("root")
        self.ssh_pass_input = QLineEdit();
        self.ssh_pass_input.setEchoMode(QLineEdit.Password)
        ssh_layout.addRow("Хост (IP):", self.ssh_host_input);
        ssh_layout.addRow("Пользователь:", self.ssh_user_input);
        ssh_layout.addRow("Пароль:", self.ssh_pass_input)
        ssh_buttons_layout = QHBoxLayout();
        self.upload_button = QPushButton("Загрузить на сервер");
        self.restart_button = QPushButton("Перезапустить бота")
        ssh_buttons_layout.addWidget(self.upload_button);
        ssh_buttons_layout.addWidget(self.restart_button);
        ssh_layout.addRow(ssh_buttons_layout)
        ssh_group.setLayout(ssh_layout)
        control_group = QGroupBox("Управление торговлей (Основной бот)")
        control_layout = QHBoxLayout()
        self.server_status_label = QLabel("СТАТУС: НЕИЗВЕСТНО");
        self.server_status_label.setStyleSheet("font-weight: bold;")
        self.pause_button, self.resume_button, self.stop_button = QPushButton("Пауза"), QPushButton(
            "Продолжить"), QPushButton("Стоп")
        control_layout.addWidget(QLabel("Статус бота:"));
        control_layout.addWidget(self.server_status_label);
        control_layout.addStretch()
        control_layout.addWidget(self.pause_button);
        control_layout.addWidget(self.resume_button);
        control_layout.addWidget(self.stop_button)
        control_group.setLayout(control_layout)
        settings_group = QGroupBox("Параметры торговли")
        sl = QFormLayout()
        self.server_ip_input = QLineEdit("http://YOUR_SERVER_HOST:5000")
        self.min_fdv_input = QDoubleSpinBox();
        self.min_fdv_input.setRange(0, 1e9);
        self.min_fdv_input.setGroupSeparatorShown(True);
        self.min_fdv_input.setDecimals(0);
        self.min_fdv_input.setValue(30000);
        self.min_fdv_input.setSuffix(" $")
        self.position_size_input = QDoubleSpinBox();
        self.position_size_input.setRange(1, 1e4);
        self.position_size_input.setValue(10);
        self.position_size_input.setSuffix(" $")
        self.drawdown_input = QDoubleSpinBox();
        self.drawdown_input.setRange(0, 100);
        self.drawdown_input.setValue(15.0);
        self.drawdown_input.setSuffix(" %")
        self.stop_loss_input = QDoubleSpinBox();
        self.stop_loss_input.setRange(0, 100);
        self.stop_loss_input.setValue(20.0);
        self.stop_loss_input.setSuffix(" %")
        self.take_profit_input = QDoubleSpinBox();
        self.take_profit_input.setRange(0, 1e4);
        self.take_profit_input.setValue(50.0);
        self.take_profit_input.setSuffix(" %")
        self.private_key_input = QLineEdit();
        self.private_key_input.setEchoMode(QLineEdit.Password);
        self.private_key_input.setPlaceholderText("Вставьте ключ Base58 здесь")
        sl.addRow("Адрес API сервера:", self.server_ip_input);
        sl.addRow("Минимальный FDV:", self.min_fdv_input);
        sl.addRow("Размер позиции (USD):", self.position_size_input)
        sl.addRow("Просадка для входа:", self.drawdown_input);
        sl.addRow("Stop-Loss:", self.stop_loss_input);
        sl.addRow("Take-Profit:", self.take_profit_input)
        sl.addRow("Приватный ключ кошелька:", self.private_key_input)
        settings_group.setLayout(sl)
        self.save_button = QPushButton("Сохранить и отправить на сервер")
        self.tabs = QTabWidget();
        self.trading_log_output = QTextEdit();
        self.server_log_output = QTextEdit()
        log_stylesheet = "background-color: #2b2b2b; color: #f0f0f0; font-family: Consolas, monospace;"
        self.trading_log_output.setReadOnly(True);
        self.trading_log_output.setStyleSheet(log_stylesheet)
        self.server_log_output.setReadOnly(True);
        self.server_log_output.setStyleSheet(log_stylesheet)
        simulator_tab_widget = self.create_simulator_tab()
        self.tabs.addTab(self.trading_log_output, "Торговые логи");
        self.tabs.addTab(self.server_log_output, "Серверные логи");
        self.tabs.addTab(simulator_tab_widget, "Симулятор")
        self.layout.addWidget(ssh_group);
        self.layout.addWidget(control_group);
        self.layout.addWidget(settings_group)
        self.layout.addWidget(self.save_button);
        self.layout.addWidget(QLabel("Терминал:"));
        self.layout.addWidget(self.tabs)
        self.save_button.clicked.connect(self.send_settings);
        self.upload_button.clicked.connect(self.upload_script);
        self.restart_button.clicked.connect(self.restart_bot)
        self.pause_button.clicked.connect(lambda: self._send_main_bot_command("pause"));
        self.resume_button.clicked.connect(lambda: self._send_main_bot_command("resume"));
        self.stop_button.clicked.connect(lambda: self._send_main_bot_command("stop"))

    def create_simulator_tab(self):
        widget = QWidget();
        layout = QVBoxLayout(widget);
        layout.setAlignment(Qt.AlignTop)
        control_group = QGroupBox("Управление симуляцией");
        cl = QHBoxLayout();
        cl.addWidget(QLabel("Статус симуляции:"))
        self.sim_status_label = QLabel("НЕИЗВЕСТНО");
        self.sim_status_label.setStyleSheet("font-weight: bold;");
        cl.addWidget(self.sim_status_label);
        cl.addStretch()
        self.sim_start_button = QPushButton("Старт");
        cl.addWidget(self.sim_start_button)
        self.sim_pause_button = QPushButton("Пауза");
        cl.addWidget(self.sim_pause_button)
        self.sim_resume_button = QPushButton("Продолжить");
        cl.addWidget(self.sim_resume_button)
        self.sim_stop_button = QPushButton("Стоп");
        cl.addWidget(self.sim_stop_button)
        self.sim_restart_button = QPushButton("Перезапустить");
        cl.addWidget(self.sim_restart_button)
        control_group.setLayout(cl)
        settings_group = QGroupBox("Настройки симулятора");
        sl = QFormLayout()
        self.sim_enabled_checkbox = QCheckBox("Включить режим симуляции")
        self.sim_start_balance_input = QDoubleSpinBox();
        self.sim_start_balance_input.setRange(1, 1e6);
        self.sim_start_balance_input.setGroupSeparatorShown(True);
        self.sim_start_balance_input.setValue(1000);
        self.sim_start_balance_input.setSuffix(" $")
        sl.addRow(self.sim_enabled_checkbox);
        sl.addRow("Стартовый депозит:", self.sim_start_balance_input);
        sl.addRow(QLabel("<i>(Для применения нажмите 'Сохранить')</i>"))
        settings_group.setLayout(sl)
        status_group = QGroupBox("Текущий статус симуляции");
        stl = QFormLayout()
        self.sim_balance_label = QLabel("N/A");
        self.sim_pnl_label = QLabel("N/A");
        self.sim_positions_label = QLabel("N/A")
        self.sim_balance_label.setStyleSheet("font-size: 14px; font-weight: bold;");
        self.sim_pnl_label.setStyleSheet("font-size: 14px;")
        stl.addRow("Текущий баланс:", self.sim_balance_label);
        stl.addRow("Прибыль/Убыток (P/L):", self.sim_pnl_label);
        stl.addRow("Открыто позиций:", self.sim_positions_label)
        status_group.setLayout(stl)
        self.view_trades_button = QPushButton("Посмотреть историю сделок")
        layout.addWidget(control_group);
        layout.addWidget(settings_group);
        layout.addWidget(status_group);
        layout.addWidget(self.view_trades_button, 0, Qt.AlignRight)
        self.sim_start_button.clicked.connect(lambda: self._send_simulator_command("start"));
        self.sim_pause_button.clicked.connect(lambda: self._send_simulator_command("pause"))
        self.sim_resume_button.clicked.connect(lambda: self._send_simulator_command("resume"));
        self.sim_stop_button.clicked.connect(lambda: self._send_simulator_command("stop"))
        self.sim_restart_button.clicked.connect(self.restart_simulator)
        self.view_trades_button.clicked.connect(self.show_trades_dialog)
        return widget

    def show_trades_dialog(self):
        TradesDialog(self.all_trades_data, self).exec_()

    def run_in_thread(self, target_func, on_finish, *args, **kwargs):
        signals = WorkerSignals();
        signals.finished.connect(on_finish);
        signals.error.connect(self.on_worker_error);
        signals.message.connect(self.on_worker_message)

        def w():
            try:
                signals.finished.emit(target_func(signals, *args, **kwargs))
            except Exception as e:
                signals.error.emit(str(e))

        threading.Thread(target=w, daemon=True).start()

    def on_worker_error(self, err_msg):
        if not self.status_timer.isActive(): self.status_timer.start(5000)
        self.server_status_label.setText("ОШИБКА");
        self.server_status_label.setStyleSheet("color: red; font-weight: bold;")
        self.server_log_output.append(f"\n--- ОШИБКА ПРИЛОЖЕНИЯ ---\n{err_msg}\n")

    def on_worker_message(self, title, message):
        self.server_log_output.append(f"\n--- {title.upper()} ---\n{message}\n")

    def _get_ssh_client(self):
        h, u, p = self.ssh_host_input.text(), self.ssh_user_input.text(), self.ssh_pass_input.text()
        if not all([h, u, p]): raise ValueError("Заполните все данные для SSH.")
        c = paramiko.SSHClient();
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy());
        c.connect(hostname=h, username=u, password=p, timeout=10)
        return c

    def upload_script(self):
        self.upload_button.setEnabled(False);
        self.upload_button.setText("Загрузка...")

        def do(s):
            lp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.py")
            if not os.path.exists(lp): raise FileNotFoundError(f"'server.py' не найден: {lp}")
            ssh = self._get_ssh_client();
            sftp = ssh.open_sftp();
            sftp.put(lp, "/home/trading_bot/server.py");
            sftp.close();
            ssh.close()
            s.message.emit("Успех", "Файл server.py успешно загружен.")

        def on_f(r): self.upload_button.setEnabled(True); self.upload_button.setText("Загрузить на сервер")

        self.run_in_thread(do, on_f)

    def restart_bot(self):
        self.status_timer.stop();
        self.server_status_label.setText("ПЕРЕЗАПУСК...");
        self.server_status_label.setStyleSheet("color: #f39c12; font-weight: bold;")
        self.restart_button.setEnabled(False);
        self.restart_button.setText("Перезапуск...")

        def do_restart(signals):
            ssh = self._get_ssh_client()
            kill_cmd = "pkill -f 'python3 server.py' || true; screen -S trading_bot -X quit || true"
            start_cmd = "screen -dmS trading_bot bash -c 'cd /home/trading_bot && env PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8 ./venv/bin/python3 server.py > bot.log 2>&1'"
            ssh.exec_command(kill_cmd);
            time.sleep(1);
            ssh.exec_command(start_cmd);
            ssh.close()
            signals.message.emit("Уведомление", "Команда перезапуска отправлена.");
            time.sleep(5)

        def on_finish(result): self.restart_button.setEnabled(True); self.restart_button.setText(
            "Перезапустить бота"); self.status_timer.start(5000); self.get_status()

        self.run_in_thread(do_restart, on_finish)

    def _send_command(self, endpoint, data=None, on_finish=None):
        def do(s):
            url = f"{self.get_server_url()}{endpoint}"
            if data:
                requests.post(url, json=data, timeout=10).raise_for_status()
            else:
                requests.post(url, timeout=10).raise_for_status()
            return requests.get(f"{self.get_server_url()}/status", timeout=10,
                                headers={'Cache-Control': 'no-cache'}).json()

        self.run_in_thread(do, on_finish or self.update_ui_from_data)

    def _send_main_bot_command(self, cmd):
        self._send_command(f"/control/{cmd}")

    def _send_simulator_command(self, cmd):
        self._send_command(f"/simulator/control/{cmd}")

    def get_server_url(self):
        return self.server_ip_input.text().strip()

    def restart_simulator(self):
        if not self.sim_enabled_checkbox.isChecked():
            QMessageBox.warning(self, "Внимание", "Нельзя перезапустить симулятор, т.к. он не включен в настройках.");
            return
        self.sim_restart_button.setEnabled(False);
        self.sim_restart_button.setText("...")

        def on_finish(data):
            self.sim_restart_button.setEnabled(True);
            self.sim_restart_button.setText("Перезапустить")
            self.update_ui_from_data(data)

        def run_restart_sequence(signals):
            requests.post(f"{self.get_server_url()}/settings", json=self.get_current_settings(),
                          timeout=10).raise_for_status()
            signals.message.emit("Уведомление", "Настройки отправлены, перезапускаем симулятор...")
            requests.post(f"{self.get_server_url()}/simulator/control/restart", timeout=10).raise_for_status()
            return requests.get(f"{self.get_server_url()}/status", timeout=10,
                                headers={'Cache-Control': 'no-cache'}).json()

        self.run_in_thread(run_restart_sequence, on_finish)

    def send_settings(self):
        if not self.sim_enabled_checkbox.isChecked() and not self.private_key_input.text():
            QMessageBox.warning(self, "Ошибка", "Приватный ключ обязателен для реальной торговли.");
            return
        self.save_button.setEnabled(False);
        self.save_button.setText("Отправка...")

        def on_finish(data):
            self.save_button.setEnabled(True);
            self.save_button.setText("Сохранить и отправить")
            self.update_ui_from_data(data)

        self._send_command("/settings", data=self.get_current_settings(), on_finish=on_finish)

    def get_current_settings(self):
        d = {k: v.value() for k, v in self.__dict__.items() if isinstance(v, QDoubleSpinBox)}
        d.update({"wallet_private_key": self.private_key_input.text(),
                  "simulation_enabled": self.sim_enabled_checkbox.isChecked(),
                  "simulation_start_balance": self.sim_start_balance_input.value()})
        return d

    def get_status(self):
        def do(s): return requests.get(f"{self.get_server_url()}/status", timeout=10,
                                       headers={'Cache-Control': 'no-cache'}).json()

        self.run_in_thread(do, self.update_ui_from_data)

    def _update_log_text(self, text_edit, new_text):
        scroll_bar = text_edit.verticalScrollBar()
        is_at_bottom = scroll_bar.value() >= (scroll_bar.maximum() - 15)
        old_value = scroll_bar.value()
        text_edit.setText(new_text)
        if is_at_bottom:
            scroll_bar.setValue(scroll_bar.maximum())
        else:
            scroll_bar.setValue(old_value)

    def update_ui_from_data(self, data):
        if not data: return
        s = data.get("trading_status", "UNKNOWN").upper();
        sc = {"RUNNING": "#2ecc71", "PAUSED": "#f39c12", "STOPPED": "#e74c3c"};
        st = {"RUNNING": "ЗАПУЩЕН", "PAUSED": "ПАУЗА", "STOPPED": "ОСТАНОВЛЕН"}
        self.server_status_label.setText(st.get(s, "НЕИЗВЕСТНО"));
        self.server_status_label.setStyleSheet(f"color: {sc.get(s, '#95a5a6')}; font-weight: bold;")
        self._update_log_text(self.server_log_output,
                              "--- СЕРВЕРНЫЕ ЛОГИ ---\n" + "\n".join(data.get("server_logs", [])))
        p = data.get("active_positions", {});
        tlt = "--- ТОРГОВЫЕ ЛОГИ ---\n" + "\n".join(data.get("trading_logs", []));
        tlt += "\n\n--- АКТИВНЫЕ ПОЗИЦИИ ---\n" + (json.dumps(p, indent=2, ensure_ascii=False) if p else "Нет.");
        self._update_log_text(self.trading_log_output, tlt)
        sim_data = data.get("simulator_state");
        sim_is_active = bool(sim_data)
        [b.setEnabled(sim_is_active) for b in
         [self.sim_start_button, self.sim_pause_button, self.sim_resume_button, self.sim_stop_button,
          self.sim_restart_button, self.view_trades_button]]
        if sim_is_active:
            ss = sim_data.get("status", "UNKNOWN").upper();
            sst = {"RUNNING": "ЗАПУЩЕН", "PAUSED": "ПАУЗА", "STOPPED": "ОСТАНОВЛЕН"};
            ssc = sc.get(ss, '#95a5a6')
            self.sim_status_label.setText(sst.get(ss, "НЕИЗВЕСТНО"));
            self.sim_status_label.setStyleSheet(f"color: {ssc}; font-weight: bold;")
            bal, pnl, pnl_p = sim_data.get("balance", 0), sim_data.get("pnl", 0), sim_data.get("pnl_percent", 0)
            self.sim_balance_label.setText(f"<b>${bal:,.2f}</b>");
            self.sim_positions_label.setText(str(sim_data.get("open_positions_count", 0)))
            pnl_c = "#e0e0e0";
            if pnl > 0:
                pnl_c = sc["RUNNING"]
            elif pnl < 0:
                pnl_c = sc["STOPPED"]
            self.sim_pnl_label.setText(f"<b>${pnl:,.2f} ({pnl_p:.2f}%)</b>");
            self.sim_pnl_label.setStyleSheet(f"color: {pnl_c}; font-size: 14px;")
            self.all_trades_data = sim_data.get("all_trades", [])
        else:
            self.sim_status_label.setText("ВЫКЛЮЧЕН");
            self.sim_status_label.setStyleSheet("color: #95a5a6; font-weight: bold;")
            self.sim_balance_label.setText("N/A");
            self.sim_pnl_label.setText("N/A");
            self.sim_pnl_label.setStyleSheet("font-size: 14px;");
            self.sim_positions_label.setText("N/A")
            self.all_trades_data = []

    def save_settings(self):
        try:
            ep = base64.b64encode(self.ssh_pass_input.text().encode('utf-8')).decode('utf-8')
            s = self.get_current_settings()
            s.update({"server_url": self.get_server_url(), "ssh_host": self.ssh_host_input.text(),
                      "ssh_user": self.ssh_user_input.text(), "ssh_pass_encoded": ep})
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(s, f, indent=2)
        except Exception as e:
            print(f"Ошибка сохранения настроек: {e}")

    def load_settings(self):
        try:
            if not os.path.exists(SETTINGS_FILE): return
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
            self.server_ip_input.setText(s.get("server_url", "http://YOUR_SERVER_HOST:5000"));
            self.ssh_host_input.setText(s.get("ssh_host", "YOUR_SERVER_HOST"));
            self.ssh_user_input.setText(s.get("ssh_user", "root"))
            ep = s.get("ssh_pass_encoded", "");
            self.ssh_pass_input.setText(base64.b64decode(ep.encode('utf-8')).decode('utf-8') if ep else "")
            for k, v in self.__dict__.items():
                if isinstance(v, QDoubleSpinBox) and k in s: v.setValue(s.get(k))
            self.sim_enabled_checkbox.setChecked(s.get("simulation_enabled", False));
            self.sim_start_balance_input.setValue(s.get("simulation_start_balance", 1000.0))
            self.private_key_input.setText(s.get("wallet_private_key", ""))
        except Exception as e:
            print(f"Ошибка загрузки настроек: {e}")

    def closeEvent(self, event):
        self.save_settings(); super().closeEvent(event)


if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = TradingBotGUI()
    window.show()
    sys.exit(app.exec_())