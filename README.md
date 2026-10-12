# LeRobot WebRTC

Control a **LeRobot SO-100 or SO-101 follower arm from a physical leader arm over
WebRTC**, with each arm plugged into its own computer. Uses the real LeRobot
drivers and calibration files—not a replacement servo implementation.

```text
Leader computer                         Follower computer
LeRobot leader.get_action()             LeRobot follower.send_action()
        │                                        ▲
        └── WebRTC encrypted data channel ───────┘
             direct, or relayed through TURN

Both computers ── authenticated WebSocket ── Signaling server
                   SDP/ICE setup only; no joint commands
```

## What is included

- SO-100 and SO-101 leader/follower adapters using **LeRobot 0.6.1**.
- An `aiortc` unordered data channel with `maxRetransmits=0`. Old commands are
  dropped instead of retransmitted; a latest-only mailbox avoids a motion backlog.
- Shared-secret signaling, one leader and one follower per room.
- Follower-issued expiring permits, sequence checks, joint/value validation,
  local watchdog, and LeRobot's relative-target limiter.
- Dedicated, serialized hardware I/O worker and best-effort hold on exit.
- Simulation and tests that establish actual local WebRTC connections.

**Scope:** this version transports six joint positions. It does not stream a camera,
record datasets, run policies, provide force feedback, or provide a browser UI.
Both endpoints are Python programs. Other LeRobot arms need an adapter and a
matching protocol joint schema.

## How this differs from other LeRobot + WebRTC options

|  | This repo | `lerobot` PR #3962 | `koenvanwijk/lerobot-*` | `videosdk-teleop` |
| --- | --- | --- | --- | --- |
| Direction | Leader arm → follower arm | Cloud app → remote robot | Any teleop → any robot | Any teleop/robot + recording |
| Media | Joints only, no camera | Joints + camera | Negotiated action dicts | Frames + joints, correlated IDs |
| Network | `aiortc` P2P, self-hosted signaling | `aiortc` or LiveKit SFU | WebRTC + matchmaker (self/hosted) | Hosted VideoSDK SFU |
| Recording | None | Via `Robot` API | None advertised | LeRobot dataset export |

Full detail: [COMPARISON.md](COMPARISON.md).

## Install

