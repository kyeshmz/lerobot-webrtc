# LeRobot packages and version compatibility

## Follow the upstream package interfaces, not guessed compatibility

The SO adapter imports `SO100Leader` / `SO101Leader` and their configs from
`lerobot.teleoperators.so_leader`, and the matching followers/configs from
`lerobot.robots.so_follower`. It uses LeRobot calibration files and hardware
methods. The B601 adapter uses the native `rebot_b601_follower` config, joint
directions, limits and joint/gripper dispatcher, with the bridge's guarded I/O
and lifecycle around them. It is not a replacement servo implementation.

This distribution is named `lerobot-webrtc`. It is not an auto-discovered
`lerobot_robot_*` or `lerobot_teleoperator_*` plugin, and does not claim to make
`lerobot-record` use a network robot automatically.

## Tested dependency tracks

| Track | Package/source | Install into a separate project environment |
| --- | --- | --- |
| Simulation / signaling | No LeRobot, PyTorch or MotorBridge dependency | `uv sync --locked --extra dev` |
| SO-100 / SO-101 | Released `lerobot[feetech]==0.6.1` | `uv sync --locked --extra dev --extra hardware` |
| B601-RS | `lerobot[rebot]` at commit `ca69a2068462a37f7cdcb74180927a2f863d2bf7`, metadata version 0.6.2; `motorbridge==0.5.6` | `uv sync --locked --project examples/b601-sdk` |

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

On a prepared Jetson, do **not** sync a shared working SDK environment. A fresh
Python dependency lock does not install matching JetPack/CUDA drivers or qualify
a platform-specific PyTorch wheel. Follow LeRobot/platform installation guidance
and preserve the separately reviewed robot environment.

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
[pinned B601 guide](https://github.com/huggingface/lerobot/blob/ca69a2068462a37f7cdcb74180927a2f863d2bf7/docs/source/rebot_b601.mdx),
[upstream security/version-support policy](https://github.com/huggingface/lerobot/blob/main/SECURITY.md),
and [uv dependency conflicts](https://docs.astral.sh/uv/concepts/projects/config/#conflicting-dependencies).
