#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pc_core —— Project Console 纯逻辑层(零 tkinter 依赖,可无头测试)

包含:注册表读写、路径定位、工作区同步扫描、脚本列表、命令执行(sh/端口/
systemd/健康检查)、PATH 环境补全。UI 相关代码在 project_console.py,
主题在 pc_theme.py,自绘控件在 pc_widgets.py。

本模块约定:不 import tkinter,不碰任何 UI 状态;所有命令统一经 sh() 出口。
"""

import contextlib
import copy
import glob
import json
import os
import posixpath
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

SSH_TIMEOUT = "ConnectTimeout=6"
DEFAULT_SSH_USER = "root"   # targets[].ssh 里没写 user@ 时用的登录名

# 模块级标记:最近一次 load_config() 是否触发了「备份自愈」。
# 用模块属性而不是塞进返回的 dict——否则标志位会被 save_config 写进 json。
last_restored_from_bak = False

# 模块级标记:最近一次 load_config() 是否因「所有候选位置都没有 projects.json」
# 而返回了空注册表。App 层据此触发首次启动引导。
last_config_missing = False

# ---------------------------------------------------------------- 路径


def app_support_dir() -> str:
    """用户级默认配置目录(macOS 惯例),新用户的首选配置位置。"""
    return os.path.join(os.path.expanduser("~/Library/Application Support"), "ProjectConsole")


def base_dir() -> str:
    """app 自身所在目录(注册表首选查找位置)。

    源码运行 = 本文件目录;打包后 = .app 所在目录(PyInstaller onefile 的
    可执行文件位于 xxx.app/Contents/MacOS/ 内,向上两级即 .app 旁边)。
    可用环境变量 PROJ_CONSOLE_DIR 覆盖。
    """
    env = os.environ.get("PROJ_CONSOLE_DIR")
    if env:
        return env
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.dirname(os.path.dirname(sys.executable)))
    return os.path.dirname(os.path.abspath(__file__))


def _config_candidates() -> list[str]:
    return [
        os.path.join(base_dir(), "projects.json"),          # .app 旁边(或源码目录)
        os.path.join(app_support_dir(), "projects.json"),   # 用户级标准位置
    ]


def config_path() -> str:
    """按优先级返回实际生效的注册表路径(存在性已校验)。"""
    cands = _config_candidates()
    for p in cands:
        if os.path.exists(p):
            return p
    return cands[0]  # 都不存在时返回首选路径,让报错信息指向它


def config_write_path() -> str:
    """save_config 的落盘目标(自动创建所在目录)。

    优先级:PROJ_CONSOLE_DIR > 第一个已存在的候选 > 新建用户级目录并落盘。
    已有配置的老用户永远写回原位置;全新用户写入 app_support_dir()。
    """
    env = os.environ.get("PROJ_CONSOLE_DIR")
    if env:
        d = env
    else:
        hit = next((p for p in _config_candidates() if os.path.exists(p)), None)
        if hit:
            return hit
        d = app_support_dir()
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "projects.json")


def config_exists() -> bool:
    """任何候选位置是否存在 projects.json(首启引导判定用)。"""
    return any(os.path.exists(p) for p in _config_candidates())


MAVEN_SCRIPTS: list[tuple[str, str]] = [
    # 用户自建的 ~/bin/mvn-dev 系列(IDEA 式启动)+ mvn 常用生命周期
    ("start", "mvn-dev start"),
    ("stop", "mvn-dev stop"),
    ("restart", "mvn-dev restart"),
    ("status", "mvn-dev status"),
    ("package", "mvn-dev package"),
    ("clean", "mvn clean"),
    ("install", "mvn install -DskipTests"),
]


def load_scripts(e: dict) -> list[tuple[str, str]]:
    """读条目可用的脚本,返回 [(名称, 完整 shell 命令)]。

    - 注册表 maven:true 的条目 → MAVEN_SCRIPTS(mvn-dev 系列);
    - 否则读 dir 下 package.json 的 scripts(npm run 前缀)。
    用户切 Git 分支后 scripts 可能变化,每次渲染/手动刷新都重新读盘。
    """
    if e.get("maven"):
        return MAVEN_SCRIPTS
    d = e.get("dir") or ""
    if not d:
        return []
    pj = os.path.join(d, "package.json")
    if not os.path.isfile(pj):
        return []
    try:
        with open(pj, encoding="utf-8") as f:
            data = json.load(f)
        return [(name, f"npm run {name}")
                for name, _body in sorted((data.get("scripts") or {}).items())]
    except Exception:  # noqa: BLE001
        return []


def is_backend(e: dict) -> bool:
    """该条目是不是**后端**(后端暂时不做自动发布,UI 要藏掉发布入口)。

    判定依据(任一命中即为后端):
      · 注册表显式写 `backend: true`(留给未来非 Java 的后端,如 Python/Go);
      · `maven: true`(Java/Maven 工程);
      · 填了 `java_version`(要求 JDK 版本 → Java 后端)。

    前端项目(Vue/Vite/npm)三条都不命中。纯逻辑函数,无头可测。
    """
    e = e or {}
    if e.get("backend") is True:
        return True
    if e.get("maven"):
        return True
    if str(e.get("java_version") or "").strip():
        return True
    return False


def safe_name(s: str) -> str:
    import re as _re
    return _re.sub(r"[^A-Za-z0-9_.-]", "_", s)


def _read_json(p: str) -> dict:
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def load_config() -> dict:
    """加载注册表;主文件损坏时尝试 .bak 自愈(P0-3)。

    自愈动作:损坏文件改名为 *.bad 留档 → 复制备份回原路径 → 置
    last_restored_from_bak=True(App 层据此弹提示)。备份也没有时照常
    抛异常,由 App 弹错误框。
    """
    global last_restored_from_bak, last_config_missing
    last_restored_from_bak = False
    last_config_missing = False
    raw = None
    for p in _config_candidates():
        if not os.path.exists(p):
            continue
        try:
            raw = _read_json(p)
            break
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            bak = p + ".bak"
            if os.path.exists(bak):
                try:
                    raw = _read_json(bak)
                    os.replace(p, p + ".bad")          # 损坏文件留档供排查
                    shutil.copyfile(bak, p)            # 备份恢复为主文件
                    last_restored_from_bak = True
                    break
                except Exception:  # noqa: BLE001
                    pass
            raise   # 无备份可恢复:抛给 App 弹明确错误
    if raw is None:
        raw = {"entries": []}
        last_config_missing = True   # 所有候选都没有配置 → App 层弹首启引导
    raw["entries"] = [e for e in raw.get("entries", []) if e.get("id")]
    # v3.0:环境稳定 id(secrets 的账号挂靠键)+ 把 secrets.json 的密码/账号
    # 合回内存。合并后 UI / deploy 看到的结构与 v2.x 完全一致,
    # pc_env_panel / pc_servers_dialog 的读写代码零改动。
    ensure_env_ids(raw)
    merge_secrets(raw)
    return raw


def save_config(raw: dict):
    """把注册表写回 projects.json,同时把密钥抽到 secrets.json(v3.0)。

    备注名等 UI 修改需要持久化,重启后仍在。dict 保持插入序,
    json.dump 不重排序,_说明 注释键也随之保留。
    写入前把现有文件备份为 *.bak(保留一代,P0-3)。

    v3.0 起写盘是「一进两出」:内存结构(含密码/账号)→
      ① 深拷贝后删掉 password / accounts → projects.json(可提交,不进密钥);
      ② 抽出的密码/账号 → secrets.json(0600、原子写、留一代 .bak、gitignore)。
    内存对象保持原样:UI 那边继续拿 t["password"] / env["accounts"] 当真数据用。
    """
    ensure_env_ids(raw)                    # 环境 id 要先落进两边的文件里
    path = config_write_path()
    clean = copy.deepcopy(raw)
    strip_secrets(clean)
    secrets = extract_secrets(raw)
    # 备份现有文件(失败不阻断主流程)
    if os.path.exists(path):
        try:
            shutil.copyfile(path, path + ".bak")
        except OSError:
            pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    save_secrets(secrets)


# ------------------------------------------------ 密钥外置(v3.0 secrets.json)
#
# 背景:targets[].password 与 envs[].accounts[](网站登录账号:用户名/密码/
# 备注)都属于个人信息,而 projects.json 被 git 跟踪并推到远端 —— 明文密码
# 进仓库是一直挂着的安全债(v2.4 用户裁决「存本地 json 没关系」,前提是 json
# 不出门;后来它还是进了 git)。v3.0 起这两块抽到**同目录的 secrets.json**:
#
#   projects.json   只留结构化配置(环境 id / target / pkg / path / 脚本名…),可提交
#   secrets.json    {"targets": {tid: {"password": …}},
#                    "accounts": {"<条目id>/<环境id>": [{user, password, note}]}}
#
# 内存模型不变:load_config() 把 secrets.json 合并回内存,save_config() 把
# 密钥抽走、只把干净数据写进 projects.json。**任何新代码都不要直接 open 这
# 两个文件做读写**,一律经 load_config / save_config,否则两边会失去同步。
#
# 账号按「条目id/环境id」挂靠:环境名可以随手改、下标在删环境后会移位,都
# 不能当 key;环境 id 由 ensure_env_ids() 首次落盘时分配,持久化在
# projects.json 的 envs[].id 里(纯定位用途,不是敏感信息)。

SECRETS_FILENAME = "secrets.json"
# 模块级标记:最近一次 load_secrets() 是否从 .bak 自愈(启动横幅用,别塞进
# 返回的 dict —— 否则会被当成配置写回 json)。
last_secrets_restored_from_bak = False


def secrets_path() -> str:
    """secrets.json 的位置:永远跟实际生效的 projects.json 同目录。"""
    d = os.path.dirname(config_path()) or "."
    return os.path.join(d, SECRETS_FILENAME)


def ensure_env_ids(cfg: dict) -> None:
    """给没有 id 的环境补一个稳定 id(幂等;直接改传入的内存结构)。

    只在 load_config / save_config 里调用。已带 id 的环境(用户手写或上一次
    保存过的)原样保留 —— id 一旦生成就不能变,secrets.json 的账号都挂在它
    上面,变了账号就「丢」了(其实还在,只是没人认领)。
    """
    if not isinstance(cfg, dict):
        return
    taken: set[str] = set()
    for e in cfg.get("entries") or []:
        if not isinstance(e, dict):
            continue
        for env in (e.get("envs") or []):
            if isinstance(env, dict):
                _id = str(env.get("id") or "").strip()
                if _id:
                    taken.add(_id)
    for e in cfg.get("entries") or []:
        if not isinstance(e, dict):
            continue
        envs = e.get("envs")
        if not isinstance(envs, list):
            continue
        for env in envs:
            if not isinstance(env, dict):
                continue
            _id = str(env.get("id") or "").strip()
            if _id:
                continue
            _id = uuid.uuid4().hex[:8]
            while _id in taken:
                _id = uuid.uuid4().hex[:8]
            taken.add(_id)
            env["id"] = _id


def _env_secret_key(eid: str, env: dict) -> str:
    """环境在 secrets.accounts 里的挂靠键:「条目id/环境id」。"""
    return f"{eid}/{(env or {}).get('id') or ''}"


def load_secrets() -> dict:
    """读 secrets.json;损坏时从 .bak 自愈,再不行返回空(丢密码但不崩启动)。

    与 load_config 的自愈策略一致:损坏文件改名为 *.bad 留档 → 备份恢复为主
    文件。返回的不是 dict 也按空处理(结构异常当损坏)。
    """
    global last_secrets_restored_from_bak
    last_secrets_restored_from_bak = False
    p = secrets_path()
    if not os.path.exists(p):
        return {}
    try:
        data = _read_json(p)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        bak = p + ".bak"
        if os.path.exists(bak):
            try:
                data = _read_json(bak)
                os.replace(p, p + ".bad")          # 损坏文件留档供排查
                shutil.copyfile(bak, p)            # 备份恢复为主文件
                os.chmod(p, 0o600)
                last_secrets_restored_from_bak = True
                return data if isinstance(data, dict) else {}
            except Exception:  # noqa: BLE001
                pass
        return {}


def merge_secrets(cfg: dict) -> None:
    """把 secrets.json 合并回内存结构(targets 拿回密码、环境拿回账号)。

    只在 load_config() 里调用。合并后 UI / deploy 看到的就是「完整」数据,
    与 v2.x 的内存模型一致 —— 这是「UI 零改动」的关键。
    """
    sec = load_secrets()
    if not sec:
        return
    tpw = sec.get("targets")
    if isinstance(tpw, dict):
        for t in cfg.get("targets") or []:
            if not isinstance(t, dict):
                continue
            rec = tpw.get(str(t.get("id") or ""))
            if isinstance(rec, dict) and str(rec.get("password") or ""):
                t["password"] = str(rec["password"])
    accs = sec.get("accounts")
    if isinstance(accs, dict):
        for e in cfg.get("entries") or []:
            if not isinstance(e, dict):
                continue
            eid = str(e.get("id") or "")
            for env in (e.get("envs") or []):
                if not isinstance(env, dict):
                    continue
                lst = accs.get(_env_secret_key(eid, env))
                if isinstance(lst, list):
                    env["accounts"] = [copy.deepcopy(a) for a in lst
                                       if isinstance(a, dict)]


def extract_secrets(cfg: dict) -> dict:
    """从内存结构里抽出全部密钥,返回待写入 secrets.json 的 dict。

    只收「当前还在注册表里」的对象:删掉的服务器/环境不会在 secrets.json
    里留孤儿(下次保存自动清掉)。
    """
    sec: dict = {"targets": {}, "accounts": {}}
    for t in cfg.get("targets") or []:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id") or "")
        pw = str(t.get("password") or "")
        if tid and pw:
            sec["targets"][tid] = {"password": pw}
    for e in cfg.get("entries") or []:
        if not isinstance(e, dict):
            continue
        eid = str(e.get("id") or "")
        for env in (e.get("envs") or []):
            if not isinstance(env, dict):
                continue
            accs = env.get("accounts")
            if isinstance(accs, list) and accs:
                sec["accounts"][_env_secret_key(eid, env)] = copy.deepcopy(
                    [a for a in accs if isinstance(a, dict)])
    return sec


def strip_secrets(cfg: dict) -> None:
    """把密码 / 账号从结构里删干净(只用于写 projects.json 的那份拷贝)。"""
    for t in cfg.get("targets") or []:
        if isinstance(t, dict):
            t.pop("password", None)
    for e in cfg.get("entries") or []:
        if not isinstance(e, dict):
            continue
        for env in (e.get("envs") or []):
            if isinstance(env, dict):
                env.pop("accounts", None)


def save_secrets(sec: dict) -> None:
    """写 secrets.json(0600、原子写、留一代 .bak);没有任何密钥时连文件都不建。

    密码删光了(比如清空服务器密码)要把文件也清掉 —— 留一个空壳文件
    反而让人以为里面还有东西。
    """
    slim = {k: v for k, v in (sec or {}).items() if v}
    p = secrets_path()
    if not slim:
        for f in (p, p + ".bak"):
            try:
                os.remove(f)
            except OSError:
                pass
        return
    if os.path.exists(p):
        try:
            shutil.copyfile(p, p + ".bak")
            os.chmod(p + ".bak", 0o600)
        except OSError:
            pass
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(slim, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


# ------------------------------------------------------- 工作区同步(扫描 code)


def default_work_root() -> str:
    """缺省工作根目录:环境变量 PROJ_CONSOLE_WORK_ROOT > ~/code。"""
    return os.environ.get("PROJ_CONSOLE_WORK_ROOT") or os.path.expanduser("~/code")


def cfg_work_root(cfg: dict | None = None) -> str:
    """工作根目录:配置顶层 `work_root` 字段 > 环境变量 > ~/code。

    首启引导会让用户选择项目根目录,选完写进配置顶层 work_root 字段。
    """
    if cfg is None:
        try:
            cfg = load_config()
        except Exception:  # noqa: BLE001
            cfg = {}
    wr = str((cfg or {}).get("work_root") or "").strip()
    return os.path.expanduser(wr) if wr else default_work_root()


_SKIP_DIRS = {"node_modules", "target", "dist", ".git", ".idea", "build", "logs"}


def _unique_id(base: str, taken: set[str]) -> str:
    nid, i = base, 2
    while nid in taken:
        nid, i = f"{base}-{i}", i + 1
    taken.add(nid)
    return nid


def _guess_maven_port(d: str) -> int:
    """在 src/main/resources 下的 application*.yml/properties 里找 server.port,兜底 8080。"""
    res = os.path.join(d, "src", "main", "resources")
    for dirpath, dirnames, filenames in os.walk(res):
        for fn in filenames:
            if not (fn.startswith("application") and fn.endswith((".yml", ".yaml", ".properties"))):
                continue
            try:
                text = open(os.path.join(dirpath, fn), encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            m = (re.search(r"(?m)^\s*port:\s*['\"]?(\d{4,5})", text)
                 or re.search(r"(?m)^server\.port\s*=\s*(\d{4,5})", text))
            if m:
                return int(m.group(1))
    return 8080


def _guess_npm_port(d: str) -> int | None:
    """从 vue.config.js / vite.config.* / .env* 里猜 dev 端口,猜不出返回 None。"""
    import re as _re
    for fn in ("vue.config.js", "vite.config.js", "vite.config.ts"):
        p = os.path.join(d, fn)
        if os.path.isfile(p):
            try:
                m = _re.search(r"port\s*[:=]\s*['\"]?(\d{4,5})", open(p, encoding="utf-8", errors="ignore").read())
                if m:
                    return int(m.group(1))
            except OSError:
                pass
    for fn in os.listdir(d) if os.path.isdir(d) else []:
        if fn.startswith(".env"):
            try:
                m = _re.search(r"(?m)^(?:VITE_)?PORT\s*=\s*(\d{4,5})",
                               open(os.path.join(d, fn), encoding="utf-8", errors="ignore").read())
                if m:
                    return int(m.group(1))
            except OSError:
                pass
    return None


def _npm_dev_script(d: str) -> str:
    """package.json scripts 里挑开发命令:dev > serve > start,兜底 dev。"""
    try:
        with open(os.path.join(d, "package.json"), encoding="utf-8") as f:
            scripts = (json.load(f).get("scripts") or {})
    except Exception:  # noqa: BLE001
        return "dev"
    for s in ("dev", "serve", "start"):
        if s in scripts:
            return s
    return "dev"


def build_entry_for_dir(d: str, parent_id: str, taken_ids: set[str]) -> tuple[dict, str]:
    """根据目录特征构造注册表条目,返回 (条目, 类型描述)。

    pom.xml → maven 项目(mvn-dev 全套);package.json → npm 项目
    (读全部 scripts);都没有 → 纯代码仓(只保留目录/Trae 打开)。
    """
    base = os.path.basename(d.rstrip("/"))
    is_maven = os.path.isfile(os.path.join(d, "pom.xml"))
    is_npm = os.path.isfile(os.path.join(d, "package.json"))
    eid = _unique_id(base.lower(), taken_ids)
    if is_maven:
        port = _guess_maven_port(d)
        e = {
            "group": "work", "id": eid, "parent": parent_id, "name": base,
            "kind": "project",
            "desc": f"(同步自动识别)Java/Maven 项目,端口 {port}(自动探测,不准就改 projects.json)。",
            "dir": d, "maven": True,
            "local": {
                "start": "mvn-dev start", "stop": "mvn-dev stop",
                "ports": [{"port": port, "label": "后端"}],
                "urls": [{"label": "打开本地服务", "url": f"http://localhost:{port}"}],
            },
        }
        return e, f"Java/Maven(端口 {port})"
    if is_npm:
        port = _guess_npm_port(d)
        dev = _npm_dev_script(d)
        e = {
            "group": "work", "id": eid, "parent": parent_id, "name": base,
            "kind": "project",
            "desc": (f"(同步自动识别)Node 前端项目,dev 端口 {port}。"
                     if port else "(同步自动识别)Node 项目,dev 端口没探测到,可用脚本列表手动启动。"),
            "dir": d,
        }
        if port:
            logp = f"/tmp/pc-{eid}-start.log"
            e["local"] = {
                "start": (f"[ -d node_modules ] || npm install --registry=https://registry.npmmirror.com; "
                          f"nohup npm run {dev} > '{logp}' 2>&1 & sleep 3; "
                          f"if [ -n \"$(lsof -tiTCP:{port} -sTCP:LISTEN 2>/dev/null)\" ]; then "
                          f"echo '✅ 已启动 http://localhost:{port} (日志 {logp})'; "
                          f"else echo '⏳ 还在启动或失败,看 {logp}'; tail -5 {logp}; fi"),
                "stop": (f"for p in $(lsof -tiTCP:{port} -sTCP:LISTEN 2>/dev/null); do kill $p; done; "
                         f"echo '已停止 {base}({port})'"),
                "ports": [{"port": port, "label": "前端"}],
                "urls": [{"label": "打开本地页面", "url": f"http://localhost:{port}"}],
            }
        return e, f"Node(端口 {port})" if port else "Node(端口未知)"
    e = {
        "group": "work", "id": eid, "parent": parent_id, "name": base,
        "kind": "repo",
        "desc": "(同步自动识别)代码仓,未探测到 pom.xml / package.json,只提供目录打开。",
        "dir": d,
    }
    return e, "纯代码仓"


def _has_project_marker(d: str) -> bool:
    """目录里直接放着 pom.xml / package.json → 这个目录本身就是个独立项目。"""
    return (os.path.isfile(os.path.join(d, "pom.xml"))
            or os.path.isfile(os.path.join(d, "package.json")))


def scan_code_dir(root: str | None = None) -> tuple[list[dict], list[dict]]:
    """扫描工作根目录,返回 (新增条目列表, 磁盘已缺失的注册表条目列表)。

    目录结构**逐目录自适应**,两种布局(含混合)都能扫对,且一个项目只出一条记录:
      · 一级子目录自身带 pom.xml / package.json → 它直接就是项目,生成单条目,
        不再往下扫(避免把 src/config/public 这类子目录误当成项目);
      · 否则视为「分组仓」,往下扫一级,二级子目录才是项目(老布局)。
    已存在条目按 dir 路径精确匹配,完全不动(备注/端口修正都保留)。
    root 缺省时取 cfg_work_root()(配置顶层 work_root > 环境变量 > ~/code)。
    """
    cfg = load_config()
    existing = cfg["entries"]
    root = root or cfg_work_root(cfg)
    taken_ids = {e["id"] for e in existing}
    known_dirs = {(e.get("dir") or "").rstrip("/") for e in existing}

    def _subdirs(d: str) -> list[str]:
        return [x for x in sorted(os.listdir(d))
                if not x.startswith(".") and x not in _SKIP_DIRS
                and os.path.isdir(os.path.join(d, x))]

    new_entries: list[dict] = []
    for lvl1 in _subdirs(root):
        gdir = os.path.join(root, lvl1)

        # 布局自适应:一级目录自己就是项目 → 单条目,不往下扫
        if _has_project_marker(gdir):
            if gdir.rstrip("/") in known_dirs:
                continue                     # 已受管,完全不动
            e, _type = build_entry_for_dir(gdir, "", taken_ids)
            e.pop("parent", None)            # 顶级项目没有父节点
            new_entries.append(e)
            known_dirs.add(gdir.rstrip("/"))
            continue

        # 分组仓(两级布局):一级节点不存在就自动建,二级才是项目
        repo_e = None
        if gdir.rstrip("/") not in known_dirs:
            rid = _unique_id(lvl1.lower(), taken_ids)
            repo_e = {"group": "work", "id": rid, "name": lvl1, "kind": "repo",
                      "desc": "(同步自动创建)一级代码仓节点。", "dir": gdir}
            new_entries.append(repo_e)
            known_dirs.add(gdir.rstrip("/"))
        else:
            repo_e = next((e for e in existing
                           if (e.get("dir") or "").rstrip("/") == gdir.rstrip("/")), None)
        for lvl2 in _subdirs(gdir):
            pdir = os.path.join(gdir, lvl2)
            if pdir.rstrip("/") in known_dirs:
                continue                     # 已受管,完全不动
            e, _type = build_entry_for_dir(pdir, repo_e["id"], taken_ids)
            new_entries.append(e)
            known_dirs.add(pdir.rstrip("/"))

    # 注册表有、磁盘没有的 work 条目(含一级仓节点)
    missing = [e for e in existing
               if e.get("group") == "work" and e.get("dir")
               and e["dir"].rstrip("/").startswith(root)
               and not os.path.isdir(e["dir"])]
    return new_entries, missing


# ---------------------------------------------------------------- 命令执行工具


def _path_env(node_version: str | None = None) -> dict:
    """构造带常用工具目录的 PATH。

    .app 经 Finder/LaunchServices 启动时只拿到最小 PATH(/usr/bin:/bin:…),
    nvm 的 node、Maven、Homebrew 等统统找不到(踩过的坑:nohup: mvn: No such
    file or directory)。这里把常见位置统一补进 PATH,对 glob 命中的目录
    自适应,新增版本无需改代码。

    node_version: 条目级 node 版本覆盖(如 "20.20.2")。不同项目对 node
    版本要求不同(新 Vite 要 ≥20,老 node-sass 要 16/14),注册表里用
    node_version 字段指定,该条目的命令会把此版本排到 PATH 最前。
    """
    extras = ["/usr/local/bin", "/opt/homebrew/bin", "/opt/homebrew/sbin"]
    nvm_bins = sorted(
        glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin")), reverse=True)
    # 优先对齐用户终端:nvm default 版本排最前(读 ~/.nvm/alias/default),
    # 否则老的 node-sass 项目在错误 node 版本下装依赖会莫名失败
    try:
        with open(os.path.expanduser("~/.nvm/alias/default")) as f:
            default_ver = f.read().strip()
        preferred = os.path.expanduser(f"~/.nvm/versions/node/v{default_ver}/bin")
        if preferred in nvm_bins:
            nvm_bins.remove(preferred)
            nvm_bins.insert(0, preferred)
    except OSError:
        pass
    extras += nvm_bins
    extras += sorted(glob.glob(os.path.expanduser("~/.workbuddy/binaries/node/versions/*/bin")))
    extras += sorted(glob.glob(os.path.expanduser("~/opt/apache-maven*/bin")))
    extras.append(os.path.expanduser("~/bin"))   # 用户自建脚本(mvn-dev 等)
    if node_version:
        pinned = os.path.expanduser(f"~/.nvm/versions/node/v{node_version}/bin")
        if os.path.isdir(pinned):
            extras.insert(0, pinned)   # 条目指定版本 > nvm default > 其他
        else:
            extras.append(f"⚠️ nvm 无 v{node_version}")
    base = os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    env = dict(os.environ)
    env["PATH"] = ":".join(extras + [base])
    return env


def sh(cmd: str, cwd: str | None = None, timeout: int = 120,
       node_version: str | None = None,
       env_extra: dict | None = None) -> tuple[int, str]:
    """shell 执行命令,合并 stdout/stderr,返回 (code, 文本)。

    env_extra 会合并进 `_path_env()` 的结果:密码认证靠它注入
    `SSH_ASKPASS` / `SSH_ASKPASS_REQUIRE`(见 `_auth_ctx`)。
    """
    env = _path_env(node_version)
    if env_extra:
        env.update(env_extra)
    try:
        p = subprocess.run(
            cmd, shell=True, cwd=cwd, timeout=timeout,
            env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        return p.returncode, (p.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return 124, f"⏱ 命令超时({timeout}s): {cmd}"
    except Exception as e:  # noqa: BLE001
        return 1, f"❌ {e}"


def port_listening(port: int) -> bool:
    code, out = sh(f"lsof -tiTCP:{port} -sTCP:LISTEN", timeout=10)
    return code == 0 and bool(out.strip())


def port_pids(port: int) -> list[str]:
    """返回监听该端口的进程 PID 列表(P2-1:供按 PID 停止/占用者显示)。"""
    code, out = sh(f"lsof -tiTCP:{port} -sTCP:LISTEN", timeout=10)
    if code != 0 or not out.strip():
        return []
    return [x for x in out.split() if x.strip()]


def port_owner_cwds(port: int) -> dict[str, str]:
    """端口监听进程的 {pid: 工作目录 cwd}(读不到 cwd 的进程跳过)。

    用于「端口归属」判定:两个项目共用一个端口时,lsof 只知道「端口有人听」,
    不知道是谁听的 —— 用监听进程的 cwd 落在哪个项目目录来判断归属,
    否则 A 项目起了,B 项目也跟着亮假绿灯。
    注意 macOS 的 /tmp 是 /private/tmp 的软链,统一 realpath 归一化后再比。
    """
    out: dict[str, str] = {}
    for pid in port_pids(port):
        try:
            r = subprocess.run(
                ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                capture_output=True, text=True, timeout=6)
            for line in r.stdout.splitlines():
                if line.startswith("n/"):
                    out[pid] = os.path.realpath(line[1:].strip())
                    break
        except Exception:  # noqa: BLE001 — 单个进程读失败不影响整体
            pass
    return out


def proc_info(pids: list[str]) -> str:
    """ps 汇总占用进程信息(P2-3),一行文字,多 PID 用 | 分隔。

    格式:「命令(截断) (PID xxx · CPU x% · 内存 xMB · 已运行 x)」
    """
    if not pids:
        return ""
    code, out = sh(f"ps -p {','.join(pids)} -o pid=,%cpu=,rss=,etime=,command= 2>/dev/null",
                   timeout=10)
    if code != 0 or not out.strip():
        return ""
    parts = []
    for line in out.strip().splitlines():
        f = line.split(None, 4)
        if len(f) < 5:
            continue
        pid, cpu, rss, etime, cmd = f
        cmd = cmd.strip()
        if len(cmd) > 48:
            cmd = cmd[:48] + "…"
        try:
            mb = int(int(rss) // 1024)
        except ValueError:
            mb = rss
        parts.append(f"{cmd}  (PID {pid} · CPU {cpu}% · 内存 {mb}MB · 已运行 {etime})")
    return " | ".join(parts)


def svc_active(ssh: str, service: str) -> tuple[str, str]:
    """返回 (状态字符串, 详情)。状态: active / inactive / ssh_error。"""
    code, out = sh(
        f"ssh -o BatchMode=yes -o {SSH_TIMEOUT} {ssh} "
        f"systemctl is-active {service} 2>&1",
        timeout=20,
    )
    out = (out or "").strip()
    if out in ("active", "inactive"):
        return out, ""
    return "ssh_error", out or f"ssh 退出码 {code}"


def health_ok(url: str) -> tuple[bool, str]:
    code, out = sh(f"curl -sf -m 6 -o /dev/null -w '%{{http_code}}' {url}", timeout=15)
    return code == 0, out


def notify(title: str, body: str):
    """发 macOS 通知中心通知(P0-2)。失败静默降级,不影响主流程。"""
    t = title.replace("\\", "\\\\").replace('"', '\\"')
    b = body.replace("\\", "\\\\").replace('"', '\\"')
    sh(f'osascript -e \'display notification "{b}" with title "{t}"\'', timeout=10)


# ---------------------------------------------------------------- 注册表体检

NVM_VERSIONS_DIR = os.path.expanduser("~/.nvm/versions/node")


def check_registry(cfg: dict) -> list[str]:
    """P2-5 注册表体检:返回问题描述文字列表(空列表 = 全部通过)。

    只报告不修改。检查项:id 重复 / parent 悬空 / dir 失效 / 同组端口冲突 /
    node_version 未安装 / entries 结构异常。
    """
    issues: list[str] = []
    entries = cfg.get("entries")
    if not isinstance(entries, list):
        return [f"结构异常:entries 不是数组(实际类型 {type(entries).__name__})"]
    if not entries:
        return ["注册表里没有任何条目"]

    def nm(e: dict) -> str:
        return e.get("name") or e.get("id") or "(无名条目)"

    # 1) 缺 id / id 重复
    seen: set[str] = set()
    dups: list[str] = []
    for e in entries:
        eid = e.get("id")
        if not eid:
            issues.append(f"条目缺 id 字段:{str(e)[:60]}…")
            continue
        if eid in seen:
            dups.append(eid)
        seen.add(eid)
    if dups:
        issues.append(f"id 重复:{', '.join(sorted(set(dups)))}(重复条目状态会被互相覆盖)")

    # 2) parent 悬空
    for e in entries:
        pid = e.get("parent")
        if pid and pid not in seen:
            issues.append(f"「{nm(e)}」的 parent 指向不存在的 id:{pid}")

    # 3) dir 失效
    for e in entries:
        d = e.get("dir")
        if d and not os.path.isdir(d):
            issues.append(f"「{nm(e)}」的本地目录不存在:{d}")

    # 4) 同组端口冲突(不同条目共用同一端口 → 启停会互相影响)。
    #    kind=service 的共享基础设施(如 MySQL 隧道)不算冲突——多个业务条目
    #    本来就是把它的端口列出来看状态,这是有意设计(踩过的坑:3307 误报)。
    port_map: dict[tuple[str, int], list[str]] = {}
    for e in entries:
        grp = e.get("group", "personal")
        if e.get("kind") == "service":
            continue
        for p in ((e.get("local") or {}).get("ports") or []):
            if isinstance(p, dict) and p.get("port") is not None:
                port_map.setdefault((grp, int(p["port"])), []).append(e.get("id"))
    for (grp, port), owners in sorted(port_map.items()):
        if len(owners) > 1:
            issues.append(f"端口冲突:{grp} 组的 {port} 端口被 {len(owners)} 个条目共用 — "
                          f"{'、'.join(owners)}")

    # 5) node_version 未安装
    for e in entries:
        v = e.get("node_version")
        if v and not os.path.isdir(os.path.join(NVM_VERSIONS_DIR, f"v{v}")):
            issues.append(f"「{nm(e)}」要求 node {v},但 nvm 里没有安装该版本")

    # 6) 发布配置(v2.4+ 的 envs[]):逐环境校验 —— 路径/包名护栏 + 服务器引用。
    #    v2.1 的旧 deploy{} 早已 migrate_envs() 转走,这里不再扫(避免重复报)。
    tids = {str(t.get("id")) for t in (cfg.get("targets") or [])
            if isinstance(t, dict)}
    for e in entries:
        envs = e.get("envs")
        if not isinstance(envs, list) or not envs:
            continue
        for i, env in enumerate(envs):
            if not isinstance(env, dict):
                issues.append(f"「{nm(e)}」第 {i + 1} 个环境不是对象(配置损坏)")
                continue
            ename = str(env.get("name") or "").strip() or f"环境 {i + 1}"
            tag = f"「{nm(e)}」环境「{ename}」"

            # 后端暂不支持自动发布(UI 上也没有发布按钮),
            # 它的 envs 里那些字段填不填都无所谓 —— 报出来纯属噪音,跳过。
            if is_backend(e):
                continue

            # 6a) 部署路径/包名护栏(与发布第 0 步 deploy_validate 同一套规则)
            ok, err = deploy_validate(env.get("pkg"), env.get("path"))
            if not ok:
                issues.append(f"{tag}的发布配置不合法:{err}")

            # 6b) 服务器引用必须存在
            tid = str(env.get("target") or "")
            if not tid:
                issues.append(f"{tag}还没选服务器")
            elif tid not in tids:
                issues.append(f"{tag}引用了不存在的服务器:{tid}")

            # 6c) 打包脚本必须填(空 = 发布时必然中止)
            if not str(env.get("script") or "").strip():
                issues.append(f"{tag}还没选打包脚本")

    # 7) 服务器自身:主机为空 → 连不上,发布必失败(这个坑踩过:状态点还是绿的)
    for t in (cfg.get("targets") or []):
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id") or "")
        if not tid:
            continue
        if not target_host(t):
            issues.append(f"服务器「{t.get('name') or tid}」还没填主机地址,"
                          f"连不上也无法发布")

    return issues


# ------------------------------------------------- 运行环境版本(P3-1)


def detect_java_version(d: str) -> str | None:
    """从 pom.xml 探测 Java 版本(java.version / maven.compiler.source)。"""
    if not d:
        return None
    pom = os.path.join(d, "pom.xml")
    if not os.path.isfile(pom):
        return None
    try:
        text = open(pom, encoding="utf-8", errors="ignore").read()
    except OSError:
        return None
    m = (re.search(r"<java\.version>\s*([^<]+)\s*</java\.version>", text)
         or re.search(r"<maven\.compiler\.(?:source|target)>\s*([^<]+)\s*</", text))
    return m.group(1).strip() if m else None


def detect_node_require(d: str) -> str | None:
    """从 package.json 的 engines.node 探测要求版本(原样返回,如 '>=20')。"""
    if not d:
        return None
    pj = os.path.join(d, "package.json")
    if not os.path.isfile(pj):
        return None
    try:
        data = json.load(open(pj, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    return ((data.get("engines") or {}).get("node") or "").strip() or None


def env_report(e: dict) -> dict[str, str]:
    """条目要求的运行环境版本,形如 {"node": "20.20.2", "java": "17"}。

    版本来源:注册表 node_version / java_version;缺失时自动探测
    (package.json 的 engines.node、pom.xml 的 java.version)。
    探测不到该项就不出现在结果里(不显示「未声明」这种无信息量的行)。

    **只报要求值,不做本机比对,也不提供切换**——原因见下,改动前请先读:
    - 启动链路 _path_env() 已把条目的 node_version 排到 PATH 最前
      (优先级:条目版本 > nvm default > 其他),所以「本机默认版本」与项目
      能否启动无关;显示一个「⚠️ 不一致」只会误导人以为会起不来;
    - 切换全局 `nvm alias default` 是全局副作用操作,收益却为零;
    - 唯一真会导致启动失败的是「要求的版本在 nvm 里没装」,由 check_registry 报出。
    """
    d = e.get("dir") or ""
    out: dict[str, str] = {}

    req_n = e.get("node_version") or detect_node_require(d)
    if req_n:
        out["node"] = str(req_n).strip()

    req_j = e.get("java_version") or detect_java_version(d)
    if req_j:
        out["java"] = str(req_j).strip()

    return out


# ============================================================================
# 发布(v2.1 一键发布:构建后自动部署到测试服务器)
#
# 本段全部是纯逻辑(零 tkinter),可无头调用。设计要点:
#   - 服务器(targets)全局管理,只存 SSH,不存路径;
#   - 发布配置(deploy)下沉到 build 脚本粒度,一个脚本可配多个目标;
#   - 主流程 deploy_static() 只做串联,每一步都是独立可调用的小函数,
#     便于 QA 在不跑真实 npm build、不连真实服务器的情况下逐步骤验证。
# ============================================================================

# 系统目录黑名单:仅拦自身(/usr/share/nginx/html 这类必须放行)
BLACKLIST_EXACT = frozenset({"/", "/usr", "/var"})
# 系统目录黑名单:自身 + 全部子目录都要拦
BLACKLIST_TREE = frozenset({
    "/bin", "/sbin", "/etc", "/lib", "/usr/bin", "/usr/local", "/var/log",
    "/root", "/tmp", "/System", "/Library", "/private",
})
MIN_PATH_DEPTH = 3          # 远端路径至少 3 层(/a/b 拒绝,/a/b/c 通过)
MIN_FREE_KB = 100 * 1024    # 远端可用空间下限 100MB(df -P 的 1K 块)
# 路径里出现这些字符直接拒绝:远端命令要靠 shell 拼,避免注入与转义歧义
_FORBIDDEN_PATH_CHARS = (" ", "\t", "'", '"', "\\", "$", "`", "\n", "\r", "\x00")


def deploy_fmt_size(num_bytes: int) -> str:
    """字节数转人类可读体积(与 PRD 日志里的 3.2MB 一致)。"""
    try:
        n = int(num_bytes)
    except (TypeError, ValueError):
        return "0B"
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f}GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f}MB"
    if n >= 1024:
        return f"{n / 1024:.0f}KB"
    return f"{n}B"


def deploy_validate(pkg: str, path: str) -> tuple[bool, str]:
    """发布配置护栏(纯函数,PRD 第九节)。返回 (ok, 中文错误文案)。

    规则:
      1. 包名非空、不能含 `..`、不能是绝对路径;
      2. 远端路径必须是绝对路径、至少 3 层;
      3. 命中系统目录黑名单(自身或子目录)直接拒绝;
      4. 路径里不许出现空白/引号/反斜杠/$/`(远端命令要拼 shell)。
    """
    pkg = (pkg or "").strip()
    path = (path or "").strip()

    if not pkg:
        return False, "包名不能为空(默认填 dist)"
    if ".." in pkg:
        return False, f"包名不能包含 ..:{pkg}"
    if pkg.startswith("/") or pkg.startswith("~"):
        return False, f"包名必须是相对项目目录的子目录名:{pkg}"

    if not path:
        return False, "远端路径不能为空"
    if not path.startswith("/"):
        return False, f"远端路径必须是绝对路径(以 / 开头):{path}"
    for ch in _FORBIDDEN_PATH_CHARS:
        if ch in path:
            shown = ch.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
            return False, f"远端路径不能包含空白/引号/反斜杠/$/` 等字符:{shown}"
    parts = [p for p in path.split("/") if p]
    if ".." in parts or "." in parts:
        return False, f"远端路径不能包含 . 或 ..:{path}"

    norm = posixpath.normpath(path)
    if norm in BLACKLIST_EXACT:
        return False, f"拒绝发布到系统目录:{norm}"
    for base in BLACKLIST_TREE:
        if norm == base or norm.startswith(base + "/"):
            return False, f"拒绝发布到系统目录或其子目录:{base}"

    depth = len([p for p in norm.split("/") if p])
    if depth < MIN_PATH_DEPTH:
        return False, (f"远端路径至少需要 {MIN_PATH_DEPTH} 层(当前 {depth} 层):{norm}")
    return True, ""


