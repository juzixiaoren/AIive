"""测试用假应用：真实 HTTP 服务，用于验证 Supervisor 的进程生命周期。

参数全部由命令行给出，Supervisor 的 app_command 模板通过 {port} / {slot} 注入：

    python _fake_slot_app.py --port 12345 --slot B [--health-status 503]
        [--fail-slots B] [--unhealthy-slots B] [--unhealthy-port 8000]
        [--startup-delay 0.0] [--exit-after 0.0]

- --fail-slots：指定槽位直接以非零码退出（模拟候选启动失败）
- --unhealthy-slots：指定槽位 /health 返回 503（模拟候选健康检查失败）
- --unhealthy-port：仅在指定端口上不健康（模拟"暂存端口健康、主端口异常"）
- --exit-after：N 秒后自行退出（模拟运行期崩溃）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _build_handler(slot: str, status: int):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802 - http.server 约定
            if self.path not in {"/health", "/"}:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = json.dumps({"slot": slot, "pid": os.getpid()}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            return

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--slot", default="A")
    parser.add_argument("--health-status", type=int, default=200)
    parser.add_argument("--fail-slots", default="")
    parser.add_argument("--unhealthy-slots", default="")
    parser.add_argument("--unhealthy-port", type=int, default=0)
    parser.add_argument("--unhealthy-port-slot", default="")
    parser.add_argument("--startup-delay", type=float, default=0.0)
    parser.add_argument("--exit-after", type=float, default=0.0)
    args = parser.parse_args()

    if args.slot in [item for item in args.fail_slots.split(",") if item]:
        print(f"fake app slot={args.slot} failing on purpose", file=sys.stderr, flush=True)
        return 3

    status = args.health_status
    if args.slot in [item for item in args.unhealthy_slots.split(",") if item]:
        status = 503
    if (
        args.unhealthy_port
        and args.port == args.unhealthy_port
        and (not args.unhealthy_port_slot or args.slot == args.unhealthy_port_slot)
    ):
        status = 503

    if args.startup_delay > 0:
        time.sleep(args.startup_delay)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), _build_handler(args.slot, status))
    if args.exit_after > 0:
        threading.Timer(args.exit_after, lambda: os._exit(0)).start()
    print(f"fake app slot={args.slot} port={args.port} pid={os.getpid()} status={status}", flush=True)
    try:
        server.serve_forever(poll_interval=0.05)
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
