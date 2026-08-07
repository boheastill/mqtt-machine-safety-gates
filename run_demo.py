#!/usr/bin/env python3
"""Run the machine with a small operator HMI in your browser. No dependencies.

    python3 run_demo.py           # then open http://localhost:8878

The buttons on the left are what an operator gets. The buttons on the right are
what a normal HMI never gives you: switches to break the machine while it runs.
Press them in any order and watch the readout — every refusal states which gate
stopped it and why.

No broker, no credentials, nothing to install. For the same machine behind a
real MQTT broker, see `python3 -m machine_gates.mqtt_bridge --help`.
"""
from __future__ import annotations

import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from machine_gates.machine import new_supervisor

SUP = new_supervisor()
LOCK = threading.Lock()
VERDICTS: list[dict] = []
PORT = 8878


def machine_loop() -> None:
    """The machine runs whether or not anyone is looking at the HMI."""
    while True:
        with LOCK:
            if not SUP.state.__dict__.get("_sensor_unplugged"):
                SUP.feed_sensor()
            if not SUP.state.__dict__.get("_heartbeat_killed"):
                SUP.feed_heartbeat()
            reason = SUP.tick(0.02)
            if reason:
                VERDICTS.insert(0, {"kind": "safe_stop", "text": reason})
                del VERDICTS[40:]
        time.sleep(0.02)


