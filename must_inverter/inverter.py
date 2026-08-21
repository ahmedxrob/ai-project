import requests
import time
from flask import Flask, jsonify
import threading
import subprocess
import re

# ------------------ CONFIG ------------------
REFRESH_SECONDS = 60  # fetch every 60s

URL = "https://api.valueclouds.com/ppe/api/auth/app/queryDeviceOneDataxxx?devaddr=4&devcode=6422&pn=E50000220539172005&sn=DEV19837C24447510F"
HEADERS = {
    "Host": "api.valueclouds.com",
    "project": "IOT",
    "Sec-Fetch-Site": "cross-site",
    "auth": "eyJhbGciOiJIUzI1NiJ9.eyJqdGkiOiI1MTI1OTU3Iiwic3ViIjoiVmFsdWVDb3V...",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Mode": "cors",
    "token": "HK31ca9e78-8117-4388-af43-326a595bd03c",
    "Origin": "file://",
    "i18n": "en_US",
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
    "Sec-Fetch-Dest": "empty",
    "sign": "46ca7f42c553ccdaebcb9aab95f0fd5885409c9c20225442997ab9af1e003e7"
}
BOT_TOKEN = "8600167072:AAFU-zn-0Izg9nBDe6XqlM_GYA9ds0Dhosk"
CHAT_ID = "5809858782"
def send_telegram(message):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": message
            },
            timeout=10
        )
        print("Telegram:", r.text)

    except Exception as e:
        print(f"Telegram error: {e}")
# ------------------ HELPERS ------------------
def safe_val(val):
    try:
        return float(val)
    except:
        return 0.0

def build_usage_flow(batt_power, pv, grid, load):
    flow = ""
    if batt_power > 0 and pv == 0 and grid == 0:
        return "🔋 Battery → 🏠 Load"
    
    if pv > 0:
        if batt_power >= 0:
            flow = "☀️ PV + 🔋 Battery → 🏠 Load "
        else:
            # CHANGE IS HERE
            # If grid is importing (grid < 0), we assume PV is only charging battery
            # Otherwise, PV is charging battery AND powering load
            if grid < 0:
                flow = "☀️ PV → 🔋 Battery "
            else:
                flow = "☀️ PV → 🔋 Battery + 🏠 Load "
                
    if grid < 0:
        # This adds the Grid part and the separator "|"
        flow = flow + " | 🔌 → 🏠 Load" if flow else "🔌 → 🏠 Load"
        
    return flow or "-"

# ------------------ GLOBAL DATA ------------------
current_data = {}

# ------------------ FETCH DATA ------------------
def fetch_data():
    global current_data
    while True:
        try:
            r = requests.get(URL, headers=HEADERS, timeout=10)
            r.raise_for_status()
            js = r.json()
            data_list = js.get("data", [])

            # ✅ Get timestamp from API
            timestamp = "N/A"
            for item in data_list:
                if isinstance(item, dict) and "date" in item:
                    timestamp = item["date"]
                    break

            # Convert API data to dict
            data = {i.get("title"): i.get("val") for i in data_list if isinstance(i, dict)}

            # Fetch values safely
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

            # Update global data
            current_data = {
                "timestamp": timestamp,  # ← use API timestamp
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

    # Start fetching data in background
    threading.Thread(target=fetch_data, daemon=True).start()

    # Start Flask server
    app.run(host="0.0.0.0", port=5000)
