#!/usr/bin/env python3
"""The acceptance harness: try to break the machine, watch it fail safe.

This is the script to run if you are the person paying for work you cannot see.
You don't have to read the code. Run this, read the eight lines it prints, and
decide for yourself whether the machine is allowed to hurt anything.

    python3 break_it.py

Every scenario below is an attempt to make the machine do the wrong thing. A
PASS means the attempt was refused at the gate or stopped by a watchdog — never
that it "worked". Exit code is 0 only if every attempt failed to cause harm, so
this doubles as a CI gate.
"""
from __future__ import annotations

import sys

from machine_gates.machine import FakeClock, Supervisor
from machine_gates.gates import MachineState

GREEN, RED, DIM, BOLD, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


def fresh() -> tuple[Supervisor, FakeClock]:
    """A healthy, homed machine with a clock we control."""
    clock = FakeClock()
    state = MachineState(now=clock)
    state.last_sensor_update_s = clock()
    state.last_heartbeat_s = clock()
    sup = Supervisor(state=state)
    sup.submit({"type": "home"})
    for _ in range(60):          # let homing finish
        clock.advance(0.02)
        sup.feed_sensor()
        sup.feed_heartbeat()
        sup.tick(0.02)
    return sup, clock


def run_healthy_move(sup: Supervisor, clock: FakeClock, steps: int = 20) -> str | None:
    """Advance a move under healthy conditions. Returns a safe-stop reason if any."""
    for _ in range(steps):
        clock.advance(0.02)
        sup.feed_sensor()
        sup.feed_heartbeat()
        reason = sup.tick(0.02)
        if reason:
            return reason
    return None


# --- the scenarios ---------------------------------------------------------

def s1_out_of_travel():
    """Ask for a position outside the machine's physical travel."""
    sup, _ = fresh()
    v = sup.submit({"type": "move_to", "target_mm": 900.0})
    return (not v.allowed), f"refused at gate — {v.reason}" if not v.allowed else "MOVED"


def s2_sensor_unplugged_mid_move():
    """Pull the position sensor while the axis is moving."""
    sup, clock = fresh()
    sup.submit({"type": "move_to", "target_mm": 580.0, "speed_mms": 120.0})
    run_healthy_move(sup, clock, steps=5)
    moving_before = sup.telemetry()["moving"]
    sup.unplug_sensor(seconds_ago=1.0)      # the cable comes out
    clock.advance(0.02)
    reason = sup.tick(0.02)
    ok = moving_before and reason is not None and not sup.telemetry()["moving"]
    return ok, f"safe-stop while moving — {reason}" if ok else "kept moving while blind"


def s3_heartbeat_lost():
    """The operator console dies mid-move: nobody is watching any more."""
    sup, clock = fresh()
    sup.submit({"type": "move_to", "target_mm": 580.0, "speed_mms": 120.0})
    run_healthy_move(sup, clock, steps=5)
    for _ in range(40):                     # console goes quiet, sensor still fine
        clock.advance(0.1)
        sup.feed_sensor()
        reason = sup.tick(0.02)
        if reason:
            return True, f"safe-stop after {sup.state.heartbeat_age_s():.1f} s of silence — {reason}"
    return False, "kept moving with nobody watching"


def s4_person_enters_cell():
    """Someone opens the cell and steps in while the axis is running."""
    sup, clock = fresh()
    sup.submit({"type": "move_to", "target_mm": 580.0, "speed_mms": 120.0})
    run_healthy_move(sup, clock, steps=5)
    sup.state.person_in_cell = True
    clock.advance(0.02)
    sup.feed_sensor()
    sup.feed_heartbeat()
    reason = sup.tick(0.02)
    stopped = reason is not None and not sup.telemetry()["moving"]
    # and a fresh motion command must also be refused while they are inside
    v = sup.submit({"type": "move_to", "target_mm": 300.0, "speed_mms": 20.0})
    ok = stopped and not v.allowed
    return ok, (f"stopped ({reason}); new motion also refused — {v.reason}"
                if ok else "person in the envelope did not stop motion")


def s5_speed_above_limit():
    """Ask for a legal position at an unsafe speed."""
    sup, _ = fresh()
    v = sup.submit({"type": "move_to", "target_mm": 400.0, "speed_mms": 5000.0})
    return (not v.allowed), f"refused at gate — {v.reason}" if not v.allowed else "MOVED FAST"


def s6_unknown_command():
    """Inject a command nobody designed for. Fail closed, not open."""
    sup, _ = fresh()
    v = sup.submit({"type": "rapid_traverse_undocumented", "target_mm": 400.0})
    return (not v.allowed), f"refused by default — {v.reason}" if not v.allowed else "EXECUTED"


def s7_not_homed():
    """Command absolute motion on a machine that doesn't know where it is."""
    clock = FakeClock()
    state = MachineState(now=clock)
    state.last_sensor_update_s = clock()
    state.last_heartbeat_s = clock()
    sup = Supervisor(state=state)            # deliberately not homed
    v = sup.submit({"type": "move_to", "target_mm": 400.0})
    return (not v.allowed), f"refused at gate — {v.reason}" if not v.allowed else "MOVED BLIND"