# ---------------------------------------------------------------- SSH 认证
#
# 历史:最早只支持「密钥免密」(ssh -o BatchMode=yes),因为控制台自己的线上机
# 就是免密。但测试服务器经常只有密码登录,而 BatchMode=yes **会直接禁用**
# 密码/键盘交互认证 —— 于是「配了服务器却怎么都连不上」。
#
# 为什么不用 sshpass:本机没装(brew 装的也不在 .app 的最小 PATH 里,
# 还得让用户为控制台额外装东西);而且它会把密码写进自己的环境变量/参数。
# 为什么不用 expect:需要伪 TTY、密码要出现在 argv 里、脚本转义易错,体验差。
#
# 选定方案:OpenSSH 8.4+ 的 `SSH_ASKPASS` + `SSH_ASKPASS_REQUIRE=force`
# (本机 OpenSSH_10.3p1)。密码只存在一个 0600 的临时文件里,由我们自己写的
# askpass 脚本喂给 ssh —— **密码不经过命令行**,`ps` 看不到命令行里的密码。
#
# 存储(v3.0 起外置):`targets[]` 项里的 `"password"` 在**内存**里照样存在,
# 落盘时由 save_config() 抽到同目录的 secrets.json(0600,gitignore)——
# projects.json 里不再有任何密码。**有 password 就走密码认证,没有就走密钥**
# —— 不再需要 auth 字段(两种都配时以 password 为准,见 ssh_opts)。
#
# 日志/异常文案里**不得**出现密码本身(一律写 <pw> 占位)。


