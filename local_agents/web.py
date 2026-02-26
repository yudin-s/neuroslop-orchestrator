from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


INDEX_HTML = """<!doctype html>
<html lang=\"ru\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>local_agents</title>
  <style>
    body { font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial; margin: 16px; }
    .row { display: flex; gap: 16px; flex-wrap: wrap; }
    .card { border: 1px solid #ddd; border-radius: 10px; padding: 12px 14px; min-width: 260px; }
    .k { color: #555; font-size: 12px; }
    .v { font-size: 14px; white-space: pre-wrap; }
    .controls { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin: 12px 0; }
    .controls input { padding: 6px 8px; border: 1px solid #ddd; border-radius: 8px; font-size: 13px; }
    .controls a { display: inline-block; padding: 6px 10px; border: 1px solid #ddd; border-radius: 8px; text-decoration: none; color: #111; font-size: 13px; }
    table { width: 100%; border-collapse: collapse; margin-top: 12px; }
    th, td { border-bottom: 1px solid #eee; text-align: left; padding: 6px 8px; font-size: 13px; }
    th { color: #555; font-weight: 600; }
    code { background: #f6f6f6; padding: 2px 4px; border-radius: 6px; }
    tr.err td { background: #fff3f3; }
    tr.warn td { background: #fffbe6; }
  </style>
</head>
<body>
  <h2>local_agents — наблюдение</h2>
  <div class=\"row\">
    <div class=\"card\">
      <div class=\"k\">Phase</div>
      <div class=\"v\" id=\"phase\">-</div>
    </div>
    <div class=\"card\">
      <div class=\"k\">Role</div>
      <div class=\"v\" id=\"role\">-</div>
    </div>
    <div class=\"card\">
      <div class=\"k\">Task</div>
      <div class=\"v\" id=\"task\">-</div>
    </div>
    <div class=\"card\">
      <div class=\"k\">Counters</div>
      <div class=\"v\" id=\"counters\">artifacts: 0\npatches: 0</div>
    </div>
  </div>

  <p>Источник событий: <code>/events</code>. Лог: <code>artifacts/events.jsonl</code></p>

  <div class=\"controls\">
    <input id=\"filterType\" placeholder=\"filter type (например: patch_|llm_)\" />
    <input id=\"filterRole\" placeholder=\"filter role (backend/frontend/qa/...)\" />
    <input id=\"filterText\" placeholder=\"search in payload\" style=\"min-width: 260px;\" />
    <a href=\"/download\" target=\"_blank\">Скачать events.jsonl</a>
  </div>

  <table>
    <thead>
      <tr><th>ts</th><th>type</th><th>payload</th></tr>
    </thead>
    <tbody id=\"log\"></tbody>
  </table>

<script>
(() => {
  const phaseEl = document.getElementById('phase');
  const roleEl = document.getElementById('role');
  const taskEl = document.getElementById('task');
  const countersEl = document.getElementById('counters');
  const logEl = document.getElementById('log');
  const filterTypeEl = document.getElementById('filterType');
  const filterRoleEl = document.getElementById('filterRole');
  const filterTextEl = document.getElementById('filterText');

  let artifacts = 0;
  let patches = 0;
  let files = 0;
  let buffer = [];

  function updateCounters() {
    countersEl.textContent = `artifacts: ${artifacts}\npatches: ${patches}\nfiles: ${files}`;
  }

  function isMatch(ev) {
    const ft = (filterTypeEl.value || '').trim();
    const fr = (filterRoleEl.value || '').trim();
    const fx = (filterTextEl.value || '').trim();

    if (ft && !(String(ev.type || '').includes(ft))) return false;
    if (fr) {
      const role = (ev.payload && (ev.payload.role || ev.payload.active_role || ev.payload.kind_role)) || '';
      if (!String(role).includes(fr)) return false;
    }
    if (fx) {
      const pl = JSON.stringify(ev.payload || {});
      if (!pl.includes(fx)) return false;
    }
    return true;
  }

  function rowClass(ev) {
    const t = String(ev.type || '');
    if (t === 'error' || t.endsWith('_failed') || t.includes('failed')) return 'err';
    if (t.includes('retry') || t.includes('repair') || t.includes('skipped')) return 'warn';
    return '';
  }

  function addRow(ev) {
    if (!isMatch(ev)) return;
    const tr = document.createElement('tr');
    const cls = rowClass(ev);
    if (cls) tr.className = cls;
    const ts = document.createElement('td');
    const ty = document.createElement('td');
    const pl = document.createElement('td');
    ts.textContent = ev.ts || '';
    ty.textContent = ev.type || '';
    pl.textContent = JSON.stringify(ev.payload || {});
    tr.appendChild(ts); tr.appendChild(ty); tr.appendChild(pl);
    logEl.prepend(tr);

    // keep last 200
    while (logEl.children.length > 200) logEl.removeChild(logEl.lastChild);
  }

  function rerender() {
    logEl.innerHTML = '';
    for (const ev of buffer.slice().reverse()) addRow(ev);
  }

  const es = new EventSource('/events');
  es.onmessage = (msg) => {
    let ev;
    try { ev = JSON.parse(msg.data); } catch { return; }

    buffer.push(ev);
    if (buffer.length > 400) buffer.shift();

    if (ev.type === 'phase') phaseEl.textContent = (ev.payload && ev.payload.phase) || '-';
    if (ev.type === 'task_start') {
      roleEl.textContent = (ev.payload && ev.payload.role) || '-';
      taskEl.textContent = (ev.payload && ev.payload.title) || '-';
    }
    if (ev.type === 'artifact_written') { artifacts += 1; updateCounters(); }
    if (ev.type === 'patch_seen') { patches += 1; updateCounters(); }
    if (ev.type === 'file_written') { files += 1; updateCounters(); }

    addRow(ev);
  };

  es.onerror = () => {
    // браузер сам переподключится
  };

  updateCounters();
  filterTypeEl.addEventListener('input', rerender);
  filterRoleEl.addEventListener('input', rerender);
  filterTextEl.addEventListener('input', rerender);
})();
</script>
</body>
</html>
"""


