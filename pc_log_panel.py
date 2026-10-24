#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pc_log_panel —— 日志区分块(LogPanelMixin)

负责:底部「系统 + 各项目」tab 条的增删切换与横向滚动、每 tab 的 Text 样式与
行数上限、切主题时的日志快照/回填、项目 tab 底部常驻 pty 命令行(建会话/收发/
历史/中断)、日志文件增量跟随(_attach_tail/_tail_loop),以及日志写入的统一
入口 log / _tab_write / _log_stream 与掉线告警 _alert_down。

依赖 App 提供的属性 —— 本模块只读,**全部由 App.__init__ / App._build_ui 建立**:
    self.q             后台线程 → UI 的消息队列
    self.tails         {日志路径: 文件句柄}  日志跟随状态
    self.tail_eid      {日志路径: eid}
    self._logwrap      日志区外层容器
    self._term_muted   内置命令行的静音开关集合
本模块自己建立(其余分块请按这些名字读):
    self._logbar / self._logbar_cv / self._logbar_win   tab 条(Canvas 横向滚动)
    self._logbody      日志内容区容器
    self._logtabs      {tid: {tab,lab,close,holder,body,text,title,term,entry,hist,hist_i}}
    self._logtab_id    当前显示的 tid
    self.logbox        系统 tab 的 tk.Text(旧代码/无头脚本按这个名字取)
