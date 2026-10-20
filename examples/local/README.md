# Local P2P: start here

From the repository root:

```bash
uv sync --locked
uv run --no-sync python examples/demo.py --duration 5
```

This runs the actual signaling, leader, and follower CLIs in separate processes,
using simulated arms and a real local WebRTC data channel. It creates an in-memory
room token, observes applied commands, and stops its own children. The finite demo
requires POSIX signal handling; it never opens serial ports, CAN, or cameras.

For the equivalent manual setup, see the [three-terminal instructions](../../docs/usage.md#try-without-hardware).
For two computers or restrictive networks, continue with [Cloudflare](../cloudflare/README.md)
or [coturn](../coturn/README.md). Passing a local demo does not verify internet
connectivity, a chosen TURN relay, or physical safety.
