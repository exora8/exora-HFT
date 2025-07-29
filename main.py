import time
import requests
import hmac
import hashlib
import urllib.parse
from flask import Flask, jsonify, render_template_string, request
import random
import os
import json
import threading
from datetime import datetime, timezone

# --- KONFIGURASI AWAL ---
DEFAULT_SYMBOL = 'BRETT/USDT'
FETCH_INTERVAL = 0.5
TRIGGER_PERCENTAGE = 0.0015
TRADE_LOG_FILE = 'trades.json'
SETTINGS_FILE = 'settings.json'
MAX_LOG_HISTORY = 20

# --- KONSTANTA API ---
BYBIT_API_URL = "https://api.bybit.com"
BINGX_API_URL = "https://open-api.bingx.com"

# --- FUNGSI HELPER UNTUK API MANUAL ---
def generate_bingx_signature(secret_key, params_str):
    return hmac.new(secret_key.encode('utf-8'), params_str.encode('utf-8'), hashlib.sha256).hexdigest()

def get_bybit_symbols():
    try:
        url = f"{BYBIT_API_URL}/v5/market/tickers?category=linear"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()
        return {item['symbol'].replace("USDT", "/USDT") for item in data['result']['list'] if item['symbol'].endswith("USDT")}
    except Exception as e:
        print(f"Gagal mengambil simbol Bybit: {e}")
        return set()

def get_bingx_symbols():
    try:
        url = f"{BINGX_API_URL}/openApi/swap/v2/quote/contracts"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()
        return {item['symbol'].replace("-", "/") for item in data['data'] if item['symbol'].endswith("-USDT")}
    except Exception as e:
        print(f"Gagal mengambil simbol BingX: {e}")
        return set()

def get_bybit_latest_ohlc(symbol):
    bybit_symbol = symbol.replace('/', '')
    try:
        url = f"{BYBIT_API_URL}/v5/market/kline?category=linear&symbol={bybit_symbol}&interval=1&limit=1"
        response = requests.get(url, timeout=3)
        response.raise_for_status()
        data = response.json()
        if data.get('retCode') == 0 and data['result']['list']:
            kline = data['result']['list'][0]
            return float(kline[1]), float(kline[4])
        return None, None
    except Exception:
        return None, None

def verify_bingx_api(api_key, secret_key):
    endpoint = "/openApi/swap/v2/user/balance"
    params = {'timestamp': int(time.time() * 1000)}
    query_string = '&'.join([f"{k}={v}" for k, v in sorted(params.items())])
    signature = generate_bingx_signature(secret_key, query_string)
    params['signature'] = signature
    headers = {'X-BX-APIKEY': api_key}
    url = f"{BINGX_API_URL}{endpoint}"
    try:
        response = requests.get(url, headers=headers, params=params, timeout=5)
        if response.status_code == 200 and response.json().get('code') == 0:
            return "Berhasil terhubung ke BingX API."
        else:
            return f"Gagal terhubung: {response.status_code} - {response.text}"
    except Exception as e:
        return f"Gagal terhubung: Terjadi exception - {e}"

# --- FUNGSI BARU UNTUK MENGATUR LEVERAGE ---
def set_bingx_leverage(api_key, secret_key, symbol, leverage, side):
    """
    Mengatur leverage untuk simbol tertentu di BingX.
    Side bisa 'LONG' atau 'SHORT'.
    """
    endpoint = "/openApi/swap/v2/trade/leverage"
    url = f"{BINGX_API_URL}{endpoint}"
    
    params = {
        'symbol': symbol.replace("/", "-"),
        'leverage': leverage,
        'side': side.upper(), # Harus LONG atau SHORT
        'timestamp': int(time.time() * 1000)
    }
    
    query_string = '&'.join([f"{k}={v}" for k, v in sorted(params.items())])
    signature = generate_bingx_signature(secret_key, query_string)
    
    headers = {'X-BX-APIKEY': api_key}
    
    try:
        response = requests.post(url, headers=headers, params=params, timeout=5)
        response.raise_for_status()
        data = response.json()
        if data.get('code') == 0:
            msg = f"Berhasil mengatur leverage {leverage}x untuk {symbol} ({side})."
            print(msg)
            add_log_to_history(msg)
            return True
        else:
            msg = f"Gagal mengatur leverage: {data.get('msg', 'Error tidak diketahui')}"
            print(msg)
            add_log_to_history(msg)
            return False
    except Exception as e:
        msg = f"ERROR saat mengatur leverage: {e}"
        print(msg)
        add_log_to_history(msg)
        return False


