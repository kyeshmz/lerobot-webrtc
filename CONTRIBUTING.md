# Contributing to LeRobot WebRTC

Bug fixes, tests, documentation and networking examples are welcome. You do not
need a robot, provider account or cloud credentials to run the default checks.
For a new robot backend, provider transport or protocol change, open an
[issue](https://github.com/kyeshmz/lerobot-webrtc/issues) to discuss the scope first.

## Set up a development environment

Use Python **3.12** to match the SDK CI jobs, and install
[uv](https://docs.astral.sh/uv/getting-started/installation/). Package CI also tests
Python 3.13. Linux and macOS are covered; the subprocess demo requires POSIX
signal handling.

Clone the repository, or your fork if you do not have write access:

```bash
git clone https://github.com/kyeshmz/lerobot-webrtc.git
cd lerobot-webrtc
git switch -c my-change
uv sync --locked --extra dev
uv run --no-sync python examples/demo.py --duration 5
```

The finite demo runs separate signaling, leader and follower processes with
simulated arms and a real local WebRTC data channel. It does not open motors,
serial ports, CAN or cameras.

## Find the relevant code

- [`src/lerobot_webrtc/`](src/lerobot_webrtc/): CLI, signaling, peer transport,
  protocol, control lifecycle and robot adapters.
- [`tests/`](tests/): hardware-free transport tests and fake-bus SDK checks.
- [`examples/`](examples/README.md): local P2P, networking providers and the
  separately locked B601 SDK environment.
- [`docs/usage.md`](docs/usage.md): operation, setup and safety behavior.
- [`docs/compatibility.md`](docs/compatibility.md): dependency pins and version policy.

## Run the checks

From the repository root:

```bash
uv run --no-sync ruff format .
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync pytest -q
```

Add a regression test for behavior changes. Keep tests deterministic, finite and
independent of physical devices, public services and secrets. The default pytest
selection excludes `hardware_api` and `b601_api`; those markers test installed SDK
interfaces, not physical arms.

Video tests use synthetic frames, fake camera handles, and local WebRTC peers;
they must never open a physical camera or microphone. Camera capture belongs to
its own worker, not the motor executor. Keep browser signaling authenticated and
loopback-only, and preserve arm-first cleanup when media fails.

### SDK-specific changes

For SO-100/SO-101 changes, install the released SDK track and run its fake-bus tests:

```bash
uv sync --locked --extra dev --extra hardware
uv run --no-sync pytest -q -m hardware_api
```

For B601 changes, use the separate project and lockfile:

```bash
uv sync --locked --project examples/b601-sdk
uv run --project examples/b601-sdk --no-sync pytest -q -m 'not hardware_api'
```

Do not combine these SDK tracks in one environment or sync over a working Jetson
installation. The B601 job runs its native fake-CAN checks and the compatible
software suite. Read the [SDK environment guide](examples/b601-sdk/README.md) and
[compatibility policy](docs/compatibility.md) before changing upstream pins.

### Dependencies and packaging

Keep dependency edits scoped to the change. Regenerate the affected lock with uv;
do not hand-edit resolved versions or checksums. Do not replace a pinned SDK with
a moving branch or assume upstream pre-1.0 versions are interchangeable.

Check lock consistency and build the distributions:

```bash
uv lock --check
uv lock --check --project examples/b601-sdk
uv lock --check --script examples/livekit/probe.py
uv build --no-sources
uvx --from twine==7.0.0 twine check --strict dist/*
```

The [Verify workflow](.github/workflows/verify.yml) checks formatting, software
behavior and installed SDK APIs. The [Packages workflow](.github/workflows/packages.yml)
builds the wheel from the source archive, checks metadata, and pip-installs both
artifacts into fresh Python 3.12/3.13 environments on Linux and macOS. Its import,
CLI and simulated WebRTC checks run outside the checkout.

Neither workflow publishes to PyPI or deploys provider services. Version bumps,
tags and releases are separate decisions; follow the
[version policy](docs/compatibility.md#upstream-versions-semver-and-security).

## Preserve safety and honest test results

- Keep explicit hardware activation, authentication, model/unit validation,
  expiring command permits, bounds, watchdogs and STOP behavior intact. Reconnect
  must not silently resume motor control.
- Never use a real SDK connection as a read-only test: connecting can configure
  or enable motors. Tests must use simulation or fake device handles.
- Do not add automatic calibration, zeroing, fault clearing, CAN reconfiguration,
  public tunnels or cloud deployments to tests or examples.
- Physical testing is optional and requires a separately approved, supervised
  procedure. Follow the [operating and safety guide](docs/usage.md); a green CI
  run does not qualify hardware or guarantee physical standstill.
- Distinguish mocked checks, local end-to-end tests, provider connectivity and
  supervised hardware results. Report missing credentials or tools as blockers,
  not passes. Keep tokens, JWTs, API secrets and private device/network details
  out of commits, logs, screenshots and issue reports.

## Submit an issue or pull request

For a bug report, include the commit/version, OS, Python version, dependency track,
minimal reproduction, and expected versus actual behavior. State whether the run
used simulation, fake buses or real hardware, and redact sensitive details.

Keep pull requests focused and include:

- what changed and why;
- the commands you ran and their results, including anything not tested;
- updated tests, examples or documentation when behavior changes;
- compatibility and safety implications for protocol, driver or dependency changes.

Use draft pull requests for unfinished work. Do not include unrelated formatting
or dependency upgrades, and do not claim provider or physical validation from a
simulation-only run. Acknowledge upstream LeRobot when adapting its work.
