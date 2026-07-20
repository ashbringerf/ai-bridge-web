#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bridge.py  ——  公司内网电脑端桥接程序 (多引擎 + 截图版)

作用:
  轮询 GitHub 仓库的 to_pc/ 目录, 收到手机发来的指令后, 以无头模式调用
  对应的 AI CLI (opencode / claude / codex), 把纯文本输出写回 to_phone/;
  另支持截图指令, 把电脑当前屏幕截图回传手机.

协议 (与 android 端一致):
  指令文本第一行可带引擎标记, 可选带模型:
    @opencode <prompt>              -> opencode run "<prompt>"        (默认)
    @claude   <prompt>              -> claude -p "<prompt>"
    @codex    <prompt>              -> codex exec "<prompt>"
    @opencode:mify/ppio/pa/xxx <p>  -> opencode run -m mify/ppio/pa/xxx "<p>"
    @claude:sonnet <p>              -> claude -p --model sonnet "<p>"
    @codex:gpt-5.5 <p>              -> codex exec -m gpt-5.5 "<p>"
    (即 @引擎:模型 prompt; 不带 :模型 用各自默认)
  特殊指令:
    @screenshot          -> 截屏, 回复为 IMG:<base64png>, 手机端识别后显示图片
  回复约定:
    普通文本   -> 直接写文本
    图片       -> 首行 "IMG:" + 紧跟 base64(png), 手机端据此渲染

合规说明:
  全程只用 GitHub HTTPS API (api.github.com), 不打隧道/不映射端口.

依赖:
  pip install requests
  截图需要 pillow (pip install pillow); 若未装则截图指令返回提示.