PAGE = """<!doctype html><meta charset=utf-8>
<title>Gated machine — operator HMI</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{--bg:#12161c;--fg:#e8edf3;--dim:#8b97a6;--ok:#39b58a;--bad:#e05b5b;--line:#2a323d;--accent:#3fb0ad}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 ui-sans-serif,system-ui,sans-serif;padding:22px}
h1{font-size:19px;margin:0 0 4px}p.sub{color:var(--dim);margin:0 0 20px;font-size:13px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px;max-width:900px}
@media(max-width:720px){.grid{grid-template-columns:1fr}}
.panel{border:1px solid var(--line);border-radius:10px;padding:14px}
.panel h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);margin:0 0 12px}
button{font:inherit;background:#1c232c;color:var(--fg);border:1px solid var(--line);border-radius:7px;padding:7px 12px;margin:0 6px 6px 0;cursor:pointer}
button:hover{border-color:var(--accent)}
button.danger:hover{border-color:var(--bad)}
button.on{background:#3a2226;border-color:var(--bad);color:#ffc9c9}
.rail{position:relative;height:26px;background:#0d1116;border:1px solid var(--line);border-radius:6px;margin:6px 0 12px}
.dot{position:absolute;top:3px;width:18px;height:18px;border-radius:50%;background:var(--accent);transition:left .08s linear}
.kv{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;font-size:13px}
.kv b{font-weight:600;color:var(--dim)}
.ok{color:var(--ok)}.bad{color:var(--bad)}
#log{font:12px/1.5 ui-monospace,monospace;max-height:230px;overflow:auto}
#log div{padding:4px 0;border-bottom:1px solid var(--line)}
input{font:inherit;width:76px;background:#0d1116;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px}
</style>
<h1>Gated machine — operator HMI</h1>
<p class=sub>Travel 250–600&nbsp;mm · max 180&nbsp;mm/s. Left: what an operator can do. Right: switches to break it while it runs.</p>
<div class=grid>
  <div class=panel>
    <h2>Machine</h2>
    <div class=rail><div class=dot id=dot></div></div>
    <div class=kv>
      <b>position</b><span id=pos>—</span>
      <b>moving</b><span id=mov>—</span>
      <b>last stop</b><span id=stop>—</span>
      <b>sensor</b><span id=sens>—</span>
      <b>heartbeat</b><span id=hb>—</span>
      <b>guard</b><span id=guard>—</span>
      <b>cell</b><span id=cell>—</span>
      <b>homed</b><span id=homed>—</span>
    </div>
    <h2 style="margin-top:16px">Commands</h2>
    <button onclick="cmd({type:'home'})">Home</button>
    <button onclick="move()">Move to <input id=t value=580 onclick="event.stopPropagation()"> mm</button>
    <button onclick="cmd({type:'move_to',target_mm:900})">Move to 900 (out of travel)</button>
    <button onclick="cmd({type:'move_to',target_mm:400,speed_mms:5000})">Move at 5000 mm/s</button>
    <button onclick="cmd({type:'rapid_traverse_undocumented'})">Send unknown command</button>
    <button class=danger onclick="cmd({type:'estop'})">E-STOP</button>
    <button onclick="cmd({type:'clear_faults'})">Clear faults</button>
  </div>
  <div class=panel>
    <h2>Break it</h2>
    <button id=b_sensor class=danger onclick="fault('sensor')">Unplug position sensor</button>
    <button id=b_hb class=danger onclick="fault('heartbeat')">Kill operator heartbeat</button>
    <button id=b_guard class=danger onclick="fault('guard')">Open guard door</button>
    <button id=b_person class=danger onclick="fault('person')">Person enters cell</button>
    <h2 style="margin-top:16px">Gate log</h2>
    <div id=log></div>
  </div>
</div>
<script>
const $=id=>document.getElementById(id);
async function cmd(c){await fetch('/api/cmd',{method:'POST',body:JSON.stringify(c)});tick()}
async function fault(k){await fetch('/api/fault?k='+k,{method:'POST'});tick()}
function move(){cmd({type:'move_to',target_mm:parseFloat($('t').value),speed_mms:120})}
function yn(v,good){return '<span class="'+(v===good?'ok':'bad')+'">'+(v?'yes':'no')+'</span>'}
async function tick(){
  const s=await (await fetch('/api/state')).json(), t=s.telemetry;
  $('pos').textContent=t.position_mm.toFixed(1)+' mm';
  $('dot').style.left=(((t.position_mm-250)/350)*100)+'%';
  $('mov').innerHTML=yn(t.moving,true);
  $('stop').textContent=t.stopped_reason||'—';
  $('sens').innerHTML=yn(t.sensor_fresh,true);
  $('hb').innerHTML=yn(t.heartbeat_alive,true);
  $('guard').innerHTML=yn(t.guard_door_closed,true);
  $('cell').innerHTML=t.person_in_cell?'<span class=bad>occupied</span>':'<span class=ok>clear</span>';
  $('homed').innerHTML=yn(t.homed,true);
  for(const [id,on] of Object.entries(s.faults)) $('b_'+id).classList.toggle('on',on);
  $('log').innerHTML=s.log.map(l=>'<div class="'+(l.kind==='allow'?'ok':'bad')+'">'+l.text+'</div>').join('');
}
setInterval(tick,250);tick();
</script>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep the console clean
        pass

    def _send(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            with LOCK:
                payload = {
                    "telemetry": SUP.telemetry(),
                    "log": VERDICTS[:20],
                    "faults": {
                        "sensor": bool(SUP.state.__dict__.get("_sensor_unplugged")),
                        "hb": bool(SUP.state.__dict__.get("_heartbeat_killed")),
                        "guard": not SUP.state.guard_door_closed,
                        "person": SUP.state.person_in_cell,
                    },
                }
            self._send(json.dumps(payload).encode(), "application/json")
        else:
            self._send(PAGE.encode(), "text/html; charset=utf-8")

    def do_POST(self):
        if self.path.startswith("/api/cmd"):
            n = int(self.headers.get("Content-Length", 0))
            try:
                command = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                command = {"type": "malformed"}
            with LOCK:
                v = SUP.submit(command)
                VERDICTS.insert(0, {
                    "kind": "allow" if v.allowed else "refuse",
                    "text": f"{'ALLOW ' if v.allowed else 'REFUSE'} [{v.consequence.name}] "
                            f"{command.get('type')} — {v.reason}",
                })
                del VERDICTS[40:]
            self._send(b'{"ok":true}', "application/json")
        elif self.path.startswith("/api/fault"):
            kind = self.path.split("k=")[-1]
            with LOCK:
                st = SUP.state
                if kind == "sensor":
                    on = not st.__dict__.get("_sensor_unplugged")
                    st.__dict__["_sensor_unplugged"] = on
                    if on:
                        SUP.unplug_sensor(2.0)
                elif kind == "heartbeat":
                    on = not st.__dict__.get("_heartbeat_killed")
                    st.__dict__["_heartbeat_killed"] = on
                    if on:
                        SUP.kill_heartbeat(10.0)
                elif kind == "guard":
                    st.guard_door_closed = not st.guard_door_closed
                elif kind == "person":
                    st.person_in_cell = not st.person_in_cell
            self._send(b'{"ok":true}', "application/json")
        else:
            self._send(b"{}", "application/json")


def main() -> None:
    threading.Thread(target=machine_loop, daemon=True).start()
    url = f"http://localhost:{PORT}"
    print(f"\n  Operator HMI: {url}")
    print("  Try: Home, then Move to 580, then hit 'Unplug position sensor' while it runs.")
    print("  Ctrl-C to stop.\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
