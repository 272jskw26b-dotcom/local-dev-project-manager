#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pc_env_panel —— 详情区「环境配置」分块(EnvPanelMixin)

负责:一个条目下多个环境的 tab 化配置(环境名/打包脚本/服务器/包名/部署路径)、
网站登录账号表格(增删改 + 单拷账号 / 单拷密码)、输入即存(防抖)+ 失焦落盘 +
`_env_flush` 钩子,以及 🚀 发布的三段流程(确认弹窗 → 后台构建上传 → 进度 tick)。

依赖 App 提供的属性 —— 本模块只读:
    self.cfg            注册表内存副本(entries / targets)
    self.scripts        {eid: [(脚本名, 命令)]}
    self.dinner         详情区右栏滚动容器(预览行要挂滚轮)
    self.q              后台线程 → UI 的消息队列
    self._env_tab       {eid: 当前环境下标}
    self._env_btns      {eid: 该条目「🚀 发布」按钮}(别的环境发布中要变灰)
    self._deploy_state  {eid: 发布进度状态}
    self._deploy_running 正在发布中的 eid 集合
本模块自己建立:
    self._env_flush     当前环境表单的 flush 闭包(切项目/关窗前必调)
    self._env_debounce  「输入即存」的 after id
并调用 App 的: _c() / _btn() / _ghost_btn() / _center() / _cancel_env_debounce() /
        _servers_dialog() / select() / log() / disp() / bg() / after()
