#!/usr/bin/with-contenv sh

echo "Starting inverter..."
python3 /app/inverter.py &

echo "Waiting for Home Assistant..."
sleep 20

echo "Supervisor token length: ${#SUPERVISOR_TOKEN}"

echo "Starting Cloudflared..."

# Download cloudflared if it doesn't exist
if [ ! -f /app/cloudflared ]; then
    wget -q \
    https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
    -O /app/cloudflared
    chmod +x /app/cloudflared
fi

LAST_URL=""

while true
do
    /app/cloudflared tunnel --url http://127.0.0.1:8123 2>&1 | while read -r line
    do
        echo "$line"

        URL=$(echo "$line" | grep -oE 'https://[-a-z0-9]+\.trycloudflare\.com')

        if [ -n "$URL" ] && [ "$URL" != "$LAST_URL" ]; then

            LAST_URL="$URL"

            echo "========================================="
            echo "New Cloudflare URL:"
            echo "$URL"
            echo "========================================="

            HTTP_CODE=$(curl -s \
                -o /tmp/ha_response.txt \
                -w "%{http_code}" \
                -H "Authorization: Bearer $SUPERVISOR_TOKEN" \
                -H "Content-Type: application/json" \
                -X POST \
                -d "{\"entity_id\":\"input_text.haos_link\",\"value\":\"$URL\"}" \
                http://supervisor/core/api/services/input_text/set_value)

            echo "Home Assistant API HTTP: $HTTP_CODE"
            echo "Response:"
            cat /tmp/ha_response.txt
            echo

            curl -s \
                "https://api.telegram.org/bot8600167072:AAFU-zn-0Izg9nBDe6XqlM_GYA9ds0Dhosk/sendMessage" \
                -d chat_id=5809858782 \
                --data-urlencode "text=🌐 Home Assistant Link: $URL" \
                > /dev/null

            echo "Telegram notification sent."
        fi
    done

    echo "Cloudflared stopped."
    echo "Restarting in 5 seconds..."
    sleep 5
done