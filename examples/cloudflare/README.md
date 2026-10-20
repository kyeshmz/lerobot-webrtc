# Cloudflare: signaling and TURN

This example uses two independent Cloudflare products:

```text
leader/follower ── WSS ── Cloudflare Tunnel ── local Python signaling server
       └────────── encrypted WebRTC, direct or through Cloudflare TURN ───────┘
```

- **Tunnel** publishes the existing HTTP/WebSocket signaling server. It does not
  relay the WebRTC UDP/data-channel traffic.
- **Realtime TURN** supplies an ICE relay when the peers cannot connect directly.
  It does not replace the signaling server.
- **Realtime SFU** is a different service, not a backend implemented by this repo.

No Worker deployment or Python-on-Workers claim is involved. These instructions
do not deploy anything until you run them in your own approved environment.

## 1. Simulation through a temporary Tunnel

Install [cloudflared](https://developers.cloudflare.com/tunnel/downloads/). From
the repository root, set the same fresh, random `LEROBOT_WEBRTC_TOKEN` (at least
16 characters) in all endpoint/server environments using your secret manager.
Do not use the tunnel URL itself as a secret.

Terminal A — keep the Python origin bound to loopback:

```bash
: "${LEROBOT_WEBRTC_TOKEN:?Set the shared random signaling secret}"
uv run --no-sync lerobot-webrtc serve --host 127.0.0.1 --port 8080
```

Terminal B — expose signaling only:

```bash
cloudflared tunnel --url http://127.0.0.1:8080
```

Take the printed `https://...trycloudflare.com` hostname and set
`SIGNALING_URL=wss://...trycloudflare.com` in both endpoint environments. Do not
append `/ws` or a room path: the bridge adds those itself.

Terminal C — follower, simulation only:

```bash
uv run --no-sync lerobot-webrtc follower --simulate --room cloudflare-demo \
  --signaling "$SIGNALING_URL" --duration 10
```

Terminal D — leader, simulation only:

```bash
uv run --no-sync lerobot-webrtc leader --simulate --room cloudflare-demo \
  --signaling "$SIGNALING_URL" --duration 10
```

Look for WebRTC connection and increasing follower applied-command counts. With
different endpoint clocks, one finite endpoint may stop first; the companion then
ends its session on disconnect rather than silently reconnecting.

**Quick Tunnels are development-only public URLs.** Keep the room token private,
stop your tunnel when done, and use a named tunnel with appropriate access/rate
controls for ongoing deployments. Do not add browser email challenges such as
`--allowed-mail` to this example: these Python clients cannot complete that login.
Cloudflare Access service-token headers are not currently implemented by `Peer`.
Do not remove an existing access policy to work around that limitation.

Cloudflare can terminate WebSockets during edge updates. That intentionally ends
this bridge session. Restart both endpoints deliberately; do not weaken watchdogs
or add automatic motor re-arming.

## 2. Stable hostname: named Tunnel

These commands create account/DNS resources. Use your own approved account/domain;
the repository does not execute them for you.

```bash
cloudflared tunnel login
cloudflared tunnel create lerobot-signaling
```

Copy [cloudflared.example.yml](cloudflared.example.yml) to an untracked private
location, replace its UUID, credentials-file path and hostname, then:

```bash
cloudflared tunnel route dns lerobot-signaling signal.example.com
cloudflared tunnel --config /absolute/path/to/cloudflared.local.yml run lerobot-signaling
```

Use `--signaling wss://signal.example.com` on both endpoints. Keep the origin on
loopback and retain the bridge's bearer-token authentication. Do not commit the
tunnel credentials JSON, account certificate, real configuration, or room token.

## 3. Add Cloudflare Realtime TURN

Create a TURN key as described in Cloudflare's [credential guide](https://developers.cloudflare.com/realtime/turn/generate-credentials/).
The long-lived key/API token belongs on a **trusted backend/operator host**, not
in a browser, distributed robot application, committed `.env`, or command line.

The [with_turn.py](with_turn.py) launcher is a trusted-host development example.
It calls the documented `generate-ice-servers` endpoint, selects one compatible
TURN URI, passes only temporary TURN credentials to the child endpoint, and
removes `TURN_KEY_API_TOKEN` / `TURN_KEY_ID` from that child's environment. It
does not print credential JSON or secret exports.

Set `TURN_KEY_ID` and `TURN_KEY_API_TOKEN` securely in the **launcher's** environment,
along with the shared `LEROBOT_WEBRTC_TOKEN`. On trusted development machines:

```bash
uv run --no-sync python examples/cloudflare/with_turn.py --ttl 600 --transport udp -- \
  follower --simulate --room cloudflare-demo --signaling "$SIGNALING_URL" --duration 10
```

```bash
uv run --no-sync python examples/cloudflare/with_turn.py --ttl 600 --transport udp -- \
  leader --simulate --room cloudflare-demo --signaling "$SIGNALING_URL" --duration 10
```

Each endpoint may receive its own short-lived credentials; only the bridge room
token must be identical. Use `--transport tcp` for TURN/TCP or `--transport tls`
for TURN/TLS (the example prefers port 443). Only use endpoints returned by the
provider. Port 53 is skipped. The existing bridge accepts one TURN URL per peer,
so this helper does not promise automatic fallback across all returned URLs.

The helper requires finite endpoint durations and a TTL that leaves a 90-second
startup margin. It does **not** refresh credentials or ICE configuration mid-session.
Choose a TTL covering the complete intended run; expiry/network loss must stop the
session. The provider's long-lived key is not a valid TURN username/password.

For production, mint credentials in an authenticated, rate-limited backend and
deliver only the short-lived `username`, `credential`, and TURN URI to each
participant. Set the existing `LEROBOT_TURN_USERNAME` / `LEROBOT_TURN_PASSWORD`
variables and `--turn` flag there; do not copy the provider API token onto clients.

Adding TURN configuration does not prove a relay was selected. Verify actual
allocations/traffic on a restrictive network before making an internet-connectivity
claim. These examples and automated tests do not perform a live Cloudflare deployment.

## Official references

- [Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)
- [Locally managed Tunnel](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/create-local-tunnel/)
- [Cloudflare WebSockets and disconnect behavior](https://developers.cloudflare.com/network/websockets/)
- [TURN credential generation and current response schema](https://developers.cloudflare.com/realtime/turn/generate-credentials/)
- [Realtime products: TURN versus SFU](https://developers.cloudflare.com/realtime/)