def create_bingx_order(api_key, secret_key, symbol, side, order_type, quantity, tp_price=None, sl_price=None):
    endpoint = "/openApi/swap/v2/trade/order"
    url = f"{BINGX_API_URL}{endpoint}"
    
    params = {
        'symbol': symbol.replace("/", "-"),
        'side': 'BUY' if side.lower() == 'buy' else 'SELL',
        'positionSide': 'LONG' if side.lower() == 'buy' else 'SHORT',
        'type': order_type.upper(),
        'quantity': str(quantity),
        'timestamp': int(time.time() * 1000),
    }
    
    if tp_price and tp_price > 0:
        tp_object = {"type": "TAKE_PROFIT_MARKET", "stopPrice": round(tp_price, 5), "workingType": "MARK_PRICE"}
        params['takeProfit'] = json.dumps(tp_object)

    if sl_price and sl_price > 0:
        sl_object = {"type": "STOP_MARKET", "stopPrice": round(sl_price, 5), "workingType": "MARK_PRICE"}
        params['stopLoss'] = json.dumps(sl_object)

    sorted_params = sorted(params.items())
    query_string_to_sign = '&'.join([f"{k}={v}" for k, v in sorted_params])
    signature = generate_bingx_signature(secret_key, query_string_to_sign)
    final_payload_string = f"{query_string_to_sign}&signature={signature}"
    
    headers = {
        'X-BX-APIKEY': api_key,
        'Content-Type': 'application/x-www-form-urlencoded'
    }

    try:
        response = requests.post(url, headers=headers, data=final_payload_string, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data.get('code') == 0:
            print(f"SUKSES: Order REAL berhasil dibuat: {data}")
            return {'status': 'success', 'order_id': data['data']['order']['orderId'], 'data': data}
        else:
            print("--- DEBUGGING GAGAL ORDER ---")
            print(f"URL: {url}")
            print(f"Payload String yang Dikirim (body): {final_payload_string}")
            print(f"API Response: {json.dumps(data)}")
            print("-----------------------------")
            return {'status': 'error', 'message': json.dumps(data)}
            
    except requests.exceptions.RequestException as e:
        print(f"ERROR: Exception saat request API: {e}")
        if e.response:
            print(f"ERROR: Response Body: {e.response.text}")
        return {'status': 'error', 'message': str(e)}

# --- Sisa skrip dari sini TIDAK BERUBAH ---

# Inisialisasi Aplikasi Flask & Variabel Global
app = Flask(__name__)
app.config['TRADING_SETTINGS'] = {
    'api_key': '', 'secret_key': '', 'real_trading_enabled': False, 'demo_mode_enabled': True,
    'order_amount_usdt': 2, 'leverage': 10, 'tp_percent': 0.15, 'sl_percent': 0.15,
    'api_connection_status': 'Belum terhubung'
}
app.config['ACTIVE_TRADES'] = {}
app.config['TRADE_HISTORY_LOG'] = []
app.config['LIVE_DATA'] = {
    'symbol': DEFAULT_SYMBOL,
    'bybit_close': None,
    'hft_chance': 0,
}
trade_file_lock = threading.Lock()

def save_settings(settings_data):
    try:
        settings_to_save = settings_data.copy()
        if 'api_connection_status' in settings_to_save:
            del settings_to_save['api_connection_status']
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(settings_to_save, f, indent=4)
        print(f"Pengaturan berhasil disimpan ke {SETTINGS_FILE}")
    except Exception as e:
        print(f"ERROR: Gagal menyimpan pengaturan: {e}")

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r') as f:
                saved_settings = json.load(f)
            app.config['TRADING_SETTINGS'].update(saved_settings)
            app.config['TRADING_SETTINGS']['api_connection_status'] = 'Belum terhubung'
            app.config['TRADING_SETTINGS']['real_trading_enabled'] = False
            print(f"Berhasil memuat pengaturan dari {SETTINGS_FILE}")
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"Gagal memuat {SETTINGS_FILE} ({e}), menggunakan pengaturan default.")
    else:
        print(f"File {SETTINGS_FILE} tidak ditemukan, menggunakan pengaturan default.")

