"""Localhost server: the page, the cached scan, and your notes on each project."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .scanner import Config, scan_all

HOME = Path(__file__).resolve().parent.parent
PAGE = Path(__file__).parent / "static" / "index.html"
CACHE = HOME / ".cache" / "scan.json"
ANNOTATIONS = HOME / "annotations.json"
CONFIG = HOME / "config.json"

NOTE_FIELDS = {"theme", "description", "status", "next", "note"}
MAX_FIELD = 4000


def write_json(path: Path, value) -> None:
    """Replace atomically, so a crash can't leave half a notes file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    os.replace(temp, path)


class State:
    def __init__(self, config: Config, cache: Path = CACHE, annotations: Path = ANNOTATIONS):
        self.config = config
        self.cache = cache
        self.annotations_path = annotations
        self.lock = threading.Lock()
        self.scanning = False
        self.error: str | None = None
        self.scan = json.loads(cache.read_text()) if cache.exists() else None
        self.annotations = json.loads(annotations.read_text()) if annotations.exists() else {}

    def start_scan(self) -> None:
        with self.lock:
            if self.scanning:
                return
            self.scanning = True
        threading.Thread(target=self._scan, daemon=True).start()

    def _scan(self) -> None:
        try:
            result = scan_all(self.config)
            write_json(self.cache, result)
            with self.lock:
                self.scan, self.error = result, None
        except Exception:
            traceback.print_exc()
            with self.lock:
                self.error = "Scan failed; see the server's terminal output."
        finally:
            with self.lock:
                self.scanning = False

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "scan": self.scan,
                "annotations": self.annotations,
                "dashboards": self.config.dashboards,
                "scanning": self.scanning,
                "error": self.error,
            }

    def project_path(self, name: str) -> str | None:
        with self.lock:
            projects = (self.scan or {}).get("projects", [])
        return next((p["path"] for p in projects if p["name"] == name), None)

    def save_note(self, name: str, fields: dict) -> dict:
        clean = {
            k: str(v).strip()[:MAX_FIELD]
            for k, v in fields.items() if k in NOTE_FIELDS
        }
        with self.lock:
            note = {**self.annotations.get(name, {}), **clean}
            note = {k: v for k, v in note.items() if v}
            if note:
                self.annotations[name] = note
            else:
                self.annotations.pop(name, None)
            write_json(self.annotations_path, self.annotations)
            return note


def make_handler(state: State, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_body(self, body: bytes, content_type: str, status=HTTPStatus.OK):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def send_json(self, value, status=HTTPStatus.OK):
            self.send_body(json.dumps(value).encode(), "application/json", status)

        def host_ok(self) -> bool:
            # Stops another website reaching this server by DNS rebinding.
            return self.headers.get("Host") in allowed_hosts

        def read_json(self):
            # Requiring this type forces a CORS preflight, which is never
            # answered, so another site's page can't post here.
            if self.headers.get("Content-Type") != "application/json":
                return None
            length = int(self.headers.get("Content-Length") or 0)
            try:
                value = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return None
            return value if isinstance(value, dict) else None

        def do_GET(self):
            if not self.host_ok():
                return self.send_json({"error": "bad host"}, HTTPStatus.FORBIDDEN)
            if self.path == "/":
                self.send_body(PAGE.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/data":
                self.send_json(state.snapshot())
            else:
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self):
            if not self.host_ok():
                return self.send_json({"error": "bad host"}, HTTPStatus.FORBIDDEN)
            body = self.read_json()
            if body is None:
                return self.send_json({"error": "expected JSON"}, HTTPStatus.BAD_REQUEST)
            if self.path == "/api/scan":
                state.start_scan()
                return self.send_json({"ok": True})
            name = body.get("name")
            path = state.project_path(name) if isinstance(name, str) else None
            if path is None:
                return self.send_json({"error": "unknown project"}, HTTPStatus.NOT_FOUND)
            if self.path == "/api/note":
                fields = body.get("fields")
                if not isinstance(fields, dict):
                    return self.send_json({"error": "fields required"}, HTTPStatus.BAD_REQUEST)
                self.send_json({"note": state.save_note(name, fields)})
            elif self.path == "/api/open":
                ide = shutil.which("pycharm")
                if ide is None:
                    return self.send_json({"error": "pycharm is not on PATH"}, HTTPStatus.SERVICE_UNAVAILABLE)
                subprocess.Popen(
                    [ide, path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True,
                )
                self.send_json({"ok": True})
            else:
                self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    return Handler


def serve(port: int) -> None:
    state = State(Config.load(CONFIG))
    state.start_scan()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(state, port))
    print(f"Project dashboard: http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
