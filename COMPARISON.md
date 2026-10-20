# LeRobot WebRTC — how it differs

This repo (`lerobot-webrtc`) provides a native LeRobot `Robot` proxy and a
standalone leader-arm → follower-arm bridge over WebRTC.
This note compares it against other public LeRobot + WebRTC options. Other-project
descriptions are a snapshot from the initial October 2026 README/PR review; check
the linked upstream sources for current status. This repo's column reflects the
current implementation.

## This repo in one paragraph

- A local SO-100/SO-101 leader drives a matching remote follower, either through
  normal `lerobot-teleoperate` with the optional native `Robot` plugin or the
  standalone `leader` / `follower` commands. Both use an `aiortc` unordered,
  unreliable data channel (`maxRetransmits=0`).
- SO transports six joint positions in LeRobot-normalized units
  (`use_degrees=False`: body `[-100, 100]`, gripper `[0, 100]`). The separate
  B601-RS follower transports seven degree-valued positions, including wrist yaw
  and its angular gripper. No automatic SO-to-B601 mapping is provided. Video is
  off by default: native mode receives one track directly as headless RGB Robot
  observations; standalone mode adds a second WebRTC hop to a loopback-only,
  receive-only browser viewer. No browser arm control, audio, or force feedback.
- Freshness is follower-measured: follower-issued expiring permits (default
  300 ms), sequence numbers, latest-only mailbox, per-write
  `--max-relative-target` clipping via LeRobot's limiter, watchdog session end,
  serialized hardware worker, hold/torque-off on exit.
- Signaling is a self-hosted authenticated WebSocket (shared-secret `TOKEN`,
  one leader + one follower per room, SDP/ICE only). STUN/TURN is opt-in for
  NAT traversal; LAN works with no ICE servers.
- Entry points: `lerobot-webrtc serve | host | follower | leader | calibrate | bench`
  plus `examples/demo.py` (simulated loopback, no hardware). Native `host` and
  standalone `follower` intentionally use different data-channel protocols.
- The optional native `Robot` plugin requires explicit
  `--robot.discover_packages_path=lerobot_robot_webrtc --robot.type=webrtc` in
  both reviewed SDKs; there is no `Teleoperator` plugin. Offline physical setup
  and calibration remain local to each arm computer.
- LeRobot owns recording and conditional policy execution through the Robot API;
  the bridge is not a dataset recorder. Begin with single-episode teleop
  recording. Both SDK pins record processed requested teleop actions, **not**
  returned host-clamped targets. Setup/saves/pauses can terminally STOP under the
  5-second first-command / default 300 ms active budgets. No replay, automatic
  reconnect/rearm, or transparent multi-episode pause support is promised.
- Provider examples are in [examples/](examples/README.md): Cloudflare Tunnel/TURN
  and coturn work with the P2P bridge; the LiveKit SDK example is transport-only.

## Comparison table

