# LeRobot packages and version compatibility

## Follow the upstream package interfaces, not guessed compatibility

The SO adapter imports `SO100Leader` / `SO101Leader` and their configs from
`lerobot.teleoperators.so_leader`, and the matching followers/configs from
`lerobot.robots.so_follower`. It uses LeRobot calibration files and hardware
methods. The B601 adapter uses the native `rebot_b601_follower` config, joint
directions, limits and joint/gripper dispatcher, with the bridge's guarded I/O
and lifecycle around them. It is not a replacement servo implementation.

This distribution is named `lerobot-webrtc` and includes the optional native
**`Robot` plugin** `lerobot_robot_webrtc`; there is no `Teleoperator` plugin.
Both reviewed SDKs require explicit
`--robot.discover_packages_path=lerobot_robot_webrtc --robot.type=webrtc`.
Their automatic discovery checks **distribution names**: the Python namespace
alone does not make this distribution auto-discovered. The plugin connects to
`lerobot-webrtc host`, never the separate standalone `follower` protocol.

For installation of **both LeRobot and the bridge**, start with the
[README](../README.md#install-lerobot-and-the-bridge) or [native guide](../examples/native/README.md).
The bridge is **not published to PyPI**. Git installation of
`'lerobot-webrtc[native] @ git+https://github.com/kyeshmz/lerobot-webrtc.git'`
installs the released SDK commands plus the bridge on the operator;
`[hardware]` on that same Git requirement selects the SO host dependencies.
Use an isolated Python 3.12 environment on each computer.

## Tested dependency tracks

| Track | Package/source | Install into a separate project environment |
| --- | --- | --- |
| Simulation / signaling | No LeRobot, PyTorch or MotorBridge dependency | `uv sync --locked --extra dev` |
| Native SDK operator, SO-100 / SO-101 | Released `lerobot[core_scripts,feetech]==0.6.1` plus bridge/plugin | `uv sync --locked --python 3.12 --extra native` |
| SO-100 / SO-101 hardware host or standalone endpoints | Released `lerobot[feetech]==0.6.1` plus bridge | `uv sync --locked --python 3.12 --extra hardware` |
| B601-RS | `lerobot[rebot]` at commit `ca69a2068462a37f7cdcb74180927a2f863d2bf7`, metadata version 0.6.2; `motorbridge==0.5.6` | `uv sync --locked --project examples/b601-sdk` |

Add `--extra dev` for the root test tools, not for ordinary operation. The native
track needs the SDK's PyAV 15 constraint; the root lock resolves **`av==15.1.0`**,
which is also accepted by aiortc. Do not force PyAV 17 or ignore dependency
constraints when combining the native SDK and bridge.

The B601 snapshot is pinned by **commit**, not by assuming that its 0.6.2 metadata
means an available PyPI release. Its `rebot` extra is LeRobot's upstream hardware
extra; the bridge's small `b601` extra adds MotorBridge for an already prepared SDK.
The [B601 SDK example project](../examples/b601-sdk/README.md) provides the full
reviewed source checkout for development/CI in its own environment and lockfile.
It is not a direct-Git requirement in published bridge wheel metadata.

The root manifest selects the released SO SDK; the SDK example selects only the
B601 snapshot. Do not install one and silently replace it with the other after
syncing. The two driver CI jobs use independent manifests and fake motor buses.
Tests verify both locks and fail when their package APIs are missing/incompatible
instead of silently falling back to another robot driver.
The B601 CI job also runs the hardware-free transport/provider suite in that SDK's
resolved environment; it does not infer compatibility merely from a successful install.

Separate resolutions prevent the source checkout's platform-specific CUDA index
from leaking into the released SO/macOS track. The main lock keeps its PyPI
packages; the SDK example retains upstream's platform-specific source resolution.
Neither lock provisions GPU drivers. Prepared Jetson installations still need
separately reviewed platform-specific wheel compatibility.

The [Packages workflow](../.github/workflows/packages.yml) builds an sdist and a
wheel from that sdist, checks their metadata with Twine, and tests both artifacts
with pip in fresh Python 3.12/3.13 environments on Linux and macOS. Import, CLI and simulated
WebRTC checks run outside the checkout, without the optional robot SDKs. It also
checks the root, B601 and LiveKit script locks. This is validation only: it does
not publish to PyPI, deploy providers or access motors. The separate Verify jobs
exercise the installed SO/B601 SDK APIs with fake buses.

On a prepared Jetson, do **not** install the SO `native` / `hardware` extras over
its SDK or sync a shared working environment. A fresh
Python dependency lock does not install matching JetPack/CUDA drivers or qualify
a platform-specific PyTorch wheel. Follow LeRobot/platform installation guidance
and preserve the separately reviewed robot environment.

## Native API compatibility is not workflow qualification

The released SO SDK and pinned B601 snapshot share the relevant `Robot` API.
The adapter exposes joint/action features before connecting, measured joint
observations, an optional single HWC RGB image, a `cameras` proxy mapping, and
the correlated dispatched/clamped target from `send_action()`. Model `so100` /
`so101` uses normalized SO positions; a local SDK SO leader must explicitly use
`--teleop.use_degrees=false` (its SDK default is degrees). Model `b601-rs` uses
seven degree-valued joints; it adds no SO-to-B601 mapping.

| SDK workflow | Scope / limitation |
| --- | --- |
| `lerobot-teleoperate` | Native remote Robot plus local matching SO leader; begin at 30 Hz after offline calibration |
| `lerobot-record` | SDK-owned teleop dataset recording; start with one episode, not a promise of transparent saves/pauses between episodes |
| `lerobot-rollout` | Advanced/conditional: exact features, units, metadata, and startup/inference cadence must fit; use `--return_to_initial_position=false` |
| DAgger / interactive or idle strategies | Not transparently supported: paused DAgger replays targets; idle phases can stop sending actions |

**Neither SDK's recorder uses the returned clamped targets for action labels.**
Both store processed requested teleop actions, despite an upstream comment
suggesting applied actions are saved. Correct plugin return values do not fix
that recorder behavior. Dataset metadata is `robot_type="webrtc"`, not a
physical SO follower type, so arbitrary SO checkpoints do not automatically match.

Host startup allows 5 seconds for the first command, followed by a default
300 ms active watchdog. The client binds a host permit at SDK `get_observation()`
**before** computation. Setup, saving, inference, or pauses beyond these budgets
can terminally STOP. There is no motion replay, fake-hold keepalive, automatic
reconnect/rearm, or watchdog suspension. Restart/rearm both endpoints; do not
arbitrarily extend budgets to mask incompatible workflow phases.

Native physical B601 hosting retains the separate prepared SDK, 3–30-second
duration, CAN lock, relative/rate/native bounds, and first-target alignment
guards. SDK-marked API tests with fake clients/buses do not qualify native
physical control. See the [native guide](../examples/native/README.md) for
recording, camera, lifecycle, and advanced B601 details.

## Upstream versions, SemVer and security

Do not assume that every LeRobot patch/minor release, or a moving `main`, is
interchangeable with a tested SDK. LeRobot is still pre-1.0, and its
[security policy](https://github.com/huggingface/lerobot/blob/main/SECURITY.md)
explicitly describes a **rolling release**: security fixes are prioritized for
the latest `main`; older releases do not receive backports. That is not a stable
SemVer API-compatibility or long-term-support promise.

Our exact pins are compatibility/reproducibility choices, **not** a claim that
upstream supports their security indefinitely. Monitor upstream advisories and
revalidate pins when fixes or new releases appear. Neither `>=0.6` without an
upper bound nor `lerobot @ ...@main` is a substitute for testing this integration.

This project's version is independent of LeRobot's. For future releases:

- use three-part Python/PEP 440 versions in `pyproject.toml`, with matching `vX.Y.Z`
  Git tags when a release is deliberately made;
- while this package is `0.x`, use the minor component for intentional public
  CLI/protocol/default-behavior changes and the patch component for compatible fixes;
- explicitly label prereleases and document breaking behavior rather than inferring
  compatibility from an upstream version number;
- do not publish/tag a release merely because documentation or a dependency pin changed.

## Updating a supported SDK

1. Review the upstream release notes/source and change the explicit package pin
   or the source revision in `examples/b601-sdk/pyproject.toml`.
2. Regenerate the relevant `uv.lock` (root or example project); do not edit
   resolved versions/checksums by hand.
3. Run software checks, then each installed-driver suite in its own environment.
4. Verify model names, imports, action features, units, native bounds, calibration
   behavior, startup/cleanup semantics and dispatcher signatures.
5. Update this compatibility matrix. Keep fake-bus success separate from any
   newly authorized physical test; CI never qualifies or activates an arm.

Sources: [LeRobot installation](https://huggingface.co/docs/lerobot/installation),
[SO driver source](https://github.com/huggingface/lerobot/tree/v0.6.1/src/lerobot/robots/so_follower),
[Robot API](https://github.com/huggingface/lerobot/blob/v0.6.1/src/lerobot/robots/robot.py),
[distribution discovery](https://github.com/huggingface/lerobot/blob/v0.6.1/src/lerobot/utils/import_utils.py),
[explicit plugin parser](https://github.com/huggingface/lerobot/blob/v0.6.1/src/lerobot/configs/parser.py),
[record action labels](https://github.com/huggingface/lerobot/blob/v0.6.1/src/lerobot/scripts/lerobot_record.py),
[pinned B601 guide](https://github.com/huggingface/lerobot/blob/ca69a2068462a37f7cdcb74180927a2f863d2bf7/docs/source/rebot_b601.mdx),
[upstream security/version-support policy](https://github.com/huggingface/lerobot/blob/main/SECURITY.md),
and [uv dependency conflicts](https://docs.astral.sh/uv/concepts/projects/config/#conflicting-dependencies).