def target_password(t: dict) -> str:
    """该服务器的 SSH 密码(内存里明文;落盘在 secrets.json,v3.0 起);空串 = 走密钥认证。"""
    return str((t or {}).get("password") or "")


def target_auth(t: dict) -> str:
    """该服务器的认证方式:"password" / "key"。

    规则:有 `password` 就是密码认证;但用户显式选了「密钥」
    (面板写 `"auth": "key"`,密码保留不删)时以密钥为准 ——
    否则「切密钥却不生效」会让人以为设置没保存。
    旧数据(既没 password 也没 auth)自然落到 key,行为与改造前一致。
    """
    t = t or {}
    if str(t.get("auth") or "").strip() == "key":
        return "key"
    return "password" if target_password(t) else "key"


def target_user(t: dict) -> str:
    """登录用户名。优先级:`ssh` 里的 `user@` → 单独字段 `user` → root。

    用户踩过的坑:只填了主机 IP,ssh 就用本机当前用户名去登录,
    必然 Permission denied —— 所以缺省必须是 root,而不是「交给 ssh 猜」。
    """
    t = t or {}
    ssh = str(t.get("ssh") or "").strip()
    if "@" in ssh:
        u = ssh.split("@", 1)[0].strip()
        if u:
            return u
    u = str(t.get("user") or "").strip()
    return u or DEFAULT_SSH_USER


