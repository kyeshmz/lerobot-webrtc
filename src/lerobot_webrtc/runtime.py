from __future__ import annotations

import asyncio
import logging
import secrets
import sys
import time

from .arms import AsyncArm, SimulatedArm
from .peer import Peer
from .protocol import FollowerInbox, LeaderInbox, decode, envelope

logger = logging.getLogger(__name__)


async def leader_loop(arm: AsyncArm, peer: Peer, inbox: LeaderInbox, fps: float) -> None:
    while not peer.closed.is_set():
        started = time.monotonic()
        if inbox.permit is not None:
            # Bind freshness before sampling, not after a potentially slow serial read.
            permit = inbox.permit
            action = await arm.call("get_action")
            message = inbox.command(action, permit)
            if message is not None:
                peer.send(message)
        await asyncio.sleep(max(0, 1 / fps - (time.monotonic() - started)))


async def follower_loop(
    arm: AsyncArm, peer: Peer, inbox: FollowerInbox, fps: float, startup_timeout: float = 5
) -> None:
    started = last_applied = time.monotonic()
    has_applied = False
    count = 0
    last_log = started
    while not peer.closed.is_set():
        tick = time.monotonic()
        # Check BEFORE a fresh packet can revive a timed-out session.
        deadline = last_applied + inbox.timeout if has_applied else started + startup_timeout
        if tick >= deadline:
            peer.closed.set()
            raise TimeoutError("Follower watchdog tripped; session stopped, restart both peers")
        b601 = getattr(inbox, "model", None) == "b601-rs"
        if not b601:
            peer.send(inbox.issue_permit(tick))
        command = inbox.take(time.monotonic())
        if command is not None and not peer.closed.is_set():
            watchdog_deadline = deadline
            if await arm.send(
                command.action,
                min(command.deadline, watchdog_deadline),
                session_active=lambda: not peer.closed.is_set(),
            ):
                applied_at = time.monotonic()
                if applied_at >= command.deadline or applied_at >= watchdog_deadline:
                    peer.closed.set()
                    raise TimeoutError(
                        "Follower command overran its permit/watchdog deadline; session stopped"
                    )
                last_applied = applied_at
                has_applied = True
                count += 1
        if b601 and not peer.closed.is_set():
            # Consume pending commands before spending their freshness budget on telemetry.
            position = await arm.call("get_observation")
            observed_at = time.monotonic()
            deadline = last_applied + inbox.timeout if has_applied else started + startup_timeout
            if observed_at >= deadline:
                peer.closed.set()
                raise TimeoutError("Follower watchdog tripped during feedback; session stopped")
            peer.send(inbox.issue_permit(observed_at, position))
        if tick - last_log >= 2:
            logger.info("Follower applied %d commands", count)
            last_log = tick
        await asyncio.sleep(max(0, 1 / fps - (time.monotonic() - tick)))


async def run_control(
    arm: AsyncArm, peer: Peer, model: str, fps: float, timeout: float, duration: float | None
) -> None:
    verified = asyncio.Event()
    challenge = secrets.token_hex(16)
    if peer.role == "leader":
        inbox = LeaderInbox(model)

        def receive(message):
            response = inbox.receive(message)
            if response is not None:
                if isinstance(arm.arm, SimulatedArm) and inbox.simulated is False:
                    raise ValueError(
                        "Synthetic B601 leader cannot activate physical hardware; use bench"
                    )
                peer.send(response)

        peer.on_message = receive
        loop = leader_loop(arm, peer, inbox, fps)
    else:
        inbox = FollowerInbox(model, timeout, simulated=isinstance(arm.arm, SimulatedArm))

        def receive(message):
            if model == "b601-rs":
                data = decode(message, model, ("ready", "action"))
                if data["type"] == "ready":
                    if data["permit"] != challenge or data.get("role") != "leader":
                        raise ValueError("Invalid B601 model handshake")
                    verified.set()
                    return
            inbox.receive(message, time.monotonic())

        peer.on_message = receive
        loop = follower_loop(arm, peer, inbox, fps)
    # Install handlers before negotiating, so no initial message is lost.
    try:
        await peer.connect()
        if model == "b601-rs" and peer.role == "follower":
            async with asyncio.timeout(5):
                while not verified.is_set():
                    if peer.closed.is_set():
                        raise peer.error or ConnectionError(
                            "Peer ended before B601 model handshake"
                        )
                    peer.send(envelope(model, "hello", permit=challenge, simulated=inbox.simulated))
                    await asyncio.sleep(0.05)
        if peer.closed.is_set():
            raise peer.error or ConnectionError("Peer ended before arm connection")
        await arm.call("connect")
        logger.info("%s connected; control running at %.1f Hz", peer.role, fps)
        tasks = {asyncio.create_task(loop), asyncio.create_task(peer.closed.wait())}
        if duration is not None:
            tasks.add(asyncio.create_task(asyncio.sleep(duration)))
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if peer.closed.is_set():
                raise peer.error or ConnectionError("Peer disconnected")
        finally:
            # Revoke queued work before cancellation/join can wait on serial I/O.
            peer.closed.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        # loop was allocated before connection, but may never have been scheduled.
        loop.close()
        peer.closed.set()
        # Stop the arm before potentially slow network teardown/close handshakes.
        primary = sys.exception()
        cleanup_errors = []
        try:
            await arm.close()
        except BaseException as exc:
            cleanup_errors.append(exc)
        finally:
            # Peer.close shields its own teardown task. Own and join the public
            # close operation here too, so cancellation cannot orphan it.
            close_task = asyncio.create_task(peer.close())
            cancelled_during_close = False
            while not close_task.done():
                try:
                    await asyncio.shield(close_task)
                except asyncio.CancelledError as exc:
                    cancelled_during_close = True
                    if primary is None:
                        primary = exc
                except BaseException:
                    break
            try:
                close_task.result()
            except BaseException as exc:
                cleanup_errors.append(exc)
            if cancelled_during_close and primary is None:
                primary = asyncio.CancelledError()
        if cleanup_errors:
            if (
                primary is None
                and len(cleanup_errors) == 1
                and isinstance(cleanup_errors[0], asyncio.CancelledError)
            ):
                raise cleanup_errors[0]
            if isinstance(primary, asyncio.CancelledError) and all(
                isinstance(error, asyncio.CancelledError) for error in cleanup_errors
            ):
                raise primary
            failures = ([primary] if primary is not None else []) + cleanup_errors
            raise BaseExceptionGroup("Session failed during shutdown", failures)
        if isinstance(primary, asyncio.CancelledError):
            raise primary
