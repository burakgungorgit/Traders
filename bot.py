# bot.py

import os
import time
import math
import json
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv
from binance.client import Client
from binance.enums import *
import requests

# --- Ortam değişkenleri (.env) ---
load_dotenv()
API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# --- Log tekrarını azaltmak için zaman takip sözlüğü ---
log_cooldowns = {}

# --- Telegram mesaj gönder (spam korumalı) ---
def send_telegram(msg, key=None, cooldown=180):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        if key:
            now = time.time()
            if key in log_cooldowns and now - log_cooldowns[key] < cooldown:
                return
            log_cooldowns[key] = now

        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
        requests.post(url, data=data, timeout=5)
    except Exception as e:
        print(f"Hata - Telegram gönderilemedi: {e}")

# --- Log yaz ---
def write_log(msg):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    full_msg = f"[{now}] {msg}"
    with open("log.txt", "a", encoding="utf-8") as f:
        f.write(full_msg + "\n")
    print(full_msg)
    send_telegram(full_msg, key=msg)  # spam kontrolü aktif

# --- Log tekrarını sınırlı yaz ---
def write_log_limited(msg, key, cooldown=1000):
    now = time.time()
    if key not in log_cooldowns or now - log_cooldowns[key] > cooldown:
        write_log(msg)
        log_cooldowns[key] = now

# --- Durum dosyası (pozisyon takibi) ---
STATE_FILE = "state.json"

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except:
        return {"in_position": False, "entry_price": 0.0}

def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        write_log(f"Durum kaydedilemedi: {e}")

# --- Binance zaman farkı ---
def get_time_offset_ms():
    try:
        server_time = requests.get("https://api.binance.com/api/v3/time", timeout=5).json()["serverTime"]
        local_time = int(time.time() * 1000)
        offset = local_time - server_time
        write_log(f"Zaman farkı: {offset} ms")
        return offset
    except Exception as e:
        write_log_limited(f"Hata - zaman farkı alınamadı: {e}", key="time_offset")
        return 0

# --- Binance istemcisi ---
client = Client(API_KEY, API_SECRET)
client.time_offset = -get_time_offset_ms()

# --- Bot ayarları ---
SYMBOL = "BTCUSDT"
INTERVAL = Client.KLINE_INTERVAL_30MINUTE
COMMISSION = 0.001
MIN_USDT = 10

# --- Strateji parametreleri ---
EMA_SHORT = 150
EMA_LONG = 200
TAKE_PROFIT_MULT = 1.08
STOP_LOSS_MULT = 0.97
STOPLOSS_ADJUST_TRIGGER = 1.05
STOPLOSS_ADJUST_TO = 1.03

# --- Bakiye kontrolü ---
def get_balance(asset):
    try:
        balance = client.get_asset_balance(asset=asset)
        return float(balance['free']) if balance else 0.0
    except:
        return 0.0

# --- Cüzdanı yazdır ---
def print_balances():
    try:
        account = client.get_account()
        balances = account["balances"]
        print("Cüzdan:")
        for b in balances:
            if float(b["free"]) > 0:
                print(f"{b['asset']}: {b['free']}")
    except:
        pass

# --- Kline verisi çek ---
def get_klines(symbol, interval, limit=999):
    klines = client.get_klines(symbol=symbol, interval=interval, limit=limit)
    df = pd.DataFrame(klines, columns=[
        'timestamp', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'qav', 'trades', 'tbbav', 'tbqav', 'ignore'
    ])
    df['close'] = df['close'].astype(float)
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    return df[['timestamp', 'close']]

# --- EMA hesapla ---
def calculate_ema(df, period):
    return df['close'].ewm(span=period, adjust=False).mean()

# --- Emir miktarını yuvarla ---
def round_quantity(symbol, qty):
    info = client.get_symbol_info(symbol)
    for f in info['filters']:
        if f['filterType'] == 'LOT_SIZE':
            step = float(f['stepSize'])
            precision = int(round(-math.log(step, 10), 0))
            return round(qty, precision)
    return qty

# --- Minimum notional kontrolü ---
def check_min_notional(symbol, qty, price):
    info = client.get_symbol_info(symbol)
    for f in info['filters']:
        if f['filterType'] == 'MIN_NOTIONAL':
            return qty * price >= float(f['minNotional'])
    return qty * price >= 10

# --- Emir gönder ---
def place_order(symbol, side, qty, price):
    try:
        if not check_min_notional(symbol, qty, price):
            write_log("Emir reddedildi: minimum notional değerin altında.")
            return None
        return client.create_order(
            symbol=symbol,
            side=side,
            type=ORDER_TYPE_MARKET,
            quantity=qty
        )
    except Exception as e:
        write_log_limited(f"Hata - emir gönderilemedi: {e}", key="order_error")
        return None