Use **Python 3.12+**. With [uv](https://docs.astral.sh/uv/):

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e '.[dev]'
```

For physical arms, install on **both arm computers**:

```bash
uv pip install -e '.[hardware]'
```

Or use `python3.12 -m venv .venv` and `python -m pip install -e '.[hardware,dev]'`.
The signaling machine only needs `pip install -e .`. LeRobot's hardware extra
also installs its base dependencies, including PyTorch; simulation does not.

If developing against a local LeRobot checkout instead of the pinned release:

```bash
uv pip install -e .
uv pip install -e '/path/to/lerobot[feetech]'
```

That checkout must expose `lerobot.teleoperators.so_leader` and
`lerobot.robots.so_follower` and the configuration fields used in `arms.py`.
Older LeRobot versions with `so101_leader`/`so101_follower` module paths need an
adapter change; they are not silently guessed.

## Try without hardware

For a single-command, finite loopback demonstration (no serial ports, calibration,
or LeRobot hardware extra), run from the repository root:

```bash
python examples/demo.py --duration 5
```

It creates a private temporary-in-memory token, starts the signaling server and
simulated follower and leader as child CLI processes, verifies follower control,
and cleans up all children. Duration must be between 3 and 30 seconds. The demo
does not print the token or create secret/log files. It demonstrates local WebRTC
control for the requested duration after positive follower command evidence is
observed (with a separate bounded startup wait); it does not verify physical arms
or internet/TURN traversal. The subprocess demo currently requires POSIX SIGINT
process handling and rejects other platforms explicitly.

The equivalent manual setup uses three terminals as follows.

Generate a secret once:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

In **each of three terminals**, activate the environment and export the **same**
secret (replace the placeholder):

```bash
source .venv/bin/activate
export LEROBOT_WEBRTC_TOKEN='paste-the-generated-secret-here'
```

Terminal 1—signaling:

```bash
lerobot-webrtc serve
```

Terminal 2—simulated follower:

```bash
lerobot-webrtc follower --simulate --room demo
```

Terminal 3—simulated leader:

```bash
lerobot-webrtc leader --simulate --room demo
```

The follower logs its applied command count every two seconds. The simulated
leader moves `shoulder_pan` with a sine wave and sets the gripper to 25%.
Press **Ctrl+C** to stop. Stopping either endpoint terminates the other session;
restart **both** endpoints to reconnect. `--duration 10` optionally stops an
endpoint after ten seconds of connected control.

## Set up physical arms

**Before using this bridge**, assemble your arms and assign motor IDs using
LeRobot's [SO-101 setup guide](https://huggingface.co/docs/lerobot/en/so101) or
[SO-100 guide](https://huggingface.co/docs/lerobot/en/so100). This project does
not run motor setup. Mount the follower securely, clear its workspace, support
its weight, and have a physical emergency stop/power cutoff within reach.

### 1. Calibrate locally, with the follower supported

On the leader computer (replace the example port):

```bash
lerobot-webrtc calibrate --role leader --robot so101 \
  --port /dev/ttyACM0 --id remote_leader
```

On the follower computer:

```bash
lerobot-webrtc calibrate --role follower --robot so101 \
  --port /dev/ttyACM0 --id remote_follower
```

On macOS ports typically look like `/dev/tty.usbmodem...`; on Windows use
`COM...`. The same `/dev/ttyACM0` on two different computers is fine. LeRobot
saves calibration in its normal cache. Reuse the **same ID, user account, and
LeRobot cache** when starting the endpoint. Runtime refuses missing/mismatched
calibration; interactive calibration never runs inside a teleoperation session.
Press **Ctrl+C once** to abort interactive calibration; the CLI then attempts a
torque-off disconnect. Support the arm throughout, since disconnect is best-effort.

### 2. Start signaling on a reachable computer

Export the shared `LEROBOT_WEBRTC_TOKEN` on the server and both clients.
For a **trusted LAN**, on the signaling computer:

```bash
lerobot-webrtc serve --host 0.0.0.0 --port 8080
```

The default bind is loopback. Do **not** expose the plain HTTP/WebSocket server
to the public internet. For public deployments, put it behind a TLS reverse
proxy with WebSocket upgrade support, use `wss://`, restrict access, and apply
connection/rate limits. Authentication sends the token in an Authorization
header, not in URLs; plain `ws://` still exposes it to the network.

### 3. Start the follower, then the leader

On the follower computer:

```bash
lerobot-webrtc follower --robot so101 \
  --port /dev/ttyACM0 --id remote_follower \
  --signaling ws://192.168.1.50:8080 --room bench \
  --fps 30 --timeout 0.3 --max-relative-target 5 --arm
```

On the leader computer:

```bash
lerobot-webrtc leader --robot so101 \
  --port /dev/ttyACM0 --id remote_leader \
  --signaling ws://192.168.1.50:8080 --room bench --fps 30
```

Replace `192.168.1.50` with your signaling computer's IP. `--arm` is required
for a real follower, but not for simulation. Both sides must use the same robot
model and room. To use SO-100, change **both** sides to `--robot so100`.
Never pair a simulated leader with an armed physical follower unintentionally:
the simulation emits real, valid position commands.

**Motion begins automatically when the first valid command arrives after
connection.** There is no keyboard dead-man switch. `--arm` is startup consent,
not a hold-to-run safety device. Put the leader in a suitable initial pose before
connecting, and start with an unloaded follower and conservative limits.

### Across NAT / over the internet

No ICE servers are used by default, suitable for directly reachable LAN peers.
Add the same STUN/TURN settings to **both endpoints**:

```bash
export LEROBOT_TURN_USERNAME='your-turn-user'
export LEROBOT_TURN_PASSWORD='your-turn-password'

lerobot-webrtc follower --robot so101 \
  --port /dev/ttyACM0 --id remote_follower --arm \
  --signaling wss://signal.example.com --room bench \
  --stun stun:stun.l.google.com:19302 \
  --turn turn:relay.example.com:3478?transport=udp
```

Accepted STUN syntax is `stun:host[:port]` (aiortc does not support `stuns`).
TURN accepts `turn:host[:port]` (UDP by default), `turn:host[:port]?transport=udp`,
`turn:host[:port]?transport=tcp`, and `turns:host[:port]` or
`turns:host[:port]?transport=tcp` (secure TURN over TCP). `turns` defaults to
port 5349; other forms default to 3478. Credentials are read only from
`LEROBOT_TURN_USERNAME` and `LEROBOT_TURN_PASSWORD`; do not put them in URLs.

STUN alone cannot traverse every NAT/firewall. Supply your own authenticated
TURN server for restrictive networks. TURN relays encrypted WebRTC traffic;
the signaling server still does not carry motor commands. Allow peer UDP
traffic/TURN ports through the applicable firewalls. The signaling server
does **not** include a TURN server.

## Safety behavior and limits

- Uses `use_degrees=False` on **both** LeRobot drivers: five body joints are
  normalized to `[-100, 100]`, gripper to `[0, 100]`. Missing/extra joints,
  non-finite numbers, incompatible model/units, and malformed packets end the
  session. Older/out-of-order or expired packets are ignored.
- Default `--max-relative-target 5` clips each target to within **5 normalized
  units of the measured current joint position per write**, using LeRobot's
  limiter. It is **not** a degrees/second, force, acceleration, or collision
  limit. At 30 Hz, repeated 5-unit steps can still be fast. Tune it on your arm.
- The follower publishes random permits valid for `--timeout` seconds
  (default **300 ms**). The leader binds the permit **before** reading its arm;
  the follower checks expiry on reception, mailbox consumption, and immediately
   before entering the blocking LeRobot action operation. The driver may read
   positions internally before its servo write, so this is not a guarantee that
   freshness is rechecked at the hardware write boundary. A slow operation can
   overrun the permit/watchdog deadline; that overrun is detected after the
   operation completes and cannot undo a target already written. No synchronized
   wall clocks are required.
- Once motion starts, no successfully applied fresh command for the timeout
  trips the watchdog. The first command has a 5-second startup grace period.
  A watchdog trip, transport/signaling loss, invalid message, or hardware error
  **ends the session**. It cannot automatically resume on a new packet.
- On session exit/Ctrl+C the follower tries to read and command its current
  position, then disconnects. Default `--on-exit hold` leaves torque enabled
  to avoid dropping an unsupported arm. `--on-exit torque-off` disables torque
  instead; **the arm can fall**. Neither is a guaranteed safe state for every
  load. Hold can fail if the serial connection or power has failed.
- This is best-effort Python software, **not a safety-rated controller**.
  Watchdog checks need a responsive process and serial bus. Blocking motor
  I/O is not forcibly interrupted, a process crash cannot run cleanup, and a
  previously sent servo target may continue until the hold write succeeds.
  Physical calibration/connect may configure or enable motors; simulation is
  not physical safety verification. `--arm` is explicit startup consent, not a
  dead-man switch.
  A physical stop and independent hardware safeguards remain necessary.
- A signaling token grants room access to anyone who has it. The signaling
  server is trusted to introduce the peers; application commands have no
  additional cryptographic identity beyond the WebRTC session. Use a private
  server/network, protect tokens/TLS, and never accept unknown peers.

## Development

```bash
uv pip install -e '.[dev]'
pytest
ruff check .
ruff format --check .
```

For a reproducible development install using the included `uv.lock`, run
`uv sync --extra dev` (or `uv sync --extra dev --extra hardware` on an arm host).

Tests cover packet validation, freshness, ordering, signaling authentication
and role limits, real local WebRTC control of simulated arms, cancellation,
and a watchdog trip when a leader stops producing commands. Physical hardware
and public-network TURN traversal require separate testing.

Source layout:

| File | Purpose |
| --- | --- |
| `src/lerobot_webrtc/arms.py` | LeRobot adapter, simulator, serialized hardware worker |
| `src/lerobot_webrtc/protocol.py` | Joint schema, expiring permits, latest-only mailbox |
| `src/lerobot_webrtc/peer.py` | aiortc connection and WebRTC data channel |
| `src/lerobot_webrtc/signaling.py` | Authenticated WebSocket SDP relay |
| `src/lerobot_webrtc/runtime.py` | Leader/follower loops and shutdown |
| `src/lerobot_webrtc/cli.py` | Server, endpoint, and calibration commands |

Upstream references:
[LeRobot API examples](https://huggingface.co/docs/lerobot/en/il_robots),
[SO follower implementation](https://github.com/huggingface/lerobot/tree/v0.6.1/src/lerobot/robots/so_follower),
[SO leader implementation](https://github.com/huggingface/lerobot/tree/v0.6.1/src/lerobot/teleoperators/so_leader),
[aiortc](https://aiortc.readthedocs.io/).
