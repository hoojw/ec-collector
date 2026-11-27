#!/usr/bin/env python3
"""Small internal API for the DataEase Meituan sync button."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import subprocess
import threading
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

try:
    import fcntl
except ImportError:  # Windows development/test fallback; production runs on Linux.
    fcntl = None


TZ = ZoneInfo("Asia/Shanghai")
COOLDOWN_SECONDS = 300
SERVICE_NAME = "subsidy-pipeline.service"
ALLOWED_ORIGIN = "http://172.21.0.2:8100"
LOCAL_LOCK = threading.Lock()


def load_json(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value).astimezone(TZ)
    except ValueError:
        return None


class SyncController:
    def __init__(self, state_file: pathlib.Path, lock_file: pathlib.Path):
        self.state_file = state_file
        self.lock_file = lock_file
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)

    def service_active(self) -> bool:
        result = subprocess.run(
            ["systemctl", "is-active", "--quiet", SERVICE_NAME],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0

    def status(self) -> dict:
        payload = load_json(self.state_file)
        if self.service_active():
            payload["state"] = "running"
        payload.setdefault("state", "idle")
        payload["cooldown_seconds"] = self.cooldown_remaining(payload)
        return payload

    def cooldown_remaining(self, payload: dict | None = None) -> int:
        payload = payload if payload is not None else load_json(self.state_file)
        if payload.get("state") != "success":
            return 0
        finished = parse_time(payload.get("finished_at"))
        if finished is None:
            return 0
        elapsed = (dt.datetime.now(TZ) - finished).total_seconds()
        return max(0, COOLDOWN_SECONDS - int(elapsed))

    @contextmanager
    def exclusive_lock(self):
        if fcntl is None:
            LOCAL_LOCK.acquire()
            try:
                yield
            finally:
                LOCAL_LOCK.release()
            return
        with self.lock_file.open("a+") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def trigger(self) -> tuple[int, dict]:
        try:
            lock_context = self.exclusive_lock()
            lock_context.__enter__()
        except BlockingIOError:
            return HTTPStatus.CONFLICT, {"state": "running", "message": "同步已在启动"}
        try:
            current = self.status()
            if current.get("state") == "running":
                return HTTPStatus.CONFLICT, current
            remaining = self.cooldown_remaining(current)
            if remaining:
                return HTTPStatus.TOO_MANY_REQUESTS, {
                    **current,
                    "message": "上一次同步成功后的冷却时间尚未结束",
                    "retry_after_seconds": remaining,
                }

            result = subprocess.run(
                ["systemctl", "start", "--no-block", SERVICE_NAME],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return HTTPStatus.INTERNAL_SERVER_ERROR, {
                    "state": "failed",
                    "message": (result.stderr or result.stdout or "无法启动同步服务").strip(),
                }
            return HTTPStatus.ACCEPTED, {"state": "running", "message": "美团同步已启动"}
        finally:
            lock_context.__exit__(None, None, None)


class Handler(BaseHTTPRequestHandler):
    controller: SyncController

    def log_message(self, format, *args):
        return

    def send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        origin = self.headers.get("Origin")
        self.send_response(status)
        if origin == ALLOWED_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
            self.send_header("Vary", "Origin")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        origin = self.headers.get("Origin")
        self.send_response(HTTPStatus.NO_CONTENT)
        if origin == ALLOWED_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self.send_json(HTTPStatus.OK, {"state": "ok"})
        elif self.path == "/v1/meituan/status":
            self.send_json(HTTPStatus.OK, self.controller.status())
        else:
            self.send_json(HTTPStatus.NOT_FOUND, {"message": "not found"})

    def do_POST(self):
        if self.path == "/v1/meituan/sync":
            status, payload = self.controller.trigger()
            self.send_json(status, payload)
        else:
            self.send_json(HTTPStatus.NOT_FOUND, {"message": "not found"})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8311)
    parser.add_argument("--state-file", type=pathlib.Path, default=pathlib.Path("/var/lib/subsidy-sync/meituan-status.json"))
    parser.add_argument("--lock-file", type=pathlib.Path, default=pathlib.Path("/run/lock/meituan-sync-api.lock"))
    args = parser.parse_args()
    Handler.controller = SyncController(args.state_file, args.lock_file)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
