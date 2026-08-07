"""The gate layer: the only path from a command to the machine.

Every command is classified by *what it could do wrong*, not by what it is meant
to do. Each consequence class carries hard preconditions. If a precondition is
not provably satisfied, the command does not reach the machine — the same way a
type system refuses to compile a wrong type rather than trusting you to be
careful.

Design rule this file exists to enforce:

    Software will be wrong someday. The gate layer's job is to make sure
    "wrong" has no capability to become "harm".

Nothing here trusts the caller, the network, or the operator console. The gates
run next to the machine, evaluate machine-owned state, and are the only writer
of motion commands.
"""
from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field


class Consequence(enum.IntEnum):
    """What this command could do wrong if everything about it is mistaken.

    Ordering matters: a higher class must satisfy every precondition of the
    classes below it, so the checks compose instead of competing.
    """

    UI_ONLY = 0          # annoying, harmless (a label, a unit toggle)
    WONT_MOVE = 1        # machine refuses to act; production stops, nothing breaks
    RECOVERY = 2         # clears a latched fault; enables nothing on its own
    DAMAGES_MACHINE = 3  # a crash into a hard stop, a ruined tool
    HURTS_PERSON = 4     # motion while someone can reach the working envelope
    CATASTROPHIC = 5     # unrecoverable loss


@dataclass(frozen=True)
class Verdict:
    """Result of putting one command through the gates."""

    allowed: bool
    consequence: Consequence
    reason: str = ""
    checks: tuple[str, ...] = ()

    def __str__(self) -> str:  # pragma: no cover - display helper
        head = "ALLOW" if self.allowed else "REFUSE"
        return f"{head} [{self.consequence.name}] {self.reason}".rstrip()


@dataclass
class Limits:
    """Machine-owned physical truth. Not client-supplied, not negotiable."""

    pos_min_mm: float = 250.0
    pos_max_mm: float = 600.0
    speed_max_mms: float = 180.0
    sensor_max_age_s: float = 0.25
    heartbeat_timeout_s: float = 3.0


@dataclass
class MachineState:
    """What the gates are allowed to reason about — all of it machine-owned."""

    limits: Limits = field(default_factory=Limits)
    position_mm: float = 250.0
    estop_engaged: bool = False
    guard_door_closed: bool = True
    person_in_cell: bool = False
    homed: bool = False
    homing: bool = False   # a home move is in flight; `homed` is set on arrival
    last_sensor_update_s: float = field(default_factory=time.monotonic)
    last_heartbeat_s: float = field(default_factory=time.monotonic)
    now: callable = time.monotonic  # injectable clock, so tests need no sleeps

    # --- derived truths the gates ask about -------------------------------
    def sensor_age_s(self) -> float:
        return self.now() - self.last_sensor_update_s

    def sensor_is_fresh(self) -> bool:
        return self.sensor_age_s() <= self.limits.sensor_max_age_s

    def heartbeat_age_s(self) -> float:
        return self.now() - self.last_heartbeat_s

    def heartbeat_is_alive(self) -> bool:
        return self.heartbeat_age_s() <= self.limits.heartbeat_timeout_s


# Commands are plain dicts (that is what arrives off the wire). Classification
# is explicit and total: an unknown command is not "probably harmless", it is
# refused. Fail closed.
_MOTION = {"move_to", "jog", "run_program", "home"}
_DISPLAY = {"set_units", "set_language", "set_brightness"}


def classify(command: dict) -> Consequence:
    """Map a command to the worst thing it could do if it were wrong."""
    kind = command.get("type")
    if kind in _DISPLAY:
        return Consequence.UI_ONLY
    if kind in {"stop", "estop"}:
        return Consequence.WONT_MOVE
    if kind in _MOTION:
        # Motion is judged by reach, not by intent: if a person can be inside
        # the envelope, the same command carries a person-level consequence.
        return Consequence.HURTS_PERSON
    if kind == "clear_faults":
        # Recovery, not action. It enables nothing by itself — the next motion
        # command still faces every gate. Classifying it as DAMAGES_MACHINE (as
        # this file first did) made an engaged E-stop permanently unclearable,
        # because class >= 2 refuses while the E-stop is engaged. A safety layer
        # you cannot recover from is an availability failure wearing safety's
        # clothes: the operator's only remaining option is to power-cycle the
        # machine, which is exactly the uncontrolled action gates exist to avoid.
        return Consequence.RECOVERY
    return Consequence.CATASTROPHIC  # unknown command: assume the worst


