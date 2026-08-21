#!/usr/bin/with-contenv sh

# Extract Telegram config
CONFIG_PATH=/data/options.json
BOT_TOKEN=$(jq --raw-output '.telegram_bot_token' $CONFIG_PATH)
CHAT_ID=$(jq --raw-output '.telegram_chat_id' $CONFIG_PATH)

echo "Starting inverter..."
python3 /app/inverter.py &

# ... [Keep your existing Cloudflared download and loop setup] ...

            # Update the curl command to use the variables
            curl -s \
                "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
                -d chat_id="${CHAT_ID}" \
                --data-urlencode "text=🌐 Home Assistant Link: $URL" \
                > /dev/null

            echo "Telegram notification sent."
# ... [Keep the rest of the script exactly the same] ...
