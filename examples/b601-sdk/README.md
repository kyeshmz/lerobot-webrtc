# Pinned B601 LeRobot SDK environment

This is a **separate environment/lockfile**, not another robot controller or a
published package. It installs this checkout with its `b601`/`dev` extras and the
official LeRobot `rebot` extra at the reviewed source revision. It never selects
the main project's SO-only `hardware` extra.

From the repository root:

```bash
uv sync --locked --project examples/b601-sdk
uv run --project examples/b601-sdk --no-sync pytest -q -m b601_api
```

The tests use the actual LeRobot classes with fake MotorBridge handles and forbid
real controller construction, zeroing, and fault clearing. They are not a physical
arm test. The environment defaults to `examples/b601-sdk/.venv`, separate from the
root `.venv` used for released SO support.

Use the same prepared environment to inspect bridge help without hardware access:

```bash
uv run --project examples/b601-sdk --no-sync lerobot-webrtc follower --help
```

For real hardware, follow the [B601 operating guide](../../docs/usage.md#b601-rs-opt-in-supervised-bench),
obtain exclusive CAN ownership, and use explicit activation/physical-stop readiness.
Do not sync this environment over another project's working Jetson SDK. Python
dependency resolution does not install compatible GPU/JetPack drivers.

See the [package/version policy](../../docs/compatibility.md) for why the upstream
0.6.2 metadata is not treated as an assumed PyPI release or stable SemVer promise.
