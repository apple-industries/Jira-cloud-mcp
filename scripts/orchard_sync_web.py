#!/usr/bin/env python3
"""Beta-lab status page for the Orchard→Jira sync.

A tiny, dependency-free web UI over scripts/orchard_jira_sync.py:
  GET  /             → status page (last run, per-module counts, "Run sync now")
  POST /run          → kick off full `orchard_jira_sync.py --apply` in the background
  POST /run/<module> → run ONE module synchronously, return JSON result. Token-authed via
                       the X-Sync-Token header (machine-to-machine; e.g. the Jira
                       "Sync Back Office Org/Operator now" manual automation). module ∈
                       {content-codes, products, unit-product, sim-types, org-operator}.
  GET  /api/status   → JSON (last-run file + whether a run is in progress)

The GET page / POST /run are meant to sit behind the beta-lab Cloudflare Access + Entra
portal (no per-user auth here). The /run/<module> route additionally requires the shared
X-Sync-Token so automated callers are authorized even through an Access service token.
Binds 127.0.0.1 by default (set ORCHARD_SYNC_WEB_HOST=0.0.0.0 only behind the tunnel).

Run:  python3 scripts/orchard_sync_web.py         (http://127.0.0.1:8787)
Env:  ORCHARD_SYNC_WEB_HOST, ORCHARD_SYNC_WEB_PORT, ORCHARD_SYNC_STATUS_FILE,
      ORCHARD_SYNC_RUN_TOKEN (shared secret for /run/<module>)
"""
import html
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SYNC = os.path.join(HERE, "orchard_jira_sync.py")
STATUS_FILE = os.environ.get("ORCHARD_SYNC_STATUS_FILE", os.path.join(HERE, ".last_sync.json"))
HOST = os.environ.get("ORCHARD_SYNC_WEB_HOST", "127.0.0.1")
PORT = int(os.environ.get("ORCHARD_SYNC_WEB_PORT", "8787"))
# Shared secret required on the machine-to-machine /run/<module> route (e.g. the Jira
# "Sync now" automation). If unset, the route is open (local dev only). Set in prod.
RUN_TOKEN = os.environ.get("ORCHARD_SYNC_RUN_TOKEN", "")
ALLOWED_MODULES = {"content-codes", "products", "unit-product", "sim-types", "org-operator"}

_state = {"running": False, "started_at": None, "output": ""}
_lock = threading.Lock()


def _run_module(module: str) -> dict:
    """Run a single sync module synchronously; return a compact result for the caller
    (e.g. the Jira automation's web-request, so its comment can report the outcome)."""
    try:
        proc = subprocess.run([sys.executable, SYNC, "--only", module, "--apply"],
                              capture_output=True, text=True, timeout=180)
        out = ((proc.stderr or "") + (proc.stdout or "")).strip()
        tail = "\n".join(out.splitlines()[-6:])
        return {"module": module, "ok": proc.returncode == 0, "returncode": proc.returncode,
                "summary": tail}
    except Exception as e:  # noqa: BLE001
        return {"module": module, "ok": False, "returncode": -1,
                "summary": f"run failed: {type(e).__name__}: {e}"}


def _run_sync():
    try:
        proc = subprocess.run([sys.executable, SYNC, "--apply"],
                              capture_output=True, text=True, timeout=1800)
        out = (proc.stdout or "") + (proc.stderr or "")
    except Exception as e:  # noqa: BLE001
        out = f"run failed: {type(e).__name__}: {e}"
    with _lock:
        _state["running"] = False
        _state["output"] = out[-8000:]


def _start_run() -> bool:
    with _lock:
        if _state["running"]:
            return False
        _state["running"] = True
        _state["output"] = ""
    threading.Thread(target=_run_sync, daemon=True).start()
    return True


def _load_status() -> dict:
    try:
        return json.load(open(STATUS_FILE))
    except Exception:
        return {}


