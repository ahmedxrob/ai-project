# Xrob PC Sleep Home Assistant Add-on

A small local Home Assistant add-on that exposes a token-protected HTTP endpoint and sends the working SSH sleep command to a Windows PC.

## API

GET `/health` — health check.

GET `/sleep` with header `X-API-Token: <token>` — puts the configured Windows PC to sleep.