def evaluate(command: dict, state: MachineState) -> Verdict:
    """Put one command through every gate its consequence class requires.

    Returns a Verdict. The caller may only act on `allowed is True`; there is no
    partial success and no "warn and continue".
    """
    consequence = classify(command)
    checks: list[str] = []

    if consequence is Consequence.CATASTROPHIC:
        return Verdict(
            False, consequence,
            f"unknown command type {command.get('type')!r} — refused by default",
            tuple(checks),
        )

    # Class 0: a display change can always proceed; it cannot move anything.
    if consequence is Consequence.UI_ONLY:
        return Verdict(True, consequence, "display-only", ("no-motion-capability",))

    # Class 1: stopping is always permitted. A gate that can block a stop is a
    # gate that has become the hazard.
    if consequence is Consequence.WONT_MOVE:
        return Verdict(True, consequence, "stop is never gated", ("always-allowed",))

    # Class 2: recovery. Allowed while faulted — that is the whole point — but
    # only when the reason for the fault is actually gone, so clearing cannot be
    # used to paper over a live hazard. It grants no motion by itself.
    if consequence is Consequence.RECOVERY:
        if state.person_in_cell:
            return Verdict(False, consequence,
                           "cannot clear faults while a person is in the envelope",
                           ("hazard-still-present",))
        if not state.guard_door_closed:
            return Verdict(False, consequence,
                           "cannot clear faults with the guard door open",
                           ("hazard-still-present",))
        return Verdict(True, consequence, "hazard cleared — fault may be reset",
                       ("cell-clear", "guard-closed"))

    # Class 2+: anything that can touch the machine needs a live, trustworthy
    # picture of it. Stale truth is treated as no truth.
    if state.estop_engaged:
        return Verdict(False, consequence, "E-stop engaged", tuple(checks))
    checks.append("estop-clear")

    if not state.sensor_is_fresh():
        return Verdict(
            False, consequence,
            f"position sensor stale ({state.sensor_age_s() * 1000:.0f} ms > "
            f"{state.limits.sensor_max_age_s * 1000:.0f} ms) — blind, so no motion",
            tuple(checks),
        )
    checks.append("sensor-fresh")

    if not state.heartbeat_is_alive():
        return Verdict(
            False, consequence,
            f"operator heartbeat lost ({state.heartbeat_age_s():.1f} s > "
            f"{state.limits.heartbeat_timeout_s:.1f} s) — nobody is watching",
            tuple(checks),
        )
    checks.append("heartbeat-alive")

    if consequence is Consequence.DAMAGES_MACHINE:
        return Verdict(True, consequence, "machine-state preconditions met", tuple(checks))

    # Class 3: motion. Everything below is about a person being able to reach
    # the moving part.
    if not state.guard_door_closed:
        return Verdict(False, consequence, "guard door open", tuple(checks))
    checks.append("guard-closed")

    if state.person_in_cell:
        return Verdict(
            False, consequence,
            "person detected inside the working envelope — motion refused, "
            "not slowed",
            tuple(checks),
        )
    checks.append("cell-clear")

    if command.get("type") != "home" and not state.homed:
        return Verdict(
            False, consequence,
            "machine not homed — absolute position is unknown",
            tuple(checks),
        )
    checks.append("homed-or-homing")

    limits = state.limits
    if command.get("type") in {"move_to", "jog"}:
        target = command.get("target_mm")
        if not isinstance(target, (int, float)) or isinstance(target, bool):
            return Verdict(False, consequence, "target_mm missing or not a number", tuple(checks))
        if not (limits.pos_min_mm <= float(target) <= limits.pos_max_mm):
            return Verdict(
                False, consequence,
                f"target {float(target):.1f} mm outside travel "
                f"[{limits.pos_min_mm:.0f}, {limits.pos_max_mm:.0f}] mm",
                tuple(checks),
            )
        checks.append("target-in-travel")

        speed = command.get("speed_mms", limits.speed_max_mms)
        if not isinstance(speed, (int, float)) or isinstance(speed, bool):
            return Verdict(False, consequence, "speed_mms not a number", tuple(checks))
        if float(speed) <= 0:
            return Verdict(False, consequence, "speed must be positive", tuple(checks))
        if float(speed) > limits.speed_max_mms:
            return Verdict(
                False, consequence,
                f"speed {float(speed):.0f} mm/s above safe limit "
                f"{limits.speed_max_mms:.0f} mm/s",
                tuple(checks),
            )
        checks.append("speed-within-limit")

    return Verdict(True, consequence, "all preconditions satisfied", tuple(checks))
