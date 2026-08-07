"""A simulated linear axis, and the supervisor that owns it.

The machine here is deliberately dumb: it moves, it reports where it is, and it
can be made to misbehave (unplug its sensor, stall its heartbeat, wedge a person
into the cell). All the judgment lives in `gates.py`.

The important structural claim this file makes:

    `Supervisor.submit()` is the only way a command reaches `_Axis`.

`_Axis` is private and the supervisor never exposes it. A caller cannot reach
past the gates by holding a reference to the machine, which is what makes the
gate layer a boundary rather than a convention.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .gates import Consequence, MachineState, Verdict, evaluate


@dataclass
class _Axis:
    """The moving part. Private on purpose — see module docstring."""

    position_mm: float = 250.0
    target_mm: float = 250.0
    speed_mms: float = 0.0
    moving: bool = False
    stopped_reason: str = ""

    def command_move(self, target_mm: float, speed_mms: float) -> None:
        self.target_mm = target_mm
        self.speed_mms = speed_mms
        self.moving = True
        self.stopped_reason = ""

    def safe_stop(self, reason: str) -> None:
        self.moving = False
        self.speed_mms = 0.0
        self.target_mm = self.position_mm
        self.stopped_reason = reason

    def step(self, dt_s: float) -> None:
        if not self.moving:
            return
        delta = self.speed_mms * dt_s
        remaining = self.target_mm - self.position_mm
        if abs(remaining) <= abs(delta):
            self.position_mm = self.target_mm
            self.moving = False
            self.speed_mms = 0.0
        else:
            self.position_mm += delta if remaining > 0 else -delta


@dataclass
class Supervisor:
    """Owns the machine, the gates, and the watchdogs.

    Two independent defences, on purpose:

    * `submit()` gates a command *before* motion starts.
    * `tick()` re-checks the same truths *during* motion and stops the axis the
      moment one of them goes away. A command that was legal when it was issued
      does not stay legal if the world changes underneath it.
    """

    state: MachineState = field(default_factory=MachineState)
    log: list[str] = field(default_factory=list)
    _axis: _Axis = field(default_factory=_Axis, repr=False)

    # ---- the only door in ------------------------------------------------
    def submit(self, command: dict) -> Verdict:
        verdict = evaluate(command, self.state)
        self.log.append(f"{command.get('type')}: {verdict}")
        if not verdict.allowed:
            return verdict

        kind = command["type"]
        if kind in {"stop", "estop"}:
            if kind == "estop":
                self.state.estop_engaged = True
            self._axis.safe_stop("commanded stop")
        elif kind == "home":
            self.state.homed = True
            self._axis.command_move(self.state.limits.pos_min_mm, 40.0)
        elif kind in {"move_to", "jog"}:
            self._axis.command_move(
                float(command["target_mm"]),
                float(command.get("speed_mms", self.state.limits.speed_max_mms)),
            )
        elif kind == "clear_faults":
            self.state.estop_engaged = False
        return verdict

    # ---- watchdogs: the gates keep applying while the axis moves ---------
    def tick(self, dt_s: float = 0.02) -> str | None:
        """Advance the simulation. Returns a safe-stop reason if one fired."""
        reason = self._watchdog_reason()
        if reason and self._axis.moving:
            self._axis.safe_stop(reason)
            self.log.append(f"SAFE-STOP: {reason}")
            return reason
        self.state.position_mm = self._axis.position_mm
        self._axis.step(dt_s)
        self.state.position_mm = self._axis.position_mm
        return None

    def _watchdog_reason(self) -> str | None:
        s = self.state
        if s.estop_engaged:
            return "E-stop engaged"
        if not s.sensor_is_fresh():
            return (f"position sensor stale ({s.sensor_age_s() * 1000:.0f} ms) "
                    f"— machine is blind")
        if not s.heartbeat_is_alive():
            return f"operator heartbeat lost ({s.heartbeat_age_s():.1f} s)"
        if not s.guard_door_closed:
            return "guard door opened"
        if s.person_in_cell:
            return "person entered the working envelope"
        return None

    # ---- what the HMI is allowed to see ----------------------------------
    def telemetry(self) -> dict:
        s = self.state
        return {
            "position_mm": round(self._axis.position_mm, 2),
            "target_mm": round(self._axis.target_mm, 2),
            "speed_mms": round(self._axis.speed_mms, 1),
            "moving": self._axis.moving,
            "stopped_reason": self._axis.stopped_reason,
            "estop_engaged": s.estop_engaged,
            "guard_door_closed": s.guard_door_closed,
            "person_in_cell": s.person_in_cell,
            "homed": s.homed,
            "sensor_fresh": s.sensor_is_fresh(),
            "heartbeat_alive": s.heartbeat_is_alive(),
        }

    # ---- fault injection: this is what makes the demo worth running ------
    def feed_sensor(self) -> None:
        self.state.last_sensor_update_s = self.state.now()

    def feed_heartbeat(self) -> None:
        self.state.last_heartbeat_s = self.state.now()

    def unplug_sensor(self, seconds_ago: float = 1.0) -> None:
        self.state.last_sensor_update_s = self.state.now() - seconds_ago

    def kill_heartbeat(self, seconds_ago: float = 5.0) -> None:
        self.state.last_heartbeat_s = self.state.now() - seconds_ago


class FakeClock:
    """A hand-cranked clock, so the acceptance tests contain no sleeps."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def new_supervisor(clock=time.monotonic) -> Supervisor:
    state = MachineState(now=clock)
    state.last_sensor_update_s = clock()
    state.last_heartbeat_s = clock()
    return Supervisor(state=state)
