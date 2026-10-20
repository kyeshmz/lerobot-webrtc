# Local video: simulated arms and a synthetic source

Run from the repository root with Python 3.12+. This example needs no camera,
LeRobot, NumPy, PIL, serial port, or CAN interface. The test pattern uses PyAV
from the base installation. It is a software exercise, **not hardware qualification**.
Video remains off unless both endpoints opt in.

## Three-terminal setup

Install once:

```bash
uv sync --locked
```

Generate a **fresh random session token** (at least 32 characters) in a password
manager. In **each of three terminals**, activate the environment and enter the
**same** token at the hidden prompt:

```bash
source .venv/bin/activate
export LEROBOT_WEBRTC_TOKEN="$(python -c 'import getpass; print(getpass.getpass("Shared session token: "))')"
```

The entered secret is captured into the environment, not printed to the terminal
or included in shell history. Do not echo it, put it in URLs, or share it in logs.
Keep it available in the password manager for the browser login below.

**Terminal 1 — loopback signaling:**

```bash
lerobot-webrtc serve
```

**Terminal 2 — simulated follower and synthetic video:**

```bash
lerobot-webrtc follower --simulate --test-video --room video \
  --video-codec vp8 --video-width 320 --video-height 240 --video-fps 10 --duration 30
```

**Terminal 3 — simulated leader and loopback viewer:**

```bash
lerobot-webrtc leader --simulate --view-video 8081 --room video \
  --video-codec vp8 --duration 20
```

On this computer, open **http://127.0.0.1:8081**. Authenticate with username
**`viewer`** and the shared **`LEROBOT_WEBRTC_TOKEN`** value as password. Click
**Connect**. The leader waits for the browser's WebRTC connection before joining
robot signaling, then for its first decoded follower frame before connecting its
simulated arm. If browser playback asks for an extra play gesture, click play.

The follower sends one video track to the Python leader on the existing WebRTC
peer connection. The leader sends decoded frames over a **second native WebRTC
hop** to the browser's HTML `<video>` MediaStream. HTTP serves only the page and
SDP, with Basic authentication on `127.0.0.1`; the secret is not embedded in the
URL/page or logged. This is not MJPEG, frame polling, snapshots, or recording.
There is no microphone/audio or browser camera capture. The browser cannot send
arm commands; with real arms the Python leader still owns the physical teleoperator.

The finite leader session ends after 20 connected seconds. Either endpoint
ending, viewer transport loss, or frame failure/idle ends current control and
closes the peer; the other endpoint stops too. There is **no automatic reconnect
or resume**: restart both endpoints and connect a fresh viewer session to try
again. Stop the separate signaling terminal with Ctrl+C when finished.

## Settings and real cameras

- Source defaults are **640×480 at 15 FPS**. Follower width must be even 16–1280,
  height even 16–720, and video FPS an integer 1–30. `--video-fps` is separate
  from joint-control `--fps`.
- Both endpoints accept **`--video-codec auto|vp8|h264`** (default `auto`). H.264
  is a supported choice, not a hardware-encoding promise. An explicit codec
  requires common support on the relevant peers and fails instead of silently
  falling back. AV1 and unknown codec values are unsupported. Unexpected or
  incompatible video SDP fails before camera capture or motor connection.
- Follower `--test-video` and `--camera-index INTEGER` are mutually exclusive.
  **`--simulate --camera-index 0` opens a real camera**: simulation applies only
  to arms. Keep `--test-video` for hardware-free use.
- Only a real camera accepts **`--camera-fourcc MJPG`** (or e.g. `YUYV`): exactly
  four uppercase letters/digits. FOURCC is the **capture format**, not the
  VP8/H.264 network codec. Requested camera resolution/FPS and explicit FOURCC
  are checked; an unsupported mode fails rather than silently falling back.
- For real cameras, use the released SO `hardware` extra or the separate prepared
  B601 SDK environment, never resync a shared Jetson SDK. Capture uses an
  independent LeRobot OpenCV camera worker, not robot observations or motor I/O.
- `--view-video` without a port uses `8081`; it only binds `127.0.0.1`.
  `bench` and `calibrate` remain data-only and have no video flags.

Both WebRTC hops share CPU/network resources with control: there are **no
real-time video/control guarantees**. A connected browser or decoded frame does
not prove a current image is visible or a safe scene. The browser is **not a
safety-rated dead-man or guaranteed physical stop**; retain an independent
physical cutoff for hardware work. Nothing records video or datasets.

See the [video operating guide](../../docs/usage.md#optional-video),
[hardware safety limits](../../docs/usage.md#safety-behavior-and-limits), official
[LeRobot camera docs](https://huggingface.co/docs/lerobot/en/cameras),
[LeRobot teleoperator source](https://github.com/huggingface/lerobot/tree/v0.6.1/src/lerobot/teleoperators/so_leader),
and [aiortc API](https://aiortc.readthedocs.io/en/latest/api.html).
