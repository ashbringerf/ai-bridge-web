#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mify_relay_client.py  ——  同事电脑B 端的 mify 中转客户端 (需求3, 走 GitHub, 无穿透)

原理:
  在同事电脑B 本地跑一个 HTTP 服务, agent(opencode/claude/codex) 把模型请求发到它.
  它把请求写进 GitHub 信箱 mify_req/<id>.json, 轮询 mify_resp/<id>.json 取回结果,
  返回给 agent. 电脑A 的 bridge.py 负责真正用内网 key 调 mify.

  全程只连 GitHub, 双方都不开入站端口 —— 与需求1/2 同样合规, 无内网穿透.

  代价: 每次模型往返都经 GitHub 轮询(秒级延迟), agent 复杂任务会较慢. 这是合规的取舍.

配置(环境变量或同目录 .env):
  BRIDGE_GH_TOKEN    GitHub token(对信箱仓库有 Contents 读写)
  BRIDGE_REPO_OWNER  信箱仓库 owner
  BRIDGE_REPO_NAME   默认 opencode-bridge
  BRIDGE_BRANCH      默认 main
  RELAY_PORT         本地监听端口, 默认 8799
  RELAY_POLL         轮询间隔秒, 默认 2
  RELAY_TIMEOUT      单次请求最长等待秒, 默认 300

同事电脑B 的 agent 配置: baseURL 指向 http://127.0.0.1:8799/v1 (或 /anthropic)

依赖: requests
"""

import os
import sys
import time
import json
import base64
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv():
    path = os.path.join(_HERE, ".env")
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception:
        pass


_load_dotenv()

GH_TOKEN   = os.environ.get("BRIDGE_GH_TOKEN", "")
REPO_OWNER = os.environ.get("BRIDGE_REPO_OWNER", "")
REPO_NAME  = os.environ.get("BRIDGE_REPO_NAME", "opencode-bridge")
BRANCH     = os.environ.get("BRIDGE_BRANCH", "main")
RELAY_PORT = int(os.environ.get("RELAY_PORT", "8799"))
RELAY_POLL = float(os.environ.get("RELAY_POLL", "1.2"))
RELAY_TIMEOUT = int(os.environ.get("RELAY_TIMEOUT", "300"))

API = "https://api.github.com"
DIR_REQ  = "mify_req"
DIR_RESP = "mify_resp"

S = requests.Session()
S.headers.update({
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
})


def _url(path):
    return f"{API}/repos/{REPO_OWNER}/{REPO_NAME}/contents/{path}"


def _put(path, text, msg):
    body = {"message": msg,
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "branch": BRANCH}
    r = S.put(_url(path), json=body)
    r.raise_for_status()


def _get(path):
    r = S.get(_url(path), params={"ref": BRANCH})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    obj = r.json()
    text = base64.b64decode(obj["content"]).decode("utf-8", "replace")
    # 消费掉
    try:
        S.delete(_url(path), json={"message": "consume", "sha": obj["sha"], "branch": BRANCH})
    except Exception:
        pass
    return text


def relay(path, method, body):
    """把一次模型请求经 GitHub 中转, 返回 (status, text).
    轮询策略: 快速起步(前几秒高频 0.5s), 之后放缓到 RELAY_POLL, 兼顾延迟与 API 用量.
    """
    rid = str(int(time.time() * 1000)) + "_" + str(os.getpid() % 10000)
    req = json.dumps({"path": path, "method": method, "body": body})
    _put(f"{DIR_REQ}/{rid}.json", req, f"mify req {rid}")
    waited = 0.0
    # 首次不白等一整轮: 先给 A 一点处理时间再查
    first_delay = 0.8
    time.sleep(first_delay)
    waited += first_delay
    while waited < RELAY_TIMEOUT:
        resp = _get(f"{DIR_RESP}/{rid}.json")
        if resp is not None:
            obj = json.loads(resp)
            return obj.get("status", 200), obj.get("body", "")
        # 前 8 秒高频查(0.5s), 之后放缓到 RELAY_POLL(默认更大)以省 API
        step = 0.5 if waited < 8 else RELAY_POLL
        time.sleep(step)
        waited += step
    return 504, '{"error":"relay timeout (电脑A bridge 可能没运行)"}'


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _do(self):
        if self.path.split("?", 1)[0] in ("/", "/health"):
            self._send(200, '{"ok":true,"service":"mify-relay-client"}')
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        status, text = relay(self.path, self.command, body)
        self._send(status, text)

    def _send(self, code, text):
        data = text.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass  # opencode 提前断开连接, 无害, 静默

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.close_connection = True  # 客户端断开, 静默收尾

    def do_GET(self):
        self._do()

    def do_POST(self):
        self._do()

    def log_message(self, fmt, *args):
        sys.stderr.write("[relay] " + (fmt % args) + "\n")


def main():
    print("=" * 60)
    print(" mify 中转客户端 (同事电脑B, 走 GitHub 无穿透)")
    print(f" 本地监听 : http://127.0.0.1:{RELAY_PORT}")
    print(f" 信箱     : {REPO_OWNER}/{REPO_NAME}@{BRANCH}")
    print(f" 轮询/超时: {RELAY_POLL}s / {RELAY_TIMEOUT}s")
    print("=" * 60)
    print(" agent 配置: baseURL = http://127.0.0.1:%d/v1 (或 /anthropic)" % RELAY_PORT)
    print(" 注意: 每次模型往返经 GitHub, 会比直连慢. 这是合规(不穿透)的取舍.")
    print("=" * 60)
    if not GH_TOKEN or not REPO_OWNER:
        print("!! 需在 .env 配 BRIDGE_GH_TOKEN / BRIDGE_REPO_OWNER")
        sys.exit(1)
    ThreadingHTTPServer(("127.0.0.1", RELAY_PORT), Handler).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已停止.")
