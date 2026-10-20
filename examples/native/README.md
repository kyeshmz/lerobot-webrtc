# Native LeRobot Robot over WebRTC

Use LeRobot's **normal `lerobot-teleoperate` command** with a local SO leader and
a remote follower. The optional `lerobot_robot_webrtc` package implements the
SDK's `Robot` API; the remote computer runs `lerobot-webrtc host`. This is not a
remote `Teleoperator` plugin, and does not replace the standalone
[`follower` / `leader` workflow](../../docs/usage.md#3-start-the-follower-then-the-leader).

```text
Operator computer                         Robot computer
lerobot-teleoperate                       lerobot-webrtc host
local SO leader → WebRTCRobot ── WebRTC ── guarded LeRobot follower
                   ▲      ◀── measured positions + optional RGB video
                   └──── authenticated SDP signaling ────┘
```

Start with SO-101 or SO-100. Native B601 is an [advanced path](#b601-rs-advanced),
not a way to map a six-joint SO leader onto seven joints. Nothing in this guide
authorizes physical activation or qualifies an arm as safe.

## Install on both computers

Use separate **Python 3.12** environments. On **each** computer:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

On the **operator**, install both the SDK commands and bridge in one step:

```bash
python -m pip install 'lerobot-webrtc[native] @ git+https://github.com/kyeshmz/lerobot-webrtc.git'
```

This selects `lerobot[core_scripts,feetech]==0.6.1`: LeRobot's command, dataset,
and leader dependencies **plus this bridge**. The bridge is not published to PyPI;
do not substitute a bare `pip install lerobot-webrtc`.

On the **SO robot host**:

```bash
python -m pip install 'lerobot-webrtc[hardware] @ git+https://github.com/kyeshmz/lerobot-webrtc.git'
```

This selects `lerobot[feetech]==0.6.1` plus the bridge. A separate signaling-only
machine needs the base Git install, without SDK extras.

### Checkout / uv equivalent

Clone the repository on each computer:

```bash
git clone https://github.com/kyeshmz/lerobot-webrtc.git
cd lerobot-webrtc
```

Operator:

```bash
uv sync --locked --python 3.12 --extra native
source .venv/bin/activate
```

SO host:

```bash
uv sync --locked --python 3.12 --extra hardware
source .venv/bin/activate
```

Keep each machine on the same reviewed bridge revision. The native dependency
track resolves PyAV 15 (`av==15.1.0` in the root lock), compatible with both the
released SDK's constraint and aiortc. Do not force a different PyAV major over
the lock. See [compatibility](../../docs/compatibility.md).

**Do not install `native` or `hardware` over a prepared B601/Jetson SDK.** Those
extras select the released SO SDK, not the separately reviewed B601 source SDK.

## Local setup and calibration first

Complete LeRobot's [SO motor setup and calibration](../../docs/usage.md#set-up-physical-so-arms)
before network operation. These are deliberate, offline hardware operations, not
part of connecting the proxy. For example, after motor setup, with arms supported:

Operator computer:

```bash
lerobot-webrtc calibrate --role leader --robot so101 \
  --port /dev/ttyACM0 --id my_leader
```

Robot computer:

```bash
lerobot-webrtc calibrate --role follower --robot so101 \
  --port /dev/ttyACM0 --id my_follower
```

Replace ports with those on each computer (`/dev/tty.usbmodem...` on macOS).
Reuse the **same physical arm IDs, user account, and calibration cache** below.
The host refuses missing/mismatched calibration; the proxy cannot calibrate
remote hardware. Its `is_calibrated` property describes only the transport's
lack of local calibration, not proof that the physical arm is ready.

Mount/support the unloaded follower, clear the sweep, arrange an independent
physical cutoff, and place the leader in a suitable initial pose. Connecting
hardware can configure/enable motors. `--arm` is startup consent, **not a
hold-to-run dead-man**. Read the [full safety limits](../../docs/usage.md#safety-behavior-and-limits).

## Three-terminal SO-101 teleoperation

### Shared token

Generate a fresh session secret once, for example in a private terminal:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Distribute it only through your secure channel. In **all three terminals**,
activate that computer's environment and read the same value without putting
the secret in shell history:

```bash
source .venv/bin/activate
export LEROBOT_WEBRTC_TOKEN="$(python -c 'import getpass; print(getpass.getpass("Shared session token: "))')"
```

Do not use shell tracing, commit credentials, put them in URLs, or supply raw
secrets in LeRobot configs. The native config stores **environment variable
names**, resolved at connection time. Tokens must be at least 16 printable ASCII
characters; the generator above satisfies that requirement.

### Terminal 1: robot computer, signaling

For a **trusted LAN only**:

```bash
lerobot-webrtc serve --host 0.0.0.0 --port 8080
```

The default bind without `--host` is loopback. Binding all interfaces makes the
server reachable; it does not add TLS or make public exposure safe. Plain
`ws://` exposes the Authorization credential to the network. On untrusted
networks, use restricted **WSS** signaling behind a TLS proxy. The server carries
SDP/ICE only, not joint commands or video.

### Terminal 2: robot computer, native host

```bash
lerobot-webrtc host --robot so101 \
  --port /dev/ttyACM0 --id my_follower \
  --signaling ws://127.0.0.1:8080 --room native --arm
```

This runs the follower driver locally on the robot computer. Defaults include
30 Hz, `--timeout 0.3`, `--max-relative-target 5`, and `--on-exit hold`.
Five normalized units per write is **not** a degrees/second or collision limit.
Choose conservative limits for your physical setup.

### Terminal 3: operator computer, normal LeRobot command

```bash
lerobot-teleoperate \
  --robot.discover_packages_path=lerobot_robot_webrtc \
  --robot.type=webrtc --robot.model=so101 \
  --robot.signaling=ws://ROBOT_HOST:8080 --robot.room=native \
  --robot.remote_id=my_follower --robot.allow_physical=true \
  --robot.id=remote_so101 \
  --teleop.type=so101_leader --teleop.port=/dev/ttyACM0 \
  --teleop.id=my_leader --teleop.use_degrees=false \
  --fps=30 --display_data=false
```

Replace `ROBOT_HOST` with the robot computer's reachable address. Do not use
`127.0.0.1` here unless both programs really run on the same computer.

- **Discovery is required in both reviewed SDKs.** The distribution is named
  `lerobot-webrtc`. SDK automatic discovery checks distribution names, so shipping
  the `lerobot_robot_webrtc` Python namespace alone is not enough. Always pass
  `--robot.discover_packages_path=lerobot_robot_webrtc` before selecting `webrtc`.
- **`--teleop.use_degrees=false` is essential.** The SDK's SO leader defaults to
  degrees, but this bridge's SO schema is normalized: body `[-100, 100]`, gripper
  `[0, 100]`. Merely selecting `so101_leader` does not change those units.
- `robot.remote_id` verifies the remote host's **physical calibration ID**.
  `robot.id` names the local transport proxy. The leader still uses its own
  locally calibrated `teleop.id`.
- Physical operation requires **both** host `--arm` and client
  `--robot.allow_physical=true`. Without the latter, the client rejects a
  physical host before it connects its motors.
- Native `host` uses the `lerobot-robot-v1` data-channel protocol. Standalone
  `follower` is intentionally incompatible, preventing a wrong-mode connection.

For **SO-100**, change host `--robot so101` to `--robot so100`, client
`--robot.model=so101` to `--robot.model=so100`, and
`--teleop.type=so101_leader` to `--teleop.type=so100_leader`. Keep normalized
units, use each arm's real calibration ID, and recalibrate only through its
proper local workflow when needed.

Ctrl+C or a failure ends the current session. Restart/rearm **both endpoints**
for another run; leave signaling running only if you still need it.

## Camera observations

Native video is **one WebRTC hop from host to SDK**, decoded headlessly into RGB
observations. There is no browser, local viewer service, audio, or browser arm
control on this path. The existing [standalone browser viewer](../video/README.md)
remains a separate, two-hop workflow.

Append to the host command for a synthetic source:

```bash
--test-video --video-width 320 --video-height 240 --video-fps 10 --video-codec vp8
```

Append to the LeRobot command:

```bash
--robot.camera_name=front --robot.video_width=320 --robot.video_height=240 \
  --robot.video_fps=10 --robot.video_codec=vp8
```

Both ends must opt in and match width, height, and FPS; codec negotiation must
also be compatible. To use a **real camera**, explicitly replace `--test-video`
with `--camera-index 0`. Only that opt-in opens capture. Even `--simulate
--camera-index 0` opens a real camera: for hardware-free sources use
`--simulate --test-video`. Real capture requires the prepared SDK, a supported
capture mode, and optionally `--camera-fourcc MJPG`; see [video settings](../../docs/usage.md#video-settings).

The image feature is `front: (240, 320, 3)` in **HWC RGB**, converted/copied on
the SDK caller's thread. `robot.cameras` maps `front` to a virtual camera view
with width, height, FPS, connection status, `read()`, and `async_read()`; it
shares the Robot connection rather than opening another device. Without
`camera_name`, `cameras` is empty and no image feature is advertised. Missing
SDK support, absent video, stale frames, or incompatible geometry is an error,
not a fabricated image. Video reads do **not** renew motion permits.

## Single-episode teleop recording

The **SDK**, not this bridge, writes LeRobot datasets. Once teleoperation is
working, restart/rearm the host and replace the operator command with this
initial **single-episode, joint-only** recipe:

```bash
lerobot-record \
  --robot.discover_packages_path=lerobot_robot_webrtc \
  --robot.type=webrtc --robot.model=so101 \
  --robot.signaling=ws://ROBOT_HOST:8080 --robot.room=native \
  --robot.remote_id=my_follower --robot.allow_physical=true \
  --robot.id=remote_so101 \
  --teleop.type=so101_leader --teleop.port=/dev/ttyACM0 \
  --teleop.id=my_leader --teleop.use_degrees=false \
  --dataset.repo_id=your-user/so101-webrtc-teleop \
  --dataset.single_task="Supervised unloaded SO-101 teleoperation" \
  --dataset.fps=30 --dataset.num_episodes=1 --dataset.episode_time_s=10 \
  --dataset.video=false --dataset.push_to_hub=false \
  --display_data=false --play_sounds=false
```

Choose your own appropriate `repo_id` and accurate `single_task`. Do not use the
reserved `eval_` dataset-name prefix for `lerobot-record`. For images, add the
matching host/client [camera settings](#camera-observations) and change
`--dataset.video=true`; encoding/writes add load and are subject to the same
cadence limits. SDK dataset FPS is separate from the camera's source FPS.

For H.264 MP4 dataset files, the tested SDK encoder settings are:

```bash
--dataset.video=true --dataset.rgb_encoder.vcodec=h264 \
  --dataset.rgb_encoder.preset=ultrafast --dataset.rgb_encoder.crf=18 \
  --dataset.encoder_threads=1 --dataset.video_backend=pyav
```

These control **dataset files**, not the WebRTC network codec
(`--robot.video_codec`) or the camera capture FOURCC (`--camera-fourcc`). The
native acceptance tests use synthetic video and a temporary local dataset; they
do not qualify a physical camera, multi-episode timing, or arbitrary policies.

**Action-label caveat, in both v0.6.1 and the pinned B601 SDK:** the recorder
assigns `robot.send_action()`'s returned value but does not use it for the stored
action frame. It records **processed requested teleop actions**, not the host's
actual dispatched/clamped targets, despite an upstream comment suggesting
otherwise. The plugin returns the correlated actual target correctly; that
does not make the unmodified SDK recorder applied-action ground truth. Measured
joint observations are separate from both requested and dispatched targets.

Metadata uses `robot_type="webrtc"`, not `so101_follower` or `so100_follower`.
Do not assume that an arbitrary physical-SO dataset/checkpoint automatically
matches the proxy's metadata, feature names/order, units, or camera shape.

## Cadence, STOP, and rollout limits

The native host has a **5-second first-command budget** after guarded hardware
connection and, after the first successful action, a default **300 ms active
watchdog**. At each SDK `get_observation()`, the client pins that measured
sample's follower-issued permit **before** camera conversion, teleop reads,
processors, or policy computation. `send_action()` consumes that permit; newer
background telemetry cannot bless an action computed from an old observation.
There is one outstanding action, no motion queue/retransmission, no background
hold keepalive, and no automatic reconnect/rearm.

SDK initialization, compilation, dataset saves/finalization, interactive pauses,
or slow inference can exceed these budgets and terminally **STOP**. A software
STOP triggers best-effort host hold/disconnect; it is not proof of physical
standstill. A late packet cannot revive a stopped session. Investigate the delay
and restart/rearm **both endpoints**; do not hide it by replaying a previous
target, adding fake holds, or arbitrarily increasing timeout budgets.

Start recording with **one episode**. A reset loop may continue sending fresh
teleop actions, but episode saves and pauses need not. There is **no transparent
multi-episode pause/resume guarantee**. Saving the final episode may itself outlast
the watchdog; inspect SDK output and saved data rather than assuming a clean
motion session or complete dataset merely because a file exists.

Policy deployment uses **`lerobot-rollout`**, not `lerobot-record --policy`.
It is an advanced, conditional integration: validate exact feature names/order,
units, camera shape, metadata, processors, startup time, inference latency, and
steady action cadence in simulation before considering physical use. Disable
the SDK's return motion with `--return_to_initial_position=false`. A typed
`Robot` API is **not qualification of every rollout strategy or checkpoint**.
DAgger's PAUSED behavior replays old targets, and other interactive/idle phases
can omit actions; these are not transparently supported by this freshness model.
The bridge must not suspend the watchdog to make them appear compatible.

## Native config and API

Native fields are **top-level Robot config fields**, e.g. `--robot.room=native`,
not a nested `--robot.remote.*` object. Host limits stay on the host CLI.

| Robot field | Default | Purpose |
| --- | --- | --- |
| `model` | `so101` | `so100`, `so101`, or `b601-rs`; fixed joint schema/units |
| `signaling`, `room` | `ws://127.0.0.1:8080`, `native` | Signaling base URL and room |
| `allow_physical`, `remote_id` | `false`, unset | Explicit physical permission and optional host ID match |
| `token_env` | `LEROBOT_WEBRTC_TOKEN` | Environment variable **name**, not token value |
| `stun`, `turn` | `[]`, unset | STUN URL list and optional TURN URL |
| `turn_username_env`, `turn_password_env` | `LEROBOT_TURN_USERNAME`, `LEROBOT_TURN_PASSWORD` | Credential environment variable **names** |
| `connect_timeout`, `request_timeout`, `observation_timeout` | `45`, `2`, `0.5` seconds | Client setup/request/local observation bounds; do not extend a host permit/watchdog |
| `camera_name` | unset | Optional single RGB observation key, e.g. `front` |
| `video_width`, `video_height`, `video_fps`, `video_codec` | `640`, `480`, `15`, `auto` | Must agree with host source; codec `auto`, `vp8`, or `h264` |
| `video_timeout` | `3` seconds | Received-frame age/idle bound, not motion freshness |

Use the same ICE servers on both peers; host `--stun` / `--turn` correspond to
client `--robot.stun='["stun:host:3478"]'` / `--robot.turn=turn:host:3478`.
TURN credentials remain in the referenced environment variables. See
[NAT/TURN syntax and deployment](../../docs/usage.md#across-nat--over-the-internet).

With the native extra installed, features are available **before connection**:

```python
from lerobot_robot_webrtc import WebRTCRobot, WebRTCRobotConfig

robot = WebRTCRobot(WebRTCRobotConfig(model="so101", id="remote_so101"))
print(robot.action_features)  # six named .pos float fields
print(robot.observation_features)  # measured joint fields; optional HWC image
print(robot.cameras)  # empty without camera_name
```

Construction/feature inspection starts no network thread or hardware I/O.
Connecting is explicit. Observation/action calls must share one controlling
thread; each action requires a fresh observation. `send_action()` returns the
correlated driver's dispatched/clamped target, not measured feedback or an echo
of the request. Proxy `calibrate()` refuses remote calibration; `configure()`
does not mutate remote motors. A disconnected proxy does not auto-connect from
reads or writes.

## B601-RS (advanced)

The plugin/core supports `model="b601-rs"`: seven degree-valued `.pos` joints,
including wrist yaw and an angular gripper. That schema does **not** qualify
normal SO teleoperation, arbitrary policies, or unbounded native hardware runs.

Only use an already commissioned, separately reviewed B601 source SDK host at
`ca69a2068462a37f7cdcb74180927a2f863d2bf7` with MotorBridge 0.5.6. Preserve the
prepared Jetson SDK; do not add the SO `native` / `hardware` extras or resync a
shared environment to try this. See the [separate environment](../b601-sdk/README.md)
and [full B601 guard/ownership guide](../../docs/usage.md#b601-rs-opt-in-supervised-bench).

Native `host --robot b601-rs` retains **all** physical gates: explicit `--arm`,
an agreed absolute `--can-lock`, a healthy already-UP 1-Mbps SocketCAN interface,
`--duration` **3–30 seconds**, at most 30 Hz, relative targets at most **1°**, and
target step rate at most **5°/s**. Defaults are not a ready-made physical B601
command: explicitly choose its required bounds. The first complete target must
align within **1° of every measured joint**; all later targets retain native
position, measured-relative, rate, temperature, and per-joint deadline guards.
No motor zero, fault clearing, CAN reconfiguration, or SO-to-B601 mapping is added.
The standalone `bench` command is a different protocol/source, not a native
Robot client; do not connect it to `host`.

## Compatibility and evidence

Both reviewed SDKs expose the relevant `Robot` API: released v0.6.1 for SO and
the separately pinned `ca69a2068462a37f7cdcb74180927a2f863d2bf7` source snapshot
for B601. SDK-marked adapter/API tests use fake clients/devices and simulated
workflows; they are **not physical native teleoperation qualification**. See
[dependency tracks](../../docs/compatibility.md) and keep actual test-run results
separate from hardware claims.

Pinned upstream sources for the behavior above:

- [Robot API, v0.6.1](https://github.com/huggingface/lerobot/blob/v0.6.1/src/lerobot/robots/robot.py)
  and [B601 snapshot API](https://github.com/huggingface/lerobot/blob/ca69a2068462a37f7cdcb74180927a2f863d2bf7/src/lerobot/robots/robot.py).
- [Automatic discovery/factory](https://github.com/huggingface/lerobot/blob/v0.6.1/src/lerobot/utils/import_utils.py)
  and [explicit discovery parser](https://github.com/huggingface/lerobot/blob/v0.6.1/src/lerobot/configs/parser.py).
- [Teleoperate loop](https://github.com/huggingface/lerobot/blob/v0.6.1/src/lerobot/scripts/lerobot_teleoperate.py),
  [record loop](https://github.com/huggingface/lerobot/blob/v0.6.1/src/lerobot/scripts/lerobot_record.py),
  and [B601 snapshot record loop](https://github.com/huggingface/lerobot/blob/ca69a2068462a37f7cdcb74180927a2f863d2bf7/src/lerobot/scripts/lerobot_record.py).
- [Rollout command/strategies](https://github.com/huggingface/lerobot/blob/v0.6.1/src/lerobot/scripts/lerobot_rollout.py).