def target_host(t: dict) -> str:
    """主机部分(去掉可能残留的 `user@` 前缀)。"""
    ssh = str((t or {}).get("ssh") or "").strip()
    return ssh.split("@", 1)[1] if "@" in ssh else ssh


def target_addr(t: dict) -> str:
    """`user@host`(不带端口)—— ssh/scp 的目标地址统一从这里取。"""
    host = target_host(t)
    return f"{target_user(t)}@{host}" if host else ""


def ssh_opts(t: dict) -> str:
    """ssh / scp 的通用选项串(不含端口;端口由调用方拼 `-p` / `-P`)。

    - 密钥模式:`BatchMode=yes` —— 绝不弹任何交互,连不上立即失败;
    - 密码模式:**必须去掉 BatchMode**。它会禁用 password 与
      keyboard-interactive 认证,这就是「配了密码却仍报
      Permission denied (publickey)」的根因;同时限定认证方式并只试一次,
      免得 ssh 反复提示把超时耗光;
    - 两种模式都带 `StrictHostKeyChecking=accept-new`:目标机第一次连接、
      不在 known_hosts 里时,不要卡在 yes/no 交互上(否则「测试连接」
      会因为一个没人回答的问题直接失败)。
    """
    base = f"-o {SSH_TIMEOUT} -o StrictHostKeyChecking=accept-new"
    if target_auth(t) == "password":
        return (f"{base} -o PreferredAuthentications=password,keyboard-interactive"
                f" -o NumberOfPasswordPrompts=1")
    return f"{base} -o BatchMode=yes"


def target_ssh_prefix(t: dict) -> str:
    """target → ssh 命令行前缀(认证选项按 t["auth"] 切换 + 非 22 端口)。"""
    try:
        port = int((t or {}).get("port") or 22)
    except (TypeError, ValueError):
        port = 22
    port_opt = f" -p {port}" if port != 22 else ""
    return f"ssh {ssh_opts(t)}{port_opt}"


def target_ssh(t: dict) -> str:
    """target → 完整的「user@host(:port)」串(端口非 22 时附在末尾)。

    `ssh` 字段里已经写了 `user@` 就原样用;否则用 `user` 字段(默认 root)补上 ——
    这就是「只填 IP 也能连对账号」的落点。
    """
    host = target_host(t)
    if not host:
        return ""
    ssh = f"{target_user(t)}@{host}"
    try:
        port = int((t or {}).get("port") or 22)
    except (TypeError, ValueError):
        port = 22
    return f"{ssh}:{port}" if port != 22 else ssh