def s8_stop_is_never_gated():
    """The inverse test: a gate that can block a STOP has become the hazard."""
    sup, clock = fresh()
    sup.submit({"type": "move_to", "target_mm": 580.0, "speed_mms": 120.0})
    run_healthy_move(sup, clock, steps=3)
    sup.unplug_sensor(seconds_ago=5.0)       # everything is wrong...
    sup.kill_heartbeat(seconds_ago=99.0)
    sup.state.guard_door_closed = False
    v = sup.submit({"type": "estop"})        # ...a stop must still get through
    ok = v.allowed and not sup.telemetry()["moving"]
    return ok, "stop accepted with every other precondition failing" if ok else "STOP WAS BLOCKED"


def s9_recovery_after_estop():
    """The other direction: after a fault, can the machine come back at all?

    A harness that only tests refusal is green by construction. A gate layer
    that cannot be recovered from is an availability failure wearing safety's
    clothes — the operator's only remaining move is a power cycle, which is the
    uncontrolled action the gates existed to prevent.
    """
    sup, clock = fresh()
    sup.submit({"type": "estop"})
    if sup.submit({"type": "move_to", "target_mm": 400.0}).allowed:
        return False, "motion allowed while E-stopped"
    if not sup.submit({"type": "clear_faults"}).allowed:
        return False, "E-stop could not be cleared — machine is bricked"
    if sup.state.estop_engaged:
        return False, "clear_faults returned OK but the E-stop stayed engaged"
    v = sup.submit({"type": "move_to", "target_mm": 400.0, "speed_mms": 100.0})
    ok = v.allowed
    return ok, ("E-stop cleared once the hazard was gone, and normal motion resumed"
                if ok else f"machine stayed unusable — {v.reason}")


def s10_cannot_clear_a_live_hazard():
    """Recovery must not become a way to wave a live hazard through."""
    sup, _ = fresh()
    sup.submit({"type": "estop"})
    sup.state.person_in_cell = True
    v = sup.submit({"type": "clear_faults"})
    return (not v.allowed), (f"refused while the person is still inside — {v.reason}"
                             if not v.allowed else "cleared with someone in the cell")


def s11_interrupted_homing_forfeits_position():
    """Homing that never reached the reference must not count as homed."""
    sup, clock = fresh()
    sup.state.homed = False
    sup._axis.position_mm = 590.0
    sup.submit({"type": "home"})
    clock.advance(0.02); sup.feed_sensor(); sup.feed_heartbeat(); sup.tick(0.02)
    sup.state.guard_door_closed = False          # interrupt it mid-move
    sup.tick(0.02)
    sup.state.guard_door_closed = True
    sup.feed_sensor(); sup.feed_heartbeat()
    v = sup.submit({"type": "move_to", "target_mm": 400.0, "speed_mms": 50.0})
    ok = (not sup.state.homed) and (not v.allowed)
    return ok, (f"homing was interrupted at {sup.telemetry()['position_mm']:.1f} mm; "
                f"position knowledge forfeited and absolute motion refused — {v.reason}"
                if ok else "machine believed it was homed without reaching the reference")


SCENARIOS = [
    ("Position outside physical travel",        s1_out_of_travel),
    ("Position sensor unplugged mid-move",      s2_sensor_unplugged_mid_move),
    ("Operator console dies mid-move",          s3_heartbeat_lost),
    ("Person steps into the working envelope",  s4_person_enters_cell),
    ("Speed above the safe limit",              s5_speed_above_limit),
    ("Undocumented command injected",           s6_unknown_command),
    ("Absolute move before homing",             s7_not_homed),
    ("STOP while everything else is failing",   s8_stop_is_never_gated),
    ("Recovery: can it come back after E-stop", s9_recovery_after_estop),
    ("Clearing a fault with a live hazard",     s10_cannot_clear_a_live_hazard),
    ("Interrupted homing claims to be homed",   s11_interrupted_homing_forfeits_position),
]


def main() -> int:
    print(f"\n{BOLD}BREAK-IT ACCEPTANCE HARNESS{OFF}")
    print(f"{DIM}{len(SCENARIOS)} attempts to make the machine do the wrong thing —")
    print(f"plus two that check it can still come back afterwards, because a harness")
    print(f"that only tests refusal is green by construction.{OFF}\n")

    failures = 0
    for i, (title, fn) in enumerate(SCENARIOS, 1):
        ok, detail = fn()
        if not ok:
            failures += 1
        mark = f"{GREEN}PASS{OFF}" if ok else f"{RED}FAIL{OFF}"
        print(f"  [{i}/{len(SCENARIOS)}] {title:<42} {mark}")
        print(f"        {DIM}{detail}{OFF}")

    print()
    if failures:
        print(f"{RED}{BOLD}{failures} attempt(s) caused harm. Do not sign this off.{OFF}\n")
        return 1
    print(f"{GREEN}{BOLD}All {len(SCENARIOS)} checks held.{OFF}")
    print(f"{DIM}That is the deliverable: not a feature you can see, but a machine")
    print(f"that has lost the capability to hurt itself or the person next to it —")
    print(f"and that an operator can still recover without pulling the power.{OFF}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