配置: 见下方 CONFIG 区, 或用环境变量覆盖.
"""

import os
import re
import io
import sys
import time
import base64
import subprocess
import traceback

# 匹配 ANSI 转义序列(颜色/光标控制码)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _clean(text: str) -> str:
    """去掉 ANSI 转义码, 让手机端看到干净文本."""
    return _ANSI_RE.sub("", text)


import shutil
import requests

# ============ 可移植配置 (支持迁移到任意电脑) ============
# 优先级: 环境变量 > 同目录 .env 文件 > 自动探测 > 内置默认
#
# 迁移到新电脑只需:
#   1. 装好 opencode / claude / codex (在 PATH 里即可, 无需改路径)
#   2. 复制 pc_bridge 目录 + 填一个 .env (至少 BRIDGE_GH_TOKEN)
#   3. 双击 start_bridge.bat
# 路径会自动探测, 不用再改任何硬编码.

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv():
    """读取脚本同目录的 .env, 写入 os.environ (不覆盖已有环境变量)."""
    path = os.path.join(_HERE, ".env")
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8-sig") as f:  # utf-8-sig 自动去 BOM
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception as e:
        print(f"[warn] 读取 .env 失败: {e}")


_load_dotenv()


def _find_bin(env_key, *candidates):
    """按 环境变量 > which(PATH) > 候选路径 顺序找可执行文件.
    找不到就返回第一个名字(让运行时报清晰错误)."""
    v = os.environ.get(env_key)
    if v:
        return v
    # 逐个候选: 有的直接是命令名(交给 shutil.which 查 PATH), 有的是绝对路径
    for c in candidates:
        if os.path.isabs(c):
            if os.path.isfile(c):
                return c
        else:
            found = shutil.which(c)
            if found:
                return found
    return candidates[0]


# ---- GitHub 信箱 (这些跨电脑不变, 但仍可用 .env / 环境变量覆盖) ----
GITHUB_TOKEN = os.environ.get("BRIDGE_GH_TOKEN", "")  # 真实值放 .env, 不硬编码
REPO_OWNER   = os.environ.get("BRIDGE_REPO_OWNER", "ashbringerf")
REPO_NAME    = os.environ.get("BRIDGE_REPO_NAME", "opencode-bridge")
BRANCH       = os.environ.get("BRIDGE_BRANCH", "main")

# ---- 工作目录: 默认取脚本同目录下的 workspace, 自动跨电脑 ----
WORKDIR      = os.environ.get("BRIDGE_WORKDIR", os.path.join(_HERE, "workspace"))
if not os.path.isdir(WORKDIR):
    try:
        os.makedirs(WORKDIR, exist_ok=True)
    except Exception:
        WORKDIR = _HERE  # 兜底

# ---- 各引擎可执行文件: 自动探测 PATH, 无需硬编码 ----
# 候选里既有命令名(走 PATH), 也保留原电脑A的绝对路径做兜底
OPENCODE_BIN = _find_bin("BRIDGE_OPENCODE_BIN",
                         "opencode.cmd", "opencode",
                         r"C:\Users\MI\AppData\Roaming\npm\opencode.cmd")
CLAUDE_BIN   = _find_bin("BRIDGE_CLAUDE_BIN",
                         "claude.exe", "claude",
                         r"C:\Users\MI\.local\bin\claude.exe")
CODEX_BIN    = _find_bin("BRIDGE_CODEX_BIN",
                         "codex.cmd", "codex",
                         r"C:\Users\MI\AppData\Roaming\npm\codex.cmd")

POLL_INTERVAL = float(os.environ.get("BRIDGE_POLL", "1"))          # 轮询间隔(秒)
CMD_TIMEOUT   = int(os.environ.get("BRIDGE_CMD_TIMEOUT", "600"))   # 单条指令超时(秒)

# ---- 多用户 (需求2) ----
# 信箱结构:  to_pc/<user>/<ts>.msg   to_phone/<user>/<ts>.msg
# 兼容: 根目录 to_pc/*.msg 视为默认用户 (DEFAULT_USER)
# 主人(OWNER_USER)用完整 WORKDIR; 其他用户各自受限子目录 WORKDIR/users/<user>
#
# .env 里可配:
#   BRIDGE_OWNER_USER=me            主人用户名(拥有完整 workdir)
#   BRIDGE_ALLOW_USERS=me,alice,bob 允许的用户白名单(逗号分隔, 空=允许任意)
DEFAULT_USER = os.environ.get("BRIDGE_DEFAULT_USER", "default")
OWNER_USER   = os.environ.get("BRIDGE_OWNER_USER", DEFAULT_USER)
_allow = os.environ.get("BRIDGE_ALLOW_USERS", "").strip()
ALLOW_USERS  = [u.strip() for u in _allow.split(",") if u.strip()] if _allow else None  # None=不限

# ---- 统一账号体系: users.json (口令 + 配额 + 自配key) ----
import json as _json
USERS = {}          # {user: {token, quota_mb, owner, keys}}
_users_path = os.path.join(_HERE, "users.json")
if os.path.isfile(_users_path):
    try:
        with open(_users_path, "r", encoding="utf-8-sig") as f:
            USERS = (_json.load(f) or {}).get("users", {})
        print(f"[users] 加载 {len(USERS)} 个账号: {', '.join(USERS.keys())}")
    except Exception as e:
        print(f"[warn] 读取 users.json 失败: {e}")

DEFAULT_QUOTA_MB = int(os.environ.get("BRIDGE_QUOTA_MB", "1024"))  # 默认每用户 1GB
# ========================================================

API = "https://api.github.com"
DIR_TO_PC       = "to_pc"        # 手机 -> 电脑A (模式①: 在A执行)
DIR_TO_PC_LOCAL = "to_pc_local"  # 手机 -> 用户本地电脑 (模式③: 在用户电脑执行, 模型借A)
DIR_TO_PHONE    = "to_phone"     # 电脑 -> 手机

# 角色: admin=电脑A(默认, 扫 to_pc + mify_req); local=用户本地(扫 to_pc_local/<自己>, 不碰 mify_req)
BRIDGE_ROLE = os.environ.get("BRIDGE_ROLE", "admin").strip().lower()
# 本地角色只认领这个用户名下的指令 (模式③每台用户电脑绑定一个登录用户)
BRIDGE_LOCAL_USER = re.sub(r"[^a-zA-Z0-9_\-]", "", os.environ.get("BRIDGE_LOCAL_USER", ""))[:40]

SESSION = requests.Session()
SESSION.headers.update({
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
})


def _contents_url(path: str) -> str:
    return f"{API}/repos/{REPO_OWNER}/{REPO_NAME}/contents/{path}"


def list_dir(path: str):
    """列目录; 目录不存在时返回空列表."""
    r = SESSION.get(_contents_url(path), params={"ref": BRANCH})
    if r.status_code == 404:
        return []
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict):   # 单文件而非目录
        return [data]
    return data


def read_file(item: dict) -> str:
    """读取一个文件条目的文本内容."""
    if item.get("content"):
        return base64.b64decode(item["content"]).decode("utf-8", "replace")
    dl = item.get("download_url")
    if dl:
        r = SESSION.get(dl)
        r.raise_for_status()
        return r.text
    r = SESSION.get(_contents_url(item["path"]), params={"ref": BRANCH})
    r.raise_for_status()
    return base64.b64decode(r.json()["content"]).decode("utf-8", "replace")


def put_file(path: str, text: str, message: str):
    """创建/覆盖一个文件."""
    body = {
        "message": message,
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
        "branch": BRANCH,
    }
    r = SESSION.put(_contents_url(path), json=body)
    r.raise_for_status()
    return r.json()


def delete_file(item: dict, message: str):
    """删除一个文件(消费掉指令)."""
    body = {"message": message, "sha": item["sha"], "branch": BRANCH}
    r = SESSION.delete(_contents_url(item["path"]), json=body)
    if r.status_code not in (200, 422):  # 422: 已被删
        r.raise_for_status()


# ============ 引擎调度 ============

def _parse_engine(prompt: str):
    """解析引擎标记 + 可选模型, 返回 (engine, model, real_prompt).

    支持首个 token 为:
      @opencode / @claude / @codex / @screenshot
      @引擎:模型   (模型可含 / . - 等, 如 @opencode:mify/ppio/pa/claude-opus-4-8)
    不带标记默认 opencode; 不带 :模型 则 model 为 None(用各引擎默认).
    """
    stripped = prompt.lstrip()
    # 引擎名 = 字母数字; 模型 = 冒号后到第一个空白前的任意非空白
    m = re.match(r"^@(\w+)(?::(\S+))?[ \t]*", stripped)
    if not m:
        return "opencode", None, prompt
    engine = m.group(1).lower()
    model = m.group(2)  # 可能为 None
    rest = stripped[m.end():]
    if engine in ("opencode", "claude", "codex", "screenshot"):
        return engine, model, rest
    # 未知标记, 当普通 prompt 交给默认引擎
    return "opencode", None, prompt


def _run_cli(argv, label, cwd=None, extra_env=None):
    """通用: 无头执行一个 CLI, 返回清洗后的合并输出. extra_env=按用户注入的key."""
    env = None
    if extra_env:
        env = os.environ.copy()
        env.update(extra_env)
    # Windows: 若可执行是 .cmd/.bat/.ps1 (npm 全局装的 opencode 常是 .cmd/.ps1),
    # 不能直接被 subprocess 当 exe 跑 (WinError 193). 用对应解释器包裹.
    if os.name == "nt" and argv:
        low = str(argv[0]).lower()
        if low.endswith((".cmd", ".bat")):
            argv = ["cmd", "/c"] + list(argv)
        elif low.endswith(".ps1"):
            argv = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"] + list(argv)
    try:
        proc = subprocess.run(
            argv,
            cwd=cwd or WORKDIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CMD_TIMEOUT,
            env=env,
        )
        out = _clean(proc.stdout or "").strip()
        err = _clean(proc.stderr or "").strip()
        combined = out
        if err:
            combined += ("\n" if combined else "") + err
        if proc.returncode != 0:
            combined += f"\n[{label} exit code: {proc.returncode}]"
        return combined.strip() or f"({label} 无输出)"
    except subprocess.TimeoutExpired:
        return f"(执行超时, 超过 {CMD_TIMEOUT}s)"
    except FileNotFoundError:
        return f"(找不到 {label} 可执行文件: {argv[0]})"
    except Exception as e:
        return f"({label} 执行出错: {e})"


def run_opencode(prompt: str, model: str = None, cwd: str = None, extra_env=None) -> str:
    argv = [OPENCODE_BIN, "run"]
    if model:
        argv += ["-m", model]
    argv.append(prompt)
    return _run_cli(argv, "opencode", cwd, extra_env)


def run_claude(prompt: str, model: str = None, cwd: str = None, extra_env=None) -> str:
    # -p 无头; 权限由 ~/.claude/settings.json 的 bypassPermissions 放开
    argv = [CLAUDE_BIN, "-p"]
    if model:
        argv += ["--model", model]
    argv.append(prompt)
    return _run_cli(argv, "claude", cwd, extra_env)


def run_codex(prompt: str, model: str = None, cwd: str = None, extra_env=None) -> str:
    # codex 无头子命令 exec; --skip-git-repo-check 允许在非 git 目录运行;
    # --output-last-message 把最终回复写到文件, 避免 banner/session 等噪声;
    # 放权/沙箱由 ~/.codex/config.toml (approval_policy=never, danger-full-access) 控制;
    # 走公司内网需要 MIFY_API_KEY 环境变量 (见 config.toml 的 env_key)
    import tempfile
    fd, last_path = tempfile.mkstemp(suffix=".txt", prefix="codex_last_")
    os.close(fd)
    argv = [CODEX_BIN, "exec", "--skip-git-repo-check"]
    if model:
        argv += ["-m", model]
    argv += ["--output-last-message", last_path, prompt]
    try:
        raw = _run_cli(argv, "codex", cwd, extra_env)
        # 优先返回最终消息文件内容; 读不到则回退到原始输出
        try:
            with open(last_path, "r", encoding="utf-8", errors="replace") as f:
                final = f.read().strip()
            if final:
                return final
        except Exception:
            pass
        return raw
    finally:
        try:
            os.remove(last_path)
        except Exception:
            pass


def take_screenshot() -> str:
    """截取整个桌面, 返回 'IMG:' + base64(png). 失败返回文本说明."""
    try:
        from PIL import ImageGrab
    except ImportError:
        return "(截图需要 pillow: pip install pillow)"
    try:
        img = ImageGrab.grab(all_screens=True)
        # 控制尺寸, 避免 base64 过大 (GitHub 单文件建议 < 1MB)
        max_w = 1600
        if img.width > max_w:
            ratio = max_w / img.width
            img = img.resize((max_w, int(img.height * ratio)))
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return "IMG:" + b64
    except Exception as e:
        return f"(截图失败: {e})"


_USER_RE = re.compile(r"[^a-zA-Z0-9_\-]")


def _safe_user(name: str) -> str:
    """净化用户名, 防目录穿越. 只留字母数字_-."""
    return _USER_RE.sub("", (name or "").strip())[:40] or DEFAULT_USER


def workdir_for(user: str) -> str:
    """按用户返回工作目录:
    1) 主人 -> 完整 WORKDIR
    2) users.json 里该用户配了 workdir -> 用它(管理员给用户指定的工程目录)
    3) 否则 -> WORKDIR/temp/<user> (默认隔离目录)
    """
    if user == OWNER_USER:
        return WORKDIR
    # 模式③ local 角色: 整台电脑就服务这一个用户, 直接用其配置的工程目录 WORKDIR
    if BRIDGE_ROLE == "local":
        return WORKDIR
    # users.json 里配的绝对路径优先
    u = USERS.get(user, {})
    cfg_wd = (u.get("workdir") or "").strip()
    if cfg_wd:
        try:
            os.makedirs(cfg_wd, exist_ok=True)
        except Exception:
            pass
        return cfg_wd
    d = os.path.join(WORKDIR, "temp", user)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def check_token(user: str, token: str) -> bool:
    """校验用户口令. users.json 里没配该用户 -> 看白名单(兼容). 配了则必须匹配."""
    u = USERS.get(user)
    if u is None:
        # 未在 users.json 定义: 回退到白名单机制(无口令)
        return ALLOW_USERS is None or user in ALLOW_USERS
    # 主人/空口令用户不校验; 否则必须匹配
    if not u.get("token"):
        return True
    return (token or "") == u["token"]


def _dir_size_mb(path: str) -> float:
    """目录总大小(MB). 目录不存在返回0."""
    total = 0
    for root, _, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except Exception:
                pass
    return total / (1024 * 1024)


def check_quota(user: str, cwd: str):
    """检查用户 workspace 配额. 超限返回错误字符串, 未超返回 None."""
    u = USERS.get(user, {})
    quota = u.get("quota_mb", DEFAULT_QUOTA_MB)
    if not quota or quota <= 0:      # 0/未配 = 不限
        return None
    used = _dir_size_mb(cwd)
    if used >= quota:
        return f"(空间已满: 已用 {used:.0f}MB / 上限 {quota}MB. 请清理 workspace 后再试)"
    return None


def user_env(user: str, engine: str):
    """按用户返回要注入的环境变量(自配 key). 不配则空(用电脑A默认)."""
    env = {}
    u = USERS.get(user, {})
    keys = u.get("keys", {}) or {}
    # 引擎 -> 该用哪个 provider 的 key
    # opencode 默认走内网, 若用户配了 key 则注入对应 provider
    if engine == "claude":
        k = keys.get("anthropic", {})
        if k.get("api_key"):
            env["ANTHROPIC_AUTH_TOKEN"] = k["api_key"]
            env["ANTHROPIC_API_KEY"] = k["api_key"]
            if k.get("base_url"):
                env["ANTHROPIC_BASE_URL"] = k["base_url"]
    elif engine == "codex":
        k = keys.get("openai", {})
        if k.get("api_key"):
            env["OPENAI_API_KEY"] = k["api_key"]
    else:  # opencode: deepseek/openai 等 OpenAI 兼容
        k = keys.get("deepseek") or keys.get("openai") or {}
        if k.get("api_key"):
            env["OPENAI_API_KEY"] = k["api_key"]
    return env


def dispatch(prompt: str, cwd: str = None, user: str = None) -> str:
    engine, model, real = _parse_engine(prompt)
    if engine == "screenshot":
        print("    -> 截图")
        return take_screenshot()
    extra_env = user_env(user, engine) if user else {}
    keyinfo = "  key:自配" if extra_env else "  key:默认"
    print(f"    -> 引擎: {engine}" + (f"  模型: {model}" if model else "  模型: (默认)")
          + (f"  cwd: {cwd}" if cwd else "") + keyinfo)
    if engine == "claude":
        return run_claude(real, model, cwd, extra_env)
    if engine == "codex":
        return run_codex(real, model, cwd, extra_env)
    return run_opencode(real, model, cwd, extra_env)


def handle_message(item: dict, user: str):
    ts = item["name"].split(".")[0]
    print(f"[{time.strftime('%H:%M:%S')}] [{user}] 收到指令 {item['name']}")

    raw = read_file(item)
    # 解析可选口令行: 首行 #token:xxx
    token = ""
    prompt = raw
    if raw.startswith("#token:"):
        first, _, rest = raw.partition("\n")
        token = first[len("#token:"):].strip()
        prompt = rest
    prompt = prompt.strip()

    reply = None
    # 1) 白名单
    if ALLOW_USERS is not None and user not in ALLOW_USERS:
        reply = f"(用户 '{user}' 未被授权. 请联系管理员加入白名单)"
        print(f"    拒绝: 用户 {user} 不在白名单")
    # 2) 口令校验
    elif not check_token(user, token):
        reply = f"(用户 '{user}' 口令错误或缺失)"
        print(f"    拒绝: 用户 {user} 口令校验失败")
    else:
        cwd = workdir_for(user)               # 主人=完整workdir; 其他=受限子目录
        # 3) 配额检查(截图指令不占用户配额, 跳过)
        quota_err = None if prompt.lstrip().startswith("@screenshot") else check_quota(user, cwd)
        if quota_err:
            reply = quota_err
            print(f"    拒绝: {quota_err}")
        else:
            print(f"    指令内容: {prompt[:120]}")
            reply = dispatch(prompt, cwd, user)

    # 回复写回该用户的 to_phone 子目录 (默认用户写根目录, 兼容旧客户端)
    if user == DEFAULT_USER:
        out_name = f"{DIR_TO_PHONE}/{ts}.msg"
    else:
        out_name = f"{DIR_TO_PHONE}/{user}/{ts}.msg"
    put_file(out_name, reply, message=f"reply {user}/{ts}")
    print(f"    已回复 -> {out_name} ({len(reply)} 字符) {reply[:30]!r}")

    delete_file(item, message=f"consume {item['name']}")


def _collect_messages():
    """收集所有待处理消息, 返回 [(item, user), ...].
    admin 角色: 扫 to_pc/ 根(默认用户) + to_pc/<user>/ 各子目录.
    local 角色: 只扫 to_pc_local/<BRIDGE_LOCAL_USER>/ (模式③, 在用户本地执行).
    """
    result = []
    if BRIDGE_ROLE == "local":
        if not BRIDGE_LOCAL_USER:
            return result
        base = f"{DIR_TO_PC_LOCAL}/{BRIDGE_LOCAL_USER}"
        for sub in list_dir(base):
            if sub["type"] == "file" and sub["name"].endswith(".msg"):
                result.append((sub, BRIDGE_LOCAL_USER))
        result.sort(key=lambda x: x[0]["name"])
        return result
    # admin 角色
    top = list_dir(DIR_TO_PC)
    for it in top:
        if it["type"] == "file" and it["name"].endswith(".msg"):
            result.append((it, DEFAULT_USER))          # 根目录 = 默认用户(兼容旧客户端)
        elif it["type"] == "dir":
            user = _safe_user(it["name"])
            for sub in list_dir(it["path"]):           # 进用户子目录
                if sub["type"] == "file" and sub["name"].endswith(".msg"):
                    result.append((sub, user))
    # 按文件名(时间戳)排序保证顺序
    result.sort(key=lambda x: x[0]["name"])
    return result


# ============ 需求3: mify 请求经 GitHub 信箱中转 ============
# 同事电脑B 的 agent 把模型请求写进 mify_req/<id>.json, 本机取出用内网 key 调 mify,
# 结果写回 mify_resp/<id>.json. 全程走 GitHub, 无穿透.
# 请求 JSON 格式: {"path": "/v1/responses", "method":"POST", "body": <原始请求体字符串>}
DIR_MIFY_REQ  = "mify_req"
DIR_MIFY_RESP = "mify_resp"
MIFY_BASE_OPENAI    = os.environ.get("MIFY_BASE_OPENAI", "http://model.mify.ai.srv/v1").rstrip("/")
MIFY_BASE_ANTHROPIC = os.environ.get("MIFY_BASE_ANTHROPIC", "http://model.mify.ai.srv/anthropic").rstrip("/")
MIFY_KEY = os.environ.get("MIFY_API_KEY", "")


def _mify_upstream(path):
    if path.startswith("/v1"):
        return MIFY_BASE_OPENAI + path[len("/v1"):]
    if path.startswith("/anthropic"):
        return MIFY_BASE_ANTHROPIC + path[len("/anthropic"):]
    return None


def _call_mify(path, method, body):
    """用内网 key 调 mify, 返回 (status, text). 供中转用."""
    import urllib.request, urllib.error, json as _json
    up = _mify_upstream(path)
    if up is None:
        return 404, '{"error":"unsupported path"}'
    if not MIFY_KEY:
        return 500, '{"error":"MIFY_API_KEY not configured on bridge"}'
    req = urllib.request.Request(
        up, data=(body.encode("utf-8") if body else None), method=method or "POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {MIFY_KEY}")
    req.add_header("x-api-key", MIFY_KEY)
    try:
        r = urllib.request.urlopen(req, timeout=CMD_TIMEOUT)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 502, '{"error":"upstream: %s"}' % str(e)


def handle_mify_req(item):
    """处理一条 mify 中转请求."""
    import json as _json
    name = item["name"]
    rid = name.rsplit(".", 1)[0]
    print(f"[{time.strftime('%H:%M:%S')}] [mify] 请求 {name}")
    try:
        raw = read_file(item)
        req = _json.loads(raw)
        status, text = _call_mify(req.get("path", "/v1/responses"),
                                  req.get("method", "POST"),
                                  req.get("body", ""))
        resp = _json.dumps({"status": status, "body": text})
    except Exception as e:
        resp = _json.dumps({"status": 500, "body": '{"error":"%s"}' % str(e)})
    put_file(f"{DIR_MIFY_RESP}/{rid}.json", resp, message=f"mify resp {rid}")
    delete_file(item, message=f"consume mify {name}")
    print(f"    [mify] 已回复 {rid}")


def main():
    print("=" * 60)
    print(" AI 桥接 (电脑端, 多引擎 + 截图)")
    print(f" 角色: {BRIDGE_ROLE}" + (f" (本地用户={BRIDGE_LOCAL_USER}, 扫 to_pc_local/{BRIDGE_LOCAL_USER})" if BRIDGE_ROLE=='local' else " (电脑A: 扫 to_pc + mify_req)"))
    print(f" 仓库: {REPO_OWNER}/{REPO_NAME}@{BRANCH}")
    print(f" 工作目录: {WORKDIR}")
    print(f" 引擎: opencode={OPENCODE_BIN}")
    print(f"       claude  ={CLAUDE_BIN}")
    print(f"       codex   ={CODEX_BIN}")
    print(f" 轮询间隔: {POLL_INTERVAL}s")
    print(f" 主人用户: {OWNER_USER} (完整 workdir)")
    print(f" 白名单  : {ALLOW_USERS if ALLOW_USERS is not None else '(不限, 任意用户可用)'}")
    print("=" * 60)

    if not GITHUB_TOKEN:
        print("!! 未读到 GITHUB_TOKEN. 请在 .env 里填 BRIDGE_GH_TOKEN")
        sys.exit(1)

    _tick = 0
    # 聊天指令扫描的降频倍数(相对 mify): admin 时聊天不敏感, 每 CHAT_EVERY 轮扫一次,
    # 让 mify 中转(模式③模型往返, 延迟敏感)能每轮快速处理.
    CHAT_EVERY = 1 if BRIDGE_ROLE == "local" else 3
    while True:
        try:
            # admin: 优先且每轮处理 mify 中转(模式③模型往返, 延迟敏感)
            if BRIDGE_ROLE != "local":
                for it in list_dir(DIR_MIFY_REQ):
                    if it["type"] == "file" and it["name"].endswith(".json"):
                        handle_mify_req(it)
            # 聊天指令(需求1/2, 或模式③本地执行): local 每轮扫, admin 降频扫
            if _tick % CHAT_EVERY == 0:
                for it, user in _collect_messages():
                    handle_message(it, user)
        except requests.HTTPError as e:
            print(f"[HTTP错误] {e} - {getattr(e.response,'text','')[:200]}")
        except Exception:
            print("[异常]\n" + traceback.format_exc())
        _tick += 1
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已停止.")
