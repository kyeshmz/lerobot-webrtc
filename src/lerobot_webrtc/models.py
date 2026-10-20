"""Wire units and joint bounds. SO normalization never applies to a CAN B601."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ArmModel:
    units: str
    bounds: tuple[tuple[str, float, float], ...]
    feedback_tolerance: float = 0.0

    @property
    def joints(self) -> tuple[str, ...]:
        return tuple(joint for joint, _, _ in self.bounds)

    @property
    def observation_bounds(self) -> tuple[tuple[str, float, float], ...]:
        return tuple(
            (joint, low - self.feedback_tolerance, high + self.feedback_tolerance)
            for joint, low, high in self.bounds
        )


_SO = ArmModel(
    "lerobot-normalized",
    (
        ("shoulder_pan.pos", -100, 100),
        ("shoulder_lift.pos", -100, 100),
        ("elbow_flex.pos", -100, 100),
        ("wrist_flex.pos", -100, 100),
        ("wrist_roll.pos", -100, 100),
        ("gripper.pos", 0, 100),
    ),
)

# RS_PROFILE in LeRobot commit ca69a2068462a37f7cdcb74180927a2f863d2bf7.
_B601_RS = ArmModel(
    "degrees",
    (
        ("shoulder_pan.pos", -145, 145),
        ("shoulder_lift.pos", -170, 0),
        ("elbow_flex.pos", -200, 0),
        ("wrist_flex.pos", -90, 80),
        ("wrist_yaw.pos", -90, 90),
        ("wrist_roll.pos", -90, 90),
        ("gripper.pos", -270, 0),
    ),
    feedback_tolerance=0.35,
)

MODELS = ("so100", "so101", "b601-rs")


def arm_model(name: str) -> ArmModel:
    if name in {"so100", "so101"}:
        return _SO
    if name == "b601-rs":
        return _B601_RS
    raise ValueError(f"Unsupported arm model: {name}")
