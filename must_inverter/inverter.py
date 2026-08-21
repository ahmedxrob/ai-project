import requests
import time
import json
from flask import Flask, jsonify
import threading

# ------------------ CONFIG ------------------
REFRESH_SECONDS = 60

# Load configuration from Home Assistant
try:
    with open('/data/options.json') as f:
        options = json.load(f)
except FileNotFoundError:
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
    "Sec-Fetch-Dest": "empty",
    "sign": "46ca7f42c553ccdaebcb9aab95f0fd5885409c9c20225442997ab9af1e003e7" # Leave static unless this rotates often
}