# ---- 认证通道(askpass;密码不经过命令行)

class SshAuthError(Exception):
    """SSH 认证的前置条件不满足(典型:配了密码认证,但 json 里密码为空)。"""


@contextlib.contextmanager
def _auth_ctx(t: dict):
    """为一次 ssh/scp 准备认证环境,退出时**一定**清掉临时文件。

    密钥模式:yield 空 dict,行为与改造前完全一致。
    密码模式:`mkdtemp()`(默认 0700)→ 写 pass(0600)→ 写 askpass(0700)
    → 把 `SSH_ASKPASS` / `SSH_ASKPASS_REQUIRE=force` / `DISPLAY` 交给 ssh。

    为什么还要临时文件(密码明明已经在 json 里了):ssh 的密码只能通过
    「交互式提问」拿到,唯一不经过命令行 argv 的喂法就是 askpass 脚本 ——
    argv 是 `ps` 全机可见的,密码绝不能出现在那里。

    权限不是可选细节:askpass 不可执行 ssh 会直接报错;pass 若是 0644,
    同机其他用户就能读到密码。
    """
    if target_auth(t) != "password":
        yield {}
        return
    pw = target_password(t)
    if not pw:
        raise SshAuthError(
            "这台服务器走密码认证,但 secrets.json 里没有密码"
            "(打开「⚙ 服务器」→ 选中它 → 认证选「密码」→ 填密码)")
    tmpdir = tempfile.mkdtemp(prefix="pc-ssh-")
    try:
        os.chmod(tmpdir, 0o700)
        passfile = os.path.join(tmpdir, "pass")
        with open(passfile, "w", encoding="utf-8") as f:
            f.write(pw + "\n")
        os.chmod(passfile, 0o600)
        askpass = os.path.join(tmpdir, "askpass")
        with open(askpass, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\ncat " + shlex.quote(passfile) + "\n")
        os.chmod(askpass, 0o700)
        yield {
            "SSH_ASKPASS": askpass,
            "SSH_ASKPASS_REQUIRE": "force",
            "DISPLAY": os.environ.get("DISPLAY") or ":0",
        }
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _run_authed(t: dict, cmd: str, timeout: int = 60,
                node_version: str | None = None) -> tuple[int, str]:
    """跑一条需要认证的 ssh/scp 命令(密码模式自动注入 askpass 环境)。

    认证准备失败(如钥匙串没密码)返回 (255, 中文原因),不抛异常 ——
    调用方(测试连接 / 发布流程)按「连不上」处理即可。
    """
    try:
        with _auth_ctx(t) as env_extra:
            return sh(cmd, timeout=timeout, node_version=node_version,
                      env_extra=env_extra)
    except SshAuthError as ex:
        return 255, str(ex)
    except Exception as ex:  # noqa: BLE001 — 认证环境出问题也不能炸掉发布
        return 255, f"SSH 认证准备失败: {ex}"


def ssh_error_hint(text: str, t: dict | None = None) -> str:
    """把 ssh/scp 的英文报错翻成能定位问题的中文提示(空串 = 没识别出来)。"""
    raw = text or ""
    low = raw.lower()
    if "permission denied" in low:
        if target_auth(t or {}) == "password":
            return "密码认证失败(检查「⚙ 服务器」里这台机器的密码 / 用户名)"
        return ("密钥认证失败(免密 key 没配好:cat ~/.ssh/id_*.pub >> 服务器 "
                "~/.ssh/authorized_keys)")
    if "⚙ 服务器" in raw:                    # 已经是我们自己的中文文案
        return raw.strip()
    if "connection refused" in low:
        return "端口没开(Connection refused)"
    if "no route to host" in low:
        return "网络不通(No route to host)"
    if "timed out" in low:
        return "连接超时(检查 IP / 端口 / 防火墙 / 服务器是否开机)"
    if "host key verification failed" in low:
        return "主机指纹校验失败(known_hosts 里有不同记录:ssh-keygen -R <host>)"
    if "could not resolve hostname" in low:
        return "域名解析失败(检查地址拼写)"
    return ""


def ssh_probe_hint(t: dict | None) -> str:
    """「连不上」时给用户看的下一步该查什么(按认证方式分支)。

    发布链路第一步探活失败时用;密钥/密码的排查动作完全不同,
    统一写成「检查免密 key」会让密码用户彻底找不到方向。
    """
    if target_auth(t or {}) == "password":
        return ("检查密码 / 端口 / 地址(密码存在同目录 secrets.json 里:"
                "「⚙ 服务器」→ 选中这台 → 认证选「密码」→ 重填密码)")
    return ("检查免密 key / 端口 / 地址(cat ~/.ssh/id_*.pub >> 服务器 "
            "~/.ssh/authorized_keys)")


# ---------------------------------------------------------------- targets 读写


def load_targets() -> list[dict]:
    """读全局服务器列表(顶层 targets 数组),缺失/损坏时返回 [].

    返回的 dict 就是注册表里的对象(不拷贝),UI 改完直接 save_targets 落盘。
    """
    try:
        cfg = load_config()
    except Exception:  # noqa: BLE001 — 注册表坏了也不能让发布面板崩掉
        return []
    raw = cfg.get("targets")
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for x in raw:
        if not isinstance(x, dict):
            continue
        tid = str(x.get("id") or "").strip()
        if not tid:
            continue
        x["id"] = tid
        if not str(x.get("name") or "").strip():
            x["name"] = tid
        x["ssh"] = str(x.get("ssh") or "").strip()
        try:
            x["port"] = int(x.get("port") or 22)
        except (TypeError, ValueError):
            x["port"] = 22
        out.append(x)
    return out


def save_targets(targets: list[dict]) -> dict:
    """写回全局服务器列表(经 save_config 原子写 + .bak),返回最新注册表。"""
    cfg = load_config()
    cfg["targets"] = list(targets or [])
    save_config(cfg)
    return cfg


def new_target_id(targets: list[dict]) -> str:
    """生成不重复的服务器 id(t-1、t-2 …)。"""
    taken = {str(t.get("id") or "") for t in (targets or [])}
    i = 1
    while f"t-{i}" in taken:
        i += 1
    return f"t-{i}"


def target_refs(entries: list[dict], tid: str) -> list[tuple[str, str]]:
    """某台服务器被哪些环境引用,返回 [(条目id, 环境名)]。

    删服务器前用它算影响面(PRD 第四节:被引用时弹确认并列出清单)。
    v2.4 起引用点在条目级 `envs[]`;旧 `deploy{}` 也一起扫,
    这样迁移前的老注册表同样能算出影响面。
    """
    out: list[tuple[str, str]] = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        for env in (e.get("envs") or []):
            if isinstance(env, dict) and env.get("target") == tid:
                out.append((str(e.get("id") or ""),
                            str(env.get("name") or env.get("script") or "")))
        dep = e.get("deploy") or {}
        if not isinstance(dep, dict):
            continue
        for script, items in dep.items():
            if not isinstance(items, list):
                continue
            if any(isinstance(it, dict) and it.get("target") == tid for it in items):
                out.append((str(e.get("id") or ""), str(script)))
    return out


def check_target(t: dict) -> dict:
    """测试单台服务器 SSH 连通性,返回 {ok, ms, time, out, hint}。

    命令:ssh <ssh_opts(t)> [-p 端口] <ssh> "echo ok; uname -n"
    选项由 `ssh_opts(t)` 按认证方式生成(密钥 BatchMode / 密码 askpass),
    走 `_run_authed()` 注入密码认证环境;整体超时 10s。
    结果由调用方写回 targets[].last_check 持久化。

    `hint` 是英文报错翻成的中文提示(识别不出来时为空串),UI 优先显示它;
    `out` 保留原始输出前 200 字,便于「复制诊断信息」。
    """
    ssh = target_addr(t)
    if not ssh or not target_host(t):
        return {"ok": False, "ms": 0, "time": time.strftime("%m-%d %H:%M"),
                "out": "未填 SSH", "hint": "未填 SSH(服务器面板里填主机,默认 root)"}
    started = time.time()
    code, out = _run_authed(
        t, f"{target_ssh_prefix(t)} {shlex.quote(ssh)} "
           f"{shlex.quote('echo ok; uname -n')}", timeout=10)
    ms = int((time.time() - started) * 1000)
    text = (out or "").strip()
    ok = code == 0 and "ok" in text
    hint = "" if ok else ssh_error_hint(text, t)
    return {
        "ok": ok,
        "ms": ms,
        "time": time.strftime("%m-%d %H:%M"),
        "out": text[:200] if text else ("" if ok else f"退出码 {code}"),
        "hint": hint,
    }


# ------------------------------------------------------------ 产物目录探测


def guess_pkg(d: str) -> str:
    """探测本地产物目录名:vue.config.js 的 outputDir → vite outDir → dist。

    猜不出(或目录不存在)一律返回 dist——前端脚手架默认值,允许用户改。
    """
    if not d or not os.path.isdir(d):
        return "dist"
    vue = os.path.join(d, "vue.config.js")
    if os.path.isfile(vue):
        try:
            text = open(vue, encoding="utf-8", errors="ignore").read()
        except OSError:
            text = ""
        m = re.search(r"outputDir\s*:\s*['\"]([^'\"]+)['\"]", text)
        if m:
            return m.group(1).strip().strip("/") or "dist"
        return "dist"
    for fn in ("vite.config.js", "vite.config.ts", "vite.config.mjs", "vite.config.cjs"):
        p = os.path.join(d, fn)
        if not os.path.isfile(p):
            continue
        try:
            text = open(p, encoding="utf-8", errors="ignore").read()
        except OSError:
            text = ""
        m = re.search(r"outDir\s*:\s*['\"]([^'\"]+)['\"]", text)
        if m:
            return m.group(1).strip().strip("/") or "dist"
        return "dist"
    # 既没有 vue.config.js 也没有 vite 配置:按已有目录猜,兜底 dist
    for cand in ("dist", "build"):
        if os.path.isdir(os.path.join(d, cand)):
            return cand
    return "dist"


# ------------------------------------------------------------ 环境(envs)
#
# v2.4 用户模型:一个条目可以有多个「环境」(tab),每个环境 =
# 打包脚本 + 服务器 + 包名 + 路径 + 一组网站登录账号(仅备忘)。
# 旧的条目级 `deploy{}` / `last_deploy` 废弃,启动时自动迁移。
#
#   "envs": [{"name","script","target","pkg","path","accounts":[{user,password,note}],
#             "last": {time, ok, size, ms}}]


def git_remote_short(url: str) -> str:
    """把 origin 地址压成「@ 之后的那段」,详情头部跟分支并排显示。

    用户要求(v2.7.3):顶部只显示 `git@github.com:me/repo.git` 里 `@` 后面的
    `github.com:me/repo.git`,前面 `git@` / `https://` 这些协议噪声一律省掉。
    没有 `@` 的 https 地址就退化成「去掉协议 + 去掉末尾 .git」。
    """
    u = str(url or "").strip()
    if not u:
        return ""
    if "@" in u:
        u = u.rsplit("@", 1)[1]
    else:
        for pfx in ("https://", "http://", "ssh://", "git://"):
            if u.lower().startswith(pfx):
                u = u[len(pfx):]
                break
    if u.endswith(".git"):
        u = u[:-4]
    return u.strip().rstrip("/")


def env_name(env: dict, idx: int = 0) -> str:
    """环境 tab 标题:自定义名 → 「环境 N」。"""
    nm = str((env or {}).get("name") or "").strip()
    return nm or f"环境 {idx + 1}"


def env_last_text(env: dict) -> str:
    """「上次 10-23 14:02 · 47s · 3.2MB · 成功」,没有记录时说明还没发过。"""
    ld = (env or {}).get("last") or {}
    if not ld:
        return "还没发布过"
    parts = [str(ld.get("time") or "?")]
    if ld.get("ms"):
        parts.append(f"{int(ld.get('ms') or 0) // 1000}s")
    if ld.get("size"):
        parts.append(str(ld["size"]))
    parts.append("成功" if ld.get("ok") else "失败")
    return "上次 " + " · ".join(parts)


def env_job_item(env: dict) -> dict:
    """环境 → `deploy_static` 的单个配置项(引擎按 items 循环,单元素即可)。"""
    return {"target": (env or {}).get("target") or "",
            "pkg": (env or {}).get("pkg") or "dist",
            "path": (env or {}).get("path") or ""}


def migrate_envs(cfg: dict) -> bool:
    """把 v2.1 的 `deploy{}`/`last_deploy` 迁移成条目级 `envs[]`。

    返回是否有改动(有改动也不立刻写盘:等用户下一次保存时落盘,
    避免启动即改文件把 git 工作区弄脏)。任何异常都不影响启动。
    """
    changed = False
    try:
        tmap: dict[str, dict] = {}
        for t in (cfg.get("targets") or []):
            if isinstance(t, dict) and t.get("id"):
                tmap[str(t["id"])] = t
        for e in (cfg.get("entries") or []):
            if not isinstance(e, dict):
                continue
            dep = e.get("deploy")
            if not isinstance(dep, dict) or not dep:
                if "deploy" in e or "last_deploy" in e:
                    e.pop("deploy", None)
                    e.pop("last_deploy", None)
                    changed = True
                continue
            envs = e.get("envs")
            if not isinstance(envs, list):
                envs = []
            if envs:
                # 已经有环境:只清旧字段,不覆盖用户新配的东西
                e.pop("deploy", None)
                e.pop("last_deploy", None)
                changed = True
                continue
            ld = e.get("last_deploy") or {}
            new_envs: list[dict] = []
            for script, lst in dep.items():
                if not isinstance(lst, list):
                    continue
                for it in lst:
                    if not isinstance(it, dict):
                        continue
                    tid = str(it.get("target") or "")
                    t = tmap.get(tid) or {}
                    env = {
                        "name": str(t.get("name") or tid or script),
                        "script": str(script),
                        "target": tid,
                        "pkg": str(it.get("pkg") or "dist"),
                        "path": str(it.get("path") or ""),
                        "accounts": [],
                    }
                    if it.get("ack"):
                        env["ack"] = True
                    if isinstance(ld, dict) and ld.get("script") == script:
                        env["last"] = {k: ld.get(k) for k in ("time", "ok", "size", "ms")
                                       if k in ld}
                    new_envs.append(env)
            if new_envs:
                e["envs"] = new_envs
            e.pop("deploy", None)
            e.pop("last_deploy", None)
            changed = True
    except Exception:  # noqa: BLE001 — 迁移失败绝不能挡住启动
        return False
    return changed



# ---------------------------------------------------------- 发布:分步函数
#
# 每个 job 是「deploy 配置项 + 全局 target」合并后的可执行单元:
#   {"target": {id,name,ssh,port}, "tid": "t-1", "pkg": "dist",
#    "path": "/data/web/x", "ack": False}
# ack = 用户勾过「不再询问」的前端特征检查豁免,存在 deploy 配置项上。


def _rq(s: str) -> str:
    """远端 shell 的引用(单引号)。路径已过 deploy_validate,这里只做兜底。"""
    return shlex.quote(str(s or ""))


def _remote(job: dict, remote_cmd: str, timeout: int = 30) -> tuple[int, str]:
    """在目标服务器上执行一条命令(经 sh() 出口,自动补 PATH)。

    统一走 `_run_authed()`:密钥模式原样、密码模式自动注入 askpass 环境 ——
    发布链路上的每一步(探测/解压/切换/清理)都不用关心认证方式。
    """
    t = job.get("target") or {}
    ssh = target_addr(t)
    if not ssh:
        return 1, "该服务器没有填主机"
    return _run_authed(t, f"{target_ssh_prefix(t)} {_rq(ssh)} {_rq(remote_cmd)}",
                       timeout=timeout)


def deploy_job(item: dict, targets: list[dict]) -> dict | None:
    """把 deploy 配置项 + 全局 target 合并成 job;target 不存在返回 None。"""
    tid = (item or {}).get("target")
    t = next((x for x in (targets or []) if x.get("id") == tid), None)
    if not t:
        return None
    return {
        "target": t,
        "tid": tid,
        "pkg": (item.get("pkg") or "dist").strip(),
        "path": (item.get("path") or "").strip(),
        "ack": bool(item.get("ack")),
        "item": item,
    }


def _ask_confirm(confirm, question: str, ctx: dict) -> bool:
    """调用确认回调。

    约定签名是 confirm(question, ctx) -> bool(ctx 里带 tid/name/item,UI 层
    据此把「不再询问」写回配置项);同时兼容只收一个参数的旧式回调。
    """
    try:
        return bool(confirm(question, ctx))
    except TypeError:
        return bool(confirm(question))


def deploy_step_validate(entry: dict, script: str, items: list[dict],
                         busy: bool = False) -> tuple[bool, str]:
    """第 0 步:前置校验(脚本已配置、有勾选目标、未并发、每项护栏通过)。"""
    if busy:
        return False, "该条目已有发布任务在执行,请等它结束"
    if not script:
        return False, "未选择要发布的脚本"
    if not (entry or {}).get("dir"):
        return False, "该条目没有本地目录"
    if not os.path.isdir(entry["dir"]):
        return False, f"本地目录不存在:{entry['dir']}"
    if not items:
        return False, "没有勾选任何发布目标"
    for it in items:
        if not isinstance(it, dict):
            return False, "发布配置项格式异常"
        if not it.get("target"):
            return False, "发布目标缺少 target 引用(可能配错了)"
        ok, err = deploy_validate(it.get("pkg"), it.get("path"))
        if not ok:
            return False, f"配置校验失败({it.get('target')}):{err}"
    return True, ""


# ------------------------------------------------ 发布前 Git 检查(v3.0)
#
# PRD-v3 第二节原本把它划到二期,v3.0 落地。防的是「拿旧代码 / 拿没提交的
# 实验代码打了个包发出去」—— 构建用的是本地工作区,它和远程分支的偏差
# 用户自己心里没数时,发出去的包就是薛定谔的版本。


def git_state(d: str, timeout: int = 10) -> dict:
    """本地目录的 git 状态快照(纯查询,不改任何东西)。

    返回 {is_repo, branch, dirty, dirty_n, ahead, behind, err}:
      · 不是 git 仓库 → is_repo=False,其余字段无意义;
      · ahead/behind 相对 @{upstream};没配上游就是 0(rev-list 失败不算错,
        「没有上游」不该拦着发布);
      · git 本身跑不动(权限/仓库损坏)→ is_repo=True + err 带原因,
        调用方决定要不要拦。
    """
    st = {"is_repo": False, "branch": "", "dirty": False, "dirty_n": 0,
          "ahead": 0, "behind": 0, "err": ""}
    if not d or not os.path.isdir(os.path.join(d, ".git")):
        return st
    st["is_repo"] = True
    code, out = sh(f"git -C {shlex.quote(d)} rev-parse --abbrev-ref HEAD",
                   timeout=timeout)
    if code != 0:
        st["err"] = (out or "")[:160]
        return st
    st["branch"] = (out or "").strip()
    code, out = sh(f"git -C {shlex.quote(d)} status --porcelain", timeout=timeout)
    if code == 0:
        lines = [ln for ln in (out or "").splitlines() if ln.strip()]
        st["dirty"] = bool(lines)
        st["dirty_n"] = len(lines)
    # @{upstream} 里的花括号在 f-string 中要转义成 @{{upstream}};
    # 没配上游时 rev-list 报错,保持 ahead/behind = 0 即可
    code, out = sh(f"git -C {shlex.quote(d)} rev-list --left-right --count "
                   f"'@{{upstream}}'...HEAD", timeout=timeout)
    if code == 0:
        parts = (out or "").split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            # left=远程有而本地没有(落后),right=本地有而远程没有(领先)
            st["behind"], st["ahead"] = int(parts[0]), int(parts[1])
    return st


def deploy_git_check(entry: dict, confirm=None, log=print) -> tuple[bool, str]:
    """发布链路第 0.5 步:Git 分支 / 脏工作区检查。

    规则:
      · 不是 git 仓库 → 放行(有的项目就是没建仓);
      · git 跑不动 → 放行但日志提示(状态查不出原因时不该拦正常发布);
      · 干净 + 无偏差 → 放行;
      · 有未提交改动 / 领先远程(没推)/ 落后远程(不是最新)→ 弹确认;
        用户拒绝或无交互环境(confirm=None)→ 中止,**不开始构建**。
    与目录特征检查不同,这里的确认弹窗**不提供「不再询问」**:脏工作区每次
    都该提醒一次(勾一次永久放行就失去意义了),ctx 里也不带 item,
    `_handle_msg` 那侧自然不会写 ack。
    """
    st = git_state((entry or {}).get("dir") or "")
    if not st["is_repo"]:
        log("⏭ 发布前 Git 检查:不是 git 仓库,跳过")
        return True, ""
    if st.get("err"):
        log(f"⚠️ 发布前 Git 检查没跑成,放行:{st['err']}")
        return True, ""
    issues: list[str] = []
    if st["dirty"]:
        issues.append(f"有 {st['dirty_n']} 处未提交改动")
    if st["ahead"]:
        issues.append(f"领先远程 {st['ahead']} 个提交(还没 push)")
    if st["behind"]:
        issues.append(f"落后远程 {st['behind']} 个提交(远程有新代码没拉)")
    if not issues:
        log(f"   ✓ Git 检查通过:分支 {st['branch']} · 工作区干净")
        return True, ""
    summary = "、".join(issues)
    log(f"⚠️ Git 检查:分支 {st['branch']} — {summary}")
    if confirm is None:
        return False, f"Git 工作区异常({summary}),无交互环境无法确认,已中止"
    question = ("发布前 Git 检查发现:\n\n"
                f"当前分支:{st['branch']}\n"
                + "".join(f"· {i}\n" for i in issues)
                + "\n仍要用当前这份代码构建并发布吗?\n"
                  "(先提交 / 推送再发布,通常才是你想要的结果)")
    ctx = {"kind": "git", "branch": st["branch"]}
    if not _ask_confirm(confirm, question, ctx):
        return False, f"用户取消发布(Git 检查未通过:{summary})"
    return True, ""


def deploy_probe(job: dict, log=print) -> tuple[bool, str]:
    """第 1 步:探活 —— 目录必须已存在,且可用空间 ≥ 100MB。

    命令等价于 `test -d <path> && df -P <path> | tail -1`,但用显式分支
    把「SSH 不通 / 目录不存在 / df 读不出」区分开,便于给出准确的中止原因。
    """
    name = (job.get("target") or {}).get("name") or job.get("tid") or "?"
    path = job.get("path") or ""
    cmd = (f"test -d {_rq(path)} || echo NO_DIR; "
           f"df -P {_rq(path)} 2>/dev/null | tail -1")
    code, out = _remote(job, cmd, timeout=20)
    text = out or ""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    last = lines[-1] if lines else f"退出码 {code}"
    if (code == 255 or "Permission denied" in text or "Connection refused" in text
            or "No route to host" in text or "Operation timed out" in text):
        return False, f"[{name}] SSH 连不上:{last}\n→ {ssh_probe_hint(job.get('target'))}"
    if "NO_DIR" in text:
        return False, f"[{name}] 远端目录不存在:{path}(不会自动创建,请先手工建好)"
    avail_kb = None
    for ln in reversed(lines):
        f = ln.split()
        if len(f) >= 4 and f[3].isdigit():
            avail_kb = int(f[3])
            break
    if avail_kb is None:
        return False, f"[{name}] 读不到磁盘可用空间(df 输出异常):{last}"
    if avail_kb < MIN_FREE_KB:
        return False, (f"[{name}] 磁盘可用空间不足:{avail_kb // 1024}MB < 100MB"
                       f"({path})")
    log(f"   ✓ [{name}] 目录存在 · 可用 {avail_kb // 1024}MB")
    return True, ""


def deploy_feature_check(job: dict, confirm=None, log=print) -> tuple[bool, str]:
    """第 2 步:前端特征检查。目录为空或含 index.html 放行,否则要确认。

    confirm 回调签名 confirm(question: str) -> bool;无头时传 None 表示
    「遇到需要确认就中止」。ack=True 的配置项跳过检查(用户勾过不再询问)。
    """
    name = (job.get("target") or {}).get("name") or job.get("tid") or "?"
    path = job.get("path") or ""
    if job.get("ack"):
        log(f"   ✓ [{name}] 已确认过目录特征(ack),跳过检查")
        return True, ""
    cmd = (f"if [ -z \"$(ls -A {_rq(path)} 2>/dev/null)\" ]; then echo EMPTY; "
           f"elif [ -f {_rq(path)}/index.html ]; then echo HAS_INDEX; "
           f"else echo NO_INDEX; fi")
    code, out = _remote(job, cmd, timeout=15)
    text = out or ""
    if code != 0:
        return False, f"[{name}] 前端特征检查失败(SSH?):{text[-160:]}"
    if "NO_INDEX" not in text:
        # EMPTY(空目录)或 HAS_INDEX(有 index.html)都放行
        if "EMPTY" in text or "HAS_INDEX" in text:
            return True, ""
        return False, f"[{name}] 前端特征检查返回异常:{text[-160:]}"
    question = (f"[{name}] 目标目录非空,且没有 index.html:\n{path}\n\n"
                f"看起来不像前端静态目录(可能是后端目录)。仍要发布到这里吗?\n"
                f"(勾选「不再询问」后该目标以后不再检查)")
    if confirm is None:
        return False, (f"[{name}] 目标目录非空且无 index.html,无交互环境无法确认,"
                       f"已中止:{path}")
    ctx = {"tid": job.get("tid"), "name": name, "path": path,
           "item": job.get("item")}
    if not _ask_confirm(confirm, question, ctx):
        return False, f"[{name}] 用户取消发布(目录内容不像前端):{path}"
    return True, ""


def deploy_prepare_incoming(job: dict, log=print) -> tuple[bool, str]:
    """第 3 步:清理残留的 <path>.incoming 并重建空目录。

    注意:目标目录本身**绝不** mkdir(PRD 第九节第 2 条);这里只碰
    工具自己创建的 `<path>.incoming`,父目录已由探活确认存在。
    """
    name = (job.get("target") or {}).get("name") or job.get("tid") or "?"
    inc = (job.get("path") or "") + ".incoming"
    cmd = f"rm -rf {_rq(inc)} && mkdir -p {_rq(inc)} && echo READY"
    code, out = _remote(job, cmd, timeout=30)
    if code != 0 or "READY" not in (out or ""):
        return False, f"[{name}] 清理/准备远端临时目录失败:{(out or '')[-200:]}"
    log(f"   ✓ [{name}] 已清理并准备 {inc}")
    return True, ""


def deploy_build(local_dir: str, script: str, node_version: str | None = None,
                 timeout: int = 900, log=print,
                 build_cmd: str | None = None) -> tuple[bool, str]:
    """第 4 步:本地构建(超时 900s,失败回显最后 20 行)。

    build_cmd 可覆盖默认的 `npm run <script>`(无头测试时传 `true` 之类)。
    """
    if not local_dir or not os.path.isdir(local_dir):
        return False, f"本地目录不存在:{local_dir}"
    cmd = build_cmd or f"npm run {shlex.quote(script)}"
    log(f"▶ 构建 {script}: {cmd}")
    code, out = sh(cmd, cwd=local_dir, timeout=timeout, node_version=node_version)
    if code != 0:
        tail = "\n".join((out or "").splitlines()[-20:])
        return False, f"构建失败(退出码 {code}),最后 20 行输出:\n{tail}"
    return True, ""


def deploy_count_files(d: str) -> tuple[int, int]:
    """统计目录下文件数与本机总字节数(用于「148 个文件 · 3.2MB」)。"""
    count, total = 0, 0
    for root, _dirs, files in os.walk(d):
        for fn in files:
            count += 1
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return count, total


def deploy_verify_pkg(local_dir: str, pkg: str) -> tuple[bool, str, dict]:
    """第 5 步:校验本地产物目录存在且非空。返回 (ok, err, info)。"""
    p = os.path.join(local_dir or "", pkg or "")
    if not os.path.isdir(p):
        return False, f"产物目录不存在:{p}", {}
    count, total = deploy_count_files(p)
    if count == 0:
        return False, f"产物目录是空的(没有任何文件):{p}", {}
    return True, "", {"dir": p, "files": count, "bytes": total}


def deploy_pack(local_dir: str, pkg: str, entry_id: str,
                ts: str | None = None) -> tuple[bool, str, str]:
    """第 6 步:打包 tar -czf /tmp/pc-<id>-<ts>.tgz -C <dir>/<pkg> .

    返回 (ok, tgz 路径, err)。
    """
    pkg_dir = os.path.join(local_dir or "", pkg or "")
    if not os.path.isdir(pkg_dir):
        return False, "", f"打包失败:产物目录不存在:{pkg_dir}"
    stamp = ts or time.strftime("%m%d%H%M%S")
    tgz = f"/tmp/pc-{safe_name(entry_id or 'x')}-{stamp}.tgz"
    old = glob.glob(f"/tmp/pc-{safe_name(entry_id or 'x')}-*.tgz")
    for p in old:                       # 残留的旧包先清掉,免得 /tmp 越堆越多
        try:
            os.remove(p)
        except OSError:
            pass
    code, out = sh(f"tar -czf {shlex.quote(tgz)} -C {shlex.quote(pkg_dir)} .",
                   timeout=300)
    if code != 0 or not os.path.isfile(tgz):
        return False, "", f"打包失败(退出码 {code}):{(out or '')[:300]}"
    return True, tgz, ""


def deploy_upload(job: dict, tgz: str, log=print, progress=None,
                  timeout: int = 600) -> tuple[bool, str]:
    """第 7 步:scp 上传到 <path>.incoming/a.tgz(超时 600s)。

    progress(pct: int) 由后台监控线程按「文件大小 / 估算带宽」推算上传
    百分比(scp 非 TTY 下不输出进度,PRD 允许按文件大小估算),不引入
    任何新依赖。传到 100% 以 scp 退出码为准,不以估算为准。
    """
    t = job.get("target") or {}
    name = t.get("name") or job.get("tid") or "?"
    ssh = target_addr(t)
    if not ssh:
        return False, "该服务器没有填主机"
    try:
        port = int(t.get("port") or 22)
    except (TypeError, ValueError):
        port = 22
    port_opt = f" -P {port}" if port != 22 else ""
    remote_file = (job.get("path") or "") + ".incoming/a.tgz"
    try:
        size = os.path.getsize(tgz)
    except OSError as ex:
        return False, f"[{name}] 本地包不存在:{ex}"
    # scp 的端口选项是 -P(大写),认证选项与 ssh 完全一致,同样走 _run_authed
    cmd = (f"scp {ssh_opts(t)}{port_opt} "
           f"{shlex.quote(tgz)} {_rq(ssh + ':' + remote_file)}")

    stop = threading.Event()

    def monitor():
        """按体积估算进度(约 2.5MB/s),最多报到 95%,剩余等 scp 退出。"""
        started = time.time()
        expect = max(1.5, size / (2.5 * 1024 * 1024))
        while not stop.wait(0.6):
            if progress is None:
                continue
            pct = min(95, int((time.time() - started) / expect * 100))
            try:
                progress(pct)
            except Exception:  # noqa: BLE001 — 进度回调失败绝不能影响上传
                pass

    if progress is not None:
        try:
            progress(0)
        except Exception:  # noqa: BLE001
            pass
    th = threading.Thread(target=monitor, daemon=True)
    th.start()
    try:
        code, out = _run_authed(t, cmd, timeout=timeout)
    finally:
        stop.set()
    text = (out or "").strip()
    if code != 0:
        hint = ssh_error_hint(text, t)
        detail = f"{hint};{text[-200:]}" if hint else text[-200:]
        return False, f"[{name}] 上传失败(退出码 {code}):{detail}"
    if progress is not None:
        try:
            progress(100)
        except Exception:  # noqa: BLE001
            pass
    log(f"⬆ 上传 → {name} ({deploy_fmt_size(size)})")
    return True, ""


def deploy_unpack(job: dict, log=print) -> tuple[bool, str]:
    """第 8 步:远端解压 tar -xzf a.tgz -C <path>.incoming。"""
    name = (job.get("target") or {}).get("name") or job.get("tid") or "?"
    inc = (job.get("path") or "") + ".incoming"
    cmd = (f"tar -xzf {_rq(inc + '/a.tgz')} -C {_rq(inc)} "
           f"&& rm -f {_rq(inc + '/a.tgz')} && echo UNPACKED")
    code, out = _remote(job, cmd, timeout=300)
    if code != 0 or "UNPACKED" not in (out or ""):
        return False, f"[{name}] 远端解压失败:{(out or '')[-200:]}"
    return True, ""


def deploy_switch(job: dict, log=print) -> tuple[bool, str]:
    """第 9 步:切换 —— 只留一代 .bak,再把 .incoming 提升为目标目录。"""
    name = (job.get("target") or {}).get("name") or job.get("tid") or "?"
    path = job.get("path") or ""
    bak, inc = path + ".bak", path + ".incoming"
    cmd = (f"rm -rf {_rq(bak)} && mv {_rq(path)} {_rq(bak)} && "
           f"mv {_rq(inc)} {_rq(path)} && echo SWITCHED")
    code, out = _remote(job, cmd, timeout=120)
    if code != 0 or "SWITCHED" not in (out or ""):
        return False, f"[{name}] 切换失败(检查目录权限):{(out or '')[-200:]}"
    return True, ""


def deploy_cleanup_incoming(job: dict, log=print) -> None:
    """清理远端 <path>.incoming(传输/解压/切换失败后兜底,失败静默)。"""
    inc = (job.get("path") or "") + ".incoming"
    try:
        _remote(job, f"rm -rf {_rq(inc)}", timeout=30)
        log(f"🧹 已清理远端临时目录 {inc}")
    except Exception:  # noqa: BLE001
        pass


def deploy_static(entry: dict, script: str, items: list[dict], *,
                  targets: list[dict] | None = None,
                  log=print, confirm=None, progress=None,
                  skip_build: bool = False, build_cmd: str | None = None,
                  build_timeout: int = 900, upload_timeout: int = 600,
                  busy: bool = False) -> dict:
    """发布主流程(PRD 第八节 0-10 步),串行、本地只构建一次。

    参数:
        entry   注册表条目(需要 id / dir / node_version)
        script  build 脚本名(如 build:uat),只用于日志与 npm run
        items   本次要发的 deploy 配置项列表(UI 只传勾选上的)
        targets 全局服务器列表(用于 target 引用解析)
        log     日志回调,App 传日志区写入
        confirm 确认回调 confirm(question)->bool;None = 需确认就中止
        progress 上传进度回调 progress(pct:int)
        skip_build / build_cmd  无头测试用:跳过或替换构建命令
        busy    并发互斥标记(由调用方判断该条目是否已有任务在跑)

    返回 {ok, err, ms, size, files, script, targets[], done[]}。
    任一步失败立即中止,后续目标不再发,已成功目标不回滚(PRD 第八节末)。
    """
    started = time.time()
    eid = (entry or {}).get("id") or "?"
    res: dict = {"ok": False, "err": "", "ms": 0, "size": "", "files": 0,
                 "script": script, "targets": [], "done": []}

    def _log(text: str):
        try:
            log(text)
        except Exception:  # noqa: BLE001 — 日志回调不能拖垮发布
            pass

    def _fail(msg: str) -> dict:
        res["err"] = msg
        res["ms"] = int((time.time() - started) * 1000)
        _log(f"❌ [{eid}] {msg}")
        return res

    # 0) 前置校验
    ok, err = deploy_step_validate(entry, script, items, busy=busy)
    if not ok:
        return _fail(err)

    jobs: list[dict] = []
    for it in items:
        job = deploy_job(it, targets or [])
        if job is None:
            return _fail(f"发布目标不存在(可能服务器已被删除):{it.get('target')}")
        jobs.append(job)
    res["targets"] = [j["tid"] for j in jobs]

    # 0.5) 发布前 Git 检查(脏工作区 / 未推送 → 用户确认;拒绝则不开始构建)
    ok, err = deploy_git_check(entry, confirm=confirm, log=_log)
    if not ok:
        return _fail(err)

    _log(f"▶ [{eid}] 构建 {script}({len(jobs)} 个目标)")

    # 1) 探活:全部目标先过一遍,有一台不行就不开始构建
    for job in jobs:
        ok, err = deploy_probe(job, log=_log)
        if not ok:
            return _fail(err)

    # 2) 前端特征检查
    for job in jobs:
        ok, err = deploy_feature_check(job, confirm=confirm, log=_log)
        if not ok:
            return _fail(err)

    # 3) 清理残留 .incoming
    for job in jobs:
        ok, err = deploy_prepare_incoming(job, log=_log)
        if not ok:
            return _fail(err)

    # 4) 构建(本地只做一次)
    local_dir = entry.get("dir") or ""
    if skip_build:
        _log(f"⏭ 跳过构建(skip_build),直接用现有 {local_dir}")
    else:
        ok, err = deploy_build(local_dir, script,
                               node_version=entry.get("node_version"),
                               timeout=build_timeout, log=_log,
                               build_cmd=build_cmd)
        if not ok:
            return _fail(err)

    # 5) 校验产物
    pkg = jobs[0]["pkg"]
    ok, err, info = deploy_verify_pkg(local_dir, pkg)
    if not ok:
        return _fail(err)
    res["files"] = info["files"]
    res["size"] = deploy_fmt_size(info["bytes"])
    _log(f"📦 产物 {pkg} · {info['files']} 个文件 · {res['size']}")

    # 6) 打包(本地只打一次)
    ok, tgz, err = deploy_pack(local_dir, pkg, eid)
    if not ok:
        return _fail(err)

    # 7-9) 逐目标:上传 → 解压 → 切换
    done: list[str] = []
    try:
        for job in jobs:
            t0 = time.time()
            name = (job.get("target") or {}).get("name") or job.get("tid") or "?"

            ok, err = deploy_upload(job, tgz, log=_log, progress=progress,
                                    timeout=upload_timeout)
            if not ok:
                deploy_cleanup_incoming(job, log=_log)
                res["done"] = done
                return _fail(err)

            ok, err = deploy_unpack(job, log=_log)
            if not ok:
                deploy_cleanup_incoming(job, log=_log)
                res["done"] = done
                return _fail(err)

            _log(f"🔁 切换 {name}")
            ok, err = deploy_switch(job, log=_log)
            if not ok:
                deploy_cleanup_incoming(job, log=_log)
                res["done"] = done
                return _fail(err)

            done.append(job["tid"])
            _log(f"✅ {name} 完成({int(time.time() - t0)}s)")
    finally:
        try:                            # 本地包用完就删,别在 /tmp 堆着
            os.remove(tgz)
        except OSError:
            pass

    res["done"] = done
    res["ok"] = True
    res["ms"] = int((time.time() - started) * 1000)
    _log(f"✅ 全部完成 · {len(done)} 个目标 · 用时 {res['ms'] // 1000}s")
    return res


# ============================================================================
# 交互式 shell(pty)——「项目日志 tab 里能直接输命令」的底层
#
# 为什么用 pty 而不是管道:需要 tty 的命令(npm run dev 的进度、git 的分页器、
# 交互式 y/N 确认)在普通管道下行为会变;pty 让它们以为自己在终端里。
# 为什么用 `zsh -f`:跳过 .zshrc —— 用户装了 powerlevel10k 之类的花哨提示符,
# 会把日志区刷满控制序列。PATH 由 _path_env() 注入,功能不受影响。
# 本段零 tkinter,UI 只负责把 pty_read 的输出丢进队列。
# ============================================================================

PTY_COLS, PTY_ROWS = 120, 40

# ANSI 转义序列(CSI 颜色/光标、OSC 标题、字符集、单字符转义)+ 其余控制字符。
# 保留 \n \t \r —— 它们决定换行与缩进,不能当噪音删掉。
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[a-zA-Z]"                 # CSI:颜色、清屏、光标移动
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"     # OSC:窗口标题等
    r"|\x1b[()][A-Za-z0-9]"                   # 字符集切换
    r"|\x1b[@-Z\\-_]"                         # 其他单字符转义
    r"|[\x00-\x08\x0b\x0c\x0e-\x1f]"          # 控制字符(含孤立 ESC)
)


