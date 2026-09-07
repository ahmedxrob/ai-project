# Xrob Port Publisher

Home Assistant App that publishes local HTTP/HTTPS services using Cloudflare Quick Tunnels.

Examples:

- `http://192.168.1.162:2283` → Immich
- `http://192.168.1.73:8099/recommendations` → Xrob Music

Open the app from Home Assistant. Add a service and its public `trycloudflare.com` URL will appear automatically.

## Important

Cloudflare Quick Tunnels are intended for development/testing. They generate random `trycloudflare.com` hostnames and have documented limitations, including a 200 concurrent request limit and no Server-Sent Events.

For permanent production URLs, replace Quick Tunnels with a named Cloudflare Tunnel and route hostnames such as:

- `immich.example.com`
- `music.example.com`

The app stores its service list in `/data/services.json`.

## Local repository installation

Put this directory in your Home Assistant add-on repository, then add that repository under:

Settings → Add-ons → Add-on Store → ⋮ → Repositories

The app uses Home Assistant Ingress for its management UI.
