"""Authenticated loopback WebRTC viewer; never captures or records browser media.

The inbound receiver drains independently of the browser, retaining only its latest
decoded frame. Browser transport readiness deliberately does not require any RTP.
The parent must stop motors before closing this viewer, not the other way around.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import math
from collections.abc import Callable
from contextlib import suppress
from hmac import compare_digest

from aiohttp import BasicAuth, web
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
from av import VideoFrame

from .video import (
    MAX_VIDEO_HEIGHT,
    MAX_VIDEO_WIDTH,
    VideoSettings,
    prefer_video_codec,
    validate_video_sdp,
)

MAX_REQUEST_SIZE = 65536
SETUP_TIMEOUT = 15.0

# Static script: no credentials, SDP, settings, or other user values are interpolated.
_SCRIPT = """
const button = document.getElementById('connect');
const status = document.getElementById('status');
const video = document.getElementById('video');
let pc;
button.addEventListener('click', async () => {
  button.disabled = true;
  status.textContent = 'Connecting…';
  const current = new RTCPeerConnection({iceServers: []});
  pc = current;
  let connected = false;
  const abort = new AbortController();
  const timer = setTimeout(() => {
    abort.abort();
    current.close();
    status.textContent = 'Connection timed out. Restart the viewer to try again.';
  }, 15000);
  video.srcObject = new MediaStream();
  current.ontrack = ({track}) => {
    video.srcObject.addTrack(track);
    video.play().catch(() => { status.textContent = 'Press play to display video.'; });
  };
  current.onconnectionstatechange = () => {
    if (current.connectionState === 'connected') {
      connected = true;
      clearTimeout(timer);
      status.textContent = 'Connected; waiting for robot video.';
    } else if (['failed', 'disconnected', 'closed'].includes(current.connectionState)) {
      clearTimeout(timer);
      video.srcObject = null;
      status.textContent = 'Connection ended. Restart the viewer; no automatic reconnect.';
    }
  };
  video.addEventListener('playing', () => { status.textContent = 'Live WebRTC video'; },
                         {once: true});
  try {
    current.addTransceiver('video', {direction: 'recvonly'});
    await current.setLocalDescription(await current.createOffer());
    if (current.iceGatheringState !== 'complete') {
      await new Promise((resolve, reject) => {
        const changed = () => {
          if (current.iceGatheringState === 'complete') { cleanup(); resolve(); }
        };
        const canceled = () => { cleanup(); reject(new Error('Timed out')); };
        const cleanup = () => {
          current.removeEventListener('icegatheringstatechange', changed);
          abort.signal.removeEventListener('abort', canceled);
        };
        current.addEventListener('icegatheringstatechange', changed);
        abort.signal.addEventListener('abort', canceled, {once: true});
        if (abort.signal.aborted) canceled(); else changed();
      });
    }
    const response = await fetch('/offer', {
      method: 'POST', credentials: 'same-origin', signal: abort.signal,
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(current.localDescription)
    });
    if (!response.ok) throw new Error('Offer rejected');
    await current.setRemoteDescription(await response.json());
  } catch (_) {
    clearTimeout(timer);
    current.close();
    video.srcObject = null;
    status.textContent = 'Could not connect. You may retry before a session is established.';
    if (!connected) button.disabled = false;
  }
});
window.addEventListener('pagehide', () => { if (pc) pc.close(); });
"""
_SCRIPT_HASH = base64.b64encode(hashlib.sha256(_SCRIPT.encode()).digest()).decode()
_CSP = (
    f"default-src 'none'; script-src 'sha256-{_SCRIPT_HASH}'; connect-src 'self'; "
    "media-src 'self' blob:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
_HTML = (
    """<!doctype html>