# 「字符 + 退格」重复回显的形态:readline/行编辑把每个字符再擦一次。
# zsh 用 `-o nozle` 关掉自带行编辑后正常不会出现,但用户可能在 shell 里
# 起别的程序(irb、psql 之类)自己开行编辑,所以保留这层兜底。
_BACKSPACE_RE = re.compile(r".\x08")
_SPACE_RUN_RE = re.compile(r"[ \t]{3,}")


def strip_ansi(text: str) -> str:
    """剥掉 ANSI 转义与控制字符,保留 \\n / \\t / \\r。

    不引入任何三方依赖:终端配色/光标序列都是纯正则能覆盖的固定形态。
    """
    if not text:
        return ""
    return _ANSI_RE.sub("", text)


def clean_term_text(text: str) -> str:
    """把 pty 原始输出洗成人能读的文本(兜底层,正常路径下几乎不做事)。

    四步:去掉「字符+\\x08」重复回显 → 剥 ANSI/控制符 → `\\r\\n` 归一 →
    折叠 3 个以上连续空格(纯空格填充的进度条会占满整行)。
    注意顺序:退格对必须在剥控制符**之前**做 —— `\\x08` 本身属于控制字符,
    先剥掉它就没有「哪个字符被擦掉」的信息了。
    只用于**显示**,不要用它去解析命令输出。
    """
    if not text:
        return ""
    out = _BACKSPACE_RE.sub("", text)
    out = strip_ansi(out)
    out = out.replace("\r\n", "\n")
    out = _SPACE_RUN_RE.sub("  ", out)
    return out


