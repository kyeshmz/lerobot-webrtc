# LeRobot WebRTC usage guide

This guide preserves the **standalone CLI**, browser video, calibration, and
hardware-safety workflows. To use normal **`lerobot-teleoperate`** or SDK-owned
recording with WebRTC, start with the [native Robot guide](../examples/native/README.md)
and [README installation](../README.md#install-lerobot-and-the-bridge).
Native mode uses `lerobot-webrtc host`; standalone mode uses `follower` +
`leader`. Their data-channel protocols are deliberately incompatible.

Run checkout/example commands from the repository root. Commands named
`lerobot-webrtc` belong to this bridge, not LeRobot's own CLI.

Control a **LeRobot SO-100 or SO-101 follower arm from a physical leader arm over
WebRTC**, with each arm plugged into its own computer. Also includes an opt-in
**B601-RS seven-joint degree-position follower and bounded encoder-checked bench**.
Uses LeRobot drivers, not a replacement servo implementation. B601 support does
not imply an SO-to-B601 mapping or completed physical qualification.

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
- An optional native `Robot` plugin for normal SDK commands, with explicit
  discovery, a separate `host`, and optional headless camera observations.
- An `aiortc` unordered data channel with `maxRetransmits=0`. Old commands are
  dropped instead of retransmitted; a latest-only mailbox avoids a motion backlog.
- Shared-secret signaling, one leader and one follower per room.
- Follower-issued expiring permits, sequence checks, joint/value validation,
  local watchdog, and LeRobot's relative-target limiter.
- Dedicated, serialized hardware I/O worker and best-effort hold on exit.
- Optional single follower video track: headless RGB observations in native
  mode, or a receive-only loopback browser viewer in standalone mode. Cameras
  are off by default; there is no audio or bridge-owned recording.
- Simulation and tests that establish actual local WebRTC connections.
- A separate B601-RS schema, guarded LeRobot startup and shutdown, shared CAN
  ownership check, and a small network-only bench source.

**Scope:** SO transports six normalized positions; B601-RS transports seven joint
angles in degrees, including `wrist_yaw` and the angular gripper. Both control
endpoints remain Python programs; the standalone browser only receives video.
The SDK, not the bridge, owns datasets and policy execution through the native
Robot API, [subject to strict cadence and recording limits](../examples/native/README.md#cadence-stop-and-rollout-limits).
There is no Teleoperator plugin, force feedback, or browser arm control. Other
LeRobot arms need an adapter and a matching protocol joint schema.

## How this differs from other LeRobot + WebRTC options

|  | This repo | `lerobot` PR #3962 | `koenvanwijk/lerobot-*` | `videosdk-teleop` |
| --- | --- | --- | --- | --- |
| Direction | SDK Robot proxy or standalone leader → follower | Cloud app → remote robot | Any teleop → any robot | Any teleop/robot + recording |
| Media | Joints; optional native RGB observations or standalone browser viewer | Joints + camera | Negotiated action dicts | Frames + joints, correlated IDs |
| Network | `aiortc` P2P, self-hosted signaling | `aiortc` or LiveKit SFU | WebRTC + matchmaker (self/hosted) | Hosted VideoSDK SFU |
| Recording | SDK-owned via optional `Robot` plugin; label/cadence caveats | Via `Robot` API | None advertised | LeRobot dataset export |

Full detail: [COMPARISON.md](../COMPARISON.md).

## Install

Use an **isolated Python 3.12 environment on each arm computer**. The bridge is
not published to PyPI. For Git installation of both SDK and bridge, use
`'lerobot-webrtc[native] @ git+https://github.com/kyeshmz/lerobot-webrtc.git'`
on a native SDK operator and the same requirement with `[hardware]` on an SO
robot host. See the [complete pip instructions](../README.md#install-lerobot-and-the-bridge).

For a checkout, with [uv](https://docs.astral.sh/uv/):

```bash
uv sync --locked --python 3.12
source .venv/bin/activate
```

For **standalone physical SO** control, install on **both arm computers**:

```bash
uv sync --locked --python 3.12 --extra hardware
```

For native SDK commands, use `--extra native` on the operator instead; it adds
`lerobot[core_scripts,feetech]==0.6.1` along with the bridge/plugin. The SO host's
`hardware` extra adds `lerobot[feetech]==0.6.1`. Add `--extra dev` only for test
tools. The signaling-only machine needs the base bridge. SDK installation also
brings its base dependencies, including PyTorch; bridge simulation does not.
Do **not** install either SO extra over a prepared B601/Jetson SDK.

If deliberately developing against a local LeRobot checkout instead of the
pinned release (not the reproducible install path):

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

## Optional video

Video is **off by default**. Start with the finite, fully hardware-free
[three-terminal synthetic example](../examples/video/README.md). It uses base
PyAV dependencies, not a camera, LeRobot, NumPy, or PIL.

```text
Follower camera/test pattern ── WebRTC video ── Python leader ── WebRTC video ── Local browser
Follower arm                ◀── WebRTC data ─── Python leader ◀── Physical teleoperator
```

The robot link uses one follower → leader video track on the existing peer
connection, directly or through TURN. The Python leader decodes it and sends it
over a **second native WebRTC connection** to an HTML `<video>` MediaStream.
This is not MJPEG, HTTP frame polling, snapshots, or recording. The browser is
receive-only: no arm commands, microphone/audio, or browser camera capture.
The Python leader still owns the LeRobot physical teleoperator.

### Video settings

| Command | Option | Meaning / default |
| --- | --- | --- |
| Follower | `--test-video` **or** `--camera-index INTEGER` | Mutually exclusive opt-in sources: synthetic pattern or explicit nonnegative real camera index |
| Follower | `--video-width INTEGER` | Even integer 16–1280; default `640` |
| Follower | `--video-height INTEGER` | Even integer 16–720; default `480` |
| Follower | `--video-fps INTEGER` | Integer 1–30; default `15`; separate from joint-control `--fps` |
| Follower | `--camera-fourcc FOURCC` | Camera capture format, e.g. `MJPG` or `YUYV`; exactly four uppercase letters/digits; only with `--camera-index`; unset by default |
| Leader | `--view-video [PORT]` | Opt in to the local viewer; omitted port means `8081`; binds only `127.0.0.1` |
| Both | `--video-codec auto\|vp8\|h264` | Network codec; default `auto` negotiates a common codec; explicit VP8/H.264 requires a common supported codec |

Both sides must opt in; unexpected/incompatible video SDP fails before camera
capture or motor connection. Video settings without an opt-in source/viewer are
rejected. Unsupported codec values, including `av1`, are rejected; an explicit
codec does not silently fall back. Camera FOURCC is **not** the network codec:
`MJPG` capture can still be encoded as VP8 or H.264 over WebRTC. Requested camera
resolution, FPS, and an explicit FOURCC are checked against the selected mode;
an unsupported mode fails rather than silently selecting another one. This does
not guarantee delivered frame rate or hardware-accelerated encoding.

### Viewer startup and stopping

On the leader computer, open `http://127.0.0.1:8081` (or your chosen port).
Authenticate with HTTP Basic username **`viewer`** and password equal to the
shared **`LEROBOT_WEBRTC_TOKEN`**. The page and SDP exchange are loopback HTTP;
the token is not embedded in the URL/page or logged. Do not expose or proxy the
viewer to other machines; it is not a remote browser-control service.

Click **Connect** before the Python leader joins robot signaling. Once the local
browser WebRTC connection is ready, robot negotiation proceeds; the Python leader
waits for its first decoded follower frame before connecting the leader arm.
This does not prove the browser has displayed a fresh image or that the scene is safe.
Frame failure/idle, browser transport loss, or either endpoint exiting ends current
control. There is **no automatic reconnect or resume**; restart both endpoints and
connect a fresh viewer session. A finite session ending also closes the peer;
stop the separate signaling process with Ctrl+C when finished.

The browser is **not a safety-rated dead-man or guaranteed physical stop**.
Both WebRTC hops share CPU/network resources with control, without real-time
video or control guarantees. Keep the control terminal accessible and use the
independent physical cutoff and [hardware safeguards](#safety-behavior-and-limits).

### Real camera opt-in

**`--simulate` simulates arms only.** Adding `--camera-index 0` explicitly opens a
**real camera**, even in arm simulation. Use `--simulate --test-video` for a fully
hardware-free session. `bench` and `calibrate` have no video flags and remain data-only.

For a physical source, use the released SO `hardware` extra or the separately
prepared [B601 SDK environment](../examples/b601-sdk/README.md). Do not sync or
replace a shared Jetson SDK to add video. Camera capture uses an independent
LeRobot `OpenCVCamera`, never `robot.get_observation()` or the motor worker.
It passes `index_or_path`, `width`, `height`, `fps`, and `fourcc` to
`OpenCVCameraConfig`, uses RGB frames, and runs blocking `async_read()` off the
event loop on its own worker. The imports/configuration match both v0.6.1 and the
reviewed `ca69a2068462a37f7cdcb74180927a2f863d2bf7` snapshot. This section describes
the standalone browser path, not SDK observations or recording. Use the separate
[native camera guide](../examples/native/README.md#camera-observations) for those.

References: official [LeRobot cameras](https://huggingface.co/docs/lerobot/en/cameras),
[camera source (v0.6.1)](https://github.com/huggingface/lerobot/tree/v0.6.1/src/lerobot/cameras/opencv),
[teleoperator source (v0.6.1)](https://github.com/huggingface/lerobot/tree/v0.6.1/src/lerobot/teleoperators/so_leader),
and [aiortc API](https://aiortc.readthedocs.io/en/latest/api.html).

## Set up physical SO arms

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

These are **standalone** commands. To keep using LeRobot's native
`lerobot-teleoperate`, follow the [three-terminal native recipe](../examples/native/README.md#three-terminal-so-101-teleoperation)
instead, with `host` rather than `follower` and `--teleop.use_degrees=false`.

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
this includes joint data and optional video on the robot peer connection, not
the separate local browser hop. The signaling server carries neither commands
nor video. Allow peer UDP traffic/TURN ports through the applicable firewalls. The signaling server
does **not** include a TURN server.

## Safety behavior and limits

These host-side hardware safeguards apply to both modes. Native SDK commands
add an observation-bound action/result exchange; read its
[cadence and STOP limits](../examples/native/README.md#cadence-stop-and-rollout-limits).
Native SDK `get_observation()` binds the permit before computation, and each
action consumes that permit without background replay or renewal. Startup has
a 5-second first-command budget; active control defaults to 300 ms. SDK setup,
recording saves, inference, and interactive pauses can STOP; restart/rearm both
endpoints. Neither SDK records returned clamped targets as action labels.

- SO uses `use_degrees=False` on both leader/follower drivers: five body joints are
  normalized to `[-100, 100]`, gripper to `[0, 100]`. Missing/extra joints,
  non-finite numbers, incompatible model/units, and malformed packets end the
  session. Standalone mode ignores older/out-of-order or expired command packets;
  native mode ends the session on out-of-sequence submissions or expired/unknown
  permits. The native SDK leader must explicitly set `--teleop.use_degrees=false`.
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
- With video enabled, frame failure/idle or browser transport loss ends current
  control in standalone mode; native mode has no browser but still fails on
  video loss/idle. Neither automatically reconnects/resumes. Browser readiness
  and decoded frames are software checks, not a safety-rated dead-man or proof
  of physical stop.
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

## B601-RS (opt-in, supervised bench)

This integration targets **RobStride RS over Linux SocketCAN**, not the Damiao
variant. The seven named positions use LeRobot's public **degrees**, including
negative gripper angles; normalized SO packets are incompatible. There is no
automatic six-to-seven joint mapping. B601 hardware is follower-only.

B601 **feedback** has a **0.35° margin around each native command boundary**.
Measured values are preserved in telemetry; commanded targets still obey the exact
native limits, including `[-270°, 0°]` for the gripper. Startup/exit hold may make a
small correction to the nearest legal boundary only when it fits the configured
relative-target limit. Larger overshoot and non-finite feedback still stop the
session. No motor zero, calibration or native joint limit is changed.

The prepared driver is LeRobot commit
`ca69a2068462a37f7cdcb74180927a2f863d2bf7` (reports version 0.6.2, not a PyPI
release), plus MotorBridge 0.5.6. The SO `.[hardware]` extra does not install this
driver. For a new, isolated B601 environment:

```bash
uv sync --locked --project examples/b601-sdk
```

LeRobot's [B601 setup and calibration guide](https://github.com/huggingface/lerobot/blob/ca69a2068462a37f7cdcb74180927a2f863d2bf7/docs/source/rebot_b601.mdx)
describes first-time commissioning. Its calibration operation changes motor zeros.
This bridge assumes the arm is already commissioned with the intended zeros and
directions; it never performs that operation during connection or teleoperation.
Do not recalibrate an already prepared arm merely to start this bridge.

Do not mix the main SO `hardware` environment with `examples/b601-sdk`; these
select different LeRobot versions. The [compatibility guide](compatibility.md) explains the separate
locked dependency tracks and upstream's rolling-release security policy.
The same warning applies to the SO `native` extra. Native `Robot` model support
does not bypass any B601 gate; see [advanced native B601](../examples/native/README.md#b601-rs-advanced).

On a shared prepared Jetson, reuse its reviewed driver without replacing or
resyncing another project's SDK environment. Missing bridge dependencies belong
in an isolated environment. Agree exclusive ownership with the current CAN user
first; a snapshot or an advisory lock cannot fence unrelated software.

After supporting the unloaded arm, clearing the sweep and arranging an independent
power cutoff, start signaling as above. On the follower host, using its prepared
Python environment and an **agreed existing controller lock path**:

```bash
uv run --project examples/b601-sdk --no-sync lerobot-webrtc follower --robot b601-rs --port can1 --id webrtc_b601 \
  --room b601-bench --max-relative-target 0.5 --max-speed 2 --duration 20 \
  --can-lock /absolute/path/to/shared/can-can1.lock --arm
```

On the other computer, using the same token, room and signaling/ICE settings:

```bash
.venv/bin/python -m lerobot_webrtc bench --room b601-bench --signaling ws://SIGNAL_HOST:8080 \
  --offset 0.5 --duration 5 --execute
```

The bench captures the follower's measured current pose, gently ramps shoulder
pan to the requested offset, then holds. No saved home or return-to-start is
required. It fails if commands are accepted without sufficient encoder-observed
motion, final feedback is stale, the motion envelope is exceeded, or an uncommanded
axis drifts more than one degree. Add `--require-return` only when an out-and-back
test and strict return check are wanted. JSON reports the actual final offset,
requested final target, and whether return was required/observed; it distinguishes
simulation from hardware and does not claim verified physical standstill.

For a hardware-free test, use `follower --robot b601-rs --simulate --room b601-bench`
and `bench --simulate --room b601-bench`. A simulated source cannot activate a
physical B601 follower. Arbitrary seven-joint sources must implement the model
handshake and start within one degree of the measured pose.

B601 startup never calibrates, changes zeros, clears faults, restores torque caps,
or reconfigures CAN. It requires an already-UP healthy 1-Mbps normal-mode interface,
refuses existing CAN receivers, and seeds a legal hold near the measured pose
before enabling motors, including the bounded boundary corrections above.
Per-joint dispatch checks retain the original command deadline. Driver/metadata
mismatches fail closed. Targets must simultaneously satisfy the native command
limits, the delta from fresh measured positions, and the rate from the last sent
target. `--max-speed` is **not guaranteed mechanical velocity**.
Physical runs are deliberately limited to 3–30 seconds and at most 30 Hz, with
relative targets <= 1 degree and target step rate <= 5 degrees/second.
No valid positive cached temperature, or a cached motor temperature >= 60 C,
refuses startup/new targets. This conservative short-bench gate is not a continuous
thermal controller, and it does not restore or increase existing torque caps.

Each B601 command and hold acquires fresh feedback once (seven positions plus
gripper velocity), validates the entire target set, then uses LeRobot's per-joint
dispatcher and gripper controller. It does not repeat those serial reads in the
SDK's outer action loop or reuse observations across commands. Pending commands
are consumed before the separate telemetry acquisition. The 150-ms operation
freshness guard, original permits and follower watchdog are unchanged.

**Exit is best-effort:** default `--on-exit hold` attempts a measured hold and closes
transport without explicitly disabling torque; it does not verify torque state,
maintain feedback or prove indefinite safe holding. `--on-exit torque-off` may let the arm fall. The driver can
overrun a deadline inside a blocking SDK call. Support, physical cutoff, thermal
management and an exclusive supervised test window remain necessary.

## Development

```bash
uv sync --locked --extra dev
uv run --no-sync pytest -q
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
```

### Verify the real drivers too, without moving hardware

Run the software and SO driver suite, including the pinned LeRobot dependency:

```bash
uv run --locked --extra dev --extra hardware pytest -q -m 'not b601_api'
```

The default suite excludes `hardware_api` and `b601_api` tests so simulation development does
not require PyTorch. To run only the installed-driver checks, use
`uv run --locked --extra dev --extra hardware pytest -q -m hardware_api`.
These checks fail, rather than skip, if LeRobot is missing or incompatible.
They use temporary calibration files and forbid opening serial ports.
In the isolated B601 environment above, run
`uv run --project examples/b601-sdk --no-sync pytest -q -m b601_api` to exercise the installed B601
driver with a fake MotorBridge. These tests forbid real controller construction,
motor zeroing and fault clearing. Neither driver suite opens physical hardware.

| Check | Evidence |
| --- | --- |
| Local control, both SO-100 and SO-101 | Real authenticated WebSocket negotiation and DTLS WebRTC data channels; all six joints reach two distinct leader poses; relative-target steps stay bounded. |
| Fail-stop behavior | While controlling a simulated arm, close the real data channel, shut down signaling, silence leader commands, or inject invalid packets. Both endpoints exit, the follower attempts hold, and no later commands are applied. |
| LeRobot 0.6.1 compatibility | Actual leader/follower constructors, calibration loading, normalized joint schema, action methods, relative-target limiter, hold write, and torque-on/off disconnect policies. Only the motor bus is replaced with an in-memory implementation. Both real drivers also run together over WebRTC. |
| CLI lifecycle | Separate signaling, leader, and follower processes establish control in the finite demo; startup failures and cleanup are bounded. |

`.github/workflows/verify.yml` runs the software suite on Linux and macOS and
the installed SO/B601 driver checks in separate macOS jobs for pushes and pull requests. Each job
uploads its JUnit results. This is not evidence of serial communication,
physical motion, calibration accuracy, real-time deadlines, or NAT traversal.

### Physical and public-network acceptance (manual)

Do not call a deployment hardware-verified until these checks have been performed
on its actual arms and network. For SO, follow [Set up physical SO arms](#set-up-physical-so-arms)
first; clear the workspace, support the unloaded follower, keep the physical
power cutoff within reach, and use conservative relative-target limits.

1. On two computers, run both endpoints with `--simulate` using the deployment's
   signaling URL and ICE settings. Confirm the follower's applied count keeps
   increasing, then stop either endpoint and confirm the other exits.
2. For internet use, repeat across the intended networks. Separately test a
   restrictive network that requires TURN and confirm relay traffic at the TURN
   server: merely supplying `--turn` does not prove that a relay was used.
3. For SO, after local calibration, start the physical follower with explicit `--arm`
   consent and the physical leader, using the commands above. Never substitute
   the synthetic leader here. Move each of the six joints slowly in both
   directions and confirm the correct follower joint tracks it without a jump
   or growing backlog. An increasing applied count alone does not prove motion.
4. Stop the leader, then repeat with a network interruption. Confirm the follower
   ends the session, attempts hold, and cannot resume until both endpoints restart.
   Support the follower before any torque-off test; it can fall.
5. Record the commit, arm IDs, host/driver versions, network/relay path, limits,
   observed motion, and stop behavior. Simulation and driver-API results must
   remain labeled separately from these physical observations.

Source layout:

| File | Purpose |
| --- | --- |
| `src/lerobot_webrtc/arms.py` | LeRobot adapter, simulator, serialized hardware worker |
| `src/lerobot_webrtc/models.py` | Explicit SO/B601 joint schemas, units and bounds |
| `src/lerobot_webrtc/b601.py` | Guarded B601-RS driver lifecycle and exclusive CAN lease |
| `src/lerobot_webrtc/bench.py` | Bounded network-only source with encoder-observed verdict |
| `src/lerobot_webrtc/protocol.py` | Joint schema, expiring permits, latest-only mailbox |
| `src/lerobot_webrtc/peer.py` | aiortc connection, control data channel, optional video negotiation |
| `src/lerobot_webrtc/video.py` | Video settings and SDP/codec policy |
| `src/lerobot_webrtc/video_sources.py` | Synthetic frames and independent LeRobot camera capture |
| `src/lerobot_webrtc/video_viewer.py` | Authenticated loopback HTTP setup and native WebRTC browser video |
| `src/lerobot_webrtc/signaling.py` | Authenticated WebSocket SDP relay |
| `src/lerobot_webrtc/runtime.py` | Leader/follower loops and shutdown |
| `src/lerobot_webrtc/native_host.py` | Separate native Robot host, measured telemetry, correlated dispatched targets |
| `src/lerobot_webrtc/native_client.py` | Synchronous observation-bound client with an owned network loop |
| `src/lerobot_webrtc/native_options.py` | Shared native config; credential environment names, not secrets |
| `src/lerobot_webrtc/video_receiver.py` | Headless decoded video for native RGB observations |
| `src/lerobot_robot_webrtc/` | Optional LeRobot Robot config/adapter and virtual camera mapping |
| `src/lerobot_webrtc/cli.py` | Server, native host, standalone endpoint, and calibration commands |

Upstream references:
[LeRobot API examples](https://huggingface.co/docs/lerobot/en/il_robots),
[SO follower implementation](https://github.com/huggingface/lerobot/tree/v0.6.1/src/lerobot/robots/so_follower),
[SO leader implementation](https://github.com/huggingface/lerobot/tree/v0.6.1/src/lerobot/teleoperators/so_leader),
[aiortc](https://aiortc.readthedocs.io/).
