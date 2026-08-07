# mqtt-machine-safety-gates

**A machine you can command over MQTT — and cannot make hurt anyone.** An
operator HMI, a simulated axis, and a gate layer that classifies every command
by *what it could do wrong* and refuses the ones it cannot prove are safe.

Run the acceptance harness and try to break it:

```bash
git clone https://github.com/boheastill/mqtt-machine-safety-gates
cd mqtt-machine-safety-gates
python3 break_it.py          # no dependencies, no broker, ~1 second
```

```
BREAK-IT ACCEPTANCE HARNESS
Eight attempts to make the machine do the wrong thing.
PASS = the attempt was refused or stopped. Nothing here 'works'.

  [1/8] Position outside physical travel           PASS
        refused at gate — target 900.0 mm outside travel [250, 600] mm
  [2/8] Position sensor unplugged mid-move         PASS
        safe-stop while moving — position sensor stale (1020 ms) — machine is blind
  [3/8] Operator console dies mid-move             PASS
        safe-stop after 3.0 s of silence — operator heartbeat lost (3.0 s)
  [4/8] Person steps into the working envelope     PASS
        stopped (person entered the working envelope); new motion also refused
  [5/8] Speed above the safe limit                 PASS
        refused at gate — speed 5000 mm/s above safe limit 180 mm/s
  [6/8] Undocumented command injected              PASS
        refused by default — unknown command type 'rapid_traverse_undocumented'
  [7/8] Absolute move before homing                PASS
        refused at gate — machine not homed — absolute position is unknown
  [8/8] STOP while everything else is failing      PASS
        stop accepted with every other precondition failing

All eight attempts failed to cause harm.
```

Prefer clicking to reading? `python3 run_demo.py` opens an operator HMI on
`http://localhost:8878` with the normal controls on the left — and switches to
break the machine *while it runs* on the right. Unplug the position sensor
mid-move and watch what happens.

---

## Why this exists

The hard part of software that moves a physical machine is not writing the
motion. It is this: **you cannot finish fixing bugs.** You fix the ones you know
about; the ones that hurt someone are, by definition, the ones you could not
imagine. Asking an engineer — or a model — to simply be more careful is not a
safety design, because carefulness is exactly the thing that fails on a bad day.

So don't put safety in anyone's vigilance. Put it in a layer that refuses.

Classify what the machine can do wrong — UI-level, won't move, damages the
machine, hurts a person, catastrophic — and give each class hard preconditions,
the way a type system refuses to compile a wrong type rather than trusting you
to remember. **The software will be wrong someday. The design's job is to make
sure "wrong" has no capability to become "harm."**

That is the whole idea, and this repo is the smallest honest implementation of
it I could write.

## What it does

* **`machine_gates/gates.py`** — the gate layer. Every command is classified by
  consequence and must satisfy every precondition of its class: E-stop clear,
  position sensor fresh (stale truth is treated as *no* truth), operator
  heartbeat alive, guard closed, cell empty, machine homed, target inside
  physical travel, speed within limit. An unknown command is refused, not
  guessed at — it fails closed.
* **`machine_gates/machine.py`** — the simulated axis plus the supervisor that
  owns it. Two independent defences: commands are gated *before* motion starts,
  and the same truths are re-checked ~50×/s *during* motion, so a command that
  was legal when issued does not stay legal if the world changes underneath it.
  The axis is private; `Supervisor.submit()` is the only path to it, which is
  what makes the gate layer a boundary rather than a convention.
* **`machine_gates/mqtt_bridge.py`** — the MQTT front door. An app publishes
  JSON to `machine/cmd`; verdicts, telemetry and safe-stop events come back on
  their own topics. Reading a command off the network does not make it trusted:
  the bridge has no power the gates don't grant it.
* **`break_it.py`** — the acceptance harness above.
* **`run_demo.py`** — the browser HMI, standard library only.

## Run it against a real broker

The demo needs no broker. To drive the same machine over real MQTT — Mosquitto,
EMQX, HiveMQ, anything:

```bash
docker run --rm -p 1883:1883 eclipse-mosquitto
pip install paho-mqtt
python3 -m machine_gates.mqtt_bridge --broker localhost
```

```
machine/cmd        <- {"type":"move_to","target_mm":400,"speed_mms":120}
machine/telemetry  -> position, speed, and every safety flag, ~10 Hz
machine/verdict    -> one message per command: allowed/refused, class, and why
machine/event      -> safe-stops fired by the watchdogs
```

Refusals are published with a human-readable reason, so the phone or browser on
the other end can *show the operator why*, instead of silently doing nothing —
which is its own kind of failure.

Your app must also publish `{"type":"heartbeat"}` at least every 3 seconds. Stop
sending it — close the app, lose the Wi-Fi, let the phone sleep — and motion is
refused and any move in progress is stopped. That is deliberate: a machine that
keeps moving after the only person watching it has gone away is the failure mode
this whole layer exists to remove.

## What this proves — and what it doesn't

This is a **simulated axis**, not a validated safety system. There is no real
hardware here, and nothing in this repo is a substitute for a rated safety
controller, a risk assessment, or the standards that govern your machine
(ISO 13849 / IEC 62061 and friends). Safety-rated stops belong in hardware and
in a certified controller — that layer sits *below* this one and is not what
this code replaces.

What it does demonstrate, concretely and runnably:

* how to classify commands by consequence instead of by intent;
* how to make the gate layer the only path to the actuator, so there is nothing
  to bypass;
* why the same checks must run as watchdogs during motion, not only at command
  time;
* what "fail closed" costs you in code (very little) and buys you (the whole
  argument above);
* how to hand a non-technical buyer a way to verify invisible work — by trying
  to break the machine themselves, rather than reading a diff.

That last point is the reason this repo exists in public. If you have paid for a
refactor you cannot see, the honest thing to hand you is not an architecture
diagram. It is a checklist for breaking the machine, and the confidence that you
won't be able to.

## The gate table

| Class | Example command | Must hold before it reaches the machine |
|---|---|---|
| `UI_ONLY` | set units, brightness | nothing — it cannot move anything |
| `WONT_MOVE` | stop, e-stop | nothing — **a gate that can block a stop has become the hazard** |
| `DAMAGES_MACHINE` | clear faults | e-stop clear · sensor fresh · heartbeat alive |
| `HURTS_PERSON` | move, jog, home | all of the above · guard closed · cell empty · homed · target in travel · speed within limit |
| `CATASTROPHIC` | anything unrecognised | refused unconditionally |

## Related writing

* [You can't see a refactor. So try to break the machine.](https://boheastill.com/notes/try-to-break-the-machine?r=gh-mqtt-machine-safety-gates) — the acceptance method this repo implements
* [Industrial reliability: change the denominator](https://boheastill.com/notes/industrial-reliability-change-the-denominator?r=gh-mqtt-machine-safety-gates) — why counting bugs is the wrong denominator
* [I use AI heavily. The machine still can't hurt you.](https://boheastill.com/notes/ai-on-machines-that-can-hurt-people?r=gh-mqtt-machine-safety-gates) — what gates like these let you safely do with AI

More runnable demos: **[boheastill.com/demos](https://boheastill.com/demos/?r=gh-mqtt-machine-safety-gates)**

## License

MIT — see [LICENSE](LICENSE).
