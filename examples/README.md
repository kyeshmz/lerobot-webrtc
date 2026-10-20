# Networking examples

Run bridge commands from the repository root. Start with simulation; none of
these examples grants permission to open a physical arm, start a public service,
or create cloud resources automatically.

| Example | What it demonstrates | Current bridge compatible? |
| --- | --- | --- |
| [Local P2P](local/README.md) | Finite, three-process signaling and real WebRTC control of simulated arms | Yes |
| [Local video](video/README.md) | Synthetic follower video, simulated arms, and a receive-only native WebRTC browser viewer | Yes; both endpoints explicitly opt in |
| [Cloudflare](cloudflare/README.md) | Tunnel for WSS signaling; short-lived Realtime TURN credentials | Yes; Tunnel and TURN have separate jobs |
| [coturn](coturn/README.md) | An authenticated self-hosted STUN/TURN relay | Yes, through the existing `--stun` / `--turn` flags |
| [LiveKit](livekit/README.md) | A finite, optional SDK data-packet probe and SFU references | Probe only; not a robot-control backend |

The bridge's own SDP signaling protocol is not interchangeable with a LiveKit
room endpoint, Cloudflare Realtime SFU, Daily, or other SDK/SFU services. Using
their URLs as `--signaling` will not add those transports. See the [LiveKit guide](livekit/README.md)
for the integration boundary.

TURN credentials alone do **not** prove that a relay was selected. Test on the
intended networks and inspect relay allocations/traffic. Network probes, simulated
motor movement, submitted SDK packets, and physical encoder movement are different
results; keep them labeled separately.

Video is off by default. The local viewer adds a second native WebRTC hop, not
browser arm control, audio, recording, or a safety-rated dead-man. `--simulate`
only simulates arms: use `--test-video`, not `--camera-index`, to avoid real cameras.

The examples never renew robot command permits, relax motor bounds, clear a STOP,
or automatically resume a stopped bridge session.

For SDK installation rather than a networking provider, see the separately locked
[B601 environment example](b601-sdk/README.md) and [version policy](../docs/compatibility.md).