|  | This repo (`lerobot-webrtc`) | `huggingface/lerobot` PR #3962 (`WebRTCProxyRobot`) | `koenvanwijk/lerobot-*` (remote + reach + matchmaker) | `videosdk-teleop` (VideoSDK) |
| --- | --- | --- | --- | --- |
| Direction | Native SDK `Robot` proxy or standalone leader → follower | Cloud policy/app → remote robot (proxy `Robot`) | Any LeRobot teleop → any LeRobot robot (plugins) | Leader/operator → follower, or any `Robot`/`Teleoperator` pair |
| Payload | 6 normalized SO or 7 B601 degree positions; native measured observations/correlated target results; optional single video track, no audio | Joints + camera (tiled single track, RTP pts seq) | Action dicts via negotiated `ActionMode` | Joints + camera frames with observation/action ID correlation |
| Transport | `aiortc` P2P datachannel; native video directly to SDK, standalone video adds browser hop | `aiortc` P2P (default) or LiveKit SFU (experimental) | WebRTC datachannel via `RemoteRobot`/`RemoteTeleop` | VideoSDK SFU (`videosdk` + `vsaiortc`) |
| Signaling | Bundled WS server, shared secret, self-host only | Bundled WS relay (aiortc path); LiveKit server for SFU path | `lerobot-matchmaker`, self-hosted or hosted Firebase function | VideoSDK cloud meeting (`meeting_id` + token) |
| Cross-NAT story | Manual STUN/TURN flags + env creds; you run TURN | aiortc for intranet; LiveKit SFU for public internet | Hosted matchmaker URL option | Hosted SFU (internet-first) |
| Safety model | Expiring permits, seq + validation, watchdog ends session, relative-target clip, serialized I/O, hold/torque-off on exit; no deadman | Capture-side watchdog neutralizes on stall; control-plane RPC | Action-space negotiation (`ActionBridge.auto()`); see their issues list | `SafeRobot` envelope (slew/watchdog/estop latch) around the real arm |
| Recording / training | SDK-owned native teleop recording; requested-action label caveat; rollout conditional on schema/cadence, not all strategies qualified | Works as a normal `Robot`, so record/teleoperate/eval paths apply | None advertised (plugins focus) | First-class: synced observation log + `export_lerobot.py` to LeRobot dataset; `last_observation_id` |
| Hardware scope | Matching SO-100/SO-101 pairs; separate Linux B601-RS follower/bench | SO-100 example; `webrtc_proxy` package is robot-generic | Robot-agnostic by design; SO-100 compat presets | Arm-agnostic (reads `observation_features`/`action_features`); SO-101 examples; ROS 2 nodes |
| LeRobot version | Native SO operator: `lerobot[core_scripts,feetech]==0.6.1`; SO host: `[feetech]==0.6.1`; B601: separate source pin | Snapshot of `lerobot` main + new `webrtc`/`webrtc-livekit` extras | LeRobot plugin API (`RemoteRobot`/`RemoteTeleop`) | `lerobot` adapter extra; `lerobot-record` compatible |
| Verification scope | Real local WebRTC tests, fake-bus SDK checks, one supervised standalone B601 shoulder-pan move-and-hold; no native physical qualification | PR implementation; consult its current status | Public plugin projects; consult their current READMEs/issues | Published package; consult its current docs and safety requirements |

Sources:

- This repo: [native guide](examples/native/README.md), `pyproject.toml`,
  `src/lerobot_webrtc/native_protocol.py`, and `src/lerobot_webrtc/protocol.py`.
- WebRTCProxyRobot: https://github.com/huggingface/lerobot/pull/3962
- koenvanwijk: https://github.com/koenvanwijk/lerobot-reach,
  https://github.com/koenvanwijk/lerobot-remote,
  https://github.com/koenvanwijk/lerobot-matchmaker
- videosdk-teleop: https://pypi.org/project/videosdk-teleop/,
  examples at https://github.com/videosdk-live/videosdk-teleops-examples

## When to pick this repo

- Two SO-100/SO-101 arms, one leader and one follower, each on its own
  machine, trusted LAN or your own TURN server.
- You want normal LeRobot teleoperation with an explicitly discovered remote
  Robot, and can meet its freshness budgets. For SDK recording and conditional
  `lerobot-rollout`, read the [native workflow limits](examples/native/README.md#cadence-stop-and-rollout-limits)
  before assuming feature/API compatibility means strategy compatibility.
- You want no cloud dependency and data-only defaults, with optional single-camera
  viewing while the Python leader retains physical teleoperator ownership.
- You want explicit VP8/H.264 network codec selection and camera resolution/FPS/
  capture-format settings; see the [video example](examples/video/README.md).
- You want matching SO normalized actions, or a bounded B601-RS degree-position
  follower with an encoder-checked standalone move-and-hold source.

Pick something else when you need browser arm control, multiple camera tracks,
audio, applied-action-fidelity recording out of the box, arbitrary interactive
rollout strategies, heterogeneous-arm mapping, or hosted internet traversal.
The table above points at closer fits, not safety qualification. The local viewer
is not a safety-rated dead-man, recorder, or automatic reconnect mechanism.
