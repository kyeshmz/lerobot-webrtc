# coturn: your own STUN/TURN relay

This works with the existing bridge's ICE flags. coturn is **not** the room/SDP
signaling server; keep the normal authenticated signaling server running at a
reachable `wss://` URL. TURN relays encrypted peer traffic, not robot API calls.

## Dedicated Linux relay example

The provided [Compose file](compose.yml) pins the upstream
`coturn/coturn:4.18.0-r0` image revision and uses Linux host networking. It is not
a request to install Docker or a relay on a shared robot/Jetson.

1. Copy [turnserver.conf.example](turnserver.conf.example) to
   `examples/coturn/turnserver.local.conf` (git-ignored).
2. Replace the realm/username/password placeholders, then uncomment the `user=`
   line. No static account is enabled in the unedited template. Use a strong unique secret
   and keep the file private, readable only by the operator/container's intended
   user. The template is **not ready for public deployment**.
3. Give the relay a reachable public IP/DNS name. If behind 1:1 NAT, configure
   `external-ip=PUBLIC_IP/PRIVATE_IP` and matching port forwarding.
4. Allow TCP/UDP 3478 and UDP relay ports 49160–49200 through the relay's firewall.
   Review peer-address restrictions, quotas, logs and abuse controls for your network.

Then, on that approved Linux relay host:

```bash
docker compose -f examples/coturn/compose.yml up -d
```

The bind mount refuses a missing local config rather than creating a directory;
the container command also explicitly enables long-term authentication. Use a
current Docker Compose v2 supporting `bind.create_host_path`.
Authentication is required; do not enable `no-auth`. Loopback/multicast peer access
is blocked. This minimal config deliberately disables TLS/DTLS because it does
not provision certificates. **Do not use a `turns:` URL with this config.** For
production TURN/TLS, follow coturn's certificate/key configuration, remove `no-tls`,
and expose the configured TLS port. Keep TLS verification enabled on clients.

## Simulated bridge endpoints

Set the shared `LEROBOT_WEBRTC_TOKEN` and relay `LEROBOT_TURN_USERNAME` /
`LEROBOT_TURN_PASSWORD` in each endpoint environment through a private channel.
Use the configured coturn account, not the bridge's signaling token, for TURN.

```bash
uv run --no-sync lerobot-webrtc follower --simulate --room coturn-demo \
  --signaling wss://signal.example.com --duration 10 \
  --stun stun:turn.example.com:3478 \
  --turn 'turn:turn.example.com:3478?transport=udp'
```

```bash
uv run --no-sync lerobot-webrtc leader --simulate --room coturn-demo \
  --signaling wss://signal.example.com --duration 10 \
  --stun stun:turn.example.com:3478 \
  --turn 'turn:turn.example.com:3478?transport=udp'
```

Use `?transport=tcp` if needed. TURN/TCP changes the client-to-relay leg; you still
need the relay's UDP allocation range. Supplying these flags alone is not evidence
that ICE selected the relay. Confirm allocations/traffic and endpoint control on
your intended network. Static credentials are for this small trusted example;
production deployments should consider per-user, expiring credentials.

Stop only the relay you created when finished:

```bash
docker compose -f examples/coturn/compose.yml down
```

References: [upstream Docker instructions and tag policy](https://github.com/coturn/coturn/blob/master/docker/coturn/README.md),
[configuration reference](https://github.com/coturn/coturn/blob/master/examples/etc/turnserver.conf),
and [coturn project](https://github.com/coturn/coturn).