并调用 App 的: _c() / _btn() / _ghost_btn() / _entry() / disp() / bg() / after()
"""

import os
import threading
import time
import tkinter as tk

import pc_core
from pc_const import LOG_MAX_LINES, LOG_TITLE_MAX, SYSTEM_TAB
from pc_core import notify


class LogPanelMixin:
    """日志区 tab、内置终端、tail 跟随、日志写入路由。"""

    # ------------------------------------------------ 日志区(tab 化 + 内置终端)

    def _build_log_area(self):
        """日志区:横向可滚动的 tab 条 + 内容区(同一时刻只显示一个 tab)。

        为什么 tab 化:此前所有项目共用一个日志框,`npm run dev` 的输出与
        「打开目录」这类操作日志混在一条时间线上,看不出哪行属于哪个项目。
        现在「系统」tab 只留关键动作与告警,每个项目 tab 装它自己的运行输出,
        底部还挂一个常驻交互式 shell(见 _ensure_term)。
        """
        bar = tk.Frame(self._logwrap, bg=self._c("bg"))
        bar.pack(fill="x")
        self._logbar_cv = tk.Canvas(bar, bg=self._c("bg"), height=30, bd=0,
                                    highlightthickness=0)
        self._logbar_cv.pack(fill="x", expand=True)
        self._logbar = tk.Frame(self._logbar_cv, bg=self._c("bg"))
        self._logbar_win = self._logbar_cv.create_window(
            (0, 0), window=self._logbar, anchor="nw")
        # tab 数量/窗口宽度变化都要重算滚动区域,否则溢出后滚不到末尾
        self._logbar.bind("<Configure>", lambda _e: self._logbar_sync())
        self._logbar_cv.bind("<Configure>", lambda _e: self._logbar_sync())
        self._logbar_cv.bind("<MouseWheel>", self._logbar_wheel)

        self._logbody = tk.Frame(self._logwrap, bg=self._c("log_bg"))
        self._logbody.pack(fill="both", expand=True, pady=(4, 0))

        self._logtabs = {}
        self._logtab_add(SYSTEM_TAB, "🖥 系统", closable=False)
        self._logtab_id = SYSTEM_TAB
        self._logtab_show(SYSTEM_TAB)

    def _logtab_add(self, tid: str, title: str | None = None,
                    closable: bool = True) -> dict:
        """建一个日志 tab(tab 头 + 内容区 + 项目 tab 的命令行),返回信息 dict。

        `tid=SYSTEM_TAB` 时它的 Text 会登记成 `self.logbox`,保持旧代码兼容。
        """
        if tid in self._logtabs:
            return self._logtabs[tid]
        if title is None:
            e = self._entry(tid)
            title = self.disp(e) if e else str(tid)
        if len(title) > LOG_TITLE_MAX:
            title = title[:LOG_TITLE_MAX] + "…"

        holder = tk.Frame(self._logbar, bg=self._c("bg"))
        holder.pack(side="left", padx=(0, 2))
        tab = tk.Frame(holder, bg=self._c("panel"), cursor="hand2")
        tab.pack(fill="both")
        # bd=0 + highlightthickness=0:aqua 下 Label 默认的 2px 边框会在
        # 激活态的 tab 外圈画一道浅色描边(截图核对过,像一圈脏框)。
        # v2.9:选中态不再只靠底色差(深色下几乎看不出),加一条 2px accent 下划线。
        lab = tk.Label(tab, text=title, bg=self._c("panel"), fg=self._c("mut"),
                       font=("PingFang SC", 11, "bold"), padx=10, pady=5,
                       bd=0, highlightthickness=0, cursor="hand2")
        lab.pack(side="left")
        # × 与标题之间留出间距(原来紧贴,看着像连在一起的字)
        close = None
        if closable:
            close = tk.Label(tab, text="×", bg=self._c("panel"), fg=self._c("mut"),
                             font=("PingFang SC", 12, "bold"), padx=8,
                             bd=0, highlightthickness=0, cursor="hand2")
            close.pack(side="left")
            close.bind("<Button-1>", lambda _e, t=tid: self._logtab_close(t))
            close.bind("<Enter>", lambda _e, w=close: w.configure(
                fg=self._c("red")) if w.winfo_exists() else None)
            close.bind("<Leave>", lambda _e, w=close, t=tid: w.configure(
                fg=self._c("fg") if t == self._logtab_id else self._c("mut"))
                if w.winfo_exists() else None)
        # 选中态下划线(未选中时与底色同色,看不见)
        under = tk.Frame(tab, height=2, bg=self._c("panel"))
        under.pack(side="bottom", fill="x")
        for w in (holder, tab, lab):
            w.bind("<Button-1>", lambda _e, t=tid: self._logtab_show(t))
            w.bind("<MouseWheel>", self._logbar_wheel)

        body = tk.Frame(self._logbody, bg=self._c("log_bg"))
        text = tk.Text(body, height=6, relief="flat", bd=0, font=("Menlo", 10),
                       state="disabled", wrap="word", bg=self._c("log_bg"),
                       fg=self._c("log_fg"), insertbackground=self._c("fg"),
                       highlightthickness=0)
        info = {"tab": tab, "lab": lab, "close": close, "holder": holder,
                "body": body, "text": text, "title": title, "term": None,
                "entry": None, "hist": [], "hist_i": 0, "under": under}
        if tid == SYSTEM_TAB:
            text.pack(fill="both", expand=True, padx=4, pady=4)
            self.logbox = text          # 兼容旧代码与无头脚本的 app.logbox
        else:
            # 先 pack 底部的命令行,再让 Text 吃掉剩余空间(顺序反了命令行会被挤没)
            self._build_term_bar(tid, body, info)
            text.pack(fill="both", expand=True, padx=4, pady=(4, 2))
        self._logtabs[tid] = info
        self._logbar_sync()
        self._style_logbox()      # 统一由它上色(新增 tab 也要走一遍)
        return info

    def _logtab_close(self, tid: str):
        """关掉项目 tab:先收掉它的 shell,再销毁控件(系统 tab 不可关)。"""
        if tid == SYSTEM_TAB:
            return
        info = self._logtabs.get(tid)
        if not info:
            return
        self._term_stop(tid)
        try:
            info["body"].destroy()
            info["holder"].destroy()
        except tk.TclError:
            pass
        self._logtabs.pop(tid, None)
        if self._logtab_id == tid:
            self._logtab_show(SYSTEM_TAB)
        self._logbar_sync()

    def _logtab_show(self, tid: str, spawn: bool = True):
        """切到某个 tab(不存在则退回系统 tab)。

        `spawn=False` 用于主题重建后的还原:只切显示,不主动起 shell
        (用户按回车时 _term_send 会自动补一个),避免重建瞬间刷出
        「shell 已退出 / 就绪」两条噪音。
        """
        if tid not in self._logtabs:
            tid = SYSTEM_TAB
        self._logtab_id = tid
        for k, info in self._logtabs.items():
            on = k == tid
            if on:
                info["body"].pack(fill="both", expand=True)
            else:
                info["body"].pack_forget()
            bg = self._c("log_bg") if on else self._c("panel")
            fg = self._c("fg") if on else self._c("mut")
            try:
                info["tab"].configure(bg=bg)
                info["lab"].configure(bg=bg, fg=fg)
                if info["close"] is not None:
                    info["close"].configure(bg=bg, fg=fg)
                # 选中态下划线:激活用 accent,其余与底色同色(看不见)
                under = info.get("under")
                if under is not None:
                    under.configure(bg=self._c("accent") if on else bg)
            except tk.TclError:
                pass
        info = self._logtabs.get(tid) or {}
        self._logbar_sync()
        text = info.get("text")
        if text is not None:
            try:
                text.see("end")
            except tk.TclError:
                pass
        if spawn and tid != SYSTEM_TAB:
            self._ensure_term(tid)      # 第一次打开项目 tab 时才起 shell

    def _logbar_sync(self):
        """tab 条:同步滚动区域,并保证当前 tab 在可视区内。"""
        cv = self._logbar_cv
        if cv is None:
            return
        try:
            if not cv.winfo_exists():
                return
            cv.configure(scrollregion=cv.bbox("all"))
        except tk.TclError:
            return
        self._logtab_reveal(self._logtab_id)

    def _logtab_reveal(self, tid: str):
        """把当前 tab 滚进可视区(不移动未溢出的 tab 条)。"""
        cv, info = self._logbar_cv, self._logtabs.get(tid)
        if cv is None or not info:
            return
        try:
            total = max(1, self._logbar.winfo_reqwidth())
            vw = max(1, cv.winfo_width())
            if total <= vw:
                cv.xview_moveto(0)
                return
            # 注意:tab 在 holder 里,winfo_x 是相对 holder 的,要取 holder 的 x
            holder = info["holder"]
            left = holder.winfo_x()
            right = left + holder.winfo_width()
            cur = cv.canvasx(0)
            if left < cur:
                cv.xview_moveto(max(0, left - 6) / total)
            elif right > cur + vw:
                cv.xview_moveto(max(0, right - vw + 6) / total)
        except tk.TclError:
            pass

    def _logbar_wheel(self, ev):
        """tab 条横向滚动(溢出时才接管滚轮,否则让给外层)。"""
        cv = self._logbar_cv
        if cv is None:
            return None
        try:
            bb = cv.bbox("all")
            if not bb or bb[2] <= cv.winfo_width():
                return None
        except tk.TclError:
            return None
        delta = getattr(ev, "delta", 0)
        num = getattr(ev, "num", None)
        if num == 4:
            delta = 1
        elif num == 5:
            delta = -1
        cv.xview_scroll(-1 if delta > 0 else 1, "units")
        return "break"

    def _style_logbox(self):
        """主题切换后重新配色(覆盖全部 tab 的 Text 与命令行的每一处)。"""
        for info in getattr(self, "_logtabs", {}).values():
            try:
                info["text"].configure(bg=self._c("log_bg"), fg=self._c("log_fg"),
                                       insertbackground=self._c("fg"))
                if info.get("termbar") is not None:
                    info["termbar"].configure(bg=self._c("card_hi"),
                                              highlightbackground=self._c("line"))
                if info.get("entry") is not None:
                    info["entry"].configure(bg=self._c("card_hi"),
                                            fg=self._c("fg"),
                                            insertbackground=self._c("fg"))
                if info.get("prompt") is not None:
                    info["prompt"].configure(bg=self._c("card_hi"),
                                             fg=self._c("accent"))
                if info.get("inthint") is not None:
                    info["inthint"].configure(bg=self._c("card_hi"),
                                              fg=self._c("red"))
            except (tk.TclError, KeyError):
                pass
        self._logbar_sync()

    def _snapshot_log(self) -> dict:
        """切主题前存下各 tab 的状态(文本 + 已问候标记 + 命令历史)。

        文本丢了等于日志全没;`greeted` / `hist` 挂在 `info` 上,重建时会一起没:
        丢了 `greeted` 会在下次打开该 tab 时**再打一遍「内置 shell 就绪」**
        (用户截图里出现两条一模一样的就是这个原因)。
        """
        snap: dict[str, dict] = {}
        for tid, info in getattr(self, "_logtabs", {}).items():
            try:
                snap[tid] = {
                    "title": info.get("title") or str(tid),
                    "text": info["text"].get("1.0", "end-1c")[-6000:],
                    "greeted": bool(info.get("greeted")),
                    "hist": list(info.get("hist") or []),
                }
            except tk.TclError:
                pass
        return snap

    def _restore_log(self, snap: dict):
        """切主题后把文本与 tab 状态放回去,并恢复原本显示的 tab。"""
        want = self._logtab_id
        for tid, it in (snap or {}).items():
            text = it.get("text") or ""
            if tid == SYSTEM_TAB:
                self._tab_write(SYSTEM_TAB, text)
                continue
            info = self._logtab_add(tid, it.get("title"), closable=True)
            info["greeted"] = bool(it.get("greeted"))   # 别在重建后再问候一次
            info["hist"] = list(it.get("hist") or [])   # ↑/↓ 历史也带过去
            info["hist_i"] = len(info["hist"])
            self._tab_write(tid, text)
        self._logtab_show(want if want in self._logtabs else SYSTEM_TAB,
                          spawn=False)

    # ---- 项目 tab 底部的常驻 pty 命令行

    def _build_term_bar(self, tid: str, body: tk.Frame, info: dict):
        """项目 tab 底部的命令行(UI 部分;shell 在第一次显示时才启动)。"""
        bar = tk.Frame(body, bg=self._c("card_hi"),
                       highlightthickness=1, highlightbackground=self._c("line"))
        bar.pack(fill="x", side="bottom")
        prompt = tk.Label(bar, text="❯", bg=self._c("card_hi"),
                          fg=self._c("accent"), font=("Menlo", 13, "bold"),
                          bd=0, highlightthickness=0)
        prompt.pack(side="left", padx=(8, 5), pady=2)
        ent = tk.Entry(bar, relief="flat", bd=0, bg=self._c("card_hi"),
                       fg=self._c("fg"), insertbackground=self._c("fg"),
                       font=("Menlo", 11), highlightthickness=0)
        ent.pack(side="left", fill="x", expand=True, pady=4)
        stop = tk.Label(bar, text="⛔", bg=self._c("card_hi"), fg=self._c("red"),
                        font=("PingFang SC", 13, "bold"), padx=8,
                        bd=0, highlightthickness=0, cursor="hand2")
        stop.pack(side="right")
        stop.bind("<Button-1>", lambda _e, t=tid: self._term_int(t))
        ent.bind("<Return>", lambda _e, t=tid: self._term_send(t))
        ent.bind("<Up>", lambda _e, t=tid: self._term_hist(t, -1))
        ent.bind("<Down>", lambda _e, t=tid: self._term_hist(t, 1))
        ent.bind("<Control-c>", lambda _e, t=tid: self._term_int(t))
        info["termbar"] = bar
        info["prompt"] = prompt
        info["entry"] = ent
        info["inthint"] = stop

    def _ensure_term(self, tid: str):
        """确保该 tab 有一个活着的 shell;已退出则重开一个。"""
        info = self._logtabs.get(tid)
        if not info or tid == SYSTEM_TAB:
            return
        sess = info.get("term")
        if sess is not None and pc_core.pty_alive(sess):
            return
        e = self._entry(tid) or {}
        cwd = e.get("dir") or os.path.expanduser("~")
        if not os.path.isdir(cwd):
            cwd = os.path.expanduser("~")
        try:
            sess = pc_core.pty_spawn(cwd, e.get("node_version"))
        except Exception as ex:  # noqa: BLE001  起不来也不能把 UI 拖崩
            info["term"] = None
            self._tab_write(tid, f"❌ 内置 shell 启动失败: {ex}\n")
            return
        info["term"] = sess
        if not info.get("greeted"):    # 只问候一次,重开 shell 不再重复刷屏
            info["greeted"] = True
            self._tab_write(
                tid, f"❯ 内置 shell 就绪 · {cwd}"
                     f"(Enter 执行,↑/↓ 翻历史,⛔ 中断,exit 退出)\n")
        self._term_reader(tid, sess)

    def _term_reader(self, tid: str, sess: dict):
        """后台线程:把 pty 输出丢进队列,主线程再写控件(铁律:后台不碰控件)。"""

        def job():
            while True:
                chunk = pc_core.pty_read(sess, 4096)
                if not chunk:          # b"" = shell 退出 / master 已关,安静收工
                    self.q.put(("term_exit", tid))
                    return
                text = pc_core.clean_term_text(chunk.decode("utf-8", "replace"))
                if text:
                    self.q.put(("term", tid, text))

        self.bg(job)

    def _term_send(self, tid: str):
        """命令行回车:回显 → 记历史 → 写进 pty。"""
        info = self._logtabs.get(tid)
        if not info or info.get("entry") is None:
            return "break"
        cmd = info["entry"].get()
        info["entry"].delete(0, "end")
        if not cmd.strip():
            return "break"
        hist = info.setdefault("hist", [])
        if not hist or hist[-1] != cmd:      # 相邻重复不重复记
            hist.append(cmd)
        info["hist_i"] = len(hist)
        self._tab_write(tid, f"❯ {cmd}\n")
        sess = info.get("term")
        if sess is None or not pc_core.pty_alive(sess):
            self._ensure_term(tid)           # 之前退出了:重开再发
            sess = info.get("term")
        if not sess or not pc_core.pty_write(sess, cmd):
            self._tab_write(tid, "❌ shell 不可用,命令未执行\n")
            self._tab_write(SYSTEM_TAB, f"❌ [{info.get('title')}] 内置 shell 不可用\n")
        return "break"

    def _term_hist(self, tid: str, step: int):
        """↑/↓ 翻历史命令。"""
        info = self._logtabs.get(tid)
        if not info or info.get("entry") is None:
            return "break"
        hist = info.get("hist") or []
        if not hist:
            return "break"
        i = int(info.get("hist_i", len(hist))) + step
        i = max(0, min(i, len(hist)))
        info["hist_i"] = i
        ent = info["entry"]
        ent.delete(0, "end")
        if i < len(hist):
            ent.insert(0, hist[i])
        return "break"

    def _term_int(self, tid: str):
        """⛔ 中断:对 shell 所在进程组发 SIGINT(等价 Ctrl-C)。"""
        info = self._logtabs.get(tid)
        sess = (info or {}).get("term")
        if sess and pc_core.pty_alive(sess) and pc_core.pty_int(sess):
            self._tab_write(tid, "⛔ 已中断(Ctrl-C)\n")
        else:
            self._tab_write(tid, "⛔ 当前没有可中断的进程\n")

    def _term_stop(self, tid: str):
        """收掉某个 tab 的 shell(关 tab / 关窗口 / 切主题时调用)。"""
        info = self._logtabs.get(tid)
        if not info:
            return
        sess = info.get("term")
        info["term"] = None
        if sess:
            # 主动收掉的 shell:读线程随后会报 EOF,不要把它写成「意外退出」
            self._term_muted.add(tid)
            pc_core.pty_kill(sess)      # SIGTERM 整组 → SIGKILL → wait,不留僵尸

    def _term_stop_all(self):
        for tid in list(getattr(self, "_logtabs", {}) or {}):
            self._term_stop(tid)

    def _attach_tail(self, path: str, eid: str | None = None):
        """开始跟踪一个日志文件:先带出末尾 2KB,之后增量追加到**该项目的 tab**。

        必须只在主线程调用(它改 self.tails 并写控件);后台线程请改用
        `self.q.put(("tail", path, eid))`。
        """
        if not path or not os.path.isabs(path):
            return
        if eid:
            self.tail_eid[path] = eid
        if path not in self.tails:
            self.tails[path] = -1
            self.log(f"🧵 开始跟踪日志: {path}", eid)
        # 只保留最近 2 个跟踪目标,避免日志区被历史任务刷屏
        while len(self.tails) > 2:
            self.tails.pop(next(iter(self.tails)))

    def _tail_loop(self):
        for path in list(self.tails):
            off = self.tails[path]
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if off == -1:                       # 首次附着:从末尾 2KB 开始
                off = max(0, size - 2048)
            if size < off:                      # 文件被清空/重建
                off = 0
            if size > off:
                try:
                    with open(path, "rb") as f:
                        f.seek(off)
                        chunk = f.read(65536)
                except OSError:
                    continue
                self.tails[path] = off + len(chunk)
                text = chunk.decode("utf-8", "replace").rstrip("\n")
                if text:
                    # 项目的运行输出只进它自己的 tab,不进「系统」tab
                    self._log_stream(text, self.tail_eid.get(path))
        self.after(1500, self._tail_loop)

    def _alert_down(self, text: str, eid: str | None = None):
        """P0-2 服务掉线:日志醒目告警 + macOS 通知中心通知。

        双写:既进该项目的 tab(排查时只看这个项目的历史),也进「系统」tab
        (全局时间线要能看出「什么时候发生过什么」)。
        通知失败(osascript 不可用)时静默降级为仅日志,不影响主流程。
        osascript 有百毫秒级耗时,放后台线程避免卡 UI。
        """
        self.log(text, eid)
        if eid:
            self.log(text)
        threading.Thread(
            target=lambda: notify("Project Console", text.lstrip("⚠️ ").strip()),
            daemon=True).start()

    def _tab_write(self, tid: str, s: str):
        """向指定 tab 追加**原始**文本(已带换行),并裁掉超长历史。"""
        info = self._logtabs.get(tid)
        if not info or not s:
            return
        box = info["text"]
        try:
            at_bottom = box.yview()[1] > 0.999    # 用户往上翻时不要强行拽到底
            box.configure(state="normal")
            box.insert("end", s)
            n = int(box.index("end-1c").split(".")[0])
            if n > LOG_MAX_LINES:
                box.delete("1.0", f"{n - LOG_MAX_LINES}.0")
            if at_bottom:
                box.see("end")
            box.configure(state="disabled")
        except tk.TclError:
            pass

    def _logtab_for(self, eid: str) -> str:
        """确保某条目有日志 tab,返回 tab id(条目不存在则退回系统 tab)。"""
        if eid in self._logtabs:
            return eid
        if not self._entry(eid):
            return SYSTEM_TAB
        self._logtab_add(eid, closable=True)
        return eid

    def log(self, text: str, eid: str | None = None):
        """写日志(带时间戳)。

        - `eid=None` → 「系统」tab:全局时间线,只放关键动作与告警;
        - `eid=条目 id` → 该项目的 tab(没有就新建),放这个项目自己的输出。
        """
        ts = time.strftime("%H:%M:%S")
        tid = SYSTEM_TAB if not eid else self._logtab_for(eid)
        self._tab_write(tid, f"[{ts}] {text}\n")

    def _log_stream(self, text: str, eid: str | None):
        """运行日志增量(无时间戳,`┈ ` 前缀),写进对应项目 tab。"""
        tid = SYSTEM_TAB if not eid else self._logtab_for(eid)
        for line in text.splitlines() or [""]:
            self._tab_write(tid, f"┈ {line}\n")
