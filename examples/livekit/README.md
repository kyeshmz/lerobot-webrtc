# LiveKit: transport-only SDK probe

LiveKit is an SFU/room platform with its own SDK signaling, participant tokens,
permissions and connection lifecycle. It is **not a drop-in URL** for this repo's
`--signaling` flag. The current robot bridge does not implement a LiveKit backend.

[probe.py](probe.py) is a small, finite example using the **real LiveKit Python
SDK**, pinned independently in inline script metadata. It sends only harmless
readiness messages, `network-probe` packets and delivery receipts. It never imports
a robot driver, opens a motor bus, or uses the bridge's action topic/protocol.

## Run against your own LiveKit room

Create/use an approved LiveKit Cloud or self-hosted server. From your authenticated
backend, issue two short-lived, room-scoped participant JWTs with permission to
join and publish data. Give them distinct identities such as `probe-sender` and
`probe-receiver`. Keep `LIVEKIT_API_SECRET` on the backend; the probe needs only
the participant JWT in `LIVEKIT_TOKEN`.

In each terminal, securely set `LIVEKIT_URL=wss://YOUR_SERVER` and its participant's
`LIVEKIT_TOKEN`. Local development may use `ws://localhost:7880`. From the repo root,
start the receiver first:

```bash
uv run --locked --script examples/livekit/probe.py receive --peer probe-sender --duration 15
```

In the sender terminal (with the sender JWT):

```bash
uv run --locked --script examples/livekit/probe.py send --peer probe-receiver --duration 10
```

The inline SDK dependency and [script lock](probe.py.lock) use an isolated script environment, not the bridge or
LeRobot hardware environment. The receiver first sends bounded, reliable readiness
messages once the expected sender is visible. The sender waits for readiness from
that expected identity, then submits five `reliable=False` packets,
targeted to the other identity on a dedicated probe topic. The receiver counts
unique packets only from the expected participant/topic and run ID. After all five,
it sends a reliable receipt on that same topic. The sender keeps the room open
until that matching receipt or its finite wait deadline, avoiding teardown while
the receiver is still processing packets. `delivery_verified_by_sender` becomes
true only after the receipt; submission alone is not delivery confirmation.
The receiver remains connected for its configured receive window. Keep that
window longer than the sender's wait, and inspect both outputs: packet loss is
still possible by design. Automated unit tests do not contact a live room.

## A future robot backend is a separate integration

The Python SDK pattern is `rtc.Room.connect(url, participant_token)` and
`room.local_participant.publish_data(..., reliable=False)`, not direct exchange of
this bridge's SDP offers/answers. A real backend would also need:

- server-issued room/identity grants and strict peer/topic filtering;
- model/unit negotiation, follower-issued original deadlines, sequence/replay
  checks, a latest-only mailbox and bounded publish/backpressure behavior;
- loss/reconnect fencing with fresh explicit activation, never automatic motor resume;
- the same serialized device owner, motor limits, watchdog and best-effort stop;
- independent software and supervised physical validation.

LiveKit can reconnect automatically. That is convenient for media, but it must not
silently re-arm a stopped robot session. SFU-hop encryption is also not the same
claim as this bridge's direct peer DTLS; review LiveKit's security/E2EE model for
your design. Setting `reliable=False` alone supplies none of the robot safeguards.

## Official references

- [Connect to a room](https://docs.livekit.io/transport/connection/)
- [Authentication and access tokens](https://docs.livekit.io/home/get-started/authentication/)
- [Python SDK reference](https://docs.livekit.io/reference/python/livekit/rtc/)
- [Data packets](https://docs.livekit.io/transport/data/packets/)
- [Self-hosting](https://docs.livekit.io/home/self-hosting/)
- [Python SDK source](https://github.com/livekit/python-sdks)