<html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Robot WebRTC viewer</title>
<h1>Robot WebRTC viewer</h1>
<p>Receive-only video. No microphone, camera capture, or recording.</p>
<p>Closing this page is not a guaranteed physical stop or dead-man switch.
Use the robot's physical stop and keep the control terminal accessible.</p>
<button id="connect" type="button">Connect</button>
<p id="status" role="status">Connect before starting robot control.</p>
<video id="video" autoplay playsinline muted controls></video>
<script>"""
    + _SCRIPT
    + "</script></html>"
)


class _LatestVideoTrack(VideoStreamTrack):
    def __init__(self, viewer: WebRTCViewer):
        super().__init__()
        self._viewer = viewer
        self._generation = 0

    async def recv(self) -> VideoFrame:
        viewer = self._viewer
        while True:
            if self.readyState != "live" or viewer._closing or viewer._error is not None:
                raise MediaStreamError
            if viewer._frame is not None and self._generation != viewer._generation:
                self._generation = viewer._generation
                return viewer._frame
            # No await between checking the generation and clearing the notification.
            viewer._frame_changed.clear()
            await viewer._frame_changed.wait()

    def stop(self) -> None:
        was_live = self.readyState == "live"
        super().stop()
        viewer = self._viewer
        viewer._frame_changed.set()
        # Retirement clears _pc before stopping; _fail suppresses intentional
        # shutdown and preserves an existing failure when aiortc stops the sender.
        if was_live and viewer._outgoing is self and viewer._pc is not None:
            viewer._fail(ConnectionError("Browser video relay ended unexpectedly"))


class WebRTCViewer:
    def __init__(
        self,
        token: str,
        port: int = 8081,
        settings: VideoSettings | None = None,
        frame_timeout: float = 3,
    ):
        if not isinstance(token, str) or not token:
            raise ValueError("Viewer token must be nonempty")
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("Viewer port must be between 0 and 65535")
        if not math.isfinite(frame_timeout) or frame_timeout <= 0:
            raise ValueError("Frame timeout must be finite and positive")
        self.port = port
        self.settings = settings or VideoSettings()
        self.frame_timeout = frame_timeout
        self.on_error: Callable[[Exception], None] = lambda error: None
        self.started = asyncio.Event()
        self.browser_ready = asyncio.Event()
        self.ready = asyncio.Event()
        self.frames_received = 0
        self._password = token.encode()
        self._closing = False
        self._error: Exception | None = None
        self._runner: web.AppRunner | None = None
        self._pc: RTCPeerConnection | None = None
        self._outgoing: _LatestVideoTrack | None = None
        self._source: MediaStreamTrack | None = None
        self._frame: VideoFrame | None = None
        self._generation = 0
        self._frame_changed = asyncio.Event()
        self._start_task: asyncio.Task | None = None
        self._offer_task: asyncio.Task | None = None
        self._setup_task: asyncio.Task | None = None
        self._receive_task: asyncio.Task | None = None
        self._close_task: asyncio.Task | None = None
        self._cleanup_tasks: set[asyncio.Task] = set()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        if self._closing:
            raise RuntimeError("Viewer is closed")
        if self._start_task is None:
            self._start_task = asyncio.create_task(self._start_owned(), name="viewer-http-start")
        try:
            await asyncio.shield(self._start_task)
        except BaseException:
            await self.close()
            raise

    async def _start_owned(self) -> None:
        app = web.Application(middlewares=[self._secure], client_max_size=MAX_REQUEST_SIZE)
        app.router.add_get("/", self._page)
        app.router.add_post("/offer", self._offer)
        self._runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self.port)
        await site.start()
        self.port = self._runner.addresses[0][1]
        self.started.set()

    @web.middleware
    async def _secure(self, request: web.Request, handler) -> web.StreamResponse:
        try:
            if request.headers.getall("Host", []) != [f"127.0.0.1:{self.port}"]:
                raise web.HTTPForbidden(text="Invalid viewer host")
            origins = request.headers.getall("Origin", [])
            if (origins and origins != [self.url]) or (request.method == "POST" and not origins):
                raise web.HTTPForbidden(text="Same-origin requests required")
            site = request.headers.get("Sec-Fetch-Site")
            if site is not None and site not in {"same-origin", "none"}:
                raise web.HTTPForbidden(text="Same-origin requests required")
            if request.query_string:
                raise web.HTTPBadRequest(text="Viewer URLs do not accept query parameters")
            try:
                auth = BasicAuth.decode(request.headers.get("Authorization", ""), encoding="utf-8")
                valid = compare_digest(auth.login.encode(), b"viewer") & compare_digest(
                    auth.password.encode(), self._password
                )
            except ValueError:
                valid = False
            if not valid:
                raise web.HTTPUnauthorized(
                    text="Viewer authentication required",
                    headers={"WWW-Authenticate": 'Basic realm="viewer", charset="UTF-8"'},
                )
            if self._closing:
                raise web.HTTPServiceUnavailable(text="Viewer is closing")
            response = await handler(request)
        except web.HTTPException as exc:
            response = web.Response(status=exc.status, body=exc.body, headers=exc.headers)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "Content-Security-Policy": _CSP,
                "Referrer-Policy": "no-referrer",
                "Permissions-Policy": "camera=(), microphone=(), display-capture=()",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
            }
        )
        return response

    async def _page(self, request: web.Request) -> web.Response:
        return web.Response(text=_HTML, content_type="text/html")

    async def _offer(self, request: web.Request) -> web.Response:
        if (
            self._pc is not None
            or self._offer_task is not None
            or self._cleanup_tasks
            or self._error is not None
        ):
            raise web.HTTPConflict(text="Only one browser session is allowed")
        self._offer_task = asyncio.current_task()
        pc = None
        deadline = asyncio.get_running_loop().time() + SETUP_TIMEOUT
        try:
            async with asyncio.timeout_at(deadline):
                if request.content_type != "application/json":
                    raise web.HTTPUnsupportedMediaType(text="Expected JSON offer")
                data = await request.json()
                if not isinstance(data, dict) or data.get("type") != "offer":
                    raise ValueError("Expected an offer")
                sdp = data.get("sdp")
                validate_video_sdp(sdp, "recvonly", allow_application=False)
                pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
                self._pc = pc
                self._outgoing = _LatestVideoTrack(self)
                changed = asyncio.Event()

                def state_changed():
                    if self._closing or self._pc is not pc:
                        return
                    if pc.connectionState == "connected":
                        self.browser_ready.set()
                        changed.set()
                    elif pc.connectionState in {"failed", "disconnected", "closed"} or (
                        pc.iceConnectionState in {"failed", "disconnected", "closed"}
                    ):
                        changed.set()
                        if self.browser_ready.is_set():
                            self._fail(ConnectionError("Browser WebRTC connection ended"))

                pc.on("connectionstatechange", state_changed)
                pc.on("iceconnectionstatechange", state_changed)
                transceiver = pc.addTransceiver(self._outgoing, direction="sendonly")
                prefer_video_codec(transceiver, self.settings.codec)
                await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
                await pc.setLocalDescription(await pc.createAnswer())
                answer = {"type": "answer", "sdp": pc.localDescription.sdp}
                self._setup_task = asyncio.create_task(
                    self._watch_browser(pc, changed, deadline), name="viewer-browser-setup"
                )
                return web.json_response(answer)
        except BaseException as exc:
            if pc is not None:
                await self._retire_browser(pc)
            if isinstance(exc, (asyncio.CancelledError, web.HTTPException)):
                raise
            if isinstance(exc, TimeoutError):
                raise web.HTTPRequestTimeout(text="Browser setup timed out") from None
            if isinstance(exc, Exception):
                # Never reflect or log SDP, credentials, or parser error details.
                raise web.HTTPBadRequest(text="Invalid or unsupported video offer") from None
            raise
        finally:
            self._offer_task = None

    async def _watch_browser(self, pc, changed, deadline) -> None:
        try:
            async with asyncio.timeout_at(deadline):
                await changed.wait()
            if self.browser_ready.is_set():
                return
        except TimeoutError:
            pass
        await self._retire_browser(pc)

    async def _retire_browser(self, pc: RTCPeerConnection) -> None:
        if self._pc is pc:
            self._pc = None
            if self._outgoing is not None:
                self._outgoing.stop()
                self._outgoing = None
        # aiortc.close() must not itself be canceled halfway through transport cleanup.
        task = asyncio.create_task(self._close_browser(pc), name="viewer-browser-close")
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)
        await asyncio.shield(task)

    async def _close_browser(self, pc: RTCPeerConnection) -> None:
        await asyncio.gather(pc.close(), return_exceptions=True)

    def attach(self, track: MediaStreamTrack) -> None:
        if track.kind != "video":
            raise ValueError("Viewer accepts video only")
        if self._source is not None:
            raise ValueError("Viewer accepts only one source track")
        if self._closing or self._error is not None:
            raise RuntimeError("Viewer is closed or failed")
        self._source = track
        track.on("ended", self._source_ended)
        self._receive_task = asyncio.create_task(self._receive(), name="viewer-video-receiver")

    def _source_ended(self) -> None:
        self._fail(ConnectionError("Remote video track ended unexpectedly"))

    async def _receive(self) -> None:
        try:
            while not self._closing and self._error is None:
                async with asyncio.timeout(self.frame_timeout):
                    frame = await self._source.recv()
                if self._closing or self._error is not None:
                    return
                if not isinstance(frame, VideoFrame) or not (
                    0 < frame.width <= MAX_VIDEO_WIDTH and 0 < frame.height <= MAX_VIDEO_HEIGHT
                ):
                    raise ValueError("Decoded video frame exceeds permitted dimensions")
                self._frame = frame
                self._generation += 1
                self.frames_received += 1
                self.ready.set()
                self._frame_changed.set()
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._fail(TimeoutError("Remote video frame timeout"))
        except MediaStreamError:
            self._fail(ConnectionError("Remote video track ended unexpectedly"))
        except Exception as exc:
            self._fail(exc)
        finally:
            # An exception traceback may outlive the receiver in the parent's error.
            frame = None

    def _fail(self, error: Exception) -> None:
        if not self._closing and self._error is None:
            self._error = error
            self._frame = None
            self._frame_changed.set()
            with suppress(Exception):
                self.on_error(error)

    async def close(self) -> None:
        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(self._close_owned(), name="viewer-close")
        await asyncio.shield(self._close_task)

    async def _close_owned(self) -> None:
        tasks = [self._start_task, self._offer_task, self._setup_task, self._receive_task]
        for task in tasks:
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in tasks if task is not None), return_exceptions=True)
        if self._source is not None:
            with suppress(Exception):
                self._source.remove_listener("ended", self._source_ended)
                self._source.stop()
            self._source = None
        if self._pc is not None:
            await self._retire_browser(self._pc)
        await asyncio.gather(*self._cleanup_tasks, return_exceptions=True)
        if self._runner is not None:
            await asyncio.gather(self._runner.cleanup(), return_exceptions=True)
            self._runner = None
        self._frame = None
        self._frame_changed.set()
