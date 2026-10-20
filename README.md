# LeRobot WebRTC

[![Verify](https://github.com/kyeshmz/lerobot-webrtc/actions/workflows/verify.yml/badge.svg?branch=main)](https://github.com/kyeshmz/lerobot-webrtc/actions/workflows/verify.yml)

**LeRobot WebRTC** transports joint-position actions between two computers using
LeRobot's hardware interfaces and an encrypted WebRTC data channel.

Use a physical SO-100/SO-101 **Teleoperator (leader)** to control a matching
**Robot (follower)** remotely. An opt-in reBot B601-RS follower also supports a
bounded, encoder-checked move-and-hold test.

This is a standalone companion to [🤗 LeRobot](https://github.com/huggingface/lerobot),
not an official LeRobot component or an auto-discovered Robot/Teleoperator plugin.
It does not provide camera streaming, dataset recording, policies, a browser UI,
force feedback, or automatic mapping between different arms.

## Quick Start

Requires **Python 3.12+**; development and CI use Python 3.12. Install
[uv](https://docs.astral.sh/uv/getting-started/installation/), then run a finite,
hardware-free demo:

```bash
git clone https://github.com/kyeshmz/lerobot-webrtc.git
cd lerobot-webrtc
uv sync --locked --extra dev
uv run --no-sync python examples/demo.py --duration 5
```

The demo starts separate signaling, simulated leader, and simulated follower
processes, establishes real local WebRTC control, then cleans them up. It needs
no serial ports, calibration, LeRobot hardware installation, or PyTorch. The
subprocess demo currently requires POSIX signal handling.

> [!IMPORTANT]
> Simulation verifies the transport and software lifecycle, not physical safety
> or internet/TURN traversal. For real hardware, complete local setup first and
> follow the [operating and safety guide](docs/usage.md).

## Robots & Control

The SO bridge follows LeRobot's familiar control interface:

```python
action = teleop_device.get_action()  # On the leader computer
# WebRTC transport and follower-side validation
robot.send_action(action)  # On the follower computer
```

The bridge owns connection, serialized hardware I/O, command freshness, and
shutdown. Do not run this snippet as a second controller alongside an active
bridge session.

| Bridge `--robot` | LeRobot device types | Position units | Driver dependency |
| --- | --- | --- | --- |
| `so100` | `so100_leader` → `so100_follower` | Five body joints `[-100, 100]`; gripper `[0, 100]` | `lerobot[feetech]==0.6.1` |
| `so101` | `so101_leader` → `so101_follower` | Five body joints `[-100, 100]`; gripper `[0, 100]` | `lerobot[feetech]==0.6.1` |
| `b601-rs` | `rebot_b601_follower`, `motor_family="rs"`, `can_adapter="socketcan"` | Seven joint angles in degrees, including `wrist_yaw` and the gripper | Pinned LeRobot source below; MotorBridge 0.5.6 |

For SO, both drivers explicitly use `use_degrees=False`; the gripper remains a
0–100 opening value. B601 uses its native degree convention, including negative
gripper angles. These schemas cannot be mixed. This bridge supports only the
RobStride B601 variant on **Linux SocketCAN**, not every platform or motor family
supported by upstream LeRobot.

LeRobot's `--robot.type` / `--teleop.type` flags and this bridge's `--robot` flag
are separate CLI namespaces. The names above show their correspondence; the
commands are not interchangeable.

### Installation and hardware setup

For physical SO arms, install the hardware extra on both arm computers:

```bash
uv sync --locked --extra dev --extra hardware
```

Follow LeRobot's [SO-101](https://huggingface.co/docs/lerobot/en/so101) or
[SO-100](https://huggingface.co/docs/lerobot/en/so100) guide for assembly, port
discovery, motor setup, and local calibration before starting network control.
LeRobot supplies `lerobot-find-port`, `lerobot-setup-motors`, and
`lerobot-calibrate`. The bridge also provides an SO calibration wrapper; reuse
the same arm IDs and calibration cache. See [physical SO setup](docs/usage.md#set-up-physical-so-arms).

For B601-RS, use a **separate prepared environment**. The reviewed driver is
LeRobot commit `ca69a2068462a37f7cdcb74180927a2f863d2bf7`, which reports version
0.6.2. Do not substitute an assumed PyPI release or install the SO-only hardware
extra as a B601 driver:

```bash
uv venv --python 3.12 .venv-b601
uv pip install --python .venv-b601/bin/python -e '.[b601,dev]' \
  'lerobot[rebot] @ git+https://github.com/huggingface/lerobot.git@ca69a2068462a37f7cdcb74180927a2f863d2bf7'
```

The upstream [reBot B601 guide](https://github.com/huggingface/lerobot/blob/ca69a2068462a37f7cdcb74180927a2f863d2bf7/docs/source/rebot_b601.mdx)
covers first-time setup and calibration. B601 calibration changes motor zeros;
this bridge assumes an already commissioned arm and **never calibrates or zeros
it at runtime**. Do not overwrite another project's working Jetson SDK or change
CAN configuration to start a test. See the [B601-RS operating guide](docs/usage.md#b601-rs-opt-in-supervised-bench).

### Network control

```text
Teleoperator computer                       Robot computer
leader.get_action()                         follower.send_action()
        └──── encrypted WebRTC data channel ────────┘
                   direct or through TURN

Both endpoints ── authenticated WebSocket ── signaling server
                       SDP/ICE only
```

Set the same `LEROBOT_WEBRTC_TOKEN` on the signaling server and both endpoints.
The signaling server does not carry joint commands. Use a trusted private
network or WSS; public internet connections may also need an authenticated TURN
server.

- [Three-terminal simulation](docs/usage.md#try-without-hardware)
- [SO leader/follower launch](docs/usage.md#3-start-the-follower-then-the-leader)
- [B601-RS move-and-hold](docs/usage.md#b601-rs-opt-in-supervised-bench)
- [STUN/TURN configuration](docs/usage.md#across-nat--over-the-internet)

Real followers require explicit `--arm` consent; a physical B601 bench also
requires `--execute`. The default bench moves relative to its measured current
pose and holds. Home/return-to-start is optional via `--require-return`.

## Safety and Scope

- Mount/support the unloaded arm, clear its sweep, and keep an independent
  physical power cutoff within reach. Connection can configure and enable motors.
- `--arm` is startup consent, **not a hold-to-run dead-man switch**.
- Model/unit validation, follower-issued expiring permits, a latest-only mailbox,
  relative-target limits, and a local watchdog reject unsafe or stale software
  inputs. They are **not safety-rated collision, force, or real-time guarantees**.
- B601 adds CAN ownership checks, native command bounds, target-rate admission,
  and per-joint deadline checks. Its 0.35° feedback margin does not widen commanded
  limits. It reuses LeRobot's joint/gripper dispatcher with one fresh acquisition
  per command; its guarded startup differs deliberately from upstream's generic
  `connect()` lifecycle.
- On exit, `hold` attempts a measured hold without explicitly disabling torque;
  it does not maintain feedback after transport closure. `torque-off` can let an
  unsupported arm fall. Neither policy verifies electrical torque state or
  guarantees physical standstill.

Read the [complete safety behavior and limits](docs/usage.md#safety-behavior-and-limits)
before hardware use. Do not use generic SDK `connect()` as a read-only probe.

## Development & Verification

```bash
uv sync --locked --extra dev
uv run --no-sync pytest -q
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
```

The default tests use simulated arms but real local signaling/WebRTC. Separate
`hardware_api` and `b601_api` suites exercise installed LeRobot drivers with fake
motor buses and forbid physical device access. See [driver verification](docs/usage.md#verify-the-real-drivers-too-without-moving-hardware)
for the separate dependency environments and commands. GitHub Actions runs these
checks; a green software run is not physical qualification.

A supervised B601-RS trial also verified real Mac↔Jetson WebRTC shoulder-pan
movement: 36 applied commands, approximately 0.36° measured movement for a 0.5°
request, and a completed measured HOLD write. This was a one-axis move-and-hold
test, not accurate homing, full seven-axis qualification, or guaranteed standstill.

## Resources

- [Operating guide](docs/usage.md): installation details, calibration wrapper,
  signaling, STUN/TURN, safety, and manual acceptance checks.
- [Comparison with other WebRTC integrations](COMPARISON.md).
- [LeRobot README](https://github.com/huggingface/lerobot#readme),
  [installation](https://huggingface.co/docs/lerobot/installation), and
  [hardware integration](https://huggingface.co/docs/lerobot/integrate_hardware).
- [SO driver source, v0.6.1](https://github.com/huggingface/lerobot/tree/v0.6.1/src/lerobot/robots/so_follower)
  and [B601 driver source, pinned revision](https://github.com/huggingface/lerobot/tree/ca69a2068462a37f7cdcb74180927a2f863d2bf7/src/lerobot/robots/rebot_b601_follower).
- [aiortc](https://aiortc.readthedocs.io/), [aiohttp](https://docs.aiohttp.org/),
  and [MotorBridge](https://github.com/motorbridge/motorbridge).

Contributions are welcome. Run the relevant software checks and keep physical
test results separate from simulation. Please acknowledge upstream LeRobot when
using its work; its [citation section](https://github.com/huggingface/lerobot#citation)
provides the recommended references.
