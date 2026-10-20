"""Bounded network-only B601 shoulder-pan test using follower-measured degree positions."""

from __future__ import annotations

import asyncio
import math
import time

from .peer import Peer
from .protocol import LeaderInbox, hold_action, validate_action


async def run_bench(
    peer: Peer,
    offset: float,
    duration: float,
    simulated: bool,
    fps: float = 20,
    *,
    require_return: bool = False,
) -> dict:
    if not math.isfinite(offset) or not 0 < abs(offset) <= 1:
        raise ValueError("Bench offset must be nonzero and at most 1 degree")
    if not math.isfinite(duration) or not 3 <= duration <= 15:
        raise ValueError("Bench duration must be between 3 and 15 seconds")
    if not math.isfinite(fps) or not 10 <= fps <= 30:
        raise ValueError("Bench fps must be between 10 and 30")
    inbox = LeaderInbox("b601-rs")
    last_observed = 0.0
    observations = 0

    def receive(message):
        nonlocal last_observed, observations
        previous = inbox.observation_sequence
        response = inbox.receive(message)
        if response is not None:
            if inbox.simulated is not simulated:
                raise ValueError("Bench --simulate must match the follower; no commands were sent")
            peer.send(response)
        if inbox.observation_sequence > previous:
            last_observed = time.monotonic()
            observations += 1

    peer.on_message = receive
    try:
        await peer.connect()
        async with asyncio.timeout(10):
            while inbox.position is None:
                if peer.closed.is_set():
                    raise peer.error or ConnectionError(
                        "B601 follower disconnected before feedback"
                    )
                await asyncio.sleep(0.01)
        if inbox.simulated is not simulated:
            raise ValueError("Bench --simulate must match the follower; no commands were sent")
        origin = inbox.position.copy()
        hold_targets = hold_action(origin, "b601-rs")
        joint = "shoulder_pan.pos"
        destination = {**hold_targets, joint: origin[joint] + offset}
        validate_action(destination, "b601-rs")
        peak = 0.0
        sent = 0
        return_sequence = None
        final_command = hold_targets.copy()
        started = time.monotonic()
        while (elapsed := time.monotonic() - started) < duration:
            if peer.closed.is_set():
                raise peer.error or ConnectionError("B601 follower disconnected during bench")
            if time.monotonic() - last_observed >= 0.3:
                raise TimeoutError("B601 bench lost advancing encoder reports")
            position = inbox.position
            if any(
                abs(position[name] - hold_targets[name]) > 1 for name in origin if name != joint
            ):
                raise RuntimeError("An uncommanded B601 joint moved more than 1 degree")
            if abs(position[joint] - origin[joint]) > abs(offset) + 0.2:
                raise RuntimeError("Encoder-observed B601 excursion exceeded the bench envelope")
            peak = max(peak, math.copysign(1, offset) * (position[joint] - origin[joint]))
            phase = elapsed / (duration / 3)
            fraction = max(0.0, min(phase, 2 - phase)) if require_return else min(1.0, phase)
            target = {**hold_targets, joint: origin[joint] + offset * fraction}
            final_command = target
            if require_return and phase >= 2 and return_sequence is None:
                return_sequence = inbox.observation_sequence
            if peer.send(inbox.command(target)):
                sent += 1
            await asyncio.sleep(1 / fps)
        final = inbox.position.copy()
        if peak < abs(offset) * 0.4:
            raise RuntimeError(
                "No sufficient encoder-observed B601 motion; command receipts do not pass"
            )
        feedback_fresh = time.monotonic() - last_observed < 0.3
        returned = (
            return_sequence is not None
            and inbox.observation_sequence > return_sequence
            and feedback_fresh
            and abs(final[joint] - origin[joint]) <= abs(offset) * 0.4 + 0.02
        )
        if not feedback_fresh or abs(final[joint] - origin[joint]) > abs(offset) + 0.2:
            raise RuntimeError("B601 final feedback is stale or outside the bench envelope")
        if any(abs(final[name] - hold_targets[name]) > 1 for name in origin if name != joint):
            raise RuntimeError("An uncommanded B601 joint drifted outside the bench envelope")
        if require_return and not returned:
            raise RuntimeError("B601 return to the measured starting pose was not confirmed")
        if (
            not require_return
            and math.copysign(1, offset) * (final[joint] - origin[joint]) < abs(offset) * 0.4
        ):
            raise RuntimeError("B601 final encoder position did not confirm the requested motion")
        return {
            "model": "b601-rs",
            "units": "degrees",
            "simulated": simulated,
            "commands_sent": sent,
            "advancing_observations": observations,
            "joint": joint,
            "requested_offset_deg": offset,
            "peak_directed_motion_deg": peak,
            "origin": origin,
            "hold_targets": hold_targets,
            "initial_boundary_corrections_deg": {
                name: hold_targets[name] - origin[name]
                for name in origin
                if hold_targets[name] != origin[name]
            },
            "final": final,
            "final_command": final_command,
            "final_offset_deg": final[joint] - origin[joint],
            "return_required": require_return,
            "returned_to_start_observed": returned,
            "physical_standstill_verified": False,
        }
    finally:
        await peer.close()
