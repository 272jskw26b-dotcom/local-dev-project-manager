#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Project Console —— 本机项目控制台(入口 + App 核心逻辑)

统一查看/启动/停止本地项目环境,以及远程服务器上的 systemd 服务。
受管条目全部声明在旁路 projects.json,新增项目不改代码。

模块拆分(v2.6)—— 核心逻辑集中在 project_console.py,周边按功能切块:
    pc_core.py            纯逻辑层(注册表/扫描/命令执行/SSH/pty,零 tkinter)
    pc_const.py           全局常量(APP_TITLE / GROUPS / SYSTEM_TAB ...)
    pc_theme.py           双主题配色字典
    pc_widgets.py         自绘控件(_DotList / _ScrollList / _ServerList /
                          _ScriptList)+ 按钮渐变底图绘制
    pc_log_panel.py       日志区分块(LogPanelMixin):tab、内置终端、tail、日志写入
    pc_env_panel.py       环境配置分块(EnvPanelMixin):环境 tab、账号表、发布流程
    pc_servers_dialog.py  服务器面板分块(ServersMixin):targets 的 CRUD 弹窗
    project_console.py    本文件:入口 + App(窗口骨架/主题/详情渲染/线程模型/队列)

    App 由三个 mixin + tk.Tk 多重继承组成:**tk.Tk 必须写在最后**。
    三个 mixin 之间互不 import,只允许 import pc_core / pc_const / pc_theme /
    pc_widgets,跨块调用一律走 self.xxx。

用法:
    python3 project_console.py            # 源码运行
    ./build.sh run                        # 同上(项目 venv)
    ./build.sh                            # PyInstaller 打包并部署桌面 .app