"""

import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from pc_const import BUILD_PREFIX
from pc_core import (
    deploy_job, deploy_probe, deploy_static, deploy_validate, env_job_item,
    env_last_text, env_name, guess_pkg, is_backend, load_targets, save_config,
    target_addr, target_ssh,
)


class EnvPanelMixin:
    """环境配置区:tab、表单、账号表、发布流程。"""

    def _ask_env_name(self, title: str, default: str) -> str | None:
        """小弹窗问一个环境名(新建环境用)。返回 None 表示用户取消。"""
        dlg = tk.Toplevel(self)
        dlg.title(title)
        dlg.configure(bg=self._c("card"))
        dlg.transient(self)
        dlg.grab_set()
        # 宽度锁 380,高度建成后按内容量(套用 pc_servers_dialog 那套量法:
        # 定高弹窗在长标题/长说明下会把下面的按钮挤出去)
        dlg.resizable(False, True)
        dlg.minsize(380, 168)
        tk.Label(dlg, text=title, bg=self._c("card"), fg=self._c("fg"),
                 font=("PingFang SC", 13, "bold")).pack(anchor="w",
                                                       padx=20, pady=(18, 2))
        tk.Label(dlg, text="tab 标题,建好也能点 ✎ 改", bg=self._c("card"),
                 fg=self._c("mut"), font=("PingFang SC", 11)).pack(anchor="w",
                                                                   padx=20)
        var = tk.StringVar(value=default)
        ent = tk.Entry(dlg, textvariable=var, font=("PingFang SC", 13),
                       bg=self._c("log_bg"), fg=self._c("fg"),
                       insertbackground=self._c("fg"), relief="flat", bd=0,
                       highlightthickness=1,
                       highlightbackground=self._c("accent"),
                       highlightcolor=self._c("accent"))
        ent.pack(fill="x", padx=20, pady=(10, 0), ipady=4)
        out: dict = {"v": None}

        def ok(_ev=None):
            out["v"] = (var.get() or "").strip() or default
            dlg.destroy()

        def cancel(_ev=None):
            out["v"] = None
            dlg.destroy()

        row = tk.Frame(dlg, bg=self._c("card"))
        row.pack(fill="x", padx=20, pady=(14, 0))
        self._ghost_btn(row, "取消", cancel, height=26).pack(side="right")
        self._btn(row, "创建", ok, primary=True).pack(side="right", padx=(0, 8))
        ent.bind("<Return>", ok)
        dlg.bind("<Escape>", cancel)
        dlg.update_idletasks()
        need = dlg.winfo_reqheight()
        max_h = int(dlg.winfo_screenheight() * 0.9)
        self._center(dlg, 380, max(168, min(need, max_h)))
        ent.focus_set()
        ent.select_range(0, "end")
        dlg.wait_window()
        return out["v"]

    def _deploy_phase_text(self, st: dict) -> str:
        """发布按钮的实时文案:构建中 12s → 上传 62% → 切换中… → 完成。"""
        phase = (st or {}).get("phase") or "build"
        elapsed = int(time.time() - ((st or {}).get("t0") or time.time()))
        if phase == "build":
            return f"构建中 {elapsed}s"
        if phase == "upload":
            return f"上传 {int((st or {}).get('pct') or 0)}%"
        if phase == "switch":
            return "切换中…"
        return "完成"

    def _on_env_probe(self, msg):
        """「🩺 测试路径」的后台结果回填(由 `_handle_msg` 在主线程调用)。

        msg = ("env_probe", eid, idx, ok, 中文说明)
        按钮/结果行可能已随分区切换被销毁 → 一律 `winfo_exists()` + try 兜住;
        只有「同一条目仍停在同一环境」时才回填 UI,否则只写日志。
        """
        _, eid, idx, ok, text = msg
        ref = (getattr(self, "_env_test_btn", None) or {}).get(eid) or {}
        btn = ref.get("btn")
        try:
            if btn is not None and btn.winfo_exists():
                btn._testing = False
                btn.configure(text="🩺 测试路径")
        except tk.TclError:
            pass
        setter = ref.get("set")
        if callable(setter) and ref.get("idx") == self._env_tab.get(eid):
            try:
                setter(bool(ok), text)
            except tk.TclError:
                pass
        e = self._entry(eid)
        nm = self.disp(e) if e else eid
        self.log(("✅ " if ok else "❌ ")
                 + f"[{nm}] 环境{idx + 1} 路径测试:{text}", eid)

    def _rebuild_env_panel(self, e: dict):
        """只重建「环境配置」面板本体(切换 / 新建 / 删除环境时用)。

        以前这些操作统一走 `self.select(eid)`,那会把整个详情区推倒重来:
        重跑 Git 分支与远程地址(两次 git 子进程)、重建子项目列表 / 内嵌
        脚本列表 / 服务器区,并触发一次 `refresh_selected()` 的后台 SSH +
        端口 + 健康检查 —— 用户看到的就是「点一下环境 tab,整页闪一下、
        滚动位置还可能跳走」。

        这里只清空环境面板自己的 host 帧再渲染一遍,详情区其它分区原样不动。
        host 帧由 `_render_env_config` 在渲染时登记(`_env_host[eid]`)。
        """
        eid = e["id"]
        host = (getattr(self, "_env_host", None) or {}).get(eid)
        try:
            alive = host is not None and bool(host.winfo_exists())
        except tk.TclError:
            alive = False
        if not alive:
            self.select(eid)          # 面板已不在(条目被切走):退回全量渲染
            return
        titles = getattr(self, "_env_host_title", None) or {}
        show_title = bool(titles.get(eid, False))
        # 先落盘 + 摘钩子:下面的销毁会让旧闭包失效,先把没失焦的输入保存掉
        flush, self._env_flush = getattr(self, "_env_flush", None), None
        if callable(flush):
            try:
                flush()
            except Exception:     # noqa: BLE001 — 保存失败不能挡住重渲染
                pass
        self._cancel_env_debounce()
        try:
            for w in host.winfo_children():
                w.destroy()
        except tk.TclError:
            self.select(eid)
            return
        self._render_env_config(e, host=host, show_title=show_title)

    def _render_env_config(self, e: dict, host=None, show_title: bool = True):
        """详情区「环境配置」:一个条目多个环境(tab),每个环境单独发布。

        用户模型:一个环境 = 环境名 + 打包脚本 + 服务器 + 包名 + 部署路径,
        一个 🚀 发布按钮;下面再挂一组**网站登录账号**
        (只保存 / 展示 / 一键复制账号或密码,不参与发布)。

        v2.7:详情区把「脚本执行 / 环境配置」做成分段页,本方法要能渲染到
        指定的 `host` 帧里(不再硬挂 `self.dinner`)。分段页的标题已经写了
        「环境配置」,所以页内 `show_title=False` 时省掉重复的分区标题,
        只保留右侧的环境计数与「+ 新建环境」。
        """
        host = self.dinner if host is None else host
        eid = e["id"]
        # 登记「本面板长在哪个帧」——切环境/增删环境时只重建这个帧,
        # 不再走 select() 全量刷新详情区(见 _rebuild_env_panel)
        if not isinstance(getattr(self, "_env_host", None), dict):
            self._env_host = {}
        if not isinstance(getattr(self, "_env_host_title", None), dict):
            self._env_host_title = {}
        self._env_host[eid] = host
        self._env_host_title[eid] = show_title
        stored = e.get("envs")
        envs = stored if isinstance(stored, list) else []
        idx = int(self._env_tab.get(eid, 0) or 0)
        if idx >= len(envs):
            idx = max(0, len(envs) - 1)
        self._env_tab[eid] = idx

        names = [n for n, _c in (self.scripts.get(eid) or [])]
        build_names = [n for n in names if n.startswith(BUILD_PREFIX)]
        pick_names = build_names or names
        # v2.8:后端条目没有自动发布 → 「打包脚本」这一栏对它没有意义,
        # 而且 maven 的脚本表里混着 start/stop/status(启动脚本不是构建脚本),
        # 兜底全塞进来会让人误选。这里直接不给候选(下拉为空)。
        # backend 要在后面 refresh_prev() 里用,所以在这里先算出来。
        backend = is_backend(e)
        if backend:
            pick_names = []

        C_card = self._c("card")
        C_line = self._c("line")
        C_mut = self._c("mut")
        C_fg = self._c("fg")
        C_dbg = self._c("deploy_bg")
        C_accent = self._c("accent")
        C_log = self._c("log_bg")
        C_hi = self._c("card_hi")
        C_dim = self._c("dot_none")

        tabs: list[dict] = []                 # 每条 tab 的控件引用(改名/变灰用)
        acc_fields: list[tuple[dict, str, tk.StringVar]] = []   # 账号三列注册表
        v_name = None                         # 表单里的「环境名」,下面才创建

        def persist() -> None:
            """把 envs 挂回条目并落盘;空列表就删键(别在真 json 里留 `"envs": []`)。"""
            if envs:
                e["envs"] = envs
            else:
                e.pop("envs", None)
            save_config(self.cfg)

        # ---------------- 分区标题行
        # v2.9:「N 个环境 / + 新建环境」这一组改挂到**分段控件那一行的右侧**
        # (由 project_console._render_detail_panes 预留的 _env_bar_slot),
        # 不再在环境面板内部另起一行 —— 原来两处分家,视线要来回扫。
        # show_title=True(独立渲染,不跟在分段控件后面)时保持原样,自己起一行。
        slot = getattr(self, "_env_bar_slot", None)
        use_slot = (not show_title) and slot is not None and \
            getattr(self, "_env_bar_slot_eid", None) == eid
        head_pady = (14, 8) if show_title else (8, 6)
        if use_slot:
            head = slot
            # 槽位可能残留上一次渲染的控件,先清空
            for w in head.winfo_children():
                w.destroy()
        else:
            head = tk.Frame(host, bg=C_card)
            head.pack(fill="x", padx=18, pady=head_pady)
            if show_title:
                tk.Label(head, text="▎", bg=C_card, fg=C_accent,
                         font=("PingFang SC", 12, "bold")).pack(side="left")
                tk.Label(head, text="环境配置", bg=C_card, fg=C_fg,
                         font=("PingFang SC", 12, "bold")).pack(side="left", padx=(2, 8))
        v_cnt = tk.StringVar(value=(f"{len(envs)} 个环境" if envs else "还没有环境"))
        tk.Label(head, textvariable=v_cnt, bg=C_card, fg=C_mut,
                 font=("PingFang SC", 11)).pack(side="left")

        sec = tk.Frame(host, bg=C_card)
        sec.pack(fill="x", padx=18)

        # ---------------- 落盘 / 钩子
        def flush_env():
            """把当前渲染的表单立刻落盘(切 tab / 切项目 / 切分组 / 关窗的统一入口)。

            走 `self._env_flush` 而不是直接引用本地 `save_env`:
            ① 空环境态的渲染里根本不存在 `save_env` 这个局部量(`new_env` 却
               已经挂在按钮上了),直接引用会 UnboundLocalError;
            ② 它由上一次渲染设置,`select()` 在重建前已置空,不会串到下个条目。
            """
            fn = getattr(self, "_env_flush", None)
            if callable(fn):
                try:
                    fn()
                except Exception:   # noqa: BLE001 — 控件已销毁时静默
                    pass

        def detach_flush():
            """改 envs 结构之前:先落盘,再把旧闭包摘掉。

            只落盘不摘钩子是不够的 —— 改完 envs 结构后的
            `after(10, self._rebuild_env_panel)` 会再走一次保存收口,
            那时 envs 已经移位,旧闭包就把「被删环境的表单」写进了
            幸存环境(P0 数据串味)。
            """
            flush_env()
            self._env_flush = None

        # ---------------- 新建 / 删除 / 切换环境
        def new_env():
            default = f"环境 {len(envs) + 1}"
            nm = self._ask_env_name("新建环境", default)
            if nm is None:
                return
            detach_flush()                       # 先落盘 + 摘钩子,再动 envs
            envs.append({"name": nm,
                         "script": pick_names[0] if pick_names else "",
                         "target": (load_targets() or [{}])[0].get("id", ""),
                         "pkg": guess_pkg(e.get("dir") or ""),
                         "path": "", "accounts": []})
            self._env_tab[eid] = len(envs) - 1
            try:
                persist()
            except Exception as ex:              # noqa: BLE001
                self.log(f"❌ [{self.disp(e)}] 新建环境保存失败: {ex}", eid)
            self.log(f"➕ [{self.disp(e)}] 新建环境「{nm}」", eid)
            self.after(10, lambda: self._rebuild_env_panel(e))

        self._ghost_btn(head, "+ 新建环境", new_env, height=28,
                        font_size=12).pack(side="right")

        # 空态:还没建过环境(仍要留出「+ 新建环境」入口)
        if not envs:
            tk.Label(sec, text="还没有环境 —— 点右上角「+ 新建环境」开始",
                     bg=C_card, fg=C_mut,
                     font=("PingFang SC", 11)).pack(anchor="w", pady=(10, 4))
            self._env_flush = None
            return

        def del_env(i: int):
            if not (0 <= i < len(envs)):
                return
            nm = env_name(envs[i], i)
            if not messagebox.askyesno("删除环境",
                                       f"删除环境「{nm}」?(只删配置,不动服务器/代码)",
                                       parent=self):
                return
            detach_flush()                       # ★ 先落盘再摘钩子,防串味
            envs.pop(i)
            self._env_tab[eid] = max(0, min(i, len(envs) - 1))
            try:
                persist()
            except Exception as ex:              # noqa: BLE001
                self.log(f"❌ [{self.disp(e)}] 删除环境保存失败: {ex}", eid)
            self.log(f"🗑 [{self.disp(e)}] 删除环境「{nm}」", eid)
            self.after(10, lambda: self._rebuild_env_panel(e))

        def pick(i: int):
            if i == self._env_tab.get(eid):
                return
            # Tk 的 Label 不取焦点 → Entry 的 <FocusOut> 不会触发,
            # 所以这里必须**主动**落盘,否则「敲完路径直接点另一个 tab」会丢输入
            detach_flush()
            self._env_tab[eid] = i
            # 在 Label 自己的事件里销毁它会出问题,延后一帧重建;
            # 只重建环境面板本体,不动详情区其它分区(否则整页闪一下)
            self.after(10, lambda: self._rebuild_env_panel(e))

        # ---------------- tab 条(Canvas + 内帧,横向滚动;切 tab 自动露出)
        # v2.7.3 按用户要求把 tab 条整体放大一号(字号 13 / 竖向留白 9 /
        # 左右内边距 16),画布高度同步抬到 44,否则字会被裁。
        bar = tk.Frame(sec, bg=C_card)
        bar.pack(fill="x")
        tabcv = tk.Canvas(bar, bg=C_card, bd=0, highlightthickness=0, height=44)
        tabcv.pack(side="left", fill="x", expand=True)
        holder = tk.Frame(tabcv, bg=C_card)
        holder_win = tabcv.create_window((0, 0), window=holder, anchor="nw")
        self._ghost_btn(bar, "＋", new_env, height=30,
                        font_size=14).pack(side="right", padx=(8, 0), pady=(6, 0))
        tk.Frame(sec, height=1, bg=C_line).pack(fill="x", pady=(0, 12))

        def tab_hover(i: int, on: bool) -> None:
            """hover:未选中的 tab 文字提亮并露出 ×(选中态本来就带 ×)。"""
            if not (0 <= i < len(tabs)):
                return
            t = tabs[i]
            if i == self._env_tab.get(eid):
                return
            try:
                t["lab"].configure(fg=C_fg if on else C_mut)
                if on:
                    t["xb"].pack(side="left", padx=(6, 16))
                else:
                    t["xb"].pack_forget()
            except tk.TclError:
                pass

        def _safe_destroy(w) -> None:
            try:
                w.destroy()
            except tk.TclError:
                pass

        def draw_tabs():
            """重画 tab 条(只在整段重渲染时调用,不在事件回调里 destroy 自己)。

            v2.7.3:字号 12→13、竖向留白 6→9、左右内边距 12→16(整体放大一号);
            同时**去掉 ✎ 就地改名**—— 改名统一在下面表单的「环境名」里改
            (用户明确要求:tab 上不要再挂改名入口)。
            """
            for w in holder.winfo_children():
                w.destroy()
            tabs.clear()
            for i, env in enumerate(envs):
                active = (i == idx)
                item = tk.Frame(holder, bg=C_card)
                item.pack(side="left", padx=(0, 2))
                row = tk.Frame(item, bg=C_card)
                row.pack(side="top")
                lab = tk.Label(row, text=env_name(env, i), bg=C_card,
                               fg=C_fg if active else C_mut, cursor="hand2",
                               font=("PingFang SC", 13,
                                     "bold" if active else "normal"))
                lab.pack(side="left", padx=(16, 0), pady=9)
                xb = tk.Label(row, text="×", bg=C_card, fg=C_mut,
                              cursor="hand2", font=("PingFang SC", 13))
                under = tk.Frame(item, height=2,
                                 bg=C_accent if active else C_card)
                under.pack(side="top", fill="x")
                tabs.append({"item": item, "row": row, "lab": lab,
                             "xb": xb, "under": under})
                lab.bind("<Button-1>", lambda _ev, i=i: pick(i))
                xb.bind("<Button-1>", lambda _ev, i=i: del_env(i))
                item.bind("<Enter>", lambda _ev, i=i: tab_hover(i, True))
                item.bind("<Leave>", lambda _ev, i=i: tab_hover(i, False))
                if active:
                    xb.pack(side="left", padx=(6, 16))
                # 未选中:hover 时才 pack ×(见 tab_hover)

        def bar_sync(_ev=None):
            holder.update_idletasks()
            tabcv.configure(scrollregion=tabcv.bbox("all"))
        holder.bind("<Configure>", bar_sync)

        def bar_reveal():
            """把当前 tab 滚进可视区(窄窗口下靠后的 tab 会跑到画布外)。"""
            try:
                kids = holder.winfo_children()
                if not kids or not (0 <= idx < len(kids)):
                    return
                x = kids[idx].winfo_x()
                total = max(1, holder.winfo_width())
                tabcv.xview_moveto(max(0.0, (x - 40) / total))
            except tk.TclError:
                pass

        self.after(30, bar_reveal)

        env = envs[idx]

        # ---------------- 左右两栏:左 = 环境本身(配置 + 测试/发布),右 = 网站账号
        # 用户模型(v2.7.2):一个环境看一屏就够 —— 左边填「发到哪」,
        # 右边记「验收用哪个账号」。原先上下堆叠时账号表把表单顶到很下面。
        split = tk.Frame(sec, bg=C_card)
        split.pack(fill="x")
        # v2.9:原来两列强制等宽(uniform),但左边的表单有 5 项、右边的账号表
        # 只有 3 列窄控件,等宽把表单挤到溢出(卡片右边框被裁掉,看着像条断线)。
        # 改成 2:1 加权:表单列宽一点,账号列窄一点。
        split.columnconfigure(0, weight=2, minsize=260)   # 表单列
        split.columnconfigure(1, weight=1, minsize=200)   # 网站账号列
        lcol = tk.Frame(split, bg=C_card)      # 左半:5 项基本信息 + 操作
        lcol.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        rcol = tk.Frame(split, bg=C_card)      # 右半:网站账号(每组竖排三框)
        rcol.grid(row=0, column=1, sticky="nsew")

        # ---------------- 左栏:表单卡片(5 项竖排,半屏宽度下不再并排两列)
        # v2.9:卡片边框改成「外框 1px 线色 + 内框 1px 内缩」的双层结构 ——
        # 原来直接用 highlightthickness,边框只画在控件实际占到的范围,
        # 宽度被挤爆时右边框就被裁掉,看起来像条断掉的竖线。
        # 双层 frame 能保证四边都有完整边框。
        fcard = tk.Frame(lcol, bg=self._c("card_line"), bd=0)
        fcard.pack(fill="x")
        fcard_in = tk.Frame(fcard, bg=C_dbg, bd=0)
        fcard_in.pack(fill="both", expand=True, padx=1, pady=1)
        form = tk.Frame(fcard_in, bg=C_dbg)
        form.pack(fill="x", padx=14, pady=12)
        form.columnconfigure(0, minsize=56)    # 标签列:按最长「部署路径」定宽
        form.columnconfigure(1, weight=1)      # 值列:吃满剩余宽度

        v_name = tk.StringVar(value=env_name(env, idx))
        v_script = tk.StringVar(value=str(env.get("script") or
                                          (pick_names[0] if pick_names else "")))
        v_pkg = tk.StringVar(value=str(env.get("pkg") or "dist"))
        v_path = tk.StringVar(value=str(env.get("path") or ""))
        v_saved = tk.StringVar(value="")
        v_prev = tk.StringVar(value="")
        v_last = tk.StringVar(value="" if is_backend(e) else env_last_text(env))
        v_test = tk.StringVar(value="")       # 🩺 测试路径的结果回显

        targets = load_targets()

        def srv_labels() -> tuple[list[str], dict]:
            labs: list[str] = []
            mapping: dict[str, str] = {}
            for t in targets:
                lab = f"{t.get('name') or t.get('id')}({target_ssh(t)})"
                if lab in mapping:
                    lab = f"{lab} #{len(mapping)}"
                mapping[lab] = t.get("id")
                labs.append(lab)
            return labs, mapping

        labels, label2id = srv_labels()
        cur_tid = str(env.get("target") or "")
        cur_lab = next((lb for lb, tid in label2id.items() if tid == cur_tid), "")
        v_srv = tk.StringVar(value=cur_lab or (labels[0] if labels else ""))

        def mklab(row: int, text: str):
            tk.Label(form, text=text, bg=C_dbg, fg=C_mut, anchor="e",
                     font=("PingFang SC", 11)).grid(
                         row=row, column=0, sticky="e", padx=(0, 10), pady=5)

        def mkent(parent, var: tk.StringVar, font=("Menlo", 12),
                  justify: str = "left") -> tk.Entry:
            """统一输入框:高≈26、log_bg 底、聚焦描边转 accent。"""
            ent = tk.Entry(parent, textvariable=var, font=font, justify=justify,
                           bg=C_log, fg=C_fg, insertbackground=C_fg,
                           relief="flat", bd=0, highlightthickness=1,
                           highlightbackground=C_line, highlightcolor=C_accent)
            ent.bind("<FocusIn>",
                     lambda _e, w=ent: w.configure(highlightbackground=C_accent))
            ent.bind("<FocusOut>",
                     lambda _e, w=ent: w.configure(highlightbackground=C_line))
            return ent

        # 行 1:环境名
        mklab(0, "环境名")
        ent_name = mkent(form, v_name, font=("PingFang SC", 12))
        ent_name.grid(row=0, column=1, sticky="we", pady=5, ipady=2)

        # 行 2:打包脚本
        mklab(1, "打包脚本")
        if pick_names:
            cb_script = ttk.Combobox(form, textvariable=v_script,
                                     values=pick_names, state="readonly",
                                     style="PC.TCombobox", font=("Menlo", 10))
            cb_script.grid(row=1, column=1, sticky="we", pady=5)
            cb_script.bind("<<ComboboxSelected>>", lambda _e: save_env())
        else:
            tk.Label(form, text=("(后端暂不支持自动发布)"
                                 if is_backend(e)
                                 else "(该条目没有脚本 —— 先装依赖/写 package.json)"),
                     bg=C_dbg, fg=self._c("warn"), anchor="w",
                     font=("PingFang SC", 10)).grid(
                         row=1, column=1, sticky="w", pady=5)

        # 行 3:服务器
        mklab(2, "服务器")
        cb_srv = ttk.Combobox(form, textvariable=v_srv,
                              values=labels + ["+ 新建服务器…"],
                              state="readonly", style="PC.TCombobox",
                              font=("PingFang SC", 10))
        cb_srv.grid(row=2, column=1, sticky="we", pady=5)

        # 行 4:包名
        mklab(3, "包名")
        ent_pkg = mkent(form, v_pkg)
        ent_pkg.grid(row=3, column=1, sticky="we", pady=5, ipady=2)

        # 行 5:部署路径
        mklab(4, "部署路径")
        ent_path = mkent(form, v_path)
        ent_path.grid(row=4, column=1, sticky="we", pady=5, ipady=2)

        def on_srv_pick(_ev=None):
            if v_srv.get() == "+ 新建服务器…":
                self._servers_dialog(open_new=True)
                self.after(400, lambda: self._rebuild_env_panel(e))
                return
            save_env()

        cb_srv.bind("<<ComboboxSelected>>", on_srv_pick)

        # 行 6:预览(分隔线 + 绿点 + 发布目标 / 上次结果,竖排两行)
        # 半屏宽度下原来「一行到底」的写法会被截断,改成两行 + 自动折行
        tk.Frame(form, height=1, bg=C_line).grid(
            row=5, column=0, columnspan=2, sticky="we", pady=(10, 6))
        prow = tk.Frame(form, bg=C_dbg)
        prow.grid(row=6, column=0, columnspan=2, sticky="we")
        dot_last = (env.get("last") or {}).get("ok")
        dcv = tk.Canvas(prow, width=9, height=9, bg=C_dbg, bd=0,
                        highlightthickness=0)
        dcv.create_oval(1, 2, 7, 8, outline="",
                        fill=self._c("green") if dot_last else C_dim)
        dcv.pack(side="left", padx=(0, 6), anchor="n", pady=(5, 0))
        plines = tk.Frame(prow, bg=C_dbg)
        plines.pack(side="left", fill="x", expand=True)
        lab_prev = tk.Label(plines, textvariable=v_prev, bg=C_dbg, fg=C_mut,
                            anchor="w", justify="left", font=("Menlo", 11))
        lab_prev.pack(fill="x")
        lab_last = tk.Label(plines, textvariable=v_last, bg=C_dbg, fg=C_mut,
                            anchor="w", justify="left",
                            font=("PingFang SC", 11))
        lab_last.pack(fill="x")

        def refresh_prev(*_a):
            tid = label2id.get(v_srv.get()) or ""
            t = next((x for x in targets if x.get("id") == tid), None)
            pkg = v_pkg.get().strip() or "dist"
            # 后端没有发布概念,别显示"不能发布"这种发布语义文案
            if backend:
                v_prev.set("(后端暂不支持自动发布)")
                return
            if t is None:
                v_prev.set(f"→(未选服务器)· 本地 {pkg}/")
                return
            p = v_path.get().strip() or "(未填路径,不能发布)"
            v_prev.set(f"→ {target_addr(t)}:{p} · 本地 {pkg}/")

        for var in (v_srv, v_pkg, v_path, v_name):
            var.trace_add("write", lambda *_a: refresh_prev())
        refresh_prev()

        def save_env(show: bool = True) -> bool:
            """把表单写回**当前渲染的那个环境对象**并落盘。

            必须按对象身份写:按下标写会在「删环境 / 环境移位」之后把旧表单
            写进邻居(实测 P0 串味)。对象已不在 envs 里 → 直接不写。
            控件可能已被销毁(切走后防抖定时器才触发),取值一律 try 兜住。
            """
            if not any(x is env for x in envs):
                return False
            try:
                nm = str(v_name.get() or "").strip() or f"环境 {idx + 1}"
                script = str(v_script.get() or "").strip()
                srv = v_srv.get()
                pkg = str(v_pkg.get() or "").strip() or "dist"
                path = str(v_path.get() or "").strip()
            except tk.TclError:
                return False
            env["name"] = nm
            env["script"] = script
            env["target"] = label2id.get(srv) or ""
            env["pkg"] = pkg
            env["path"] = path
            try:
                persist()
            except Exception as ex:              # noqa: BLE001
                try:
                    v_saved.set("保存失败")
                except tk.TclError:
                    pass
                self.log(f"❌ [{self.disp(e)}] 环境配置保存失败: {ex}", eid)
                return False
            if show:
                try:
                    v_saved.set("已保存 " + time.strftime("%H:%M:%S"))
                except tk.TclError:
                    pass
            # 环境名在表单里改的 → tab 标题跟着变(不重渲染,免得抢焦点)
            try:
                if 0 <= idx < len(tabs):
                    tabs[idx]["lab"].configure(text=nm)
            except tk.TclError:
                pass
            return True

        def debounce(fn):
            """输入即存:500ms 防抖(先取消上一个),连鼠标直接点左侧换项目都不丢。"""
            self._cancel_env_debounce()
            self._env_debounce = self.after(500, fn)

        def debounced_env_save():
            self._env_debounce = None
            save_env(show=True)

        def touch_save(_ev=None):
            save_env()

        for ent in (ent_name, ent_pkg, ent_path):
            ent.bind("<FocusOut>", touch_save, add="+")
            ent.bind("<Return>", touch_save)
            # 打字即存:只靠 <FocusOut> 不够 —— Tk 的 Label 不取焦点,
            # 鼠标点 tab/左栏列表不会让 Entry 失焦(见坑 51)
            ent.bind("<KeyRelease>",
                     lambda _e: debounce(debounced_env_save), add="+")

        # ---------------- 左栏:操作行(🩺 测试路径 + 🚀 发布 并排)
        #
        # v2.8:后端条目**不显示「🚀 发布」按钮** —— 后端自动发布还没做
        # (发布链路是前端静态目录专用:npm run build → tar → scp → 解压 → mv;
        #  后端是 jar,链路完全不一样)。留着按钮点下去必然失败
        # (在 Maven 工程目录里跑 `npm run start`),不如直接藏掉。
        # 「🩺 测试路径」保留:它只读探活远端目录,不建不写,还能用。
        brow = tk.Frame(lcol, bg=C_card)
        brow.pack(fill="x", pady=(10, 0))
        test_btn = self._ghost_btn(brow, "🩺 测试路径",
                                   lambda: do_test(), height=34, font_size=12)
        test_btn.pack(side="left")
        btn = None
        if backend:
            tk.Label(brow, text="后端暂不支持自动发布", bg=C_card,
                     fg=C_dim, font=("PingFang SC", 11)).pack(side="left",
                                                              padx=(10, 0))
        else:
            btn = self._btn(brow, "🚀 发布", lambda: do_deploy(), primary=True)
            btn.pack(side="left", padx=(8, 0))
        tk.Label(brow, textvariable=v_saved, bg=C_card, fg=C_mut,
                 font=("PingFang SC", 11)).pack(side="left", padx=(10, 0))

        # 测试结果回显(一行,过长自动折行)
        lab_test = tk.Label(lcol, textvariable=v_test, bg=C_card, fg=C_mut,
                            anchor="w", justify="left",
                            font=("PingFang SC", 11))
        lab_test.pack(fill="x", pady=(6, 0))
        self._bind_wrap(lcol, [lab_prev, lab_last, lab_test], minus=48)

        # 后端没有发布按钮 → 这里不登记(登记空 dict,
        # 免得 _deploy_tick / _do_env_deploy 拿到上一次的旧按钮)
        self._env_btns[eid] = {} if btn is None else {idx: btn}
        if btn is not None and eid in self._deploy_running:
            st = self._deploy_state.get(eid) or {}
            btn._set_enabled(False)
            btn._set_text(self._deploy_phase_text(st) if st.get("idx") == idx
                          else "其他环境发布中…")

        def set_test(ok, msg: str):
            """回填测试结果:None=进行中(灰) / True=通过(绿) / False=不通过(红)。"""
            try:
                v_test.set(msg)
                lab_test.configure(fg=C_mut if ok is None
                                   else (self._c("green") if ok
                                         else self._c("red")))
            except tk.TclError:
                pass

        def do_test():
            """🩺 测试路径:只读探活目标目录(存在?可写?空间够?),不建不写。

            直接复用发布链路的 `deploy_probe`,所以「测试通过」= 发布的
            第一步(探活)一定能过,不会出现「测试说没事、发布却卡在探活」。
            """
            if getattr(test_btn, "_testing", False):
                return                                  # 上一轮还没回来,防重入
            if not save_env(show=False):
                return
            item = env_job_item(env)
            if not (item.get("path") or "").strip():
                set_test(False, "还没填部署路径")
                return
            ok_cfg, err = deploy_validate(item.get("pkg"), item.get("path"))
            if not ok_cfg:
                set_test(False, f"配置不合法:{err}")
                return
            job = deploy_job(item, load_targets())
            if job is None:
                set_test(False, "还没选服务器(或该服务器已被删)")
                return
            # ghost 按钮是 Label,没有 _btn 那套 _set_text/_set_enabled,
            # 直接 configure;`_testing` 标记防重入(结果回填时复位)。
            test_btn._testing = True
            try:
                test_btn.configure(text="测试中…")
            except tk.TclError:
                pass
            set_test(None, "正在连服务器检查路径…")
            if not isinstance(getattr(self, "_env_test_btn", None), dict):
                self._env_test_btn = {}
            self._env_test_btn[eid] = {"idx": idx, "btn": test_btn,
                                       "set": set_test}

            def work():
                ok2, msg2 = deploy_probe(job, log=lambda _s: None)
                self.q.put(("env_probe", eid, idx, bool(ok2), msg2 or ""))

            self.bg(work)

        def do_deploy():
            # 先落盘:保证发出去的就是看到的
            if not save_env(show=False):
                return
            self._env_tab[eid] = idx
            self._do_env_deploy(e, idx)

        # ---------------- 右栏:网站账号(仅备忘;不参与发布)
        accounts = env.get("accounts")
        if not isinstance(accounts, list):
            accounts = []                        # 不为空时不往 env 里塞键

        acc_head = tk.Frame(rcol, bg=C_card)
        acc_head.pack(fill="x", pady=(2, 6))
        tk.Label(acc_head, text="网站账号", bg=C_card, fg=C_fg,
                 font=("PingFang SC", 12, "bold")).pack(side="left")
        tk.Label(acc_head, text="验收用 · 存 secrets.json(不进 git)",
                 bg=C_card, fg=C_dim,
                 font=("PingFang SC", 10)).pack(side="left", padx=(8, 0))

        acctable = tk.Frame(rcol, bg=C_card)
        acctable.pack(fill="x", pady=(0, 4))
        acctable.columnconfigure(0, weight=1)

        rows_host: list = []

        def commit_accounts() -> bool:
            """把账号三列的值写回 dict(只改内存,落盘交给 save_env)。

            防抖定时器可能在控件销毁后才跑 → 逐条 try 兜住。
            """
            dirty = False
            for acc, key, var in list(acc_fields):
                try:
                    v = str(var.get())
                except tk.TclError:
                    continue
                if str(acc.get(key) or "") != v:
                    acc[key] = v
                    dirty = True
            return dirty

        def save_accounts(msg: str = "") -> None:
            if accounts:
                env["accounts"] = accounts
            else:
                env.pop("accounts", None)
            try:
                commit_accounts()
                persist()
                if msg:
                    v_saved.set(msg)
            except Exception as ex:              # noqa: BLE001
                self.log(f"❌ [{self.disp(e)}] 账号保存失败: {ex}", eid)

        def acc_commit(acc: dict, key: str, var: tk.StringVar) -> None:
            try:
                val = str(var.get())
            except tk.TclError:
                return
            if str(acc.get(key) or "") == val:
                return
            acc[key] = val
            save_accounts("已保存 " + time.strftime("%H:%M:%S"))

        def add_account():
            commit_accounts()
            if not accounts:
                env["accounts"] = accounts
            accounts.append({"user": "", "password": "", "note": ""})
            save_accounts()
            render_accounts()

        def del_account(acc: dict):
            commit_accounts()
            try:
                accounts.remove(acc)
            except ValueError:
                return
            save_accounts("已删除")
            self.after(10, render_accounts)

        def bind_pw_toggle(ent: tk.Entry, lab: tk.Label) -> None:
            """显示/隐藏密码(纯 UI 状态,不落盘)。

            标签文案提示的是**下一次点击会做什么**:当前掩码(show="•")→ 下一次
            是「显示」明文,所以切到明文后标签要变成「隐藏」;反之亦然。
            """
            def _tog(_e=None):
                try:
                    hidden = bool(str(ent.cget("show")))   # True:当前是掩码
                    ent.configure(show="" if hidden else "•")
                    lab.configure(text="隐藏" if hidden else "显示")
                except tk.TclError:
                    pass
            lab.bind("<Button-1>", _tog)

        def render_accounts():
            """只重建「账号卡片」这一层:标题行和「+ 添加」在销毁范围之外。

            v2.7.2:每组账号是一张卡片,账号 / 密码 / 备注**竖排三个输入框**
            —— 右半屏只有半屏宽,横排三列会挤成三条窄缝,字都看不全。
            """
            for w in rows_host:
                _safe_destroy(w)
            rows_host.clear()
            acc_fields.clear()
            if not accounts:
                empty = tk.Label(acctable, text="还没有账号 · 点右上「+ 添加」",
                                 bg=C_card, fg=C_mut, anchor="w",
                                 font=("PingFang SC", 11))
                empty.pack(fill="x", padx=2, pady=10)
                rows_host.append(empty)
                return

            for ai, acc in enumerate(accounts):
                if not isinstance(acc, dict):
                    continue
                card = tk.Frame(acctable, bg=C_hi, bd=0,
                                highlightthickness=1,
                                highlightbackground=C_line)
                card.pack(fill="x", pady=(0, 8))
                card.columnconfigure(1, weight=1)
                rows_host.append(card)

                # 卡头:序号居左,复制 / 删除居右(不占输入框宽度)
                hd = tk.Frame(card, bg=C_hi)
                hd.grid(row=0, column=0, columnspan=2, sticky="we",
                        padx=10, pady=(8, 2))
                tk.Label(hd, text=f"账号 {ai + 1}", bg=C_hi, fg=C_mut,
                         font=("PingFang SC", 10, "bold")).pack(side="left")
                ops = tk.Frame(hd, bg=C_hi)
                ops.pack(side="right")

                def mk_copy(a, key, label):
                    """复制**单个**字段(账号 / 密码各一枚,不再拼成 "user / pw")。

                    闭包必须落在 `mk_copy` 自己的作用域里:写在 for 循环体内
                    的话,`do_copy`/`btns` 会被下一次迭代覆盖 —— 点第 1 张卡片
                    的按钮拷出来的是第 2 张的账号(晚绑定,实测踩到)。
                    """
                    held: dict = {}

                    def _do():
                        txt = str(a.get(key) or "")
                        try:
                            self.clipboard_clear()
                            self.clipboard_append(txt)
                        except tk.TclError:
                            return
                        b = held.get("btn")
                        if b is None:
                            return
                        try:
                            b.configure(text="已复制", fg=self._c("green"))
                        except tk.TclError:
                            return

                        def restore():
                            try:
                                b.configure(text=label, fg=C_mut)
                            except tk.TclError:
                                pass
                        self.after(800, restore)

                    bb = self._ghost_btn(ops, label, _do, height=22)
                    bb.pack(side="left", padx=(0, 4))
                    held["btn"] = bb
                    return bb

                mk_copy(acc, "user", "复制账号")
                mk_copy(acc, "password", "复制密码")
                self._ghost_btn(ops, "×", lambda a=acc: del_account(a),
                                danger=True, height=22).pack(side="left")

                # 竖排三行:账号 / 密码(可显隐) / 备注
                for ri, (key, txt) in enumerate(
                        (("user", "账号"), ("password", "密码"),
                         ("note", "备注")), start=1):
                    tk.Label(card, text=txt, bg=C_hi, fg=C_mut, anchor="e",
                             font=("PingFang SC", 10)).grid(
                                 row=ri, column=0, sticky="e",
                                 padx=(10, 8), pady=3)
                    var = tk.StringVar(value=str(acc.get(key) or ""))
                    wrap = tk.Frame(card, bg=C_hi)
                    wrap.grid(row=ri, column=1, sticky="we", padx=(0, 10),
                              pady=3)
                    wrap.columnconfigure(0, weight=1)
                    ent = mkent(wrap, var, font=("PingFang SC", 12))
                    ent.grid(row=0, column=0, sticky="we", ipady=1)
                    if key == "password":
                        ent.configure(show="•")
                        tog = tk.Label(wrap, text="显示", bg=C_hi, fg=C_mut,
                                       cursor="hand2",
                                       font=("PingFang SC", 10))
                        tog.grid(row=0, column=1, padx=(4, 0))
                        bind_pw_toggle(ent, tog)
                    acc_fields.append((acc, key, var))
                    ent.bind("<FocusOut>",
                             lambda _e, a=acc, k=key, v=var: acc_commit(a, k, v),
                             add="+")
                    ent.bind("<Return>",
                             lambda _e, a=acc, k=key, v=var: acc_commit(a, k, v))
                    ent.bind("<KeyRelease>",
                             lambda _e, a=acc, k=key, v=var:
                             debounce(lambda: (acc_commit(a, k, v))), add="+")

        self._ghost_btn(acc_head, "+ 添加", add_account,
                        height=24).pack(side="right")
        render_accounts()

        def flush_all() -> bool:
            """重建详情区 / 关窗前的唯一收口:账号三列 + 表单全部落盘。"""
            commit_accounts()
            return save_env(show=False)

        # 登记「悬挂保存」钩子:详情区下一次重建(切 tab / 切项目 / 切分组 /
        # 关窗)之前,select() / _on_close() 会先调它把未失焦的编辑落盘。
        self._env_flush = flush_all
        draw_tabs()

    def _do_env_deploy(self, e: dict, idx: int):
        """执行某个环境:置按钮态 → 后台跑 deploy_static([单个配置项]) → 队列回 UI。"""
        eid = e["id"]
        # 后端自动发布还没做(UI 上没有发布按钮),这里再挡一道,
        # 防止别的路径误触发 → 在 Maven 工程里跑 npm run start 必失败。
        if is_backend(e):
            self.log(f"⚠️ [{self.disp(e)}] 后端暂不支持自动发布", eid)
            return
        envs = e.get("envs") or []
        if not (0 <= idx < len(envs)):
            return
        env = envs[idx]
        nm = env_name(env, idx)
        if eid in self._deploy_running:
            self.log(f"⚠️ [{self.disp(e)}] 已有发布任务在执行,请等它结束", eid)
            return
        script = str(env.get("script") or "").strip()
        if not script:
            self.log(f"⚠️ [{self.disp(e)}] 环境「{nm}」还没选打包脚本", eid)
            return
        targets = load_targets()
        t = next((x for x in targets if x.get("id") == env.get("target")), None)
        if t is None:
            self.log(f"⚠️ [{self.disp(e)}] 环境「{nm}」的服务器不存在(可能已被删)", eid)
            return
        item = env_job_item(env)
        if not (item.get("path") or "").strip():
            self.log(f"⚠️ [{self.disp(e)}] 环境「{nm}」还没填部署路径", eid)
            return
        ok, err = deploy_validate(item.get("pkg"), item.get("path"))
        if not ok:
            self.log(f"⚠️ [{self.disp(e)}] 环境「{nm}」配置不合法:{err}", eid)
            return

        self._deploy_running.add(eid)
        self._deploy_state[eid] = {"t0": time.time(), "pct": 0, "phase": "build",
                                   "idx": idx}
        # 同一条目所有环境的发布按钮都置灰(并发互斥,防重入)
        for _i, b in (self._env_btns.get(eid) or {}).items():
            try:
                if b.winfo_exists():
                    b._set_enabled(False)
                    if _i == idx:
                        b._set_text("构建中 0s")
                    else:
                        b._set_text("其他环境发布中…")
            except tk.TclError:
                pass
        self.log(f"🚀 [{self.disp(e)}] 发布环境「{nm}」:{script} → "
                 f"{target_ssh(t)}:{item.get('path')}", eid)

        def job():
            res = deploy_static(
                e, script, [dict(item)], targets=targets,
                # 分步明细进项目 tab;「开始/结束」结论在 _handle_msg 里补
                log=lambda s: self.q.put(("log", s, eid)),
                confirm=self._deploy_confirm,
                progress=lambda p: self.q.put(("deploy_progress", eid, p)))
            self.q.put(("deploy_done", eid, script, res, idx))

        self.bg(job)

    def _deploy_confirm(self, question: str, ctx: dict | None = None) -> bool:
        """发布链路的确认回调(由后台线程调用;目录特征检查与 Git 检查共用)。

        把请求丢回主线程弹窗并阻塞等待。ctx 里带 item(目录特征检查)时
        「不再询问」由 _handle_msg 写回配置项;ctx kind=git(发布前 Git 检查)
        则换标题、并且**不提供**「不再询问」—— 脏工作区每次都该提醒。
        无 UI 时(纯逻辑调用)不会走到这里。
        """
        ctx = ctx or {}
        is_git = ctx.get("kind") == "git"
        evt = threading.Event()
        box: dict = {"ok": False, "dont_ask": False, "ctx": ctx,
                     "title": ("⚠️ 发布前 Git 检查" if is_git
                               else "⚠️ 目标目录内容不像前端"),
                     "allow_dont_ask": not is_git}
        self.q.put(("deploy_confirm", question, evt, box))
        if not evt.wait(timeout=300):
            return False
        return bool(box["ok"])

    def _ask_deploy_confirm(self, question: str,
                            title: str = "⚠️ 目标目录内容不像前端",
                            allow_dont_ask: bool = True) -> tuple[bool, bool]:
        """发布确认弹窗。返回 (是否继续, 是否不再询问)。

        v3.0:标题与「不再询问」勾选由调用方决定 —— Git 检查的确认
        (标题「发布前 Git 检查」)不出现该勾选,避免「勾一次以后
        脏工作区也不提醒」这种危险的记忆。
        """
        dlg = tk.Toplevel(self)
        dlg.title("确认发布")
        dlg.configure(bg=self._c("card"))
        dlg.transient(self)
        dlg.grab_set()
        # 宽度锁 580,高度建成后按内容量:`question` 是 wraplength=520 的长句,
        # 环境/路径一长就会换行变高,定高 300 会把「取消 / 仍要发布」挤出去。
        dlg.resizable(False, True)
        dlg.minsize(580, 300)
        tk.Label(dlg, text=title, bg=self._c("card"),
                 fg=self._c("warn"),
                 font=("PingFang SC", 13, "bold")).pack(anchor="w", padx=18, pady=(16, 6))
        tk.Label(dlg, text=question, bg=self._c("card"), fg=self._c("fg"),
                 justify="left", wraplength=520,
                 font=("PingFang SC", 11)).pack(anchor="w", padx=18)
        v_dont = tk.BooleanVar(value=False)
        if allow_dont_ask:
            tk.Checkbutton(dlg, variable=v_dont, text="不再询问(该目标以后直接发布)",
                           bg=self._c("card"), fg=self._c("fg"),
                           activebackground=self._c("card"), activeforeground=self._c("fg"),
                           selectcolor=self._c("log_bg"), bd=0, highlightthickness=0,
                           font=("PingFang SC", 11)).pack(anchor="w", padx=18, pady=(10, 0))
        res = {"ok": False}
        row = tk.Frame(dlg, bg=self._c("card"))
        row.pack(fill="x", padx=18, pady=(12, 16))

        def ok():
            res["ok"] = True
            dlg.destroy()

        self._btn(row, "取消", dlg.destroy).pack(side="right", padx=(8, 0))
        self._btn(row, "仍要发布", ok, danger=True).pack(side="right")
        dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
        dlg.update_idletasks()
        need = dlg.winfo_reqheight()
        max_h = int(dlg.winfo_screenheight() * 0.9)
        self._center(dlg, 580, max(300, min(need, max_h)))
        dlg.wait_window()      # 阻塞主线程等用户回答(后台发布线程同步等待)
        return res["ok"], bool(v_dont.get())

    def _deploy_tick(self):
        """每秒刷新发布按钮文案(构建中 12s / 上传 62% / 切换中)。"""
        for eid in list(self._deploy_running):
            st = self._deploy_state.get(eid) or {}
            btn = (self._env_btns.get(eid) or {}).get(st.get("idx"))
            if btn is None:
                continue
            try:
                if btn.winfo_exists():
                    btn._set_text(self._deploy_phase_text(st))
            except tk.TclError:
                pass
        self.after(1000, self._deploy_tick)