def _page() -> bytes:
    st = _load_status()
    with _lock:
        running = _state["running"]
        output = _state["output"]
    badge = ("RUNNING…" if running else
             ("OK" if st.get("ok") else ("ERROR" if st else "—")))
    cls = "run" if running else ("ok" if st.get("ok") else ("err" if st else ""))
    rows = ""
    for m in st.get("results", []):
        warn = m.get("warnings") or ([] if m.get("ok", True) else ["see run output"])
        note = (f'<span class="warn">{html.escape(", ".join(map(str, warn)))}</span>'
                if warn else ("delegated" if m.get("delegated") else "ok"))
        counts = ("—" if m.get("delegated")
                  else f'source {m.get("source","?")} · +{m.get("create","?")} new · {m.get("update","?")} updated')
        rows += (f"<tr><td>{html.escape(m.get('module',''))}</td>"
                 f"<td>{counts}</td><td>{note}</td></tr>")
    refresh = '<meta http-equiv="refresh" content="4">' if running else ""
    disabled = "disabled" if running else ""
    out_html = (f'<pre class="out">{html.escape(output)}</pre>' if output else "")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{refresh}
<title>Orchard → Jira Sync</title><style>
:root{{color-scheme:light dark}}
body{{font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem}}
h1{{font-size:1.3rem;margin:0 0 .25rem}}
.sub{{color:#888;margin:0 0 1.25rem}}
.badge{{display:inline-block;padding:.15rem .6rem;border-radius:1rem;font-weight:600;font-size:.85rem}}
.badge.ok{{background:#1f7a3d;color:#fff}} .badge.err{{background:#b3261e;color:#fff}}
.badge.run{{background:#9a6700;color:#fff}} .badge{{background:#666;color:#fff}}
table{{width:100%;border-collapse:collapse;margin:1rem 0}}
th,td{{text-align:left;padding:.5rem .4rem;border-bottom:1px solid #8883}}
th{{font-size:.8rem;text-transform:uppercase;letter-spacing:.03em;color:#888}}
.warn{{color:#9a6700;font-weight:600}}
button{{font:inherit;font-weight:600;padding:.6rem 1.1rem;border:0;border-radius:.5rem;background:#0b5cff;color:#fff;cursor:pointer}}
button:disabled{{opacity:.5;cursor:default}}
.out{{white-space:pre-wrap;background:#8881;padding:.75rem;border-radius:.5rem;font-size:.8rem;overflow:auto;max-height:320px}}
.meta{{color:#888;font-size:.85rem}}
</style></head><body>
<h1>Orchard → Jira Reference Data Sync</h1>
<p class="sub">Assets Products &amp; Content Codes + Back Office Org/Operator field · <span class="meta">nightly 6 AM</span></p>
<p>Status: <span class="badge {cls}">{badge}</span>
&nbsp;<span class="meta">last run: {html.escape(str(st.get('finishedAt','never')))} ({html.escape(str(st.get('mode','')))})</span></p>
<table><tr><th>Module</th><th>Result</th><th>Notes</th></tr>{rows or '<tr><td colspan=3 class=meta>no run recorded yet</td></tr>'}</table>
<form method="post" action="/run"><button {disabled}>{'Running…' if running else 'Run sync now'}</button></form>
{out_html}
</body></html>""".encode()


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/status"):
            body = json.dumps({"running": _state["running"], "status": _load_status()}).encode()
            return self._send(200, body, "application/json")
        if self.path in ("/", "/index.html"):
            return self._send(200, _page())
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path == "/run":
            _start_run()
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()
            return
        # Machine-to-machine: POST /run/<module> runs one module synchronously and returns
        # JSON (used by the Jira "Sync now" automation so its comment reports the result).
        if self.path.startswith("/run/"):
            module = self.path[len("/run/"):].strip("/")
            if RUN_TOKEN and self.headers.get("X-Sync-Token") != RUN_TOKEN:
                return self._send(401, b'{"error":"unauthorized"}', "application/json")
            if module not in ALLOWED_MODULES:
                return self._send(
                    404, json.dumps({"error": "unknown module", "allowed": sorted(ALLOWED_MODULES)}).encode(),
                    "application/json")
            result = _run_module(module)
            return self._send(200 if result["ok"] else 502,
                              json.dumps(result).encode(), "application/json")
        self._send(404, b"not found", "text/plain")

    def log_message(self, *a):  # quiet
        pass


if __name__ == "__main__":
    print(f"Orchard sync status page → http://{HOST}:{PORT}", file=sys.stderr)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
