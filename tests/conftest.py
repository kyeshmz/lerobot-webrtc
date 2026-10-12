import pytest_asyncio
from aiohttp import web

from lerobot_webrtc.signaling import create_app

TOKEN = "test-secret-at-least-16-characters"


@pytest_asyncio.fixture
async def signaling_server(unused_tcp_port):
    app = create_app(TOKEN)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", unused_tcp_port)
    await site.start()
    try:
        yield f"ws://127.0.0.1:{unused_tcp_port}", app
    finally:
        await runner.cleanup()