# --- Ortalama gerçekleşen fiyat ---
def get_avg_fill_price(order):
    fills = order.get("fills", [])
    if fills:
        total = sum(float(f["price"]) * float(f["qty"]) for f in fills)
        qty = sum(float(f["qty"]) for f in fills)
        return total / qty
    return None

# --- Fiyat hesaplamaları ---
def buy_price(p): return p * (1 + COMMISSION)
def sell_price(p): return p * (1 - COMMISSION)
def calc_pnl(entry, current): return sell_price(current) - buy_price(entry)


# --- Ana döngü ---
def main():
    write_log("BTCUSDT Bot başlatıldı.")
    state = load_state()
    in_position = state["in_position"]
    entry_price = state["entry_price"]
    write_log(f"Başlangıç durumu: in_position={in_position}, entry_price={entry_price}")
    awaiting_confirmation = False
    signal_time = None
    adjusted_stop = False

    while True:
        try:
            df = get_klines(SYMBOL, INTERVAL)
            if len(df) < EMA_LONG + 2:
                time.sleep(60)
                continue

            df["ema_short"] = calculate_ema(df, EMA_SHORT)
            df["ema_long"] = calculate_ema(df, EMA_LONG)
            prev, last = df.iloc[-2], df.iloc[-1]

            if not in_position and not awaiting_confirmation:
                if prev["ema_short"] < prev["ema_long"] and last["ema_short"] > last["ema_long"]:
                    write_log("Sinyal oluştu. Mum kapanışı bekleniyor.")
                    signal_time = str(last["timestamp"])
                    awaiting_confirmation = True

            elif awaiting_confirmation:
                if str(last["timestamp"]) != signal_time:
                    if last["ema_short"] > last["ema_long"]:
                        usdt = get_balance("USDT")
                        price = float(client.get_symbol_ticker(symbol=SYMBOL)["price"])
                        qty = round_quantity(SYMBOL, usdt * 0.99 / price)
                        if usdt >= MIN_USDT and qty > 0:
                            order = place_order(SYMBOL, SIDE_BUY, qty, price)
                            if order:
                                entry_price = get_avg_fill_price(order)
                                in_position = True
                                save_state({"in_position": True, "entry_price": entry_price})
                                write_log(f"✅ Alım yapıldı: {qty} BTC @ {entry_price}")
                                send_telegram(f"✅ ALIM: {qty} BTC @ {entry_price}")
                        else:
                            write_log("Yetersiz bakiye.")
                    else:
                        write_log("Sinyal geçersizleşti.")
                    awaiting_confirmation = False

            elif in_position:
                current = float(client.get_symbol_ticker(symbol=SYMBOL)["price"])
                target = entry_price * TAKE_PROFIT_MULT
                stop = entry_price * STOP_LOSS_MULT

                # Stop-loss ayarlaması (fiyat +%5’e ulaşırsa SL = entry*1.03)
                if current >= entry_price * STOPLOSS_ADJUST_TRIGGER and not adjusted_stop:
                    adjusted_stop = True
                    stop = entry_price * STOPLOSS_ADJUST_TO
                    write_log(f"🔒 Fiyat %5 yükseldi. SL {stop:.4f} seviyesine çekildi.")
                    send_telegram(f"🔒 STOPLOSS GÜNCELLENDİ: Yeni SL = {stop:.4f}")

                if current >= target or current <= stop:
                    total_qty = get_balance("BTC")
                    sell_qty = round_quantity(SYMBOL, total_qty * 0.99)

                    if sell_qty > 0:
                        reason = "Kâr Alımı" if current >= target else "Stop-Loss"
                        write_log(f"Satış sinyali ({reason}): fiyat {current}, hedef {target}, stop {stop}")
                        order = place_order(SYMBOL, SIDE_SELL, sell_qty, current)
                        if order:
                            sell = get_avg_fill_price(order) or current
                            pnl = calc_pnl(entry_price, sell)
                            result = "Kâr" if sell >= entry_price else "Zarar"
                            write_log(f"{result}: {sell_qty} BTC satıldı @ {sell} | PnL: {round(pnl, 3)}")
                            send_telegram(f"🏁 {result}: {sell_qty} BTC satıldı @ {sell}")
                            in_position = False
                            entry_price = 0.0
                            adjusted_stop = False
                            save_state({"in_position": False, "entry_price": 0.0})
                        else:
                            write_log("Satış emri başarısız oldu.")
                    else:
                        write_log("Satış için yeterli BTC yok.")

        except Exception as e:
            write_log_limited(f"Hata - döngü: {e}", key="loop_error")
            write_log_limited("İnternet kopmuş olabilir. 60 saniye bekleniyor...", key="internet_wait")
            time.sleep(60)
            continue

        time.sleep(60)

# --- Başlat ---
if __name__ == "__main__":
    print_balances()
    main()
