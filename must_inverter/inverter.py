import requests
import time
import json
from flask import Flask, jsonify
import threading

# ------------------ CONFIG ------------------
REFRESH_SECONDS = 60  # fetch every 60s

# Load configuration from Home Assistant
try:
    with open('/data/options.json') as f:
        options = json.load(f)
except Exception:
    options = {}

URL = options.get('valueclouds_url', '')
BOT_TOKEN = options.get('telegram_bot_token', '')
CHAT_ID = options.get('telegram_chat_id', '')

HEADERS = {
    "Host": "api.valueclouds.com",
    "project": "IOT",
    "Sec-Fetch-Site": "cross-site",
    "auth": options.get('valueclouds_auth', ''),
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Mode": "cors",
    "token": options.get('valueclouds_token', ''),
    "Origin": "file://",
    "i18n": "en_US",
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
    "Sec-Fetch-Dest": "empty"
}

def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram credentials missing in config. Skipping notification.")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": message
            },
            timeout=10
        )
        print("Telegram response:", r.text)
    except Exception as e:
        print(f"Telegram error: {e}")

# ------------------ HELPERS ------------------
def safe_val(val):
    try:
        return float(val)
    except Exception:
        return 0.0

def build_usage_flow(batt_power, pv, grid, load):
    flow = ""
    if batt_power > 0 and pv == 0 and grid == 0:
        return "🔋 Battery → 🏠 Load"
    
    if pv > 0:
        if batt_power >= 0:
            flow = "☀️ PV + 🔋 Battery → 🏠 Load "
        else:
            if grid < 0:
                flow = "☀️ PV → 🔋 Battery "
            else:
                flow = "☀️ PV → 🔋 Battery + 🏠 Load "
                
    if grid < 0:
        flow = flow + " | 🔌 → 🏠 Load" if flow else "🔌 → 🏠 Load"
        
    return flow or "-"

# ------------------ GLOBAL DATA ------------------
current_data = {}

# ------------------ FETCH DATA ------------------
def fetch_data():
    global current_data
    while True:
        if not URL:
            print("⚠️ ValueClouds URL not set in Home Assistant configuration.")
        else:
            try:
                r = requests.get(URL, headers=HEADERS, timeout=10)
                r.raise_for_status()
                js = r.json()
                data_list = js.get("data", [])

                timestamp = "N/A"
                for item in data_list:
                    if isinstance(item, dict) and "date" in item:
                        timestamp = item["date"]
                        break

                data = {i.get("title"): i.get("val") for i in data_list if isinstance(i, dict)}

                batt_power = safe_val(data.get("batt power"))
                batt_voltage = safe_val(data.get("Battery Voltage"))
                batt_current = safe_val(data.get("Batt Current"))
                pv = safe_val(data.get("PV Total Charger Power"))
                load = safe_val(data.get("PLoad"))
                grid = safe_val(data.get("PGrid"))
                pinv_api = safe_val(data.get("PInverter"))
                pinverter = 0 if pinv_api == 0 else load - pinv_api

                acc_discharge = safe_val(data.get("accumulated discharger power"))
                acc_buy = safe_val(data.get("accumulated buy power"))
                acc_load = safe_val(data.get("Accumulated Load Power"))
                acc_self = safe_val(data.get("Accumulated Self_Use Power"))
                acc_pv = safe_val(data.get("PV Cumulative Power Generation"))

                current_data = {
                    "timestamp": timestamp,
                    "battery_voltage": round(batt_voltage, 1),
                    "battery_current": round(batt_current, 1),
                    "battery_power": round(batt_power, 0),
                    "pv_power": round(pv, 0),
                    "grid_power": round(grid, 0),
                    "load_power": round(load, 0),
                    "inverter_power": round(pinverter, 0),
                    "acc_pv": round(acc_pv, 0),
                    "acc_load": round(acc_load, 0),
                    "acc_discharge": round(acc_discharge, 0),
                    "acc_buy": round(acc_buy, 0),
                    "acc_self": round(acc_self, 0),
                    "usage_flow": build_usage_flow(batt_power, pv, grid, load)
                }

                print(f"[{timestamp}] Data updated ✔")
            except Exception as e:
                print(f"⚠️ ERROR fetching data: {e}")

        time.sleep(REFRESH_SECONDS)

# ------------------ FLASK ------------------
app = Flask(__name__)

@app.route("/data")
def get_data():
    return jsonify(current_data)

# ------------------ MAIN ------------------
if __name__ == "__main__":
    send_telegram("✅ MUST Inverter add-on started")
    threading.Thread(target=fetch_data, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)