def pty_spawn(cwd: str, node_version: str | None = None,
              shell: str | None = None) -> dict:
    """起一个**常驻**交互式 shell,返回会话 dict(传给下面几个 pty_* 函数)。

    会话 dict:`{master: int, pid: int, proc: Popen}`。

    - `zsh -f -i`:跳过 rc 文件(避免 powerlevel10k 之类把日志刷满控制序列),
      但保持交互式;PATH 走 `_path_env()`,.app 的最小 PATH 下才找得到 node/git;
    - `-o nozle`:关掉 zsh 自己的行编辑。输入编辑交给 UI 的 Entry,
      否则会出现「重复回显 + 退格符」(`e\\becho hi` 这种脏输出);
    - `-o noprompt_sp -o noprompt_cr`:干掉 zsh 的 `PROMPT_SP`
      (一串空格 + `%` 标记 + `\\r`),这是裸起 `zsh -i` 最脏的一处;
    - PROMPT/PS1/PS2/PROMPT_EOL_MARK 全置空 + 关闭终端回显:
      命令回显由 UI 自己打 `❯ cmd`,提示符也由 UI 画,视觉才可控;
    - TERM=dumb:不要颜色与动画(npm/vite 的进度条退化成纯文本行);
    - `os.setsid`:shell 自成一个进程组,中断/清理对整组发信号,
      不会把 `npm run dev` 的孙进程留成孤儿,也不会误伤控制台自己。
    """
    import fcntl
    import pty as _pty
    import struct
    import termios

    sh = shell or os.environ.get("SHELL") or "/bin/zsh"
    if not sh or not os.path.exists(sh):
        sh = "/bin/zsh"
    env = _path_env(node_version)
    env.update({
        "TERM": "dumb",
        "PROMPT": "", "PROMPT2": "", "PROMPT_EOL_MARK": "",
        "RPROMPT": "", "PS1": "", "PS2": "",
    })
    args = [sh, "-f", "-i"]
    if sh.endswith("zsh"):
        # 这三项只有 zsh 认;换 bash/fish 时加了会直接起不来,所以按 shell 名判断。
        args += ["-o", "noprompt_sp", "-o", "noprompt_cr", "-o", "nozle"]
    master, slave = _pty.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ,
                    struct.pack("HHHH", PTY_ROWS, PTY_COLS, 0, 0))
        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~termios.ECHO          # 关回显(命令回显我们自己打)
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
    except OSError:
        pass
    proc = subprocess.Popen(
        args,
        stdin=slave, stdout=slave, stderr=slave,
        cwd=cwd or os.path.expanduser("~"), env=env,
        preexec_fn=os.setsid, close_fds=True,
    )
    os.close(slave)
    return {"master": master, "pid": proc.pid, "proc": proc}


