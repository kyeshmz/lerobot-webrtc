# Native SDK and networking examples

Run bridge commands from the repository root. Start with simulation; none of
these examples grants permission to open a physical arm, start a public service,
or create cloud resources automatically.

| Example | What it demonstrates | Current bridge compatible? |
| --- | --- | --- |
| [Native LeRobot Robot](native/README.md) | Install both SDK and bridge; normal SO teleoperation, optional headless RGB observations, and single-episode SDK recording | Yes, with explicit discovery and native `host`; cadence/label limits apply |
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

For normal LeRobot commands, start with the **native guide**, not the standalone
`leader` / `follower` recipes. Native `host` and standalone `follower` use different
data-channel protocols. SDK discovery is explicit, and SO SDK leaders require
`--teleop.use_degrees=false` to match the bridge's normalized units.

Video is off by default. Native mode sends it directly to headless RGB Robot
observations; the standalone local viewer adds a second WebRTC hop, not browser
arm control, audio, recording, or a safety-rated dead-man. `--simulate` only
simulates arms: use `--test-video`, not `--camera-index`, to avoid real cameras.
Dataset recording belongs to the SDK; its action labels are processed requested
teleop actions, not returned host-clamped targets. Saves/pauses can STOP control.

The examples never renew robot command permits, relax motor bounds, clear a STOP,
or automatically resume a stopped bridge session.

The SO `native` and `hardware` extras install the released SDK plus the bridge.
Do not install them over a prepared B601/Jetson SDK; see the separately locked
[B601 environment example](b601-sdk/README.md) and [version policy](../docs/compatibility.md).
