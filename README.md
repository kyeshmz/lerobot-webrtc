<h1 align="center">LeRobot WebRTC</h1>

<p align="center">
  <img src="assets/lerobot-webrtc-banner.svg" alt="LeRobot × WebRTC — a Python companion for Hugging Face LeRobot" width="1280">
</p>

<div align="center">

[![Verify](https://github.com/kyeshmz/lerobot-webrtc/actions/workflows/verify.yml/badge.svg?branch=main)](https://github.com/kyeshmz/lerobot-webrtc/actions/workflows/verify.yml)
[![Packages](https://github.com/kyeshmz/lerobot-webrtc/actions/workflows/packages.yml/badge.svg?branch=main)](https://github.com/kyeshmz/lerobot-webrtc/actions/workflows/packages.yml)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://docs.python.org/3.12/)
[![Ruff](https://img.shields.io/badge/Code%20style-Ruff-D7FF64?logo=ruff&logoColor=black)](https://docs.astral.sh/ruff/)

</div>

**LeRobot WebRTC** connects a robot on one computer to an operator on another
over encrypted WebRTC. Use normal [🤗 LeRobot](https://github.com/huggingface/lerobot)
commands through the optional **native `Robot` plugin**, or run the standalone
leader/follower CLI. Video is off by default.

This is an unofficial LeRobot companion. It provides a `Robot` plugin, **not a
`Teleoperator` plugin**. LeRobot owns dataset recording and policy execution;
the bridge supplies remote observations/actions with strict freshness limits.
There is no browser arm control, audio, force feedback, or automatic arm mapping.

## Choose a mode

| Mode | Use it when | Commands / guide |
| --- | --- | --- |
| **Native SDK Robot** | **Recommended when you want normal LeRobot commands**: local SO leader → remote robot; optional camera observations and SDK recording | `lerobot-webrtc host` + `lerobot-teleoperate`; [native guide](examples/native/README.md) |
| Standalone CLI | You want the bridge's own leader/follower loop and optional receive-only browser video | `lerobot-webrtc follower` + `leader`; [operating guide](docs/usage.md) |
| Hardware-free demo | You want to try real local WebRTC without LeRobot, PyTorch, motors, or cameras | `examples/demo.py`; [demo below](#hardware-free-demo) |

Native `host` and standalone `follower` use **different data-channel protocols**.
Do not point the native plugin at `follower`, even with the same room name.

## Install LeRobot **and** the bridge

For the SO workflow, create an **isolated Python 3.12 environment on each computer**:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

**Operator computer** (LeRobot's commands, Feetech leader driver, and this bridge):

```bash
python -m pip install 'lerobot-webrtc[native] @ git+https://github.com/kyeshmz/lerobot-webrtc.git'
```

**Robot computer** (Feetech follower driver and this bridge):

```bash
python -m pip install 'lerobot-webrtc[hardware] @ git+https://github.com/kyeshmz/lerobot-webrtc.git'
```

The operator's `native` extra installs **released
`lerobot[core_scripts,feetech]==0.6.1` plus `lerobot-webrtc`**. The robot's
`hardware` extra installs `lerobot[feetech]==0.6.1` plus the bridge. This project
is **not published to PyPI**; use the Git URL above or a checkout, not a bare
`pip install lerobot-webrtc`.

Prefer [uv](https://docs.astral.sh/uv/)? Clone this repository on each computer,
then use `uv sync --locked --python 3.12 --extra native` on the operator and
`uv sync --locked --python 3.12 --extra hardware` on the robot. Activate each
`.venv` before running the commands below. See [dependency tracks](docs/compatibility.md).

> [!WARNING]
> These SO extras are **not for a prepared B601/Jetson SDK**. Do not install
> `native` or `hardware` over it: they select a different LeRobot version.
> B601 uses a [separate source-pinned environment](examples/b601-sdk/README.md).

## Native SDK teleoperation: three terminals

First complete local motor setup and [offline SO calibration](docs/usage.md#set-up-physical-so-arms).
Use the **same physical arm IDs, user account, and calibration cache** at runtime.
Replace the ports and `ROBOT_HOST` below; each port is local to its own computer.
Mount/support the unloaded follower, clear its sweep, keep a physical cutoff
within reach, and place the leader in a suitable starting pose.

Load the **same fresh secret** into `LEROBOT_WEBRTC_TOKEN` in all three terminals
via a secure environment/prompt ([token setup](examples/native/README.md#shared-token)).
Do not put secret values in LeRobot flags or saved configs.

**1. Robot computer — signaling, trusted LAN only:**

```bash
lerobot-webrtc serve --host 0.0.0.0 --port 8080
```

Plain `ws://` exposes signaling credentials to the network. Use **WSS** and
restricted access on untrusted networks; [TURN may also be needed](docs/usage.md#across-nat--over-the-internet).

**2. Robot computer — native host:**

```bash
lerobot-webrtc host --robot so101 \
  --port /dev/ttyACM0 --id my_follower \
  --signaling ws://127.0.0.1:8080 --room native --arm
```

**3. Operator computer — normal LeRobot teleoperation:**

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

**Do not omit discovery or `--teleop.use_degrees=false`.** The distribution name
`lerobot-webrtc` is not auto-discovered by either supported SDK; the import
namespace alone is insufficient. SO uses normalized positions, whereas the
SDK leader defaults to degrees. `remote_id` checks the host's physical
calibration ID; `robot.id` identifies the local proxy, not the follower calibration.
For SO-100, change host `--robot`, client `--robot.model`, and `--teleop.type` to
`so100`, `so100`, and `so100_leader` respectively.

The host allows **5 seconds for the first command**, then defaults to a
**300 ms active watchdog**. SDK setup, saving, inference, or pauses can exceed
these budgets and terminally **STOP**. Restart/rearm **both endpoints**; there is
no action replay, automatic reconnect, or transparent multi-episode pause support.

Next: [single-episode SDK recording, camera observations, config, and rollout limits](examples/native/README.md).
The SDK recorder stores **processed requested teleop actions**, not the host's
returned clamped targets; do not treat these labels as applied-action ground truth.

### Optional native camera observations

Append to the **host** command (synthetic video, no camera):

```bash
--test-video --video-width 320 --video-height 240 --video-fps 10 --video-codec vp8
```

Append to the **LeRobot** command:

```bash
--robot.camera_name=front --robot.video_width=320 --robot.video_height=240 \
  --robot.video_fps=10 --robot.video_codec=vp8
```

The native path is **one WebRTC hop directly to RGB `Robot` observations**;
no browser is needed. To capture a real camera, explicitly replace `--test-video`
with `--camera-index 0` (and use a supported capture mode). Both ends must match
geometry/FPS. See [native video](examples/native/README.md#camera-observations).

## Hardware-free demo

With [uv](https://docs.astral.sh/uv/getting-started/installation/):

```bash
git clone https://github.com/kyeshmz/lerobot-webrtc.git
cd lerobot-webrtc
uv sync --locked --python 3.12
uv run --no-sync python examples/demo.py --duration 5
```

This finite POSIX demo starts separate signaling, simulated leader, and simulated
follower processes, establishes real local WebRTC, then cleans up. It needs no
SDK, serial ports, calibration, PyTorch, or cameras. Simulation verifies software
lifecycle, **not physical safety or internet/TURN traversal**. See also
[manual simulation](docs/usage.md#try-without-hardware) and
[synthetic browser video](examples/video/README.md).

## Robots, standalone control, and networking

| Bridge model | Physical LeRobot devices | Joint positions |
| --- | --- | --- |
| `so100` | `so100_leader` → `so100_follower` | Six normalized values |
| `so101` | `so101_leader` → `so101_follower` | Six normalized values |
| `b601-rs` | `rebot_b601_follower`, RS motor family | Seven angles in degrees |

SO body joints use `[-100, 100]`, gripper `[0, 100]`. B601 adds wrist yaw and an
angular gripper and requires RobStride on **Linux SocketCAN**. There is no
six-joint SO leader → seven-joint B601 mapping. B601 native hosting remains an
[advanced, bounded path](examples/native/README.md#b601-rs-advanced); the existing
[encoder-checked move-and-hold bench](docs/usage.md#b601-rs-opt-in-supervised-bench)
is separate and never calibrates/zeros the arm at runtime.

The [standalone SO recipe](docs/usage.md#3-start-the-follower-then-the-leader)
still uses `lerobot-webrtc follower` + `leader`. Its optional video path is
follower → Python leader → **second WebRTC hop** to a loopback-only browser.
Use `--test-video` or `--camera-index` on the follower and `--view-video [PORT]`
on the leader; open `http://127.0.0.1:8081`, authenticate as `viewer` with the
session token, and click **Connect** before robot signaling. The browser is
receive-only. See [video settings/lifecycle](docs/usage.md#optional-video).
**`--simulate` simulates arms, not cameras**; `bench` and `calibrate` are data-only.

Both modes use authenticated SDP signaling that carries **neither commands nor
video**, with direct encrypted WebRTC or optional TURN. Start with the
[examples index](examples/README.md): [local P2P](examples/local/README.md),
[Cloudflare Tunnel + TURN](examples/cloudflare/README.md),
[coturn](examples/coturn/README.md), or the
[LiveKit transport-only probe](examples/livekit/README.md).
Tunnel is not TURN, and TURN is not an SFU. Provider room/SFU URLs cannot replace
this bridge's signaling URL.

## Safety and Scope

- Mount/support the unloaded arm, clear its sweep, and keep an independent
  physical power cutoff within reach. Connection can configure and enable motors.
- `--arm` is startup consent, **not a hold-to-run dead-man switch**.
- Video frame failure/idle ends current control; standalone browser connection
  loss also ends its session. There is no automatic reconnect/resume. The
  browser is **not a safety-rated dead-man or guaranteed physical stop**.
  Video and control share CPU/network resources and have no real-time guarantees.
- Model/unit validation, follower-issued expiring permits, a latest-only mailbox,
  relative-target limits, and a local watchdog reject unsafe or stale software
  inputs. They are **not safety-rated collision, force, or real-time guarantees**.
- B601 adds CAN ownership, target-rate and per-joint deadline checks. Its 0.35°
  feedback margin does not widen commanded limits. Its guarded lifecycle differs
  deliberately from upstream's generic `connect()`.
- On exit, `hold` attempts a measured hold without explicitly disabling torque;
  it does not maintain feedback after transport closure. `torque-off` can let an
  unsupported arm fall. Neither policy verifies electrical torque state or
  guarantees physical standstill.

Read the [complete safety behavior and limits](docs/usage.md#safety-behavior-and-limits)
before hardware use. Do not use generic SDK `connect()` as a read-only probe.

## Development & Verification

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, SDK-specific checks,
package validation and pull request guidance.

```bash
uv sync --locked --extra dev
uv run --no-sync pytest -q
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
```

The default tests use simulated arms but real local signaling/WebRTC. Separate
`hardware_api` and `b601_api` suites exercise installed LeRobot drivers with fake
motor buses; `native_api` checks actual SDK workflows with simulated devices and
local datasets. None opens physical devices. See [driver verification](docs/usage.md#verify-the-real-drivers-too-without-moving-hardware)
for the separate dependency environments and commands. GitHub Actions runs these
checks; a green software run is not physical qualification.

A supervised standalone B601-RS trial also verified real Mac↔Jetson WebRTC shoulder-pan
movement: 36 applied commands, approximately 0.36° measured movement for a 0.5°
request, and a completed measured HOLD write. This was a one-axis move-and-hold
test, not accurate homing, full seven-axis qualification, or guaranteed standstill.

## Resources

- [Operating guide](docs/usage.md): installation details, calibration wrapper,
  signaling, STUN/TURN, safety, and manual acceptance checks.
- [Native SDK guide](examples/native/README.md): normal LeRobot commands,
  headless camera observations, recording, and workflow limits.
- [Comparison with other WebRTC integrations](COMPARISON.md).
- [Networking examples](examples/README.md) and [package/version policy](docs/compatibility.md).
- [LeRobot README](https://github.com/huggingface/lerobot#readme),
  [installation](https://huggingface.co/docs/lerobot/installation), and
  [hardware integration](https://huggingface.co/docs/lerobot/integrate_hardware).
- [SO driver source, v0.6.1](https://github.com/huggingface/lerobot/tree/v0.6.1/src/lerobot/robots/so_follower)
  and [B601 driver source, pinned revision](https://github.com/huggingface/lerobot/tree/ca69a2068462a37f7cdcb74180927a2f863d2bf7/src/lerobot/robots/rebot_b601_follower).
- [aiortc](https://aiortc.readthedocs.io/), [aiohttp](https://docs.aiohttp.org/),
  and [MotorBridge](https://github.com/motorbridge/motorbridge).
- [Cloudflare Realtime](https://developers.cloudflare.com/realtime/) and
  [LiveKit Python SDK](https://docs.livekit.io/reference/python/livekit/rtc/).

Maintained by [@kyeshmz](https://github.com/kyeshmz). [Issues and feedback](https://github.com/kyeshmz/lerobot-webrtc/issues)
and contributions are welcome. Run the relevant checks and label physical results
separately from simulation. Please acknowledge upstream LeRobot when
using its work; its [citation section](https://github.com/huggingface/lerobot#citation)
provides the recommended references.