def serve(*, repo_root: Path, host: str = "127.0.0.1", port: int = 8080) -> None:
    repo_root = repo_root.resolve()
    events_path = repo_root / "artifacts" / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.touch(exist_ok=True)

    class Handler(BaseHTTPRequestHandler):
        server_version = "local_agents/0.1"

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/" or self.path.startswith("/?"):
                self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
                return

            if self.path.startswith("/events"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()

                # stream jsonl as SSE messages
                try:
                    with events_path.open("r", encoding="utf-8") as f:
                        # start from end: we only want new events
                        f.seek(0, 2)
                        while True:
                            line = f.readline()
                            if not line:
                                time.sleep(0.25)
                                continue
                            line = line.strip()
                            if not line:
                                continue

                            # validate it's JSON; if not, skip
                            try:
                                json.loads(line)
                            except Exception:
                                continue

                            data = f"data: {line}\n\n".encode("utf-8")
                            self.wfile.write(data)
                            self.wfile.flush()
                except BrokenPipeError:
                    return
                except ConnectionResetError:
                    return
                return

            if self.path.startswith("/health"):
                payload: dict[str, Any] = {"ok": True}
                self._send(200, json.dumps(payload).encode("utf-8"), "application/json")
                return

            if self.path.startswith("/download"):
                # Скачивание сырых событий (jsonl)
                try:
                    data = events_path.read_bytes()
                except Exception:
                    data = b""
                self.send_response(200)
                self.send_header("Content-Type", "application/jsonl; charset=utf-8")
                self.send_header("Content-Disposition", "attachment; filename=events.jsonl")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            self._send(404, b"not found", "text/plain; charset=utf-8")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            # keep console quiet
            return

    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"local_agents web UI: http://{host}:{port}  (streaming {events_path})")
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