# Inisialisasi Daftar Simbol
try:
    print("Memuat daftar market dari Bybit & BingX...")
    bybit_symbols = get_bybit_symbols()
    bingx_symbols = get_bingx_symbols()
    AVAILABLE_SYMBOLS = sorted(list(bybit_symbols.intersection(bingx_symbols)))
    if not AVAILABLE_SYMBOLS: raise Exception("Tidak ada simbol yang sama ditemukan.")
    print(f"Berhasil memuat {len(AVAILABLE_SYMBOLS)} market yang sama.")
except Exception as e:
    print(f"Gagal memuat market: {e}")
    AVAILABLE_SYMBOLS = [DEFAULT_SYMBOL]

# TEMPLATE HTML (TIDAK BERUBAH)
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>HFT Bot Control | Bybit -> BingX</title>
    <script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
    <style>
        :root {
            --bg-color: #0d1117; --card-color: #161b22; --border-color: #30363d;
            --text-color: #c9d1d9; --text-secondary-color: #8b949e; --accent-color: #58a6ff;
            --green-color: #3fb950; --red-color: #f85149; --yellow-color: #d29922;
        }
        html, body { height: 100%; margin: 0; overflow-y: auto; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; background-color: var(--bg-color); color: var(--text-color); display: flex; flex-direction: column; align-items: center; padding: 15px; box-sizing: border-box; }
        .header { flex-shrink: 0; margin-bottom: 15px; text-align: center; }
        #symbol-selector { background-color: var(--card-color); color: var(--text-color); border: 1px solid var(--border-color); border-radius: 6px; padding: 8px 12px; font-size: 1em; cursor: pointer; }
        .tradingview-widget-container { height: 50vh; width: 100%; max-width: 1200px; border-radius: 8px; overflow: hidden; }
        .info-bar { flex-shrink: 0; display: flex; flex-wrap: wrap; gap: 15px; width: 100%; max-width: 1200px; margin-top: 15px; }
        .card { flex: 1 1 300px; background-color: var(--card-color); padding: 20px; border-radius: 8px; border: 1px solid var(--border-color); text-align: center; display: flex; flex-direction: column; }
        h2 { color: var(--accent-color); border-bottom: 1px solid var(--border-color); padding-bottom: 10px; margin-top: 0; font-size: 1.2em; }
        .price { font-size: 2em; font-weight: 600; margin: 5px 0; color: #fff; }
        .hft-chance { font-size: 1.8em; font-weight: bold; }
        .progress-bar-container { width: 80%; margin: 5px auto; background-color: #0d1117; border: 1px solid var(--border-color); border-radius: 5px; height: 25px; }
        .progress-bar { width: 0%; height: 100%; background-color: #238636; border-radius: 4px; transition: width 0.3s ease-in-out; }
        .settings-grid { display: grid; grid-template-columns: 1fr auto; gap: 10px 20px; text-align: left; align-items: center; }
        .control-input, .api-input { width: 100%; background-color: #0d1117; color: var(--text-color); border: 1px solid var(--border-color); border-radius: 4px; padding: 5px 8px; box-sizing: border-box; }
        .full-width { grid-column: 1 / -1; }
        .button { background-color: var(--accent-color); color: var(--bg-color); border: none; padding: 10px 15px; border-radius: 6px; cursor: pointer; font-weight: bold; width: 100%; margin-top: 10px; }
        .status-box { margin-top: 10px; padding: 8px; border-radius: 4px; font-size: 0.9em; white-space: pre-wrap; word-break: break-all; }
        .status-connected { background-color: rgba(63, 185, 80, 0.2); color: var(--green-color); }
        .status-disconnected { background-color: rgba(248, 81, 73, 0.2); color: var(--red-color); }
        .log-box { font-family: monospace; font-size: 0.85em; margin-top: 10px; height: 100px; text-align: left; overflow-y: auto; background: #0d1117; padding: 8px; border-radius: 4px; border: 1px solid var(--border-color); }
        .log-entry { margin: 0 0 5px 0; padding: 3px; border-radius: 3px; white-space: pre-wrap; word-break: break-all; }
        .log-new { color: var(--accent-color); }
        .log-tp { background-color: rgba(63, 185, 80, 0.15); color: var(--green-color); }
        .log-sl { background-color: rgba(248, 81, 73, 0.15); color: var(--red-color); }
        .log-error { color: var(--yellow-color); }
        .switch { position: relative; display: inline-block; width: 50px; height: 24px; }
        .switch input { opacity: 0; width: 0; height: 0; }
        .slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background-color: #333; transition: .4s; border-radius: 24px;}
        .slider:before { position: absolute; content: ""; height: 18px; width: 18px; left: 3px; bottom: 3px; background-color: white; transition: .4s; border-radius: 50%;}
        input:checked + .slider { background-color: var(--green-color); } input:checked + .slider:before { transform: translateX(26px); }
    </style>
</head>
<body>
    <div class="header"> <select id="symbol-selector"> {% for symbol in symbols %} <option value="{{ symbol }}" {% if symbol == default_symbol %}selected{% endif %}>{{ symbol }}</option> {% endfor %} </select> </div>
    <div id="tradingview-container" class="tradingview-widget-container"></div>
    <div class="info-bar">
        <div class="card">
            <h2>BYBIT DATA</h2>
            <div id="bybit-price" class="price">-</div>
            <div id="hft-chance" class="hft-chance" style="margin-top: 15px;">0%</div>
            <div class="progress-bar-container"><div id="hft-progress-bar" class="progress-bar"></div></div>
        </div>
        <div class="card">
            <h2>PENGATURAN & KONTROL</h2>
            <div class="settings-grid">
                <label class="control-label">BingX API Key</label> <input id="api-key" type="text" class="api-input full-width">
                <label class="control-label">BingX Secret Key</label> <input id="api-secret" type="password" class="api-input full-width">
                <label class="control-label">Leverage</label> <input type="number" id="leverage-input" class="control-input" min="1" max="100">
                <label class="control-label">Order (USDT)</label> <input type="number" id="amount-input" class="control-input" min="1">
                <label class="control-label">Take Profit (%)</label> <input type="number" id="tp-input" class="control-input" step="0.1">
                <label class="control-label">Stop Loss (%)</label> <input type="number" id="sl-input" class="control-input" step="0.1">
                <div class="full-width"> <button id="save-settings-btn" class="button">Simpan & Hubungkan Ulang</button> </div>
            </div>
            <div id="api-status-box" class="status-box">Menunggu pengaturan...</div>
        </div>
        <div class="card">
            <h2>MODE & LOG</h2>
            <div class="settings-grid" style="margin-bottom: 15px;">
                <span class="control-label">Mode Demo</span> <label class="switch"><input type="checkbox" id="enable-demo-toggle"><span class="slider"></span></label>
                <span class="control-label">Mode Real</span> <label class="switch"><input type="checkbox" id="enable-real-toggle"><span class="slider"></span></label>
            </div>
            <h3 id="trading-status-text" style="margin: 0; font-weight: bold;">-</h3>
            <div class="log-box" id="trade-history-log"></div>
        </div>
    </div>
    <script>
        const symbolSelector = document.getElementById('symbol-selector');
        const apiKeyInput = document.getElementById('api-key'), apiSecretInput = document.getElementById('api-secret');
        const leverageInput = document.getElementById('leverage-input'), amountInput = document.getElementById('amount-input');
        const tpInput = document.getElementById('tp-input'), slInput = document.getElementById('sl-input');
        const saveBtn = document.getElementById('save-settings-btn'), apiStatusBox = document.getElementById('api-status-box');
        const demoToggle = document.getElementById('enable-demo-toggle'), realToggle = document.getElementById('enable-real-toggle');
        const tradingStatusText = document.getElementById('trading-status-text'), logBox = document.getElementById('trade-history-log');
        let tradingViewWidget = null;
        function loadTradingViewWidget(symbol) {
            const tradingViewSymbol = 'BINGX:' + symbol.replace('/', '');
            if (tradingViewWidget) { tradingViewWidget.setSymbol(tradingViewSymbol, "1", () => {}); } 
            else { tradingViewWidget = new TradingView.widget({ "container_id": "tradingview-container", "autosize": true, "symbol": tradingViewSymbol, "interval": "1", "timezone": "Asia/Jakarta", "theme": "dark", "style": "1", "locale": "en", "toolbar_bg": "#f1f3f6", "enable_publishing": false, "hide_top_toolbar": true, "allow_symbol_change": false, "save_image": false }); }
        }
        async function fetchSettings() {
            try {
                const response = await fetch('/get_settings');
                const settings = await response.json();
                apiKeyInput.value = settings.api_key;
                leverageInput.value = settings.leverage;
                amountInput.value = settings.order_amount_usdt;
                tpInput.value = settings.tp_percent;
                slInput.value = settings.sl_percent;
                demoToggle.checked = settings.demo_mode_enabled;
                realToggle.checked = settings.real_trading_enabled;
                updateApiStatus(settings.api_connection_status);
                updateTradingStatus(settings);
            } catch (error) { console.error("Error fetching settings:", error); }
        }
        async function saveSettings() {
            saveBtn.disabled = true; saveBtn.textContent = "Menyimpan...";
            const settingsData = {
                api_key: apiKeyInput.value, secret_key: apiSecretInput.value, leverage: parseInt(leverageInput.value),
                amount: parseFloat(amountInput.value), tp: parseFloat(tpInput.value), sl: parseFloat(slInput.value)
            };
            try {
                const response = await fetch('/update_settings', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(settingsData) });
                const result = await response.json();
                updateApiStatus(result.api_status);
                if (result.api_status.includes("Gagal")) { realToggle.checked = false; toggleMode('real', false); }
            } catch (error) { console.error("Error saving settings:", error); } 
            finally { saveBtn.disabled = false; saveBtn.textContent = "Simpan & Hubungkan Ulang"; }
        }
        async function toggleMode(mode, isEnabled) {
            try {
                const response = await fetch('/toggle_mode', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ mode: mode, enabled: isEnabled }) });
                const settings = await response.json();
                demoToggle.checked = settings.demo_mode_enabled;
                realToggle.checked = settings.real_trading_enabled;
                updateTradingStatus(settings);
            } catch (error) { console.error(`Error toggling ${mode}:`, error); }
        }
        function updateApiStatus(status) { apiStatusBox.textContent = status; apiStatusBox.className = status.includes("Berhasil") ? 'status-box status-connected' : 'status-box status-disconnected'; }
        function updateTradingStatus(settings) {
            if (settings.real_trading_enabled) { tradingStatusText.textContent = "MODE REAL AKTIF"; tradingStatusText.style.color = "var(--red-color)"; }
            else if (settings.demo_mode_enabled) { tradingStatusText.textContent = "MODE DEMO AKTIF"; tradingStatusText.style.color = "var(--green-color)"; }
            else { tradingStatusText.textContent = "SEMUA MODE NONAKTIF"; tradingStatusText.style.color = "var(--yellow-color)"; }
        }
        function updateLogBox(logHistory) {
            logBox.innerHTML = '';
            logHistory.forEach(logMsg => {
                const p = document.createElement('p');
                p.textContent = logMsg;
                p.className = 'log-entry';
                if (logMsg.includes('[TP HIT]')) p.classList.add('log-tp');
                else if (logMsg.includes('[SL HIT]')) p.classList.add('log-sl');
                else if (logMsg.includes('[NEW]')) p.classList.add('log-new');
                else if (logMsg.includes('ERROR') || logMsg.includes('Gagal')) p.classList.add('log-error');
                logBox.appendChild(p);
            });
        }
        async function fetchData() {
            try {
                const response = await fetch(`/data`);
                const data = await response.json();
                document.getElementById('bybit-price').textContent = data.bybit_close ? `$${data.bybit_close.toFixed(6)}` : '-';
                const hftChance = data.hft_chance || 0;
                document.getElementById('hft-chance').textContent = `${hftChance.toFixed(1)}%`;
                const progressBar = document.getElementById('hft-progress-bar');
                progressBar.style.width = `${hftChance}%`;
                if (hftChance > 85) progressBar.style.backgroundColor = 'var(--red-color)';
                else if (hftChance > 60) progressBar.style.backgroundColor = 'var(--yellow-color)';
                else progressBar.style.backgroundColor = 'var(--green-color)';
                if(data.trade_history) updateLogBox(data.trade_history);
                if (symbolSelector.value !== data.symbol) {
                    symbolSelector.value = data.symbol;
                }
            } catch (error) { console.error("Error fetching data:", error); }
        }
        symbolSelector.addEventListener('change', () => { 
            const newSymbol = symbolSelector.value;
            loadTradingViewWidget(newSymbol); 
            fetch('/update_symbol', { 
                method: 'POST', headers: {'Content-Type': 'application/json'}, 
                body: JSON.stringify({ symbol: newSymbol }) 
            });
        });
        saveBtn.addEventListener('click', saveSettings);
        demoToggle.addEventListener('change', (e) => toggleMode('demo', e.target.checked));
        realToggle.addEventListener('change', (e) => toggleMode('real', e.target.checked));
        document.addEventListener('DOMContentLoaded', () => { loadTradingViewWidget(symbolSelector.value); fetchSettings(); fetchData(); setInterval(fetchData, {{ interval * 1000 }}); });
    </script>
</body>
</html>
"""

def add_log_to_history(message):
    history = app.config['TRADE_HISTORY_LOG']
    now = datetime.now().strftime('%H:%M:%S')
    history.insert(0, f"[{now}] {message}")
    app.config['TRADE_HISTORY_LOG'] = history[:MAX_LOG_HISTORY]

def read_json_file(filepath):
    with trade_file_lock:
        if not os.path.exists(filepath): return []
        try:
            with open(filepath, 'r') as f: return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError): return []

def write_json_file(filepath, data):
    with trade_file_lock:
        with open(filepath, 'w') as f: json.dump(data, f, indent=4)

def load_initial_state():
    all_trades = read_json_file(TRADE_LOG_FILE)
    active_trades = {}
    log_history = []
    for trade in all_trades:
        status = trade.get('status', 'UNKNOWN')
        trade_id = trade.get('id', 'N/A')
        if status == "ACTIVE":
            active_trades[str(trade_id)] = trade
            log_history.append(f"ACTIVE: {trade['symbol']} {trade['side']} from {trade.get('entry_price')}")
    app.config['ACTIVE_TRADES'] = active_trades
    app.config['TRADE_HISTORY_LOG'] = log_history[:MAX_LOG_HISTORY]
    print(f"Startup: Ditemukan {len(active_trades)} trade aktif untuk dipantau.")

def update_trade_in_json(trade_id, new_status, closing_price):
    all_trades = read_json_file(TRADE_LOG_FILE)
    for trade in all_trades:
        if str(trade.get('id')) == str(trade_id):
            trade['status'] = new_status
            trade['closing_price'] = closing_price
            trade['closed_at'] = datetime.now(timezone.utc).isoformat()
            break
    write_json_file(TRADE_LOG_FILE, all_trades)

def check_active_trades(symbol, current_price):
    if not current_price: return
    active_trades = app.config['ACTIVE_TRADES']
    closed_trades = []
    for trade_id, trade in list(active_trades.items()):
        if trade['symbol'] != symbol: continue
        
        tp_hit, sl_hit = False, False
        
        if trade['side'].lower() == 'buy':
            if current_price >= trade['tp_price']: tp_hit = True
            elif current_price <= trade['sl_price']: sl_hit = True
        elif trade['side'].lower() == 'sell':
            if current_price <= trade['tp_price']: tp_hit = True
            elif current_price >= trade['sl_price']: sl_hit = True
            
        if tp_hit or sl_hit:
            status = "CLOSED_TP" if tp_hit else "CLOSED_SL"
            log_msg = f"[{'TP HIT' if tp_hit else 'SL HIT'}] {trade['symbol']} {trade['side']} closed at ${current_price:.5f}"
            print(log_msg)
            add_log_to_history(log_msg)
            update_trade_in_json(trade_id, status, current_price)
            closed_trades.append(trade_id)

    for trade_id in closed_trades:
        if str(trade_id) in app.config['ACTIVE_TRADES']:
            del app.config['ACTIVE_TRADES'][str(trade_id)]

def process_trade_trigger(symbol, side, price):
    settings = app.config['TRADING_SETTINGS']
    mode = "DEMO" if settings['demo_mode_enabled'] else "REAL"
    
    tp_price = price * (1 + settings['tp_percent'] / 100) if side == 'buy' else price * (1 - settings['tp_percent'] / 100)
    sl_price = price * (1 - settings['sl_percent'] / 100) if side == 'buy' else price * (1 + settings['sl_percent'] / 100)
    
    trade_id = f"{mode}-{int(time.time()*1000)}"
    
    trade_record = {
        "id": trade_id, "timestamp": datetime.now(timezone.utc).isoformat(), "symbol": symbol, "side": side,
        "amount_usdt": settings['order_amount_usdt'], "entry_price": price, "leverage": settings['leverage'],
        "tp_price": tp_price, "sl_price": sl_price, "status": "ACTIVE", "mode": mode,
        "closing_price": None, "closed_at": None
    }
    
    if mode == "REAL":
        if "Berhasil" not in settings['api_connection_status']:
            add_log_to_history("Gagal: REAL Mode, API tidak terhubung."); return
        
        # <<< TIDAK ADA PERUBAHAN DI SINI, HANYA MEMBACA NILAI YANG SUDAH BENAR >>>
        # Perhitungan quantity sudah benar, menggunakan nilai order_amount_usdt dari setting
        quantity = (settings['order_amount_usdt'] * settings['leverage']) / price
        
        order_result = create_bingx_order(
            settings['api_key'], settings['secret_key'], symbol, side, 'market',
            quantity, tp_price=tp_price, sl_price=sl_price
        )
        
        if order_result['status'] == 'success':
            trade_record['id'] = order_result['order_id']
        else:
            log_msg = f"ERROR: Gagal eksekusi REAL order: {order_result.get('message', 'Unknown error')}"
            add_log_to_history(log_msg)
            return

    all_trades = read_json_file(TRADE_LOG_FILE)
    all_trades.append(trade_record)
    write_json_file(TRADE_LOG_FILE, all_trades)
    
    app.config['ACTIVE_TRADES'][str(trade_record['id'])] = trade_record
    log_msg = f"[NEW] [{mode}] {side.upper()} {symbol} @ ${price:.5f} | TP: {tp_price:.5f} SL: {sl_price:.5f}"
    print(log_msg)
    add_log_to_history(log_msg)

def background_trading_loop():
    print("Background trading loop telah dimulai...")
    while True:
        try:
            symbol = app.config['LIVE_DATA']['symbol']
            settings = app.config['TRADING_SETTINGS']
            _, bybit_close = get_bybit_latest_ohlc(symbol)
            
            if bybit_close is None:
                time.sleep(FETCH_INTERVAL)
                continue

            app.config['LIVE_DATA']['bybit_close'] = bybit_close
            check_active_trades(symbol, bybit_close)
            
            state = app.config.setdefault('STATE', {}).setdefault(symbol, {"last_bybit_close": None})
            
            hft_chance = 0
            if state.get('last_bybit_close'):
                last_close = state['last_bybit_close']
                change_pct = (bybit_close - last_close) / last_close
                hft_chance = min(abs(change_pct / TRIGGER_PERCENTAGE) * 100, 100)
                
                alert_direction = 'none'
                if change_pct > TRIGGER_PERCENTAGE: alert_direction = "up"
                elif change_pct < -TRIGGER_PERCENTAGE: alert_direction = "down"
                
                if alert_direction != 'none' and (settings['real_trading_enabled'] or settings['demo_mode_enabled']):
                    is_trade_active_for_symbol = any(t['symbol'] == symbol for t in app.config['ACTIVE_TRADES'].values())
                    if not is_trade_active_for_symbol:
                        threading.Thread(target=process_trade_trigger, args=(symbol, 'buy' if alert_direction == 'up' else 'sell', bybit_close)).start()
                        state['last_bybit_close'] = None
            
            if state.get('last_bybit_close') is not None:
                state['last_bybit_close'] = bybit_close
            elif not any(t['symbol'] == symbol for t in app.config['ACTIVE_TRADES'].values()):
                state['last_bybit_close'] = bybit_close

            app.config['LIVE_DATA']['hft_chance'] = hft_chance
            
        except Exception as e:
            print(f"Error di dalam background_trading_loop: {e}")
            
        time.sleep(FETCH_INTERVAL)


@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE, interval=FETCH_INTERVAL, symbols=AVAILABLE_SYMBOLS, default_symbol=app.config['LIVE_DATA']['symbol'])

@app.route('/get_settings')
def get_settings():
    return jsonify(app.config['TRADING_SETTINGS'])

# --- ROUTE INI YANG DIPERBAIKI SECARA SIGNIFIKAN ---
@app.route('/update_settings', methods=['POST'])
def update_settings():
    data = request.get_json()
    settings = app.config['TRADING_SETTINGS']
    
    leverage_changed = False
    
    # Update nilai dari web ke memori (app.config)
    settings['api_key'] = data.get('api_key', settings['api_key']).strip()
    settings['secret_key'] = data.get('secret_key', settings['secret_key']).strip()
    if 'leverage' in data and settings['leverage'] != int(data.get('leverage')):
        settings['leverage'] = int(data.get('leverage'))
        leverage_changed = True
    if 'amount' in data: settings['order_amount_usdt'] = float(data.get('amount'))
    if 'tp' in data: settings['tp_percent'] = float(data.get('tp'))
    if 'sl' in data: settings['sl_percent'] = float(data.get('sl'))
    
    # Verifikasi koneksi API
    status_msg = verify_bingx_api(settings['api_key'], settings['secret_key'])
    settings['api_connection_status'] = status_msg
    if "Gagal" in status_msg: 
        settings['real_trading_enabled'] = False
        add_log_to_history(f"API Status: {status_msg}")
    else:
        # <-- PERUBAHAN UTAMA DI SINI -->
        # Jika koneksi berhasil DAN leverage berubah, kirim perintah ke BingX
        if leverage_changed:
            current_symbol = app.config['LIVE_DATA']['symbol']
            add_log_to_history(f"Mencoba mengatur leverage ke {settings['leverage']}x untuk {current_symbol}...")
            # Leverage harus diatur untuk LONG dan SHORT
            set_bingx_leverage(settings['api_key'], settings['secret_key'], current_symbol, settings['leverage'], 'LONG')
            set_bingx_leverage(settings['api_key'], settings['secret_key'], current_symbol, settings['leverage'], 'SHORT')

    # Simpan semua pengaturan ke file settings.json
    save_settings(settings)
    
    return jsonify({'status': 'success', 'api_status': settings['api_connection_status']})

@app.route('/update_symbol', methods=['POST'])
def update_symbol():
    data = request.get_json()
    if 'symbol' in data:
        new_symbol = data.get('symbol')
        app.config['LIVE_DATA']['symbol'] = new_symbol
        app.config.setdefault('STATE', {})[new_symbol] = {"last_bybit_close": None}
        
        # <-- TAMBAHAN: Atur leverage untuk simbol baru secara otomatis -->
        settings = app.config['TRADING_SETTINGS']
        if "Berhasil" in settings['api_connection_status']:
             add_log_to_history(f"Simbol diubah ke {new_symbol}. Mengatur leverage ke {settings['leverage']}x...")
             set_bingx_leverage(settings['api_key'], settings['secret_key'], new_symbol, settings['leverage'], 'LONG')
             set_bingx_leverage(settings['api_key'], settings['secret_key'], new_symbol, settings['leverage'], 'SHORT')
        else:
            print(f"Simbol diubah ke {new_symbol}. API tidak terhubung, leverage tidak diatur.")

        return jsonify({'status': 'success', 'symbol': new_symbol})
    return jsonify({'status': 'error', 'message': 'Symbol not provided'}), 400

@app.route('/toggle_mode', methods=['POST'])
def toggle_mode():
    settings = app.config['TRADING_SETTINGS']
    data = request.get_json()
    mode, is_enabled = data.get('mode'), data.get('enabled')

    if mode == 'real':
        if is_enabled:
            if "Berhasil" not in settings['api_connection_status']:
                add_log_to_history("Gagal: Mode REAL butuh koneksi API.");
                settings['real_trading_enabled'] = False
            else:
                settings.update({'real_trading_enabled': True, 'demo_mode_enabled': False})
                add_log_to_history("Mode REAL diaktifkan.")
        else:
            settings['real_trading_enabled'] = False
            add_log_to_history("Mode REAL dinonaktifkan.")
    elif mode == 'demo':
        settings['demo_mode_enabled'] = is_enabled
        if is_enabled:
            settings['real_trading_enabled'] = False
            add_log_to_history("Mode DEMO diaktifkan.")
        else:
            add_log_to_history("Mode DEMO dinonaktifkan.")
            
    save_settings(settings)
    return jsonify(settings)

@app.route('/data')
def data():
    response_data = app.config['LIVE_DATA'].copy()
    response_data['trade_history'] = app.config['TRADE_HISTORY_LOG']
    return jsonify(response_data)

if __name__ == '__main__':
    load_settings()
    load_initial_state()
    trade_loop_thread = threading.Thread(target=background_trading_loop, daemon=True)
    trade_loop_thread.start()
    print(f"Server berjalan di http://127.0.0.1:5000")
    print("Logika trading berjalan di background. Anda bisa menutup browser.")
    app.run(host='0.0.0.0', port=5000, debug=False)