def pty_read(sess: dict, size: int = 4096) -> bytes:
    """阻塞读一段输出(调用方必须放在守护线程里)。

    返回 b"" 表示「没得读了 / shell 已经退出」。master 被关闭时
    `os.read` 抛的是 `OSError`(EIO),这里**静默吞掉**当成 EOF ——
    读线程要能安静结束,不能把异常抛进消息泵刷错误日志。
    """
    master = (sess or {}).get("master")
    if master is None:
        return b""
    try:
        return os.read(master, size)
    except OSError:
        return b""


def pty_write(sess: dict, cmd: str) -> bool:
    """把一行命令写进 shell(自动补换行)。写入失败返回 False。"""
    master = (sess or {}).get("master")
    if master is None:
        return False
    try:
        os.write(master, (cmd.rstrip("\n") + "\n").encode("utf-8"))
        return True
    except OSError:
        return False


def pty_alive(sess: dict | None) -> bool:
    """shell 还活着吗(用 Popen.poll,不需要额外进程/命令)。"""
    proc = (sess or {}).get("proc")
    if proc is None:
        return False
    try:
        return proc.poll() is None
    except Exception:  # noqa: BLE001
        return False


def pty_int(sess: dict | None) -> bool:
    """给 shell 所在进程组发 SIGINT(等价 Ctrl-C,中断当前命令)。"""
    if not sess:
        return False
    try:
        os.killpg(os.getpgid(sess["pid"]), signal.SIGINT)
        return True
    except OSError:
        return False


def pty_kill(sess: dict | None) -> None:
    """收掉 shell 及其子进程组并关闭 pty master —— 不留僵尸/孤儿进程。

    SIGTERM 整组 → 0.5s 未退则 SIGKILL → `proc.wait()` 真正回收子进程
    (只发信号不 wait 会留僵尸),最后关闭 master。
    """
    if not sess:
        return
    master = sess.get("master")
    pid = sess.get("pid")
    proc = sess.get("proc")
    if pid:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            pass
    if proc is not None:
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            if pid:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except OSError:
                    pass
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
        except Exception:  # noqa: BLE001
            pass
    if master is not None:
        try:
            os.close(master)
        except OSError:
            pass
    sess["master"] = None