用法与配置说明见 README.md。
"""

import glob
import json
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

import pc_core
# 模块拆分后 re-export:保持 `import project_console as pc` 的旧用法兼容
# (AGENTS.md 的无头冒烟写法 pc.load_config() / pc.port_listening() 等)。
from pc_core import (  # noqa: F401
    DEFAULT_SSH_USER, MAVEN_SCRIPTS, SSH_TIMEOUT,
    base_dir, check_registry, check_target, config_path, cfg_work_root,
    deploy_fmt_size, deploy_static, deploy_validate, env_report, guess_pkg,
    health_ok, load_config, load_scripts, load_targets, new_target_id, notify,
    port_listening, port_pids, proc_info, safe_name, save_config, save_targets,
    scan_code_dir, sh, svc_active,
    env_job_item, env_last_text, env_name, git_remote_short, migrate_envs,
    ssh_error_hint,
    target_addr, target_auth, target_host, target_password, target_refs,
    target_ssh, target_user,
    # v2.2 pty 层:AGENTS.md 的冒烟片段用 `import project_console as c` 再
    # `c.pty_spawn(...)`,所以这里必须一并 re-export(否则文档写法直接 AttributeError)。
    clean_term_text, pty_alive, pty_int, pty_kill, pty_read, pty_spawn,
    pty_write, strip_ansi,
    # v3.0 密钥外置 + 发布前 Git 检查:冒烟脚本要用,同样 re-export。
    deploy_git_check, ensure_env_ids, git_state, load_secrets, save_secrets,
    secrets_path,
)
# 常量经本文件再导出一层:`pc.GROUPS` / `pc.SYSTEM_TAB` 等旧写法不能断。
from pc_const import (  # noqa: F401
    ACCOUNT_COLS, APP_TITLE, AUTH_KEY, AUTH_LABELS, AUTH_PW, AUTO_REFRESH_MS,
    BUILD_PREFIX, DOT, GROUPS, LOG_MAX_LINES, LOG_TITLE_MAX,
    START_TIMEOUT_DEFAULT, SYSTEM_TAB,
)
from pc_env_panel import EnvPanelMixin
from pc_log_panel import LogPanelMixin
from pc_servers_dialog import ServersMixin
from pc_theme import THEMES, THEME_FILE  # noqa: F401
from pc_widgets import (  # noqa: F401
    # 三个列表类原本就定义在本文件里,拆分后仍从本模块再导出一次,
    # 保证 `import project_console as pc` 后 `pc._ScriptList` 等旧写法不失效。
    _DotList, _ScriptList, _ServerList, _ScrollList, _StatusChip, _btn_cache,
    _ChipFlow, _make_btn_png,
)


# ---------------------------------------------------------------- 应用


class App(LogPanelMixin, EnvPanelMixin, ServersMixin, tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("980x640")
        self.minsize(860, 540)
        # 关窗口前必须收掉所有 pty shell(否则 npm run dev 之类会变孤儿进程)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.theme = self._load_theme()
        self.configure(bg=self._c("bg"))

        try:
            self.cfg = load_config()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(
                "配置加载失败",
                f"注册表解析失败且无可用备份:\n{config_path()}\n\n{e}\n\n"
                f"请手工修复后重启;同目录若有 projects.json.bak 可直接改名恢复。")
            self.cfg = {"entries": []}
        if pc_core.last_restored_from_bak:
            messagebox.showwarning(
                "注册表已自动恢复",
                f"检测到 projects.json 损坏,已从备份恢复:\n{config_path()}\n\n"
                f"损坏的原文件保留在 {config_path()}.bad,可打开排查原因。")
        self.entries: list[dict] = self.cfg["entries"]

        # 老注册表(deploy{} + last_deploy)自动迁移成条目级 envs[]。
        # 只在内存里改;真正落盘等用户下一次保存 —— 启动就写文件会把
        # git 工作区莫名弄脏,也容易在只读目录下报错。
        try:
            if migrate_envs(self.cfg):
                self.log_migrated = True
        except Exception:  # noqa: BLE001 — 迁移失败不能挡住启动
            pass

        # 状态缓存: {entry_id: {"ports": {port: bool|None}, "svc": str,
        #                    "svc_detail": str, "health": bool|None,
        #                    "extra_svc": {name: str}, "pids": [str]}}
        self.status: dict[str, dict] = {}
        for e in self.entries:
            self.status[e["id"]] = {
                "ports": {p["port"]: None for p in (e.get("local") or {}).get("ports", [])},
                "svc": None, "svc_detail": "", "health": None, "extra_svc": {},
                "pids": [],
            }

        # 详情区状态灯的标签索引:{eid: {key: Label}}。
        # 必须在 __init__ 里先建好:_poll_queue 从 100ms 就开始跑,
        # 而详情渲染(selected)要 500ms 才发生,晚初始化会让早到的
        # 状态消息命中 AttributeError 并中断整个轮询循环(v2.0 修复)。
        self._dot_labels: dict[str, dict] = {}
        self._proc_labels: dict[str, dict] = {}
        self.q: queue.Queue = queue.Queue()
        self.sel_id: str | None = None
        self.scripts: dict[str, list] = {}   # eid -> [(script名, 命令)]
        self.tails: dict[str, int] = {}      # 日志文件 -> 已读偏移(-1=先读末尾2KB)
        self.tail_eid: dict[str, str] = {}   # 日志文件 -> 归属条目(决定写哪个 tab)
        # ---- v2.2 日志 tab + 内置终端(状态只在主线程读写)
        self._logtabs: dict[str, dict] = {}   # tid -> {tab,lab,close,body,text,term,…}
        self._logtab_id: str = SYSTEM_TAB     # 当前显示的日志 tab
        self._logbar_cv = None                # tab 条的横向滚动画布
        self._term_muted: set[str] = set()    # 主动收掉的 shell(不再提示「已退出」)
        # 已点启动但端口尚未就绪的条目:{eid: 点启动的时间戳}(P0-1 超时判定)
        self.starting: dict[str, float] = {}
        # ---- 详情区「脚本执行 / 环境配置」分段页
        # eid -> "scripts" | "env"(缺省看有没有脚本,见 `_detail_pane_of`)
        self._detail_pane: dict[str, str] = {}
        # ---- v2.4 环境配置状态(全部由主线程读写)
        self._env_tab: dict[str, int] = {}            # eid -> 当前选中的环境下标
        self._env_btns: dict[str, dict] = {}          # eid -> {环境下标: 发布按钮}
        # 环境面板渲染到哪个帧(切环境/增删环境时只重建这个帧的内容,
        # 不再调 select() 把整个详情区推倒重来)
        self._env_host: dict[str, object] = {}        # eid -> host Frame
        self._env_host_title: dict[str, bool] = {}    # eid -> 是否带分区标题
        self._env_test_btn: dict[str, dict] = {}      # eid -> {idx,btn,set} 测试按钮引用
        # 悬挂保存钩子:详情区重建(切 tab/切项目/切分组/关窗)之前必须先落盘,
        # 否则用户「敲完路径直接点另一个 tab」会静默丢输入(见坑 51)
        self._env_flush = None
        self._env_debounce = None                     # <KeyRelease> 防抖的 after id
        self._deploy_running: set[str] = set()        # 正在发布的条目(并发互斥)
        self._deploy_state: dict[str, dict] = {}      # eid -> {t0, pct, phase, idx}
        self._srv_dlg = None                          # 服务器管理窗口(单例)
        self._build_style()
        self._build_topbar()
        self._build_ui()

        self.after(100, self._poll_queue)
        self.after(500, lambda: self.select(
            (self._visible_entries() or self.entries or [{}])[0].get("id")))
        self.after(AUTO_REFRESH_MS, self._auto_refresh)
        self.after(1500, self._tail_loop)
        self.after(1000, self._deploy_tick)
        self.log(f"🗂 注册表: {config_path()}")
        # v3.0:密码/账号外置在同目录 secrets.json(gitignore),横幅里说清楚
        # 存在哪,免得用户翻 projects.json 找不到密码字段以为丢了。
        self.log(f"🔐 密钥文件: {secrets_path()}(密码与网站账号,不进 git)")
        if pc_core.last_secrets_restored_from_bak:
            self.log("⚠️ secrets.json 损坏过,已自动从备份恢复(原文件留为 .bad)")
        # 首启引导:所有候选位置都没有 projects.json 且从未完成过引导时弹窗。
        # 延后到 UI 建完再弹——askdirectory 需要父窗口,空列表正好做背景。
        if pc_core.last_config_missing and not self.cfg.get("_onboarded"):
            self.after(400, self.run_onboarding)

    # ------------------------------------------------ UI 基建

    def _c(self, key: str) -> str:
        """取当前主题颜色。"""
        return self.theme[key]

    def _load_theme(self) -> dict:
        name = "dark"
        try:
            with open(THEME_FILE, encoding="utf-8") as f:
                t = f.read().strip()
            if t in THEMES:
                name = t
        except OSError:
            pass
        return THEMES[name]

    def _save_theme(self):
        name = "dark" if self.theme is THEMES["dark"] else "light"
        try:
            with open(THEME_FILE, "w", encoding="utf-8") as f:
                f.write(name)
        except OSError:
            pass

    def toggle_theme(self):
        """在深/浅主题间切换,并即时刷新全窗口颜色。"""
        self.theme = THEMES["light"] if self.theme is THEMES["dark"] else THEMES["dark"]
        self._save_theme()
        _btn_cache.clear()   # 清空旧主题色的按钮底图缓存
        self._apply_theme()

    def _apply_theme(self):
        self.configure(bg=self._c("bg"))
        self._build_style()     # ttk(下拉框/滚动条)必须重新配色,否则还是旧主题
        # 全量销毁并重建:顶栏 + 主体(左栏/详情/日志)一并重建,
        # 容器背景全部重新绑定 _c(),彻底消除"半新半旧"。
        # 注意 _top 本身也要销毁,否则 _build_topbar 会不断 pack 新顶栏导致顶部叠空白。
        # 日志区会被一起销毁:先存下各 tab 的文本,重建后放回去(切主题不该丢日志);
        # pty shell 则必须先收掉,否则重建后没人再持有它们 → 孤儿进程。
        snap = self._snapshot_log()
        self._term_stop_all()
        self._top.destroy()
        self._vpan.destroy()   # 级联销毁 _main/_left/detail/_logwrap 整棵树
        self._build_topbar()
        self._build_ui()
        self._restore_log(snap)
        # _build_ui 末尾已 _reload_list();这里恢复选中态并重渲染详情
        self.select(self.sel_id)

    def _build_style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=self._c("bg"), foreground=self._c("fg"),
                     fieldbackground=self._c("card"))
        # v2.1:下拉框与滚动条也要跟主题(发布面板的服务器下拉、两个自绘列表)
        try:
            st.configure("PC.TCombobox",
                         fieldbackground=self._c("card"),
                         background=self._c("btn"),
                         foreground=self._c("fg"),
                         arrowcolor=self._c("gear"),
                         bordercolor=self._c("line"),
                         lightcolor=self._c("btn"),
                         darkcolor=self._c("btn"),
                         selectbackground=self._c("accent"),
                         selectforeground=self._c("btn_fg"),
                         padding=3)
            st.map("PC.TCombobox",
                   fieldbackground=[("readonly", self._c("card"))],
                   foreground=[("readonly", self._c("fg"))],
                   background=[("active", self._c("btn_hover"))])
            st.configure("PC.Vertical.TScrollbar",
                         background=self._c("scroll_thumb"),
                         troughcolor=self._c("scroll_trough"),
                         bordercolor=self._c("line"),
                         arrowcolor=self._c("gear"),
                         lightcolor=self._c("scroll_thumb"),
                         darkcolor=self._c("scroll_thumb"),
                         width=11)
            st.map("PC.Vertical.TScrollbar",
                   background=[("active", self._c("accent"))])
        except tk.TclError:
            pass

    def _fit_btn_width(self, texts, padx: int = 16) -> int:
        """算一组按钮的**统一宽度**:取最宽文案 + 内边距。

        用途:「▶ 启动(dev)」比「■ 停止」长,两个按钮并排时右端不齐很显眼。
        把同组按钮的宽度钉成同一个值(按本组最长文案算),既等长又不会裁字;
        启动按钮文字随选中脚本变化,所以候选里要带上所有可能的脚本名。

        **必须用 Label 实测**(与 `_btn` 内部量 tw 的方式完全一致):字体
        `measure()` 与 Label 的 reqwidth 对 ■ ▶ 这类符号的宽度判定不同,
        差 1-2px 就会出现「明明传入同一个 width,渲染出来还是不齐」。
        """
        need = 0
        probe = tk.Label(self, text="x", font=("PingFang SC", 11))
        try:
            for t in texts:
                probe.configure(text=str(t))
                probe.update_idletasks()
                need = max(need, probe.winfo_reqwidth())
        finally:
            probe.destroy()
        return need + padx * 2

    def _btn(self, parent, text, cmd, danger=False, primary=False, width=None):
        """预渲染图片渐变胶囊按钮。

        macOS 原生 tk.Button 忽略 -background,Canvas 手绘渐变又有 banding/锯齿,
        故改用「预渲染 PNG 底图 + Canvas 矢量文字」:底图逐像素算平滑渐变+抗锯齿圆角,
        文字矢量叠加清晰。三档:primary 蓝→紫 / danger 红→橙 / default 玻璃底+描边。
        返回 tk.Canvas(支持 .pack),hover 提亮、按下下沉。

        width: 钉死的按钮宽度(含内边距)。同组的启动/停止传同一个值才等长
        (见 `_fit_btn_width`);不传则按本文案实测宽度自适应。
        """
        font = ("PingFang SC", 11)
        padx, pady, radius = 16, 7, 9
        probe = tk.Label(parent, text=text, font=font)
        probe.update_idletasks()
        tw = probe.winfo_reqwidth()
        th = probe.winfo_reqheight()
        probe.destroy()
        w = max(int(width), tw + padx * 2) if width else tw + padx * 2
        h = th + pady * 2

        if primary:
            ga, gb = self._c("grad_a"), self._c("grad_b")
            ga_h, gb_h = self._c("grad_a_hover"), self._c("grad_b_hover")
            fg = self._c("btn_fg")
            outline = None
            glow = True
        elif danger:
            ga, gb = self._c("danger_a"), self._c("danger_b")
            ga_h, gb_h = self._c("danger_a_hover"), self._c("danger_b_hover")
            fg = self._c("btn_fg")
            outline = None
            glow = True
        else:
            ga = gb = self._c("btn")
            ga_h = gb_h = self._c("btn_hover")
            fg = self._c("fg")
            outline = self._c("line")
            glow = False

        # 底图(带透明背景,方便 hover/pressed 时替换)
        bgcol = parent.cget("bg")

        def get_img(ca, cb):
            key = (ca, cb, w, h, radius, outline, glow)
            img = _btn_cache.get(key)
            if img is None:
                data = _make_btn_png(ca, cb, w, h, radius, outline=outline, glow=glow)
                img = tk.PhotoImage(data=data)
                _btn_cache[key] = img
            return img

        cv = tk.Canvas(parent, width=w, height=h, bg=bgcol,
                       highlightthickness=0, bd=0, cursor="hand2")
        state = {"text": text, "enabled": True}

        def paint(ca, cb, pressed=False):
            cv.delete("all")
            off = 1 if pressed else 0
            img = get_img(ca, cb)
            # 保留对当前 PhotoImage 的引用,防 GC
            cv._img = img
            cv.create_image(0, off, image=img, anchor="nw")
            cv.create_text(w / 2, h / 2 + off, text=state["text"],
                           fill=fg if state["enabled"] else self._c("mut"),
                           font=font, tags=("btntext",))

        def on_click(_e):
            if not state["enabled"]:      # 执行中置灰:点击直接吞掉(防重入)
                return
            paint(ga_h, gb_h, pressed=True)
            cmd()

        paint(ga, gb)
        cv.bind("<Enter>", lambda _e: paint(ga_h, gb_h))
        cv.bind("<Leave>", lambda _e: paint(ga, gb))
        cv.bind("<Button-1>", on_click)
        cv.bind("<ButtonRelease-1>", lambda _e: paint(ga_h, gb_h))

        def set_text(new_text: str):
            """改按钮文字(发布按钮的「构建中 12s / 上传 62%」实时状态)。"""
            state["text"] = str(new_text)
            try:
                cv.itemconfigure("btntext", text=state["text"])
            except tk.TclError:
                pass

        def set_enabled(flag: bool):
            """置灰/恢复点击(发布执行中防重入)。"""
            state["enabled"] = bool(flag)
            try:
                cv.configure(cursor="hand2" if flag else "arrow")
                cv.itemconfigure("btntext",
                                 fill=fg if flag else self._c("mut"))
            except tk.TclError:
                pass

        cv._set_text = set_text
        cv._set_enabled = set_enabled
        return cv

    def _ghost_btn(self, parent, text, cmd, danger=False, height=22, font_size=11):
        """轻量小按钮(1px 描边的 Label 自绘)。

        为什么不复用 `_btn`:它是渐变胶囊 + 16px 内边距,一行里放三枚会又大又笨
        (用户原话「按钮太大」)。macOS 的 `tk.Button` 忽略 `-background`,所以
        这里用 Label 自绘:hover 换 `card_hi` 底色 + `fg` 文字,点击执行 cmd。

        Tk 的 Label 不能按像素设高,`height` 只决定竖向留白
        (22 → 上下各 1px,视觉高度≈22;26 → 2px)。
        """
        try:
            bg = parent.cget("bg")
        except tk.TclError:
            bg = self._c("card")
        fg = self._c("red") if danger else self._c("mut")
        hover_fg = self._c("red") if danger else self._c("fg")
        pad_y = 2 if height >= 26 else 1
        lab = tk.Label(parent, text=text, bg=bg, fg=fg, bd=0,
                       font=("PingFang SC", font_size),
                       highlightthickness=1,
                       highlightbackground=self._c("line"),
                       padx=7, pady=pad_y, cursor="hand2")

        def enter(_e=None):
            try:
                lab.configure(bg=self._c("card_hi"), fg=hover_fg)
            except tk.TclError:
                pass

        def leave(_e=None):
            try:
                lab.configure(bg=bg, fg=fg)
            except tk.TclError:
                pass

        def click(_e=None):
            leave()
            try:
                cmd()
            except Exception:  # noqa: BLE001 — 回调里出啥都别炸事件循环
                pass

        lab.bind("<Enter>", enter)
        lab.bind("<Leave>", leave)
        lab.bind("<Button-1>", click)
        lab._ghost_reset = leave
        return lab

    def _build_ui(self):
        # 主体:上下两层 PanedWindow(上=左列表+右详情,下=日志),分隔条可拖动
        self._vpan = tk.PanedWindow(self, orient="vertical", sashwidth=3,
                                    bg=self._c("bg"), bd=0, sashrelief="flat")
        self._vpan.pack(fill="both", expand=True, padx=14, pady=(0, 10))

        # 上层:左右两个窗格,中间分隔条可拖动调宽度
        self._main = tk.PanedWindow(self._vpan, orient="horizontal", sashwidth=3,
                                    bg=self._c("bg"), bd=0, sashrelief="flat")

        # 左栏列表
        self._left = tk.Frame(self._main, bg=self._c("panel"))
        self._main.add(self._left, width=280, minsize=200, stretch="never")
        self._build_left()

        # 右详情:内部 Canvas 实现竖向滚动(日志区拖高遮住内容时可滚动查看)
        self.detail = tk.Frame(self._main, bg=self._c("card"))
        self._main.add(self.detail, minsize=420, stretch="always")
        self._build_detail_scroll()

        self._vpan.add(self._main, stretch="always")

        # 底部日志(tab 化:固定「系统」+ 每个项目一个 tab),高度可拖动
        self._logwrap = tk.Frame(self._vpan, bg=self._c("bg"))
        self._build_log_area()
        self._vpan.add(self._logwrap, height=215, minsize=90, stretch="never")

        self._reload_list()

    def _build_topbar(self):
        self._top = tk.Frame(self, bg=self._c("bg"))
        # 顶栏 pack 在 _build_ui 之前调用,自然排在最顶部
        self._top.pack(fill="x", padx=16, pady=(12, 8))
        tk.Label(self._top, text="🗂", bg=self._c("bg"), fg=self._c("accent"),
                 font=("PingFang SC", 18, "bold")).pack(side="left")
        tk.Label(self._top, text="项目控制台", bg=self._c("bg"), fg=self._c("top_b"),
                 font=("PingFang SC", 17, "bold")).pack(side="left", padx=(2, 0))
        tk.Label(self._top, text="本地环境 + 服务器服务 统一管理", bg=self._c("bg"),
                 fg=self._c("mut"), font=("PingFang SC", 11)).pack(side="left", padx=(10, 0), pady=(3, 0))
        # 右侧按钮:视觉顺序(从左到右)= ↻刷新 → ⚙服务器 → 读取脚本 → 更多⋯
        #
        # 注意 pack 顺序:side="right" 是「先 pack 的贴最右」,所以要得到
        # 上面的视觉顺序,必须**倒着 pack**(更多⋯ 最先 → 刷新最后)。
        # 刷新是最高频操作,放最左;「更多⋯」收纳低频动作,贴最右。
        self._more_btn = self._btn(self._top, "更多 ⋯", self._popup_more_menu)
        self._more_btn.pack(side="right", padx=(6, 0))
        self._btn(self._top, "读取脚本", self.refresh_scripts).pack(side="right", padx=(6, 0))
        self._btn(self._top, "⚙ 服务器",
                  lambda: self._servers_dialog()).pack(side="right", padx=(6, 0))
        self._btn(self._top, "↻ 刷新", self.refresh_selected).pack(side="right", padx=(6, 0))

    def _popup_more_menu(self):
        """「更多」下拉菜单:收纳体检/停止全部/同步项目目录/切换主题。"""
        menu = tk.Menu(self, tearoff=0, font=("PingFang SC", 12))
        menu.add_command(label="🩺 体检", command=self.run_checkup)
        menu.add_command(label="■ 停止全部", command=self.stop_work_group)
        menu.add_command(label="⤵ 同步项目目录", command=self.sync_code_dir)
        menu.add_command(label="📂 更改项目目录…", command=self.change_work_root)
        if self.theme is THEMES["dark"]:
            menu.add_command(label="☀️ 切换浅色", command=self.toggle_theme)
        else:
            menu.add_command(label="🌙 切换深色", command=self.toggle_theme)
        # 在「更多」按钮正下方弹出
        btn = self._more_btn
        try:
            menu.tk_popup(btn.winfo_rootx(), btn.winfo_rooty() + btn.winfo_height())
        finally:
            menu.grab_release()

    def _build_left(self):
        # 开源版没有「工作/个人」分组概念,左栏直接列出全部受管条目。
        tk.Label(self._left, text="受管条目", bg=self._c("panel"), fg=self._c("mut"),
                 font=("PingFang SC", 10)).pack(anchor="w", padx=12, pady=(12, 4))
        self.listbox = _DotList(self._left, bg=self._c("panel"),
                                on_select=self._on_item_select,
                                on_context=self._on_item_context,
                                on_toggle=lambda _eid: self._reload_list(),
                                theme_getter=self._c)
        self.listbox.pack(fill="both", expand=True, padx=6, pady=(0, 10))

    def _build_detail_scroll(self):
        dcanvas = tk.Canvas(self.detail, bg=self._c("card"), bd=0, highlightthickness=0)
        # 自绘悬浮滚动条(替代原生 tk.Scrollbar):macOS 的原生滚动条是系统控件,
        # 不认 background/troughcolor,深色主题下永远是一条白,非常扎眼(用户反馈)。
        # 这里用一块 10px 宽、card 底色的 Canvas 画滑块,颜色随主题走。
        dthumb = tk.Canvas(self.detail, width=10, bg=self._c("card"),
                           bd=0, highlightthickness=0, cursor="hand2")
        self.dinner = tk.Frame(dcanvas, bg=self._c("card"))
        self.dinner.bind("<Configure>", lambda _ev: dcanvas.configure(
            scrollregion=dcanvas.bbox("all")))
        dcanvas.create_window((0, 0), window=self.dinner, anchor="nw", tags="inner")
        dcanvas.configure(yscrollcommand=self._dthumb_yview)
        dthumb.pack(side="right", fill="y")
        dcanvas.pack(side="left", fill="both", expand=True)
        self._dcanvas = dcanvas
        self._dthumb = dthumb
        self._dthumb_drag = None

        def thumb_redraw(_ev=None):
            """窗口/主题变化后按当前 yview 重画一次滑块。"""
            try:
                f, l = dcanvas.yview()
                self._dthumb_yview(f, l)
            except tk.TclError:
                pass
        dthumb.bind("<Configure>", thumb_redraw)

        def thumb_press(ev):
            if not self._dthumb_visible():
                return
            self._dthumb_drag = max(0.0, ev.y - self._dthumb_top)
            return "break"

        def thumb_drag(ev):
            if self._dthumb_drag is None:
                return
            h = max(1.0, dthumb.winfo_height())
            frac = (ev.y - self._dthumb_drag) / h
            dcanvas.yview_moveto(max(0.0, min(1.0, frac)))
            return "break"

        dthumb.bind("<Button-1>", thumb_press)
        dthumb.bind("<B1-Motion>", thumb_drag)
        dthumb.bind("<ButtonRelease-1>", lambda _ev: setattr(self, "_dthumb_drag", None))

        # 内帧宽度跟随画布(否则 wraplength 换行错位)
        dcanvas.bind("<Configure>",
                     lambda ev: dcanvas.itemconfigure("inner", width=ev.width))

        # 鼠标滚轮:指针进入详情区才接管 bind_all,避免干扰左栏列表
        def _on_wheel(ev):
            dcanvas.yview_scroll(-1 if ev.delta > 0 else 1, "units")
        self.dinner.bind("<Enter>",
                         lambda _ev: dcanvas.bind_all("<MouseWheel>", _on_wheel))
        self.dinner.bind("<Leave>",
                         lambda _ev: dcanvas.unbind_all("<MouseWheel>"))

    def _dthumb_visible(self) -> bool:
        """滑块当前是否可拖(内容超出一屏时才有)。"""
        return bool(getattr(self, "_dthumb_geo", None))

    def _dthumb_yview(self, first, last) -> None:
        """详情区 Canvas 的 yview 回调:在右侧窄条上画/擦自绘滑块。

        `first/last` 是 Tk 传来的可见比例(0.0-1.0);内容不足一屏时清空窄条,
        只留 card 底色的暗条,不再出现白色原生滚动条。
        """
        try:
            f, l = float(first), float(last)
        except (TypeError, ValueError):
            return
        thumb = getattr(self, "_dthumb", None)
        if thumb is None:
            return
        try:
            thumb.delete("all")
        except tk.TclError:
            return
        self._dthumb_geo = None
        h = thumb.winfo_height()
        if l - f >= 0.999 or h <= 8:
            return
        top = max(3, f * h)
        bot = min(h - 3, l * h)
        if bot - top < 28:                    # 保底可抓握高度
            bot = min(h - 3, top + 28)
        thumb.create_rectangle(3, top, 7, bot, fill=self._c("scroll_thumb"),
                               outline="", width=0, tags="thumb")
        self._dthumb_geo = (f, l)
        self._dthumb_top = top

    def _cancel_env_debounce(self):
        """取消「输入即存」的待触发定时器(重建详情区/关窗时必调)。"""
        tid = getattr(self, "_env_debounce", None)
        self._env_debounce = None
        if tid:
            try:
                self.after_cancel(tid)
            except tk.TclError:
                pass

    def _on_close(self):
        """关窗口:先落盘未失焦的表单,再收掉所有 pty shell,最后销毁窗口。"""
        flush, self._env_flush = getattr(self, "_env_flush", None), None
        if callable(flush):
            try:
                flush()          # Cmd+W / 红叉直接关窗也不能丢正在编辑的环境
            except Exception:  # noqa: BLE001
                pass
        self._cancel_env_debounce()
        try:
            self._term_stop_all()
        except Exception:  # noqa: BLE001
            pass
        self.destroy()

    def _visible_entries(self) -> list[dict]:
        """开源版无分组:全部条目都可见(不再按 工作/个人 过滤)。"""
        return list(self.entries)

    def _children(self, eid: str) -> list[dict]:
        """parent=eid 的子条目(保持注册表顺序)。"""
        return [e for e in self.entries if e.get("parent") == eid]

    def disp(self, e: dict) -> str:
        """显示名:有备注名(alias)则 '备注名 (原名)',否则原名。"""
        alias = (e.get("alias") or "").strip()
        return f"{alias} ({e['name']})" if alias else e["name"]

    def _reload_list(self):
        self._row_ids = []
        rows = []
        color = {"🟢": self._c("dot_on"), "🔴": self._c("dot_off")}

        def add(e: dict, indent: bool):
            expandable = e.get("kind") == "repo" and bool(self._children(e["id"]))
            rows.append((e["id"], self.disp(e),
                         color.get(self._overall_dot(e), self._c("dot_off")),
                         indent, expandable))
            self._row_ids.append(e["id"])

        for e in self._visible_entries():
            if e.get("parent"):          # 子条目跟着父节点走,这里跳过
                continue
            add(e, indent=False)
            if e.get("kind") == "repo" and e["id"] in self.listbox._collapsed:
                continue                 # 一级节点已折叠,二级收进去
            for c in self._children(e["id"]):
                add(c, indent=True)
        # 没有父节点的孤儿(数据写错 parent 时)兜底显示。
        # 注意:父节点只是被折叠(还在 _row_ids 里)的子项不算孤儿,
        # 否则折叠后二级项目会被当成孤儿追加到列表末尾(踩过的坑)。
        visible_ids = {e["id"] for e in self._visible_entries()}
        for e in self._visible_entries():
            pid = e.get("parent")
            if pid and e["id"] not in self._row_ids and pid not in visible_ids:
                add(e, indent=False)
        self.listbox.set_rows(rows, sel_id=self.sel_id)

    def _overall_dot(self, e: dict) -> str:
        """左栏行首状态灯:🟢=服务通/正在启动,🔴=未启动(二态,用户要求)。

        带 local.health 的条目:端口在听但健康检查不通 → 红灯(见下)。
        """
        # repo 一级节点:聚合所有子项目的状态(有子项目在跑就亮绿)
        if e.get("kind") == "repo":
            return "🟢" if any(self._overall_dot(c) == "🟢"
                               for c in self._children(e["id"])) else "🔴"
        s = self.status.get(e["id"], {})
        vals = list((s.get("ports") or {}).values())
        if e.get("server"):
            if s.get("svc") == "active":
                vals.append(True)
            elif s.get("svc") in ("inactive", "ssh_error"):
                vals.append(False)
        # 配了 local.health 的条目:端口在听但健康检查不通 → 红灯。
        # 典型场景:后端进程在、端口在 LISTEN,但数据库连不上,接口全是错。
        # 只在端口确实通的时候才判红,避免启动阶段(还没查过)误报。
        if ((e.get("local") or {}).get("health") and s.get("lhealth") is False
                and any(v for v in (s.get("ports") or {}).values())):
            return "🔴"
        if e["id"] in self.starting:   # 已点启动但端口还没就绪,也算绿灯
            return "🟢"
        return "🟢" if any(vals) else "🔴"

    def _log_starting_hint(self, e: dict):
        """点启动后补一行说明:启动期间左栏绿灯只代表「已发起」,不代表已就绪。

        用户实际被误导过(后端连不上数据库起不来,左栏却一直绿),这行提示把灯的
        语义说清楚;端口始终不通时 P0-1 会在超时后自动转红并告警。
        """
        limit = int(e.get("start_timeout", START_TIMEOUT_DEFAULT))
        self.log(f"⏳ [{e['name']}] 等待端口就绪(最多 {limit}s);期间左栏绿灯 = "
                 f"已发起启动,不代表服务已可用 —— 超时未就绪会自动转红", e["id"])

    # ------------------------------------------------ 选择与详情渲染

    def select(self, entry_id: str | None):
        # 重建详情区之前把「当前环境表单 + 账号三列」的未失焦编辑落盘 ——
        # 切 tab / 切项目 / 切分组 / 服务器面板回跳都会走到这里(见坑 51)。
        # 顺序:先 flush(此刻控件还在、值还读得到),再取消防抖(免得 500ms
        # 后又拿着旧控件跑一遍)。
        flush, self._env_flush = getattr(self, "_env_flush", None), None
        if callable(flush):
            try:
                flush()
            except Exception:  # noqa: BLE001 — 保存失败不能挡住渲染
                pass
        self._cancel_env_debounce()
        self.sel_id = entry_id
        self._reload_list()
        for w in self.dinner.winfo_children():
            w.destroy()
        e = self._entry(entry_id)
        if not e:
            tk.Label(self.dinner, text="未选择条目", bg=self._c("card"),
                     fg=self._c("mut")).pack(pady=40)
            return
        self._render_detail(e)
        if e.get("log_file"):
            self._attach_tail(e["log_file"], e["id"])   # 选中即回显该项目的运行日志
        # 日志 tab:已有该项目 tab 就自动切过去,没有就停在「系统」tab
        # (不主动新建 —— 否则每点一次左栏就多一个空 shell)
        self._logtab_show(e["id"] if e["id"] in self._logtabs else SYSTEM_TAB)
        self.refresh_selected()

    def _entry(self, eid) -> dict | None:
        return next((x for x in self.entries if x["id"] == eid), None)

    def _on_item_select(self, eid: str):
        if eid != self.sel_id:
            self.select(eid)

    def _on_item_context(self, eid: str, event):
        """左栏右键菜单:改备注名(打开目录/Trae 已移到右侧本机操作区)。"""
        if eid != self.sel_id:
            self.select(eid)            # 先选中该行,详情同步切换
        e = self._entry(eid)
        if not e:
            return
        menu = tk.Menu(self, tearoff=0, font=("PingFang SC", 12),
                       bg=self._c("card"), fg=self._c("fg"),
                       activebackground=self._c("accent"),
                       activeforeground=self._c("btn_fg"))
        menu.add_command(label=f"✎ 改备注名…(当前:{self.disp(e)})",
                         command=lambda: self.rename_entry(e))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _section(self, parent, title, headpad: int = 18, innerpad: int = 24):
        """分区卡片:圆角描边 + accent 色标题点缀。

        `headpad/innerpad` 可调:两列布局(各占 50%)里必须收窄缩进,
        否则左缩进就把半屏宽度吃掉一半。
        """
        # 标题行:accent 竖条 + 加粗标题文字
        head = tk.Frame(parent, bg=self._c("card"))
        head.pack(fill="x", padx=headpad, pady=(14, 2))
        tk.Label(head, text="▎", bg=self._c("card"), fg=self._c("accent"),
                 font=("PingFang SC", 12, "bold")).pack(side="left")
        tk.Label(head, text=title, bg=self._c("card"), fg=self._c("fg"),
                 font=("PingFang SC", 12, "bold")).pack(side="left", padx=(2, 0))
        # 内容区:带左缩进的玻璃底
        inner = tk.Frame(parent, bg=self._c("card"))
        inner.pack(fill="x", padx=innerpad, pady=(4, 6))
        return inner

    def _bind_wrap(self, container, labels: list, minus: int = 28):
        """让一批 Label 的 wraplength 跟随容器宽度(两列布局防溢出)。

        Tk 的 wraplength 是像素死值:窗口一缩就溢出、一放就提前折行。
        这里在容器 <Configure> 时按实际宽度重算;`minus` 是左右留白之和。
        同一个容器只绑一次(重复渲染只替换目标列表,避免处理器越挂越多)。
        """
        container._wrap_targets = (list(labels), minus)
        if getattr(container, "_wrap_bound", False):
            return
        container._wrap_bound = True

        def on_cfg(ev):
            labs, m = getattr(container, "_wrap_targets", ([], 28))
            w = max(140, ev.width - m)
            for lab in labs:
                try:
                    if lab.winfo_exists():
                        lab.configure(wraplength=w)
                except tk.TclError:
                    pass

        try:
            container.bind("<Configure>", on_cfg, add="+")
        except tk.TclError:
            pass

    def _row(self, parent):
        r = tk.Frame(parent, bg=self._c("card"))
        r.pack(fill="x", pady=3)
        return r

    def _dotlabel(self, parent, key, grid_pos=None):
        """状态灯标签,key 用于后续更新。

        grid_pos=(row, col) 时改用 grid 竖向摆(本机环境的多端口用),
        否则沿用 pack(side="left") 横排(服务器区一行几盏灯)。
        """
        lab = tk.Label(parent, text="⚪ …", bg=self._c("card"), fg=self._c("mut"),
                       font=("PingFang SC", 11), anchor="w")
        if grid_pos is None:
            lab.configure(width=16)   # 横排才需要等宽对齐
            lab.pack(side="left")
        else:
            # 竖排:不锁宽度,让标签随内容收缩,
            # 否则三盏灯各占 16 字符会把左列撑到 500px,挤扁右列
            lab.grid(row=grid_pos[0], column=grid_pos[1], sticky="w")
        self._dot_labels.setdefault(self.sel_id, {})[key] = lab
        return lab

    def _render_actions_row(self, e: dict, scripts: list) -> None:
        """详情区最上方的操作行(无标题):打开页面 / 打开目录 / Trae / 自定义动作。

        「启动 / 停止」平时在「脚本执行」页右侧(跟随脚本选中项);
        只有**没有脚本**的条目才兜底放到这一行,保证单机工具/服务类条目
        仍能一键启停(这是 v2.2 布局调整里唯一的降级分支)。
        """
        loc = e.get("local") or {}
        acts = e.get("actions", [])
        if not (loc or acts or e.get("dir")):
            return
        # 流式容器:自定义动作多 / 窗口窄时按钮自动折行,不被右边缘裁掉
        bar = _ChipFlow(self.dinner, bg=self._c("card"))
        bar.pack(fill="x", padx=18, pady=(10, 0))
        if loc and not scripts:
            # 无脚本条目(服务/工具类)的启停兜底:同样等长,右端对齐
            bw = self._fit_btn_width(["▶ 启动", "■ 停止"])
            bar.add(self._btn(bar, "▶ 启动", lambda: self.act_local(e, "start"),
                              primary=True, width=bw))
            bar.add(self._btn(bar, "■ 停止", lambda: self.act_local(e, "stop"),
                              danger=True, width=bw))
        for u in loc.get("urls", []):
            bar.add(self._btn(bar, f"🌐 {u['label']}",
                              lambda url=u["url"]: self.open_url(url)))
        for a in acts:
            bar.add(self._btn(bar, a["label"],
                              lambda act=a: self.act_custom(e, act)))
        if e.get("dir"):
            bar.add(self._btn(bar, "📁 打开目录",
                              lambda d=e["dir"]: self.open_dir(d)))
            bar.add(self._btn(bar, "🚀 用 Trae 打开",
                              lambda d=e["dir"]: self.open_trae(d)))

    def _render_status_bar(self, e: dict):
        """详情区顶部状态总览条:本地端口 / 服务 / 健康 三颗胶囊。

        v2.9 新增。原先这三盏灯散落在下面的「本机环境」「运行环境」「服务器」
        三个分区里,用户得读完文字才知道项目跑没跑。收成一排胶囊后,
        一眼就能看清状态,下面的分区只负责给细节。

        胶囊内容随检查结果变化 —— 复用 `_dot_labels` 的更新通道:
        状态回填时顺带更新对应胶囊的文字与点色。
        """
        loc = e.get("local") or {}
        srv = e.get("server") or {}
        # 该条目有哪些维度可显示:有端口 / 有服务器服务 / 配了健康检查
        has_port = bool(loc.get("ports"))
        has_svc = bool(srv)
        has_health = bool(loc.get("health")) or bool(srv.get("health"))
        if not (has_port or has_svc or has_health):
            return

        # 流式容器:窄窗口下胶囊自动折行,不会被右边缘裁掉
        bar = _ChipFlow(self.dinner, bg=self._c("card"))
        bar.pack(fill="x", padx=16, pady=(10, 0))
        self._status_bar = bar
        self._status_chips = {}

        def chip(key: str, text: str):
            c = _StatusChip(bar, self._c, self._c("card"), text=text,
                            dot=self._c("dot_none"))
            bar.add(c, gap=8)
            self._status_chips[key] = c
            return c

        if has_port:
            names = [f"{p['label']}:{p['port']}" for p in loc.get("ports", [])]
            chip("port", f"本地 {' '.join(names)}")
        if has_svc:
            chip("svc", f"服务 {srv.get('service', '')}")
        if has_health:
            chip("health", "健康检查")

    def _refresh_status_chips(self, eid: str):
        """按最新状态缓存刷新顶部胶囊的点色与文字。

        数据源是 `self.status[eid]`(与左栏红绿灯同源),所以胶囊和
        下面的分区永远说同一件事,不会一个绿一个红。
        """
        chips = getattr(self, "_status_chips", None) or {}
        if not chips:
            return
        st = self.status.get(eid) or {}
        e = self._entry(eid)
        if not e:
            return

        # —— 本地端口:全通=绿 / 有挂=红 / 未知=灰
        c = chips.get("port")
        if c is not None:
            ports = st.get("ports") or {}
            vals = [v for v in ports.values() if v is not None]
            if vals and all(vals):
                c.set(f"本地 {' '.join(self._port_names(e))} 运行中", self._c("green"))
            elif vals:
                c.set(f"本地 {' '.join(self._port_names(e))} 有端口未启动",
                      self._c("red"))
            else:
                c.set(f"本地 {' '.join(self._port_names(e))} 检查中",
                      self._c("dot_none"))

        # —— 服务器服务:active=绿 / inactive=红 / pending·ssh_error=黄
        c = chips.get("svc")
        if c is not None:
            svc = st.get("svc")
            name = (e.get("server") or {}).get("service", "")
            if svc == "active":
                c.set(f"服务 {name} 运行中", self._c("green"))
            elif svc == "inactive":
                c.set(f"服务 {name} 未运行", self._c("red"))
            elif svc == "pending":
                c.set(f"服务 {name} 执行中", self._c("warn"))
            else:
                c.set(f"服务 {name} 状态未知", self._c("warn"))

        # —— 健康检查:通过=绿 / 不通=红 / 未查=灰
        c = chips.get("health")
        if c is not None:
            h = st.get("health")
            lh = st.get("lhealth")
            if h is not None:
                c.set("健康检查 通过" if h else "健康检查 不通",
                      self._c("green") if h else self._c("red"))
            elif lh is not None:
                c.set("健康检查 通过" if lh else "健康检查 不通",
                      self._c("green") if lh else self._c("red"))
            else:
                c.set("健康检查 检查中", self._c("dot_none"))

        # 胶囊文字变长变短后宽度变了,让流式容器重新折行
        bar = getattr(self, "_status_bar", None)
        if bar is not None and hasattr(bar, "schedule"):
            bar.schedule()

    def _port_names(self, e: dict) -> list[str]:
        """条目本地端口的 "标签:端口" 列表(胶囊标题用)。"""
        loc = e.get("local") or {}
        return [f"{p['label']}:{p['port']}" for p in loc.get("ports", [])]

    def _render_env_pair(self, e: dict):
        """「本机环境 | 运行环境」两列。

        v2.9:原来两列 `uniform="envcol"` 强制 50:50,但两列内容量差近 10 倍
        (本机环境就一行端口灯,运行环境是版本 + 一整段说明),于是左列大片空白、
        右列文字被挤到换行甚至截断。改成 **1:2 加权**(本机 1 份、运行 2 份)。
        """
        wrap = tk.Frame(self.dinner, bg=self._c("card"))
        wrap.pack(fill="x")
        wrap.columnconfigure(0, weight=1, minsize=150)   # 本机环境
        wrap.columnconfigure(1, weight=2, minsize=260)   # 运行环境
        col_l = tk.Frame(wrap, bg=self._c("card"))
        col_l.grid(row=0, column=0, sticky="nsew")
        col_r = tk.Frame(wrap, bg=self._c("card"))
        col_r.grid(row=0, column=1, sticky="nsew")
        self._render_local_env(e, col_l)
        self._render_env(e, col_r)
        has_l = bool(col_l.winfo_children())
        has_r = bool(col_r.winfo_children())
        if not has_l and not has_r:
            wrap.destroy()
            return
        if not has_r:
            col_l.grid_configure(column=0, columnspan=2)
        elif not has_l:
            col_r.grid_configure(column=0, columnspan=2)

    def _render_local_env(self, e: dict, parent=None):
        """本机环境(两列布局的左半边):端口灯 + 健康灯 + 依赖端口灯 + 占用进程。"""
        parent = parent or self.dinner
        loc = e.get("local")
        if not loc:
            return
        sec = self._section(parent, "本机环境", headpad=14, innerpad=18)
        row = self._row(sec)
        # 竖排(grid)而非横排(pack):多端口时横排会把左列撑得很宽,
        # 破坏「本机环境 : 运行环境 = 1 : 2」的列权重
        gr = 0
        for p in loc.get("ports", []):
            self._dotlabel(row, f"port:{p['port']}", grid_pos=(gr, 0)).configure(
                text=f"{DOT['warn']} {p['label']}:{p['port']} 检查中…")
            gr += 1
        if loc.get("health"):   # P2-4 本地健康检查(端口在听 ≠ 应用 ready)
            self._dotlabel(row, "lhealth", grid_pos=(gr, 0)).configure(
                text="◐ 健康检查 检查中…")
            gr += 1
        # 依赖端口(watch_ports):只看状态,不参与启停/PID/超时判定
        for p in e.get("watch_ports", []):
            self._dotlabel(row, f"wport:{p['port']}", grid_pos=(gr, 0)).configure(
                text=f"{DOT['warn']} {p['label']}:{p['port']} 检查中…")
            gr += 1
        # P2-3:每个端口下方的占用进程说明(选中/手动刷新时填充)
        plabels = []
        for p in loc.get("ports", []):
            plab = tk.Label(sec, text="", bg=self._c("card"), fg=self._c("mut"),
                            anchor="w", justify="left", wraplength=300,
                            font=("Menlo", 9))
            plab.pack(anchor="w", padx=4)
            plabels.append(plab)
            self._proc_labels.setdefault(e["id"], {})[f"port:{p['port']}"] = plab
        if plabels:
            self._bind_wrap(parent, plabels, minus=60)

    def _render_detail(self, e: dict):
        self._dot_labels: dict[str, dict] = {e["id"]: {}}
        self._proc_labels: dict[str, dict] = {e["id"]: {}}   # 端口占用者说明(P2-3)
        self._status_chips: dict = {}   # 顶部状态胶囊(键: port/svc/health)
        if e["id"] not in self.scripts:
            self.scripts[e["id"]] = load_scripts(e)   # 懒加载,渲染即读盘

        # 头部:标题独占第一行,元信息(分支/远程地址)降到第二行。
        # 原先三者同排,远程地址又长又抢眼,项目名被挤得看不清。
        head = tk.Frame(self.dinner, bg=self._c("card"))
        head.pack(fill="x", padx=16, pady=(14, 0))
        tk.Label(head, text=self.disp(e), bg=self._c("card"), fg=self._c("fg"),
                 font=("PingFang SC", 15, "bold")).pack(side="left")

        # 当前 Git 分支 + 远程地址(第二行;右键可备注 / 复制)
        meta = tk.Frame(self.dinner, bg=self._c("card"))
        meta.pack(fill="x", padx=16, pady=(4, 0))
        if e.get("dir") and os.path.isdir(os.path.join(e["dir"], ".git")):
            code, out = sh(f"git -C '{e['dir']}' rev-parse --abbrev-ref HEAD 2>&1",
                           timeout=10)
            branch = out.strip() if code == 0 else ""
            if branch:
                br_remark = (e.get("branch_remarks") or {}).get(branch, "")
                # 分支做成小胶囊:一眼能和后面的地址区分开
                blab = tk.Label(
                    meta, cursor="hand2", bg=self._c("card_hi"),
                    fg=self._c("fg"),
                    text=f"🌿 {branch}" + (f" — {br_remark}" if br_remark else ""),
                    font=("PingFang SC", 10), padx=8, pady=2)
                blab.pack(side="left")
                for seq in ("<Button-2>", "<Button-3>", "<Control-Button-1>"):
                    blab.bind(seq, lambda ev, br=branch: self._branch_context_menu(e, br, ev))
                blab.bind("<Button-1>", lambda _ev: self.select(e["id"]))  # 单击刷新分支

            # Git 远程地址:v2.7.3 起只留 `@` 之后那段,紧跟分支显示(不再单独占一行)
            code2, out2 = sh(f"git -C '{e['dir']}' remote get-url origin 2>&1", timeout=10)
            full_url = out2.strip() if code2 == 0 else ""
            short_url = git_remote_short(full_url)
            if full_url:
                ulab = tk.Label(meta, text=f"🔗 {short_url}", bg=self._c("card"),
                                fg=self._c("mut"), cursor="hand2",
                                font=("Menlo", 10))
                ulab.pack(side="left", padx=(10, 0))
                for seq in ("<Button-2>", "<Button-3>", "<Control-Button-1>"):
                    ulab.bind(seq, lambda ev, u=full_url: self._url_context_menu(u, ev))

        # 状态总览条:本地端口 / 服务 / 健康 三个胶囊,一眼看清当前状态。
        # 放在标题下面 —— 原来这三盏灯散落在下面两个分区里,得读完文字才知道跑没跑。
        self._render_status_bar(e)

        # 项目说明已移到「运行环境」里(用户要求:顶部说明行去掉)

        # —— repo 一级节点:列出子项目(点击跳转)
        if e.get("kind") == "repo":
            kids = self._children(e["id"])
            row = self._row(self._section(self.dinner, "子项目"))
            if not kids:
                tk.Label(row, text="—(注册表里还没有 parent 指向它的条目)",
                         bg=self._c("card"), fg=self._c("mut")).pack(side="left")
            for c in kids:
                dot = self._overall_dot(c)
                lab = tk.Label(row, text=f"{self.disp(c)} {dot}", bg=self._c("card"),
                               fg=self._c("accent"), cursor="hand2",
                               font=("PingFang SC", 11, "underline"))
                lab.pack(side="left", padx=(0, 14))
                lab.bind("<Button-1>", lambda _ev, cid=c["id"]: self.select(cid))

        # —— 顶部操作行(无标题):打开页面 / 打开目录 / Trae / 自定义动作
        # 「启动 / 停止」不常驻这一行 —— 它们跟着「脚本执行」页右侧走;
        # 只有**没有脚本**的条目才兜底放回这里(用户明确要求的降级方案)。
        scripts = self.scripts.get(e["id"]) or []
        self._render_actions_row(e, scripts)

        # —— 环境区:本机环境(左 1 份)+ 运行环境(右 2 份)两列
        self._render_env_pair(e)

        # 顶部胶囊先按缓存里的旧状态画一版,等后台检查回填后再刷新
        self._refresh_status_chips(e["id"])

        # —— 脚本执行 / 环境配置:同一块分区,顶部用分段控件切页
        # 两页都渲染(保持表单/发布按钮的引用有效,发布中的按钮还能继续刷新文字),
        # 只 pack 当前页;切页 = pack/pack_forget,不重建 → 不会重跑 SSH/端口检查。
        #
        # v2.8:repo 一级目录节点**整块不渲染**这段(用户要求「工作项目下选中
        # 目录不要显示脚本和环境配置」)。目录节点只是导航:它没有 package.json
        # → 没有脚本;它也不该配发布环境 → 没有环境配置。之前这里会渲染一个
        # 空分段控件 + 空环境面板,看着像"坏了",不如干脆不显示。
        if e.get("kind") == "repo":
            # 清掉上一个条目的脚本状态,避免 run_script 拿到别的条目的脚本名
            self._script_names = []
            self._script_list = None
            self._script_sel = tk.StringVar(value="")
        else:
            self._render_detail_panes(e, scripts)

        # —— 服务器区
        srv = e.get("server")
        if srv:
            row = self._row(self._section(self.dinner, f"服务器 {srv.get('ssh', '')}"))
            self._dotlabel(row, "svc").configure(text="◐ 服务 检查中…")
            for extra in srv.get("extra_services", []):
                self._dotlabel(row, f"extra:{extra}").configure(text=f"◐ {extra} 检查中…")
            if srv.get("health"):
                self._dotlabel(row, "health").configure(text="◐ 健康检查 检查中…")
            btnrow = self._row(self._section(self.dinner, "服务器操作"))
            # 同一行三个操作按钮等长(与本地启停一致,视觉成组)
            sbw = self._fit_btn_width(["▶ 启动服务", "↻ 重启", "■ 停止服务"])
            self._btn(btnrow, "▶ 启动服务",
                      lambda: self.act_server(e, "start"), primary=True,
                      width=sbw).pack(side="left")
            self._btn(btnrow, "↻ 重启",
                      lambda: self.act_server(e, "restart"),
                      width=sbw).pack(side="left", padx=8)
            self._btn(btnrow, "■ 停止服务",
                      lambda: self.act_server(e, "stop"), danger=True,
                      width=sbw).pack(side="left")
            if srv.get("health"):
                self._btn(btnrow, "🩺 查健康",
                          lambda: self.refresh_selected()).pack(side="left", padx=8)
            for u in srv.get("urls", []):
                self._btn(btnrow, f"🌐 {u['label']}",
                          lambda url=u["url"]: self.open_url(url)).pack(side="left", padx=8)

    # ------------------------------------------------ 详情区分段页

    def _pane_bodies(self) -> dict:
        """eid -> {页key: Frame}。切页只需 pack/pack_forget,不重建控件。"""
        d = getattr(self, "_pane_body_map", None)
        if d is None:
            d = self._pane_body_map = {}
        return d

    def _detail_pane_of(self, e: dict, has_scripts: bool) -> str:
        """当前显示哪一页:没脚本就只能看「环境配置」。"""
        if not has_scripts:
            return "env"
        key = self._detail_pane.get(e["id"]) or "scripts"
        return key if key in ("scripts", "env") else "scripts"

    def _render_detail_panes(self, e: dict, scripts: list):
        """「脚本执行 / 环境配置」共用一块分区,顶部分段控件切页。

        两页都渲染、只显示其中一页:
          · 环境配置的表单/发布按钮引用始终有效 —— 发布中的按钮会被
            `_deploy_tick` 持续改文字,若切走后控件没了会 TclError;
          · `_env_flush` 钩子始终存在,切页前未失焦的输入照样落盘;
          · 切页只是 pack/pack_forget,不重渲染 → 不重跑 SSH/端口检查。
        """
        names = [n for n, _cmd in scripts]
        # 选中项与脚本列表在两页间必须都可用(启动按钮与 run_script 依赖)
        pref = ["start"] if e.get("maven") else ["dev", "serve", "start"]
        default = next((n for n in names if n in pref), names[0] if names else "")
        self._script_names = names
        self._script_sel = tk.StringVar(value=default)
        self._script_list = None

        pane = self._detail_pane_of(e, bool(scripts))

        # 分段控件行(与其它分区标题同为 18px 左缩进)
        bar = tk.Frame(self.dinner, bg=self._c("card"))
        bar.pack(fill="x", padx=18, pady=(16, 4))
        tabs = [("scripts", "脚本执行"), ("env", "环境配置")]
        seg = self._segmented(bar, tabs, pane,
                              lambda k: self._pick_detail_pane(e, k))
        seg.pack(side="left")
        self._detail_seg = seg   # 冒烟测试/调试用引用
        if not scripts:
            tk.Label(bar, text="该项目没有可执行脚本", bg=self._c("card"),
                     fg=self._c("mut"), font=("PingFang SC", 10)).pack(
                         side="left", padx=(10, 0))
        # v2.9:「N 个环境 / + 新建环境」原本在**环境面板内部**的头部,
        # 与这里的分段控件上下分家,视线要来回扫。现在把它挪到同一行的右侧,
        # 与分段控件并排 —— 分段控件在左、环境计数与新建在右,一行看完。
        # 位置由 _render_env_config 决定(它才知道环境数),这里先留个空位。
        self._env_bar_slot = tk.Frame(bar, bg=self._c("card"))
        self._env_bar_slot.pack(side="right")
        self._env_bar_slot_eid = e["id"]

        holder = tk.Frame(self.dinner, bg=self._c("card"))
        holder.pack(fill="x")
        body_s = tk.Frame(holder, bg=self._c("card"))
        body_e = tk.Frame(holder, bg=self._c("card"))

        self._render_scripts_pane(e, body_s, scripts, default)
        self._render_env_config(e, host=body_e, show_title=False)

        self._pane_bodies()[e["id"]] = {"scripts": body_s, "env": body_e}
        for k, f in (("scripts", body_s), ("env", body_e)):
            if k == pane:
                f.pack(fill="x")
            else:
                f.pack_forget()

    def _pick_detail_pane(self, e: dict, key: str):
        """切页:只换显示,不重建、不重新检查状态。"""
        self._detail_pane[e["id"]] = key
        for k, f in (self._pane_bodies().get(e["id"]) or {}).items():
            try:
                if k == key:
                    f.pack(fill="x")
                else:
                    f.pack_forget()
            except tk.TclError:      # 控件已被销毁(条目切走),忽略
                pass

    def _segmented(self, parent, items, current: str, on_pick):
        """分段控件(胶囊 tab):items=[(key, 文字)]。

        一整块 Canvas 画出:圆角轨道(玻璃底 + 1px 描边) + 当前段渐变胶囊
        (与主按钮同一套 `_make_btn_png`,呼应品牌渐变);未选中段只有文字,
        hover 提亮。点击按 x 落点分段命中。
        """
        font_b = ("PingFang SC", 11, "bold")
        padx, h_track, h_pill = 22, 34, 28
        widths: list[int] = []
        probe = tk.Label(self, text="x", font=font_b)
        try:
            for _k, label in items:
                probe.configure(text=label)
                probe.update_idletasks()
                widths.append(probe.winfo_reqwidth() + padx * 2)
        finally:
            probe.destroy()
        total = sum(widths) + 6
        try:
            bgcol = parent.cget("bg")
        except tk.TclError:
            bgcol = self._c("card")

        cv = tk.Canvas(parent, width=total, height=h_track, bg=bgcol,
                       highlightthickness=0, bd=0, cursor="hand2")
        _cache: dict = {}

        def img(key, maker):
            im = _cache.get(key)
            if im is None:
                im = tk.PhotoImage(data=maker())
                _cache[key] = im
            return im

        def draw(active: str, hover: str | None = None):
            cv.delete("all")
            # 轨道
            cv._track = img(("track",), lambda: _make_btn_png(
                self._c("btn"), self._c("btn"), total, h_track,
                h_track // 2, outline=self._c("line"), glow=False))
            cv.create_image(0, 0, image=cv._track, anchor="nw")
            x = 3
            for (key, label), w in zip(items, widths):
                if key == active:
                    pill = img(("pill", w), lambda w=w: _make_btn_png(
                        self._c("grad_a"), self._c("grad_b"),
                        w - 6, h_pill, h_pill // 2, glow=True))
                    cv.create_image(x + 3, (h_track - h_pill) // 2,
                                    image=pill, anchor="nw")
                    cv._pill = pill
                    hov = None
                else:
                    hov = img(("hover", w), lambda w=w: _make_btn_png(
                        self._c("btn_hover"), self._c("btn_hover"),
                        w - 6, h_pill, h_pill // 2,
                        outline=self._c("line"), glow=False))
                    if key == hover:
                        cv.create_image(x + 3, (h_track - h_pill) // 2,
                                        image=hov, anchor="nw")
                cv.create_text(x + w / 2, h_track / 2, text=label,
                               fill=(self._c("btn_fg") if key == active
                                     else (self._c("fg") if key == hover
                                           else self._c("mut"))),
                               font=font_b)
                x += w

        def seg_at(px: float) -> str | None:
            x = 3
            for (key, _label), w in zip(items, widths):
                if x <= px < x + w:
                    return key
                x += w
            return None

        state = {"active": current, "hover": None}

        def repaint():
            draw(state["active"], state["hover"])

        def on_move(ev):
            k = seg_at(cv.canvasx(ev.x))
            if k != state["hover"]:
                state["hover"] = k
                repaint()

        def on_click(ev):
            k = seg_at(cv.canvasx(ev.x))
            if k and k != state["active"]:
                state["active"] = k
                repaint()
                on_pick(k)

        repaint()
        cv.bind("<Motion>", on_move)
        cv.bind("<Leave>", lambda _e: (state.update(hover=None), repaint()))
        cv.bind("<Button-1>", on_click)
        return cv

    def _render_scripts_pane(self, e: dict, host, scripts: list, default: str):
        """「脚本执行」页:脚本列表(双击运行/右键备注)+ 右侧启动、停止。

        「▶ 启动」跟随选中项,与「■ 停止」共用宽度(切换脚本时不忽长忽短)。
        """
        C_card = self._c("card")
        if not scripts:
            tk.Label(host, text="这个项目没有可执行脚本(目录里没有 package.json scripts)。",
                     bg=C_card, fg=self._c("mut"), wraplength=560, justify="left",
                     font=("PingFang SC", 11)).pack(anchor="w", padx=24, pady=(10, 12))
            return

        remarks = e.get("script_remarks") or {}
        names = [n for n, _cmd in scripts]
        start_label: dict = {}

        wrap = tk.Frame(host, bg=C_card)
        wrap.pack(fill="x", padx=24, pady=(6, 10))
        wrap.columnconfigure(0, weight=1)      # 列表吃满剩余宽度
        left = tk.Frame(wrap, bg=C_card)
        left.grid(row=0, column=0, sticky="nsew")
        right = tk.Frame(wrap, bg=C_card)
        right.grid(row=0, column=1, sticky="ne", padx=(10, 0))

        tip = tk.Label(left, text="", bg=C_card, fg=self._c("mut"),
                       wraplength=420, justify="left", font=("Menlo", 10))

        def show_cmd(name: str | None = None):
            """刷新命令预览与「▶ 启动(...)」按钮文字。"""
            nm = (name or self._script_sel.get() or "").strip()
            if nm:
                self._script_sel.set(nm)
            cmd = next((c for n, c in scripts if n == nm), "")
            tip.configure(text=f"$ {cmd}")
            btn = start_label.get("btn")
            if btn is not None and hasattr(btn, "_set_text"):
                btn._set_text(f"▶ 启动({nm})")

        sl = _ScriptList(left, theme_getter=self._c, bg=self._c("log_bg"),
                         height=min(max(len(names), 2), 8) * _ScriptList.ROW_H + 8)
        sl.pack(fill="x")
        sl.on_select = show_cmd
        sl.on_run = lambda _nm: self.run_script(e)
        sl.on_context = lambda nm, ev: self._script_context_menu(e, nm, ev)
        sl.set_rows([{"name": n, "remark": (remarks.get(n) or "").strip()}
                     for n in names], sel=default)
        self._script_list = sl

        bw = self._fit_btn_width([*(f"▶ 启动({n})" for n in names), "■ 停止"])
        b = self._btn(right, f"▶ 启动({default})",
                      lambda: self.run_script(e), primary=True, width=bw)
        b.pack(anchor="e")
        start_label["btn"] = b
        self._btn(right, "■ 停止", lambda: self.act_local(e, "stop"),
                  danger=True, width=bw).pack(anchor="e", pady=(6, 0))

        show_cmd(default)
        tip.pack(anchor="w", pady=(4, 0))
        self._bind_wrap(left, [tip])

    def _center(self, win: tk.Toplevel, w: int, h: int):
        """把固定尺寸的 Toplevel 居中到主窗口。"""
        try:
            x = self.winfo_rootx() + max(0, (self.winfo_width() - w) // 2)
            y = self.winfo_rooty() + max(0, (self.winfo_height() - h) // 3)
        except tk.TclError:
            x = y = 140
        win.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")

    # ------------------------------------------------ 状态刷新(后台线程)

    def bg(self, fn):
        def wrap():
            try:
                fn()
            except Exception as ex:  # noqa: BLE001
                self.q.put(("log", f"❌ 后台任务异常: {ex}"))
        threading.Thread(target=wrap, daemon=True).start()

    def refresh_selected(self):
        e = self._entry(self.sel_id)
        if not e:
            return
        self.bg(lambda: self._check_local(e, detail=True))   # 选中/手动:带占用进程信息
        if e.get("server"):
            self.bg(lambda: self._check_server(e))
        self._reload_list()

    def refresh_all_local(self):
        self.log("手动刷新:全部条目本地端口")
        for e in self.entries:
            self.bg(lambda ent=e: self._check_local(ent, detail=True))
        self._reload_list()

    # ------------------------------------------------ 工作区同步(扫描 code)

    def sync_code_dir(self):
        """后台扫描工作根目录,结果经 queue 回主线程弹确认框。"""
        root = cfg_work_root()
        self.log(f"🔍 正在扫描 {root} …")

        def job():
            new, missing = scan_code_dir()
            self.q.put(("sync", new, missing))

        self.bg(job)

    def change_work_root(self):
        """更换项目根目录:写进配置顶层 work_root,可选立即重新扫描导入。

        扫描/同步(scan_code_dir、sync_code_dir)都从配置里现读 work_root,
        所以这里改完落盘,后续扫描自动用新目录;旧条目不受影响,
        不在新目录下的条目会由同步对话框提示勾选清理。
        """
        chosen = filedialog.askdirectory(initialdir=cfg_work_root(),
                                         title="选择新的项目根目录")
        if not chosen:
            return
        chosen = os.path.abspath(chosen)
        self.cfg["work_root"] = chosen
        save_config(self.cfg)
        self.log(f"📂 项目根目录已更改为 {chosen}")
        if messagebox.askyesno(
                "更改项目目录",
                f"项目根目录已改为:\n{chosen}\n\n是否立即扫描并导入该目录下的项目?"):
            self.sync_code_dir()

    def _show_sync_dialog(self, new: list[dict], missing: list[dict]):
        """同步结果确认框:新项目预览 + 缺失条目逐个勾选是否删除。"""
        if not new and not missing:
            messagebox.showinfo("同步项目目录", "没有变化:所有项目都已在注册表里,磁盘也齐全。")
            self.log("✅ 同步完成:无变化")
            return
        dlg = tk.Toplevel(self)
        dlg.title("同步项目目录")
        dlg.configure(bg=self._c("card"))
        dlg.transient(self)
        dlg.grab_set()
        tk.Label(dlg, text=f"同步 {cfg_work_root()} 结果", bg=self._c("card"), fg=self._c("fg"),
                 font=("PingFang SC", 14, "bold")).pack(anchor="w", padx=16, pady=(14, 4))

        del_vars: list[tuple[dict, tk.BooleanVar]] = []
        if new:
            tk.Label(dlg, text=f"新发现 {len(new)} 个条目(确认后自动加入,已存在的不受影响):",
                     bg=self._c("card"), fg=self._c("fg"),
                     font=("PingFang SC", 11)).pack(anchor="w", padx=16)
            lines = "\n".join(f"  · {e['name']}  ({e.get('desc', '')[:44]})" for e in new)
            tk.Label(dlg, text=lines, bg=self._c("card"), fg=self._c("mut"), justify="left",
                     font=("PingFang SC", 10)).pack(anchor="w", padx=26, pady=(2, 8))
        if missing:
            tk.Label(dlg, text=f"注册表有、磁盘上已没有 {len(missing)} 个(勾选才删除,不勾保留):",
                     bg=self._c("card"), fg=self._c("warn"),
                     font=("PingFang SC", 11)).pack(anchor="w", padx=16)
            for e in missing:
                row = tk.Frame(dlg, bg=self._c("card"))
                row.pack(anchor="w", padx=26, pady=1)
                var = tk.BooleanVar(value=False)   # 默认保留,勾选才删
                tk.Checkbutton(row, variable=var, text=f"删除「{self.disp(e)}」",
                               bg=self._c("card"), fg=self._c("fg"),
                               activebackground=self._c("card"),
                               activeforeground=self._c("fg"),
                               selectcolor=self._c("card"),
                               font=("PingFang SC", 10)).pack(side="left")
                tk.Label(row, text=f"  {e['dir']}", bg=self._c("card"), fg=self._c("mut"),
                         font=("Menlo", 9)).pack(side="left")
                del_vars.append((e, var))

        def apply():
            removed, added = [], []
            cfg = load_config()
            for e, var in del_vars:
                if var.get():
                    dead = {e["id"]} | {c["id"] for c in cfg["entries"] if c.get("parent") == e["id"]}
                    cfg["entries"] = [x for x in cfg["entries"] if x["id"] not in dead]
                    removed.append(e["name"])
            cfg["entries"].extend(new)
            save_config(cfg)
            self.entries = cfg["entries"]
            # 清掉已删条目的状态缓存
            live_ids = {x["id"] for x in self.entries}
            for eid in [i for i in self.status if i not in live_ids]:
                self.status.pop(eid, None)
            for _id in [i for i in self.starting if i not in live_ids]:
                self.starting.pop(_id, None)
            if removed:
                self.log("🗑 已从控制台移除: " + ", ".join(removed))
            if new:
                self.log("➕ 新纳入受管: " + ", ".join(e["name"] for e in new))
                for e in new:
                    self.bg(lambda x=e: self._check_local(x, detail=True))
            self._reload_list()
            dlg.destroy()
            self.log(f"✅ 同步完成:+{len(new)} / -{len(removed)}(详见上方日志)")

        btnrow = tk.Frame(dlg, bg=self._c("card"))
        btnrow.pack(fill="x", padx=16, pady=(10, 14))
        self._btn(btnrow, "✔ 应用", apply, primary=True).pack(side="right")
        self._btn(btnrow, "取消", dlg.destroy).pack(side="right", padx=8)

    # ------------------------------------------------ 首启引导

    def run_onboarding(self):
        """首次启动引导:选项目根目录 → 后台扫描 → 预览确认 → 生成配置。

        「所有候选位置都没有 projects.json」时由 __init__ 触发;完成后写入
        配置顶层 _onboarded=true,之后不再弹。用户也可跳过,直接载入
        projects.example.json 示例配置。
        """
        dlg = tk.Toplevel(self)
        dlg.title("欢迎使用 Project Console")
        dlg.configure(bg=self._c("card"))
        dlg.transient(self)
        dlg.grab_set()
        dlg.resizable(False, False)
        dlg.update_idletasks()
        w, h = 580, 250
        x = self.winfo_x() + (self.winfo_width() - w) // 2
        y = self.winfo_y() + (self.winfo_height() - h) // 2
        dlg.geometry(f"{w}x{h}+{max(0, x)}+{max(0, y)}")
        tk.Label(dlg, text="👋 欢迎使用 Project Console", bg=self._c("card"), fg=self._c("fg"),
                 font=("PingFang SC", 16, "bold")).pack(anchor="w", padx=20, pady=(22, 6))
        tk.Label(dlg, text=("本工具通过 projects.json 管理你的本地项目与远程服务器。\n"
                            "首次使用请选择你的项目根目录 —— 其下每个子目录会被\n"
                            "扫描为一个项目(之后可用菜单「⤵ 同步项目目录」重新扫描)。"),
                 bg=self._c("card"), fg=self._c("mut"), justify="left",
                 font=("PingFang SC", 11)).pack(anchor="w", padx=20, pady=(0, 4))
        btnrow = tk.Frame(dlg, bg=self._c("card"))
        btnrow.pack(fill="x", padx=20, pady=(18, 20))
        self._btn(btnrow, "📁 选择项目目录…", lambda: self._onboard_pick(dlg),
                  primary=True).pack(side="right")
        self._btn(btnrow, "跳过(载入示例配置)", lambda: self._onboard_skip(dlg)).pack(
            side="right", padx=8)

    def _onboard_pick(self, dlg: tk.Toplevel):
        """引导第一步:选目录 → 后台扫描。"""
        chosen = filedialog.askdirectory(initialdir=cfg_work_root(),
                                         title="选择项目根目录", parent=dlg)
        if not chosen:
            return
        chosen = os.path.abspath(chosen)
        self.log(f"🔍 正在扫描 {chosen} …")
        dlg.destroy()

        def job():
            new, _missing = scan_code_dir(chosen)
            self.q.put(("onboard_scan", new, chosen))

        self.bg(job)

    def _show_onboard_preview(self, new: list[dict], root: str):
        """引导第二步:预览扫描结果,确认后写入新配置。"""
        dlg = tk.Toplevel(self)
        dlg.title("确认扫描结果")
        dlg.configure(bg=self._c("card"))
        dlg.transient(self)
        dlg.grab_set()
        dlg.resizable(False, False)
        tk.Label(dlg, text=f"在 {root} 下扫描到 {len(new)} 个条目:",
                 bg=self._c("card"), fg=self._c("fg"),
                 font=("PingFang SC", 13, "bold")).pack(anchor="w", padx=18, pady=(16, 6))
        if new:
            lines = "\n".join(f"  · {e['name']}  ({e.get('desc', '')[:44]})" for e in new[:24])
            more = f"\n  … 以及另外 {len(new) - 24} 个" if len(new) > 24 else ""
            tk.Label(dlg, text=lines + more, bg=self._c("card"), fg=self._c("mut"),
                     justify="left", font=("PingFang SC", 10)).pack(anchor="w", padx=26)
        else:
            tk.Label(dlg, text="没有发现子目录。可以先跳过用示例配置,\n或重新选择其他目录。",
                     bg=self._c("card"), fg=self._c("warn"), justify="left",
                     font=("PingFang SC", 10)).pack(anchor="w", padx=26)

        def finish():
            dlg.destroy()
            self._onboard_finish(new, root)

        btnrow = tk.Frame(dlg, bg=self._c("card"))
        btnrow.pack(fill="x", padx=18, pady=(14, 16))
        if new:
            self._btn(btnrow, "✔ 生成配置", finish, primary=True).pack(side="right")
        self._btn(btnrow, "重新选目录", lambda: (dlg.destroy(), self.run_onboarding())).pack(
            side="right", padx=8)
        self._btn(btnrow, "取消", dlg.destroy).pack(side="right")

    def _onboard_finish(self, new: list[dict], root: str):
        """引导完成(选了目录):生成带 work_root 的初始配置并落盘。"""
        cfg = {
            "_说明": "由首启引导生成,可手工编辑;密码/网站账号放同目录 secrets.json(勿提交 git)。",
            "version": 4,
            "work_root": root,
            "targets": [],       # 之后用「⚙ 服务器」按钮添加
            "entries": new,
            "_onboarded": True,
        }
        save_config(cfg)
        self._apply_new_config(
            cfg, f"✅ 引导完成:纳入 {len(new)} 个条目,工作目录 {root}")

    def _onboard_skip(self, dlg: tk.Toplevel):
        """引导跳过:载入 projects.example.json 作为初始配置。"""
        dlg.destroy()
        exs = [os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "projects.example.json"),
               os.path.join(base_dir(), "projects.example.json")]
        cfg = None
        for ex in exs:
            if os.path.exists(ex):
                try:
                    with open(ex, encoding="utf-8") as f:
                        cfg = json.load(f)
                    break
                except Exception:  # noqa: BLE001
                    pass
        if cfg is None:
            cfg = {"_说明": "示例配置加载失败,已生成空注册表;可在菜单「同步项目目录」扫描。",
                   "version": 4, "targets": [], "entries": []}
        cfg.pop("_说明", None)
        cfg["_说明"] = "由首启引导生成(示例),可手工编辑;密码/账号放同目录 secrets.json。"
        cfg.setdefault("work_root", cfg_work_root())
        cfg["_onboarded"] = True
        save_config(cfg)
        self._apply_new_config(cfg, "✅ 已载入示例配置;双击条目编辑,或直接修改 projects.json")

    def _apply_new_config(self, cfg: dict, note: str):
        """引导完成后应用新配置:刷新内存状态、重建左栏与状态缓存。"""
        self.cfg = cfg
        self.entries = cfg["entries"]
        self.status = {}
        for e in self.entries:
            self.status[e["id"]] = {
                "ports": {p["port"]: None for p in (e.get("local") or {}).get("ports", [])},
                "svc": None, "svc_detail": "", "health": None, "extra_svc": {}, "pids": [],
            }
        self._reload_list()
        first = (self._visible_entries() or self.entries or [{}])[0]
        self.select(first.get("id"))
        self.log(f"🗂 注册表: {config_path()}")
        self.log(f"🔐 密钥文件: {secrets_path()}(密码与网站账号,不进 git)")
        self.log(note)

    def _auto_refresh(self):
        """每 15s 轮询:全部条目本地端口(静默);带服务器的条目顺带查线上
        状态(红绿灯要看服务器,必须周期刷新)。

        选中条目**同样要查**(detail=True 带占用进程信息):早前的实现把它
        `continue` 掉了却没另查,导致启动后详情区的端口灯永远停在启动后 4s
        那一次复查的结果,必须手动点刷新才会更新(用户实测踩到)。
        """
        e = self._entry(self.sel_id)
        for ent in self.entries:
            if ent is e:
                # 正看着的条目:带占用进程信息,详情区才会跟着刷新
                self.bg(lambda x=ent: self._check_local(x, quiet=True, detail=True))
                if ent.get("server"):
                    self.bg(lambda x=ent: self._check_server(x, quiet=True))
                continue
            self.bg(lambda x=ent: self._check_local(x, quiet=True))
            if ent.get("server"):
                self.bg(lambda x=ent: self._check_server(x, quiet=True))
        self.after(AUTO_REFRESH_MS, self._auto_refresh)

    def _check_local(self, e: dict, quiet: bool = False, detail: bool = False):
        """检查本地端口状态。

        detail=True 时附带占用进程信息(P2-3,不进 15s 轮询以免高频 ps)。
        - 端口就绪 → 移出 starting;
        - 超时(默认 90s,条目可用 start_timeout 覆盖)仍未就绪 → 移出 starting
          并告警假绿灯(P0-1);
        - PID 随消息回主线程写入缓存(P2-1),供按 PID 精确停止使用;
        - 配了 local.health 的条目额外做健康检查(P2-4)。
        """
        loc = e.get("local") or {}
        ports = loc.get("ports", [])
        results = []
        all_pids: list[str] = []
        for p in ports:
            pids = port_pids(p["port"])
            ok = bool(pids)
            procline = proc_info(pids) if (detail and pids) else ""
            results.append((p, ok))
            for pid in pids:                       # 所有端口的并集(去重保序)
                if pid not in all_pids:
                    all_pids.append(pid)
            self.q.put(("port", e["id"], p["port"], ok, pids, procline))
        # P2-1:PID 缓存必须是**并集**,且在本轮所有 port 消息之后写入。
        # 早期实现在 port 分支里逐个覆盖 st["pids"],结果只剩最后一个端口的进程
        # —— 多端口条目(如 note 的 3100+5173)点停止只杀掉前端,后端留在后台。
        self.q.put(("pids", e["id"], all_pids))
        if any(ok for _, ok in results):
            self.starting.pop(e["id"], None)   # 端口就绪,「启动中」状态结束
        else:
            ts = self.starting.get(e["id"])
            limit = int(e.get("start_timeout", START_TIMEOUT_DEFAULT))
            if ts is not None and time.time() - ts > limit:
                self.starting.pop(e["id"], None)
                hint = f",日志见 {e['log_file']}" if e.get("log_file") else ""
                self.q.put(("log", f"⚠️ [{e['name']}] 启动疑似失败:{limit}s 内端口未就绪,"
                                   f"已红灯等待{hint}", e["id"]))
                self.q.put(("list",))
        # P2-4 本地健康检查:端口在听 ≠ 应用 ready(Spring Boot 初始化失败等)
        if loc.get("health"):
            hok, hout = health_ok(loc["health"])
            self.q.put(("lhealth", e["id"], hok, hout))
        # 依赖端口(watch_ports):只刷新显示,不写 status、不进 PID/超时判定,
        # 否则「停止本项目」会按 PID 杀掉共用的隧道(3307 被 note/wedding 共用)。
        for p in e.get("watch_ports", []):
            self.q.put(("wport", e["id"], p["port"], port_listening(p["port"])))
        if quiet:
            self.q.put(("list",))
        else:
            summary = " ".join(f"{p['label']}:{p['port']}={'🟢' if ok else '🔴'}"
                               for p, ok in results)
            self.q.put(("log", f"🔍 [{e['name']}] 本地端口检查完成 {summary}",
                        e["id"]))

    def _check_server(self, e: dict, quiet: bool = False):
        srv = e["server"]
        state, detail = svc_active(srv["ssh"], srv["service"])
        self.q.put(("svc", e["id"], srv["service"], state, detail))
        for extra in srv.get("extra_services", []):
            st, dt = svc_active(srv["ssh"], extra)
            self.q.put(("svc", e["id"], extra, st, dt))
        ok = None
        if srv.get("health"):
            ok, out = health_ok(srv["health"])
            self.q.put(("health", e["id"], ok, out))
        self.q.put(("list",))
        if not quiet:
            self.q.put(("log", f"🔍 [{e['name']}] 服务器 {srv['service']}={state}"
                               + (f" 健康={'🟢' if ok else '🔴'}" if srv.get("health") else ""),
                        e["id"]))

    # ------------------------------------------------ 操作

    def act_local(self, e: dict, op: str):
        loc = e.get("local") or {}
        cmd = loc.get(op)
        if not cmd:
            self.log(f"⚠️ [{e['name']}] 未配置 local.{op}", e["id"])
            return
        verb = "启动" if op == "start" else "停止"
        if op == "stop":
            # P2-2:有 PID 记录时优先按 PID 精确停止,避免按端口误杀无关进程
            pids = list((self.status.get(e["id"]) or {}).get("pids") or [])
            if pids:
                self.log(f"▶ [{e['name']}] 本地停止(按 PID): {'/'.join(pids)}",
                         e["id"])
                self.starting.pop(e["id"], None)
                self.bg(lambda: self._stop_by_pids(e, pids, cmd, loc))
                return
        self.log(f"▶ [{e['name']}] 本地{verb}: {cmd}", e["id"])
        if op == "start":
            self.starting[e["id"]] = time.time()   # P0-1:记录启动时刻,超时转红
        else:
            self.starting.pop(e["id"], None)
        cwd = e.get("dir") or None

        def job():
            # start 类命令可能触发首次编译(mvn-dev/npm install),给足超时
            code, out = sh(cmd, cwd=cwd, timeout=600 if op == "start" else 120,
                           node_version=e.get("node_version"))
            self.q.put(("log", f"{'✅' if code == 0 else '❌'} [{e['name']}] {verb}完成(退出码 {code})\n{out}",
                        e["id"]))
            if op == "start" and e.get("log_file"):
                self.q.put(("tail", e["log_file"], e["id"]))
            self.bg(lambda: self._check_local(e, detail=True))
            self.q.put(("list",))

        self.bg(job)

    def _kill_pids(self, e: dict, pids: list[str], loc: dict) -> bool:
        """按 PID 停止:SIGTERM → 等 3s → 仍在则 SIGKILL(P2-2)。

        返回 True 表示端口已全部释放(无需再走 local.stop 兜底)。
        """
        for pid in pids:
            sh(f"kill {pid}", timeout=15)
        time.sleep(3)
        alive = [pid for pid in pids
                 if sh(f"kill -0 {pid} 2>/dev/null", timeout=5)[0] == 0]
        for pid in alive:
            sh(f"kill -9 {pid}", timeout=15)
        time.sleep(1)
        return not [p["port"] for p in loc.get("ports", []) if port_pids(p["port"])]

    def _stop_by_pids(self, e: dict, pids: list[str], fallback_cmd: str, loc: dict):
        """P2-2 按 PID 精确停止,端口仍通则回退执行注册表 local.stop 兜底。"""
        if self._kill_pids(e, pids, loc):
            self.q.put(("log", f"✅ [{e['name']}] 已按 PID 停止({'/'.join(pids)})",
                        e["id"]))
        else:
            still = [p["port"] for p in loc.get("ports", []) if port_pids(p["port"])]
            self.q.put(("log", f"⚠️ [{e['name']}] PID 停止后端口 {still} 仍在监听,"
                               f"回退执行 local.stop", e["id"]))
            code, out = sh(fallback_cmd, cwd=e.get("dir"), timeout=120,
                           node_version=e.get("node_version"))
            self.q.put(("log", f"{'✅' if code == 0 else '❌'} [{e['name']}] stop 兜底完成"
                               f"(退出码 {code})\n{out}", e["id"]))
            time.sleep(1)
        self.bg(lambda: self._check_local(e, detail=True))
        self.q.put(("list",))

    # ------------------------------------------------ P1-1 一键全停

    def stop_work_group(self):
        """一键停止所有本地服务(逐个精确停止,失败回退 local.stop 命令)。

        后台先探哪些在跑 → 主线程弹确认清单 → 确认后顺序停止。
        """
        self.log("🔍 检查哪些本地服务在运行 …")

        def job():
            running, skipped = [], []
            for e in self.entries:
                loc = e.get("local") or {}
                if not loc.get("stop"):
                    continue
                pids = []
                for p in loc.get("ports", []):
                    pids += port_pids(p["port"])
                if pids:
                    self.q.put(("pids", e["id"], pids))   # 刷新 PID 缓存,便于精确停止
                    running.append(e)
                else:
                    skipped.append(e)
            self.q.put(("stopwork", running, skipped))

        self.bg(job)

    def _confirm_stop_work(self, running: list[dict], skipped: list[dict]):
        """确认清单弹窗;确认后在后台顺序停止。"""
        if not running:
            self.log("没有在运行的本地服务,无需停止")
            return
        names = "\n".join(f"  · {self.disp(e)}" for e in running)
        tail = f"\n\n未在运行、跳过 {len(skipped)} 个。" if skipped else ""
        if not messagebox.askyesno(
                "停止全部本地服务",
                f"将停止以下 {len(running)} 个本地服务:\n{names}{tail}"):
            self.log("已取消:停止全部本地服务")
            return

        def job():
            done = []
            for e in running:
                loc = e["local"]
                # 有 PID(上面刚刷新过)→ 精确停止,失败再回退 local.stop
                pids = list((self.status.get(e["id"]) or {}).get("pids") or [])
                if pids and self._kill_pids(e, pids, loc):
                    self.starting.pop(e["id"], None)
                    done.append(self.disp(e))
                    self.q.put(("list",))
                    continue
                code, out = sh(loc["stop"], cwd=e.get("dir"), timeout=120,
                               node_version=e.get("node_version"))
                self.starting.pop(e["id"], None)
                done.append(self.disp(e))
                if code != 0:
                    self.q.put(("log", f"❌ [{e['name']}] 停止命令退出码 {code}\n{out}",
                                e["id"]))
                self.q.put(("list",))
            self.q.put(("log", f"✅ 批量停止完成:共停止 {len(done)} 个 — "
                               + ", ".join(done)))
            for e in running:
                self.bg(lambda x=e: self._check_local(x, detail=True))
            self.q.put(("list",))

        self.bg(job)

    # ------------------------------------------------ P2-5 注册表体检

    def run_checkup(self):
        """后台跑纯逻辑体检,结果经队列弹窗(只报告不修改)。"""
        self.log("🩺 开始注册表体检 …")

        def job():
            try:
                cfg = load_config()
            except Exception as ex:  # noqa: BLE001
                self.q.put(("checkup", [f"注册表无法解析:{ex}"]))
                return
            self.q.put(("checkup", check_registry(cfg)))

        self.bg(job)

    def _show_checkup(self, issues: list[str]):
        if not issues:
            self.log("🩺 体检完成:未发现问题")
            messagebox.showinfo("注册表体检", f"全部通过 ✅\n\n{config_path()}")
            return
        body = "\n".join(f"  {i + 1}. {s}" for i, s in enumerate(issues))
        self.log(f"🩺 体检发现 {len(issues)} 个问题:\n{body}")
        messagebox.showwarning(
            "注册表体检",
            f"发现 {len(issues)} 个问题:\n\n{body}\n\n注册表:{config_path()}")

    # ------------------------------------------------ P3-1 运行环境

    def _render_env(self, e: dict, parent=None):
        """运行环境区(两列布局的右半边):项目说明 + 项目要求的版本。

        不做本机比对、不提供切换——启动链路 _path_env() 已按条目把要求版本排到
        PATH 最前,「本机默认 ≠ 要求」既不影响运行,切全局 default 又是纯副作用。
        唯一真会起不来的是「要求的版本没装」,那由注册表体检(check_registry)报出。

        v2.7.3:项目说明(`desc`)从顶部搬到这一列(用户要求)—— 原来它占着头部
        一整行,和分支/地址挤在一起;放进「运行环境」既不丢信息,也让头部只剩
        项目名 + 分支 + 地址一行。所以本方法即使**没有任何版本要求**,只要有条目
        说明也要渲染(否则只有 desc 的条目会整块消失)。
        """
        env = env_report(e)
        desc = str(e.get("desc") or "").strip()
        if not env and not desc:
            return
        host = parent or self.dinner
        inner = self._section(host, "运行环境", headpad=14, innerpad=18)
        if "node" in env:
            row = self._row(inner)
            tk.Label(row, text=f"Node  {env['node']}", bg=self._c("card"),
                     fg=self._c("fg"), font=("Menlo", 10)).pack(side="left")
            src = "注册表 node_version" if e.get("node_version") else "package.json engines"
            tk.Label(row, text=f"  ({src})", bg=self._c("card"),
                     fg=self._c("mut"), font=("PingFang SC", 9)).pack(side="left")
        if "java" in env:
            row = self._row(inner)
            tk.Label(row, text=f"JDK   {env['java']}", bg=self._c("card"),
                     fg=self._c("fg"), font=("Menlo", 10)).pack(side="left")
            src = "注册表 java_version" if e.get("java_version") else "pom.xml"
            tk.Label(row, text=f"  ({src})", bg=self._c("card"),
                     fg=self._c("mut"), font=("PingFang SC", 9)).pack(side="left")
        if desc:
            # 说明跟在版本行下面;半屏宽,所以按本列宽度折行(见 _bind_wrap)
            dlab = tk.Label(inner, text=desc, bg=self._c("card"),
                            fg=self._c("mut"), anchor="w", justify="left",
                            wraplength=300, font=("PingFang SC", 10))
            dlab.pack(anchor="w", padx=4, pady=(8 if env else 0, 0))
            self._bind_wrap(inner, [dlab], minus=40)

    def act_server(self, e: dict, op: str):
        srv = e["server"]
        verb = {"start": "启动", "restart": "重启", "stop": "停止"}[op]
        if op in ("stop", "restart") and not messagebox.askyesno(
                "确认操作", f"确定要{verb}服务器上的 {srv['service']} 吗?\n{srv['ssh']}"):
            self.log(f"已取消: {verb} {srv['service']}", e["id"])
            return
        cmd = f"ssh -o BatchMode=yes -o {SSH_TIMEOUT} {srv['ssh']} 'systemctl {op} {srv['service']}'"
        self.log(f"▶ [{e['name']}] 服务器{verb}: systemctl {op} {srv['service']}", e["id"])
        self.q.put(("svc", e["id"], srv["service"], "pending", ""))

        def job():
            code, out = sh(cmd, timeout=30)
            self.q.put(("log", f"{'✅' if code == 0 else '❌'} [{e['name']}] systemctl {op} 完成(退出码 {code})\n{out}",
                        e["id"]))
            time.sleep(1.5)
            self.bg(lambda: self._check_server(e))

        self.bg(job)

    def act_custom(self, e: dict, act: dict):
        if act.get("confirm") and not messagebox.askyesno("确认操作", f"执行「{act['label']}」?\n{act['cmd']}"):
            return
        self.log(f"▶ [{e['name']}] {act['label']}: {act['cmd']}", e["id"])

        def job():
            code, out = sh(act["cmd"], cwd=e.get("dir"), node_version=e.get("node_version"))
            self.q.put(("log", f"{'✅' if code == 0 else '❌'} [{e['name']}] {act['label']}(退出码 {code})\n{out}",
                        e["id"]))

        self.bg(job)

    def open_url(self, url: str):
        self.log(f"🌐 打开 {url}")
        sh(f"open '{url}'", timeout=10)

    def open_dir(self, d: str):
        self.log(f"📁 打开目录 {d}")
        sh(f"open '{d}'", timeout=10)

    def open_trae(self, d: str):
        """用 Trae 打开目录。

        本机实际安装名是「Trae CN」(bundle id cn.trae.app),不能写死
        'Trae'——动态探测 /Applications 下的 Trae*.app,取实际名字。
        """
        app = None
        for p in sorted(glob.glob("/Applications/Trae*.app")):
            app = os.path.basename(p)[:-4]   # 去掉 .app 后缀
            break
        if app:
            self.log(f"🚀 用 {app} 打开 {d}")
            code, out = sh(f"open -a '{app}' '{d}' 2>&1", timeout=15)
            if code != 0:
                self.log(f"⚠️ {app} 打开失败: {out[:150]}")
        else:
            self.log(f"🚀 未找到 Trae*.app,尝试 CLI 方式打开 {d}")
            sh(f"trae '{d}' 2>&1 || echo '❌ Trae 不可用'", timeout=15)

    # ------------------------------------------------ 备注名 / 脚本选择 / 日志回显

    def rename_entry(self, e: dict):
        cur = e.get("alias") or ""
        new = simpledialog.askstring(
            "改备注名", f"给「{e['name']}」设置备注名(留空则恢复显示原名):",
            initialvalue=cur, parent=self)
        if new is None:
            return  # 取消
        new = new.strip()
        if new:
            e["alias"] = new
            msg = f"✎ [{e['name']}] 备注名 → {new}"
        else:
            e.pop("alias", None)
            msg = f"✎ [{e['name']}] 已清除备注名,恢复原名"
        # 备注类改动**双写**:项目 tab 看「这个项目发生过什么」,
        # 系统 tab 留一行便于回溯「什么时候改的」(用户会靠它找回旧名字)。
        self.log(msg, e["id"])
        self.log(msg)
        try:
            save_config(self.cfg)
            self.log("💾 已写入注册表持久化", e["id"])
        except Exception as ex:  # noqa: BLE001
            self.log(f"❌ [{self.disp(e)}] 备注名保存失败: {ex}", e["id"])
        self.select(e["id"])   # 刷新列表与详情

    def _script_context_menu(self, e: dict, name: str, event):
        """脚本列表右键菜单:改备注(比如标注命令对应什么环境)。

        发布入口已经搬到详情区的「环境配置」,这里只剩改备注 ——
        别再往菜单里加发布项(`_deploy_dialog` 已随 v2.4 删除)。
        """
        if not name:
            return
        menu = tk.Menu(self, tearoff=0, font=("PingFang SC", 12),
                       bg=self._c("card"), fg=self._c("fg"),
                       activebackground=self._c("accent"),
                       activeforeground=self._c("btn_fg"))
        remarks = e.get("script_remarks") or {}
        cur = remarks.get(name, "")
        menu.add_command(
            label=f"✎ 改备注…(当前:{cur or '无'})",
            command=lambda: self.edit_script_remark(e, name))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def edit_script_remark(self, e: dict, name: str):
        remarks = e.setdefault("script_remarks", {})
        new = simpledialog.askstring(
            "脚本备注",
            f"给命令「{name}」写个备注(比如对应什么环境,留空则清除):",
            initialvalue=remarks.get(name, ""), parent=self)
        if new is None:
            return  # 取消
        new = new.strip()
        if new:
            remarks[name] = new
            self.log(f"✎ [{self.disp(e)}] 脚本 {name} 备注 → {new}", e["id"])
        else:
            remarks.pop(name, None)
            self.log(f"✎ [{self.disp(e)}] 已清除脚本 {name} 的备注", e["id"])
        try:
            save_config(self.cfg)
            self.log("💾 已写入注册表持久化", e["id"])
        except Exception as ex:  # noqa: BLE001
            self.log(f"❌ [{self.disp(e)}] 备注保存失败: {ex}", e["id"])
        self.select(e["id"])   # 重新渲染脚本列表

    def copy_text(self, text: str, what: str = "", eid: str | None = None) -> None:
        """把一段文本放进系统剪贴板(分支名 / git 地址 / 路径的通用入口)。

        为什么先 `clipboard_clear()` 再 `update_idletasks()`:macOS 上 Tk 的
        剪贴板是「延迟写入」的,不跑一次事件循环,其它 App 粘贴时可能拿到空内容。
        """
        txt = str(text or "")
        if not txt:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(txt)
            self.update_idletasks()
        except tk.TclError:
            return
        self.log(f"📋 已复制{what}:{txt}", eid)

    def _url_context_menu(self, url: str, event):
        """详情头部 git 地址标签的右键菜单:复制完整地址 / 复制精简地址。"""
        menu = tk.Menu(self, tearoff=0, font=("PingFang SC", 12))
        menu.add_command(label=f"📋 复制 git 地址({url})",
                         command=lambda: self.copy_text(url, " git 地址", self.sel_id))
        if git_remote_short(url) and git_remote_short(url) != url:
            menu.add_command(
                label=f"🔗 只复制短地址({git_remote_short(url)})",
                command=lambda: self.copy_text(git_remote_short(url), " 短地址",
                                               self.sel_id))
        menu.add_separator()
        menu.add_command(label="🌐 在浏览器打开",
                         command=lambda: self.open_url(self._url_browse(url)))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _url_browse(self, url: str) -> str:
        """`git@host:me/repo.git` 这类 scp 写法转成 https 才能丢给浏览器。"""
        u = str(url or "").strip()
        if u.startswith("git@") and ":" in u:
            host_part, path = u[4:].split(":", 1)
            return f"https://{host_part}/{path}"
        if u.startswith("ssh://"):
            return "https://" + u[6:].split("@")[-1].replace(":22/", "/")
        return u

    def _branch_context_menu(self, e: dict, branch: str, event):
        """详情头部分支标签的右键菜单:复制分支名 / 给当前分支加备注。"""
        menu = tk.Menu(self, tearoff=0, font=("PingFang SC", 12))
        remarks = e.get("branch_remarks") or {}
        menu.add_command(label=f"📋 复制分支名({branch})",
                         command=lambda: self.copy_text(branch, " 分支名", e["id"]))
        menu.add_separator()
        menu.add_command(
            label=f"✎ 改分支备注…(当前:{remarks.get(branch) or '无'})",
            command=lambda: self.edit_branch_remark(e, branch))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def edit_branch_remark(self, e: dict, branch: str):
        remarks = e.setdefault("branch_remarks", {})
        new = simpledialog.askstring(
            "分支备注",
            f"给分支「{branch}」写个备注(比如标注用途,留空则清除):",
            initialvalue=remarks.get(branch, ""), parent=self)
        if new is None:
            return  # 取消
        new = new.strip()
        if new:
            remarks[branch] = new
            self.log(f"✎ [{self.disp(e)}] 分支 {branch} 备注 → {new}", e["id"])
        else:
            remarks.pop(branch, None)
            self.log(f"✎ [{self.disp(e)}] 已清除分支 {branch} 的备注", e["id"])
        try:
            save_config(self.cfg)
            self.log("💾 已写入注册表持久化", e["id"])
        except Exception as ex:  # noqa: BLE001
            self.log(f"❌ [{self.disp(e)}] 备注保存失败: {ex}", e["id"])
        self.select(e["id"])   # 重新渲染头部分支标签

    def refresh_scripts(self, e: dict | None = None):
        """重读选中条目的 package.json(切 Git 分支后 scripts 可能变化)。"""
        e = e or self._entry(self.sel_id)
        if not e:
            return
        self.scripts[e["id"]] = load_scripts(e)
        n = len(self.scripts[e["id"]])
        self.log(f"⟳ [{self.disp(e)}] 重读 package.json: {n} 个脚本"
                 + ("" if n else "(该目录没有 package.json)"), e["id"])
        self.select(e["id"])   # 重新渲染脚本选择器

    def run_script(self, e: dict):
        scripts = self.scripts.get(e["id"]) or []
        name = (self._script_sel.get() if getattr(self, "_script_sel", None) else "").strip()
        if not name or name not in [s[0] for s in scripts]:
            self.log(f"⚠️ [{self.disp(e)}] 请先选择要运行的脚本", e["id"])
            return
        if not e.get("dir"):
            self.log(f"⚠️ [{self.disp(e)}] 该条目没有本地目录", e["id"])
            return
        cmd_full = next((c for n, c in scripts if n == name), "")
        disp = self.disp(e)

        if cmd_full.startswith("npm run"):
            # 长驻 dev server:nohup 后台跑 + 日志文件增量回显
            logpath = f"/tmp/pc-{safe_name(e['id'])}-{safe_name(name)}.log"
            open(logpath, "w").close()   # 清空旧日志
            cmd = f"cd '{e['dir']}' && nohup {cmd_full} > '{logpath}' 2>&1 &"
            # ★ 用户的原话:「运行项目 a 的启动脚本,自动新加一个日志 tab 显示这个项目的」
            # 所以启动行与结果行必须写进**这个项目**的 tab,不是系统 tab。
            self.log(f"▶ [{disp}] {cmd_full}(日志: {logpath})", e["id"])
            self.starting[e["id"]] = time.time()   # 启动中,灯先亮绿(P0-1 带时间戳)
            self._log_starting_hint(e)

            def job():
                code, out = sh(cmd, timeout=20, node_version=e.get("node_version"))
                if code != 0:
                    self.q.put(("log", f"❌ [{disp}] 启动脚本失败: {out}", e["id"]))
                # 运行输出进「这个项目」的 tab(attach 只能在主线程做)
                self.q.put(("tail", logpath, e["id"]))
                time.sleep(4)
                self.bg(lambda: self._check_local(e, detail=True))
                self.q.put(("list",))

            self.bg(job)
        else:
            # maven/mvn-dev 系列等一次性命令:同步执行,输出直接进日志区
            self.log(f"▶ [{disp}] {cmd_full}", e["id"])
            if name in ("start", "restart"):
                self.starting[e["id"]] = time.time()   # P0-1 带时间戳
                self._log_starting_hint(e)

            def job():
                code, out = sh(cmd_full, cwd=e.get("dir"), timeout=600,
                               node_version=e.get("node_version"))
                self.q.put(("log", f"{'✅' if code == 0 else '❌'} [{disp}] {name}"
                                   f"(退出码 {code})\n{out}", e["id"]))
                if e.get("log_file"):
                    self.q.put(("tail", e["log_file"], e["id"]))   # 服务类命令回显运行日志
                time.sleep(4)
                self.bg(lambda: self._check_local(e, detail=True))
                self.q.put(("list",))

            self.bg(job)

    # ------------------------------------------------ 队列 → UI 更新

    def _poll_queue(self):
        try:
            while True:
                msg = self.q.get_nowait()
                try:
                    self._handle_msg(msg)
                except Exception as ex:  # noqa: BLE001
                    # 单条消息处理失败不能中断整个轮询循环:
                    # after() 重排在末尾,一旦异常逃逸,之后所有 UI 更新都会永久停摆。
                    self.log(f"❌ 处理状态消息时出错(已跳过): {ex}")
        except queue.Empty:
            pass
        self.after(120, self._poll_queue)

    def _handle_msg(self, msg):
        """处理一条队列消息(从 _poll_queue 拆出,便于隔离异常)。"""
        kind = msg[0]
        if kind == "log":
            # 第 3 位可选:给了 eid 就写该项目 tab,否则进「系统」tab
            self.log(msg[1], msg[2] if len(msg) > 2 else None)
        elif kind == "term":       # 内置 shell 的输出(后台读线程 → 主线程写入)
            self._tab_write(msg[1], msg[2])
        elif kind == "term_exit":  # shell 退出:标记置空,下次点 tab 会重开
            _, tid = msg
            info = self._logtabs.get(tid)
            if tid in self._term_muted:      # 我们主动收掉的,不提示
                self._term_muted.discard(tid)
                if info:
                    info["term"] = None
            elif info:
                info["term"] = None
                self._tab_write(tid, "⏹ shell 已退出(重新点这个 tab 会自动重开)\n")
        elif kind == "tail":       # 开始跟踪日志文件(必须回主线程做)
            _, path, eid = msg
            self._attach_tail(path, eid)
        elif kind == "list":
            self._reload_list_keep_sel()
        elif kind == "sync":
            self._show_sync_dialog(msg[1], msg[2])
        elif kind == "onboard_scan":   # 首启引导:扫描完成,回主线程弹预览
            self._show_onboard_preview(msg[1], msg[2])
        elif kind == "pids":     # 刷新某条目的 PID 缓存(批量停止前探测用)
            _, eid, pids = msg
            if eid in self.status:
                self.status[eid]["pids"] = list(pids or [])
        elif kind == "stopwork":  # P1-1 工作组批量停止:确认清单
            self._confirm_stop_work(msg[1], msg[2])
        elif kind == "checkup":   # P2-5 注册表体检结果
            self._show_checkup(msg[1])
        elif kind == "port":
            _, eid, port, ok, pids, procline = msg
            e = self._entry(eid)
            # 写状态缓存(左栏红绿灯的数据源)。
            # v2.0 修复:此前这里只更新标签不写缓存,导致工作组条目
            # 端口起来后 self.status["ports"] 仍是初始值 None,
            # _overall_dot 读到全 None → 启动成功后灯反而变红。
            if eid in self.status:
                st = self.status[eid]
                old = st["ports"].get(port)
                st["ports"][port] = ok
                # 注意:这里**不要**写 st["pids"] —— 单端口的 PID 会覆盖掉并集
                # (多端口条目会漏杀)。并集由 _check_local 末尾的 ("pids", …) 统一写入。
                # P0-2 掉线感知:仅「绿→红」跳变告警,持续红不重复打扰
                if old is True and not ok and e:
                    plabel = next(
                        (p["label"] for p in (e.get("local") or {}).get("ports", [])
                         if p["port"] == port), port)
                    self._alert_down(
                        f"⚠️ [{e['name']}] 的{plabel}({port}端口)中断了,"
                        f"服务疑似挂掉", eid)
            lab = self._dot_labels.get(eid, {}).get(f"port:{port}")
            if lab:
                plabel = next(
                    (p["label"] for p in (e.get("local") or {}).get("ports", [])
                     if e and p["port"] == port), port)
                lab.configure(
                    text=f"{DOT['on'] if ok else DOT['off']} {plabel}:{port} "
                         f"{'运行中' if ok else '未启动'}",
                    fg=self._c("green") if ok else self._c("red"))
            self._refresh_status_chips(eid)
            # P2-3 占用进程说明行
            plab = self._proc_labels.get(eid, {}).get(f"port:{port}")
            if plab:
                plab.configure(text=(f"  ↳ {procline}" if procline else ""))
        elif kind == "wport":     # 依赖端口(watch_ports):只更新显示
            _, eid, port, ok = msg
            lab = self._dot_labels.get(eid, {}).get(f"wport:{port}")
            if lab:
                e = self._entry(eid)
                plabel = next(
                    (p["label"] for p in (e.get("watch_ports") or [])
                     if p["port"] == port), port)
                lab.configure(
                    text=f"{DOT['on'] if ok else DOT['off']} {plabel}:{port} "
                         f"{'已连接' if ok else '未开'}",
                    fg=self._c("green") if ok else self._c("red"))
        elif kind == "lhealth":   # P2-4 本地健康检查
            _, eid, ok, out = msg
            if eid in self.status:
                old = self.status[eid].get("lhealth")
                self.status[eid]["lhealth"] = ok
                if old is True and not ok:
                    e = self._entry(eid)
                    if e:
                        self._alert_down(f"⚠️ [{e['name']}] 本地健康检查变为不通"
                                         f"(HTTP {out})", eid)
            lab = self._dot_labels.get(eid, {}).get("lhealth")
            if lab:
                lab.configure(text=f"{DOT['on'] if ok else DOT['off']} 健康检查 "
                                   f"{'通过' if ok else '不通'}",
                              fg=self._c("green") if ok else self._c("red"))
            self._refresh_status_chips(eid)
        elif kind == "svc":
            _, eid, svc, state, detail = msg
            e = self._entry(eid)
            if e:   # 写入状态缓存(状态灯依赖)
                if e["server"]["service"] == svc:
                    old = self.status[eid].get("svc")
                    self.status[eid]["svc"] = state
                    # P0-2:线上服务 active → inactive/ssh_error 跳变告警
                    if old == "active" and state in ("inactive", "ssh_error"):
                        self._alert_down(f"⚠️ 服务器服务 {svc}({e['name']})已停止"
                                         f"(systemctl 状态:{state})", eid)
                else:
                    self.status[eid]["extra_svc"][svc] = state
            lab = self._dot_labels.get(eid, {}).get(
                "svc" if e and e["server"]["service"] == svc
                else f"extra:{svc}")
            if lab:
                if state == "active":
                    lab.configure(text=f"{DOT['on']} {svc} 运行中", fg=self._c("green"))
                elif state == "inactive":
                    lab.configure(text=f"{DOT['off']} {svc} 未运行", fg=self._c("red"))
                elif state == "pending":
                    lab.configure(text=f"◐ {svc} 执行中…", fg=self._c("warn"))
                else:
                    lab.configure(text=f"{DOT['warn']} {svc} 状态未知(SSH?)", fg=self._c("warn"))
                    if detail:
                        self.log(f"⚠️ {svc}: {detail[:200]}", eid)
            self._refresh_status_chips(eid)
        elif kind == "health":
            _, eid, ok, out = msg
            if eid in self.status:   # 写入状态缓存(状态灯依赖)
                old = self.status[eid].get("health")
                self.status[eid]["health"] = ok
                if old is True and not ok:   # P0-2 线上健康检查掉线
                    e = self._entry(eid)
                    if e:
                        self._alert_down(f"⚠️ 服务器健康检查不通:[{e['name']}] "
                                         f"{e['server'].get('health')}({out})", eid)
            lab = self._dot_labels.get(eid, {}).get("health")
            if lab:
                lab.configure(text=f"{DOT['on'] if ok else DOT['off']} 健康检查 "
                                   f"{'通过' if ok else '不通'}",
                              fg=self._c("green") if ok else self._c("red"))
            self._refresh_status_chips(eid)
        # ------------------------------------------------ v2.1 发布(每类独立隔离)
        elif kind == "deploy_confirm":
            # 后台发布线程 → 主线程弹确认框,结果回填并唤醒线程。
            # v3.0:box 里可带 title / allow_dont_ask(Git 检查的确认不给
            # 「不再询问」—— 脏工作区每次都该提醒;只有目录特征检查才允许
            # 勾一次永久放行,见 pc_core.deploy_git_check)。
            _, question, evt, box = msg
            allow_ack = box.get("allow_dont_ask", True)
            try:
                ok, dont_ask = self._ask_deploy_confirm(
                    question,
                    title=box.get("title") or "⚠️ 目标目录内容不像前端",
                    allow_dont_ask=allow_ack)
                box["ok"] = ok
                box["dont_ask"] = bool(dont_ask) and allow_ack
                ctx = box.get("ctx") or {}
                item = ctx.get("item")
                if ok and dont_ask and allow_ack and isinstance(item, dict):
                    item["ack"] = True     # 记住 ack:该目标以后不再询问
                    try:
                        save_config(self.cfg)
                        self.log(f"💾 已记住「{ctx.get('name') or ctx.get('tid')}」"
                                 f"不再询问目录特征")
                    except Exception as ex:  # noqa: BLE001
                        self.log(f"❌ 保存 ack 失败: {ex}")
            except Exception as ex:  # noqa: BLE001
                self.log(f"❌ 发布确认框出错(按取消处理): {ex}")
            finally:
                evt.set()
        elif kind == "deploy_progress":
            # 上传进度(scp 非 TTY 无进度条,按文件大小估算)
            _, eid, pct = msg
            st = self._deploy_state.get(eid)
            if st is not None:
                st["pct"] = int(pct)
                st["phase"] = "switch" if int(pct) >= 100 else "upload"
        elif kind == "env_probe":   # 🩺 测试路径(后台探活结果回填)
            self._on_env_probe(msg)
        elif kind == "deploy_done":
            _, eid, script, res, idx = msg
            self._deploy_running.discard(eid)
            self._deploy_state.pop(eid, None)
            e = self._entry(eid)
            if e is not None:
                # 上次发布记录挂在**环境**上(每个环境各看各的)
                envs = e.get("envs") or []
                if 0 <= idx < len(envs) and isinstance(envs[idx], dict):
                    envs[idx]["last"] = {
                        "time": time.strftime("%m-%d %H:%M"),
                        "ok": bool(res.get("ok")),
                        "size": res.get("size") or "",
                        "ms": int(res.get("ms") or 0),
                    }
                try:
                    save_config(self.cfg)
                except Exception as ex:  # noqa: BLE001
                    self.log(f"❌ 环境发布记录写盘失败: {ex}", eid)
                self.select(eid)            # 重渲染(上次发布信息立即生效)
                title = f"{e.get('name') or eid} "
                if res.get("ok"):
                    body = (f"已发布 {len(res.get('done') or [])} 个目标,"
                            f"用时 {res.get('ms', 0) // 1000}s")
                    title += "发布成功"
                else:
                    body = f"发布中断:{str(res.get('err') or '未知原因')[:120]}"
                    title += "发布失败"
                threading.Thread(target=lambda: notify(title, body),
                                 daemon=True).start()
        elif kind == "srv_checked":
            # 发布配置面板里的「测试连接」结果:写 last_check 持久化
            _, tid, r = msg
            targets = load_targets()
            t = next((x for x in targets if x.get("id") == tid), None)
            if t is not None:
                t["last_check"] = {"ok": bool(r.get("ok")),
                                   "time": r.get("time") or "",
                                   "ms": int(r.get("ms") or 0)}
                try:
                    save_config(self.cfg)
                except Exception as ex:  # noqa: BLE001
                    self.log(f"❌ last_check 写盘失败: {ex}")
            self.log(f"{'✅' if r.get('ok') else '❌'} "
                     f"{target_ssh(t) if t else tid} {r.get('ms')}ms "
                     f"{str(r.get('hint') or r.get('out') or '')[:140]}")


    def _reload_list_keep_sel(self):
        self._reload_list()   # set_rows 自带 sel_id,选中行保持不变


if __name__ == "__main__":
    App().mainloop()
