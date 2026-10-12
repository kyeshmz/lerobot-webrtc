# LeRobot WebRTC — how it differs

This repo (`lerobot-webrtc`) is a narrow leader-arm → follower-arm bridge.
This note compares it against the other public LeRobot + WebRTC options
(verified Oct 2026 from each project's README / PR text).

## This repo in one paragraph

- Physical SO-100/SO-101 leader on one computer drives a physical SO-100/SO-101
  follower on another computer over an `aiortc` unordered, unreliable data
  channel (`maxRetransmits=0`).
- Only six joint positions are transported, in LeRobot-normalized units
  (`use_degrees=False`: body `[-100, 100]`, gripper `[0, 100]`). No camera, no
  dataset recording, no policy, no browser UI, no force feedback.
- Freshness is follower-measured: follower-issued expiring permits (default
  300 ms), sequence numbers, latest-only mailbox, per-write
  `--max-relative-target` clipping via LeRobot's limiter, watchdog session end,
  serialized hardware worker, hold/torque-off on exit.
- Signaling is a self-hosted authenticated WebSocket (shared-secret `TOKEN`,
  one leader + one follower per room, SDP/ICE only). STUN/TURN is opt-in for
  NAT traversal; LAN works with no ICE servers.
- Entry points: `lerobot-webrtc serve | follower | leader | calibrate` plus
  `examples/demo.py` (simulated loopback, no hardware).

## Comparison table

|  | This repo (`lerobot-webrtc`) | `huggingface/lerobot` PR #3962 (`WebRTCProxyRobot`) | `koenvanwijk/lerobot-*` (remote + reach + matchmaker) | `videosdk-teleop` (VideoSDK) |
| --- | --- | --- | --- | --- |
| Direction | Leader arm → follower arm (teleop) | Cloud policy/app → remote robot (proxy `Robot`) | Any LeRobot teleop → any LeRobot robot (plugins) | Leader/operator → follower, or any `Robot`/`Teleoperator` pair |
| Payload | 6 joint positions only | Joints + camera (tiled single track, RTP pts seq) | Action dicts via negotiated `ActionMode` | Joints + camera frames with observation/action ID correlation |
| Transport | `aiortc` P2P datachannel, unreliable | `aiortc` P2P (default) or LiveKit SFU (experimental) | WebRTC datachannel via `RemoteRobot`/`RemoteTeleop` | VideoSDK SFU (`videosdk` + `vsaiortc`) |
| Signaling | Bundled WS server, shared secret, self-host only | Bundled WS relay (aiortc path); LiveKit server for SFU path | `lerobot-matchmaker`, self-hosted or hosted Firebase function | VideoSDK cloud meeting (`meeting_id` + token) |
| Cross-NAT story | Manual STUN/TURN flags + env creds; you run TURN | aiortc for intranet; LiveKit SFU for public internet | Hosted matchmaker URL option | Hosted SFU (internet-first) |
| Safety model | Expiring permits, seq + validation, watchdog ends session, relative-target clip, serialized I/O, hold/torque-off on exit; no deadman | Capture-side watchdog neutralizes on stall; control-plane RPC | Action-space negotiation (`ActionBridge.auto()`); see their issues list | `SafeRobot` envelope (slew/watchdog/estop latch) around the real arm |
| Recording / training | None | Works as a normal `Robot`, so record/teleoperate/eval paths apply | None advertised (plugins focus) | First-class: synced observation log + `export_lerobot.py` to LeRobot dataset; `last_observation_id` |
| Hardware scope | SO-100 + SO-101 only (matching model both sides) | SO-100 example; `webrtc_proxy` package is robot-generic | Robot-agnostic by design; SO-100 compat presets | Arm-agnostic (reads `observation_features`/`action_features`); SO-101 examples; ROS 2 nodes |
| LeRobot version | Pinned `lerobot[feetech]==0.6.1` (`so_leader` / `so_follower`) | Current `lerobot` main + new `webrtc`/`webrtc-livekit` extras | Current LeRobot plugin API (`RemoteRobot`/`RemoteTeleop`) | `lerobot` adapter extra; `lerobot-record` compatible |
| Status (Oct 2026) | Local working tree, sim + hardware CLI, pytest suite | Open PR, not in `main`; `importorskip("aiortc")` tests | Public, 0 stars, 8 open issues on `lerobot-remote` at lookup | Published on PyPI (`videosdk-teleop`), Apache-2.0 |

Sources:

- This repo: `README.md`, `pyproject.toml`, `src/lerobot_webrtc/protocol.py`.
- WebRTCProxyRobot: https://github.com/huggingface/lerobot/pull/3962
- koenvanwijk: https://github.com/koenvanwijk/lerobot-reach,
  https://github.com/koenvanwijk/lerobot-remote,
  https://github.com/koenvanwijk/lerobot-matchmaker
- videosdk-teleop: https://pypi.org/project/videosdk-teleop/,
  examples at https://github.com/videosdk-live/videosdk-teleops-examples

## When to pick this repo

- Two SO-100/SO-101 arms, one leader and one follower, each on its own
  machine, trusted LAN or your own TURN server.
- You want no cloud dependency, no camera, and a small auditable safety
  surface (permits + watchdog + relative-target clip).
- You are fine with six normalized joint positions and no recording/policy
  integration.

Pick something else when you need camera streaming, dataset capture,
policy-side control, heterogeneous arms, or hosted internet traversal — the
table above points at the closer fit.
