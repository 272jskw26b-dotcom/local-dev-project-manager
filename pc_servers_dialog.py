#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pc_servers_dialog —— 顶栏「⚙ 服务器」面板(ServersMixin)

负责:服务器(targets)CRUD 的独立 Toplevel —— 左自绘列表(状态点三态 + 名称 +
ssh 串)、右表单(名称 / host / 端口 / 用户 / 认证方式 / 密码)、失焦自动保存、
单条与全量连通性测试、刷新后通知主窗口重载列表。密码落盘时由 save_config()
抽到同目录 secrets.json(gitignore,不进 git),本模块只在内存里读写
t["password"]。

依赖 App 提供的属性 —— 本模块只读:
    self.cfg       注册表内存副本(targets 在这里增删改)
    self.entries   左栏条目(面板只用到它的数量/顺序做刷新)
    self.sel_id    当前选中的条目 id(关面板后要 select 回去)
本模块自己建立:
    self._srv_dlg  当前打开的面板(单例,重复点只 lift)
并调用 App 的: _c() / _btn() / _ghost_btn() / _center() / select() / log()
"""

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from pc_const import AUTH_KEY, AUTH_LABELS, AUTH_PW
from pc_core import (
    DEFAULT_SSH_USER, check_target, new_target_id, save_config, target_auth,
    target_host, target_password, target_refs, target_ssh, target_user,
)
from pc_widgets import _ServerList


class ServersMixin:
    """服务器管理面板(单例 Toplevel)。"""

    def _servers_dialog(self, open_new: bool = False):
        """顶栏「⚙ 服务器」:左自绘列表 + 右表单,失焦自动保存(无保存按钮)。"""
        old = self._srv_dlg
        if old is not None:
            try:
                if old.winfo_exists():
                    old.lift()
                    old.focus_force()
                    if open_new:
                        getattr(old, "_start_new", lambda: None)()
                    return
            except tk.TclError:
                pass
            self._srv_dlg = None

        dlg = tk.Toplevel(self)
        dlg.title("服务器管理 · 主机 / 账号 / 认证")
        dlg.configure(bg=self._c("card"))
        dlg.transient(self)
        dlg.grab_set()
        # 宽度锁死 720(右栏表单是按 700 余宽排的,别动),高度**建成后按内容量**:
        # 自 v2.4 起表单多了「用户 / 认证方式 / 密码」三行,下面又挂三行元信息卡,
        # 内容高度早就超过写死的 560,而按钮行又排在最后 → 整排被挤出视窗裁掉。
        dlg.resizable(False, True)
        dlg.minsize(720, 560)
        self._srv_dlg = dlg

        targets = self.cfg.setdefault("targets", [])
        if not isinstance(targets, list):
            targets = []
            self.cfg["targets"] = targets

        state: dict = {"sel": targets[0].get("id") if targets else None, "mode": "edit"}
        dq: queue.Queue = queue.Queue()

        top = tk.Frame(dlg, bg=self._c("card"))
        top.pack(fill="x", padx=16, pady=(12, 6))
        tk.Label(top, text="服务器管理", bg=self._c("card"), fg=self._c("fg"),
                 font=("PingFang SC", 14, "bold")).pack(side="left")
        tk.Label(top, text="只存主机 / 用户名 / 认证,不存路径 —— 路径在「环境配置」里填",
                 bg=self._c("card"), fg=self._c("mut"),
                 font=("PingFang SC", 10)).pack(side="left", padx=(10, 0), pady=(3, 0))

        # 按钮行做成整窗宽度(右栏只有 700-240-14 宽,5 个按钮塞不下,实测「关闭」
        # 被切掉),并且**必须在 body 之前 pack** —— pack 先到先得,晚 pack 的一方
        # 会被挤没;贴底后即使内容超高,被压缩的也是中间区域,按钮行永远可见。
        brow = tk.Frame(dlg, bg=self._c("card"))
        brow.pack(fill="x", padx=16, pady=(6, 12), side="bottom")

        body = tk.Frame(dlg, bg=self._c("card"))
        body.pack(fill="both", expand=True, padx=16, pady=(0, 0))

        left = tk.Frame(body, bg=self._c("panel"), width=240)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        slist = _ServerList(left, theme_getter=self._c, bg=self._c("panel"))
        hint = tk.Label(left, text="", bg=self._c("panel"), fg=self._c("mut"),
                        justify="left", wraplength=210, font=("PingFang SC", 10))
        slist.pack(fill="both", expand=True, padx=(0, 2), pady=4)

        right = tk.Frame(body, bg=self._c("card"))
        right.pack(side="left", fill="both", expand=True, padx=(14, 0))

        form = tk.Frame(right, bg=self._c("card"))
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        v_name = tk.StringVar()
        v_ssh = tk.StringVar()
        v_port = tk.StringVar(value="22")
        ents: dict[str, tk.Entry] = {}

        def mkfield(row: int, label: str, var: tk.StringVar, width: int = 30,
                    stretch: bool = True) -> tk.Entry:
            # v2.9:标签统一右对齐(原来 anchor="w" 左对齐,与右侧输入框错位)。
            tk.Label(form, text=label, bg=self._c("card"), fg=self._c("mut"),
                     width=6, anchor="e",
                     font=("PingFang SC", 11)).grid(row=row, column=0, sticky="e",
                                                   padx=(0, 10), pady=6)
            ent = tk.Entry(form, textvariable=var, width=width, relief="flat", bd=0,
                           bg=self._c("log_bg"), fg=self._c("fg"),
                           insertbackground=self._c("fg"), font=("Menlo", 11),
                           highlightthickness=1,
                           highlightbackground=self._c("line"),
                           highlightcolor=self._c("accent"))
            ent.grid(row=row, column=1, sticky=("we" if stretch else "w"), pady=6)
            ent.bind("<FocusOut>", lambda _e: commit())     # 失焦自动保存
            ent.bind("<Return>", lambda _e: commit())
            return ent

        ents["name"] = mkfield(0, "名称", v_name)
        # SSH 拆成「主机 + 用户名」(v2.4):用户踩过只填 IP,ssh 就拿本机当前
        # 用户名去登录 → Permission denied。默认 root,可以改。
        v_host = tk.StringVar()
        v_user = tk.StringVar(value=DEFAULT_SSH_USER)
        ents["host"] = mkfield(1, "主机", v_host)
        # v2.9:用户名 / 端口原来宽度只有主机的一半,并排放参差不齐。
        # 统一成同一档宽度(20),端口再窄一点但仍与用户名字号一致。
        ents["user"] = mkfield(2, "用户名", v_user, width=20)
        ents["port"] = mkfield(3, "端口", v_port, width=20)

        # ---- 认证方式:密钥免密 / 密码(v3.0 起密码落盘进 secrets.json,不进 git)
        v_auth = tk.StringVar(value=AUTH_LABELS[AUTH_KEY])
        tk.Label(form, text="认证", bg=self._c("card"), fg=self._c("mut"),
                 width=6, anchor="e",
                 font=("PingFang SC", 11)).grid(row=4, column=0, sticky="e",
                                               padx=(0, 10), pady=6)
        cb_auth = ttk.Combobox(
            form, textvariable=v_auth, state="readonly", width=22,
            values=[AUTH_LABELS[AUTH_KEY], AUTH_LABELS[AUTH_PW]],
            style="PC.TCombobox", font=("PingFang SC", 11))
        cb_auth.grid(row=4, column=1, sticky="w", pady=6)
        cb_auth.bind("<<ComboboxSelected>>", lambda _e: on_auth_change())

        # ---- 密码:只在「密码」认证时出现,明文可编辑(用户要求存本地别遮罩)
        pw_label = tk.Label(form, text="密码", bg=self._c("card"), fg=self._c("mut"),
                            width=6, anchor="e", font=("PingFang SC", 11))
        pw_label.grid(row=5, column=0, sticky="e", padx=(0, 10), pady=6)
        pwbox = tk.Frame(form, bg=self._c("card"))
        pwbox.grid(row=5, column=1, sticky="we", pady=6)
        v_pw = tk.StringVar()
        ent_pw = tk.Entry(pwbox, textvariable=v_pw, width=22,
                          relief="flat", bd=0,
                          bg=self._c("log_bg"), fg=self._c("fg"),
                          insertbackground=self._c("fg"), font=("Menlo", 11),
                          highlightthickness=1,
                          highlightbackground=self._c("line"),
                          highlightcolor=self._c("accent"))
        ent_pw.pack(side="left", fill="x", expand=True)
        ent_pw.bind("<FocusOut>", lambda _e: commit())
        ent_pw.bind("<Return>", lambda _e: commit())
        self._btn(pwbox, "清空", lambda: on_pw_clear()).pack(side="left", padx=(8, 0))

        v_pwhint = tk.StringVar(value="")
        lbl_pwhint = tk.Label(form, textvariable=v_pwhint, bg=self._c("card"),
                              fg=self._c("mut"), anchor="w", justify="left",
                              wraplength=400, font=("PingFang SC", 9))
        lbl_pwhint.grid(row=6, column=0, columnspan=2, sticky="w", pady=(0, 2))

        v_preview = tk.StringVar(value="")
        tk.Label(form, textvariable=v_preview, bg=self._c("card"), fg=self._c("accent"),
                 anchor="w", font=("Menlo", 10)).grid(row=7, column=0, columnspan=2,
                                                      sticky="w", pady=(2, 0))
        tk.Label(form, text="主机填 IP 或域名(不要写 user@);端口非 22 会附在 ssh 串后面。",
                 bg=self._c("card"), fg=self._c("mut"), justify="left",
                 font=("PingFang SC", 9)).grid(row=8, column=0, columnspan=2,
                                               sticky="w", pady=(2, 6))

        # ---- 认证 / 密码 的交互逻辑(闭包;current()/save_all() 在后面定义,
        #      但都是事件触发才调用,顺序无碍)

        def cur_auth() -> str:
            """下拉框当前值 → "password" / "key"。"""
            return AUTH_PW if v_auth.get() == AUTH_LABELS[AUTH_PW] else AUTH_KEY

        def refresh_preview(*_a):
            host = (v_host.get() or "").strip()
            user = (v_user.get() or "").strip() or DEFAULT_SSH_USER
            if not host:
                v_preview.set("→ (还没填主机)")
                return
            try:
                port = int((v_port.get() or "22").strip() or 22)
            except ValueError:
                port = 22
            suffix = f":{port}" if port != 22 else ""
            v_preview.set(f"→ {user}@{host}{suffix}")

        v_host.trace_add("write", lambda *_a: refresh_preview())
        v_user.trace_add("write", lambda *_a: refresh_preview())
        v_port.trace_add("write", lambda *_a: refresh_preview())

        def sync_pw_rows():
            """密码行只在「密码」认证 + 有选中服务器时出现。"""
            show = (cur_auth() == AUTH_PW
                    and (state["mode"] == "new" or current() is not None))
            for w in (pw_label, pwbox, lbl_pwhint):
                if show:
                    w.grid()
                else:
                    w.grid_remove()
            v_pwhint.set(
                "密码存在同目录 secrets.json(0600、已 gitignore,不进 git);"
                "内存里可明文编辑,重启仍能连。"
                if show else "")

        def on_auth_change():
            """切换认证方式:走统一的 commit() 落盘(主机/用户名/端口/密码一起存)。"""
            if state["mode"] == "new" or current() is None:
                sync_pw_rows()
                return
            commit()
            sync_pw_rows()
            refresh_meta()
            t = current()
            if t is not None:
                self.log(f"🔐 「{t.get('name') or t.get('id')}」认证方式:"
                         f"{'密码(存本地 secrets.json)' if cur_auth() == AUTH_PW else '密钥免密'}")

        def on_pw_clear():
            """清空密码:删掉 targets[].password 并回落到密钥认证。"""
            t = current()
            v_pw.set("")
            if t is None:
                return
            name = t.get("name") or t.get("id")
            t.pop("password", None)
            t["auth"] = "key"
            v_auth.set(AUTH_LABELS[AUTH_KEY])
            save_all()
            sync_pw_rows()
            refresh_meta()
            self.log(f"🔑 已清除「{name}」的密码,认证回落到密钥")

        meta = tk.Frame(right, bg=self._c("deploy_bg"))
        meta.pack(fill="x", pady=(6, 0))
        v_auth_meta = tk.StringVar(value="")
        v_refs = tk.StringVar(value="")
        v_check = tk.StringVar(value="")
        tk.Label(meta, textvariable=v_auth_meta, bg=self._c("deploy_bg"),
                 fg=self._c("fg"), anchor="w", justify="left", wraplength=400,
                 font=("PingFang SC", 10)).pack(fill="x", padx=10, pady=(8, 0))
        tk.Label(meta, textvariable=v_refs, bg=self._c("deploy_bg"), fg=self._c("fg"),
                 anchor="w", justify="left", wraplength=400,
                 font=("PingFang SC", 10)).pack(fill="x", padx=10, pady=(4, 2))
        tk.Label(meta, textvariable=v_check, bg=self._c("deploy_bg"), fg=self._c("mut"),
                 anchor="w", font=("Menlo", 10)).pack(fill="x", padx=10, pady=(0, 8))

        def tgt(tid):
            return next((t for t in targets if t.get("id") == tid), None)

        def current():
            return tgt(state["sel"])

        def refs_of(tid):
            return target_refs(self.entries, tid)

        def _auth_meta_text(t: dict) -> str:
            """元信息里那行「认证:…」—— 密码的存放位置要说准,别再指向 projects.json。"""
            if target_auth(t) == AUTH_PW:
                return "认证:密码(存同目录 secrets.json,不进 git)"
            if target_password(t):
                return "认证:密钥免密(留有密码但标记为 auth=key,当前走密钥)"
            return "认证:密钥免密(连不上就检查服务器上的 ~/.ssh/authorized_keys)"

        def save_all() -> bool:
            try:
                save_config(self.cfg)
                return True
            except Exception as ex:  # noqa: BLE001
                self.log(f"❌ 服务器配置保存失败: {ex}")
                return False

        def sync_enabled():
            editable = state["mode"] == "new" or current() is not None
            st = "normal" if editable else "disabled"
            for ent in ents.values():
                ent.configure(state=st)
            ent_pw.configure(state=st)
            cb_auth.configure(state=("readonly" if editable else "disabled"))
            sync_pw_rows()

        def refresh_list():
            rows = []
            for t in targets:
                lc = t.get("last_check") or {}
                stt = "none"
                # 主机为空 → 一律灰点,不看 last_check 的旧绿灯。
                # 坑:last_check 是**上次测通时**的缓存,而用户在表单里把 host
                # 清空后并不会自动重测,于是列表还显示绿 —— 但发布时
                # deploy_probe 必然失败(「读不到磁盘可用空间」)。
                if not target_host(t):
                    stt = "none"
                elif lc:
                    stt = "ok" if lc.get("ok") else "fail"
                rows.append({"id": t.get("id"), "name": t.get("name") or t.get("id"),
                             "ssh": target_ssh(t), "state": stt})
            slist.set_rows(rows, sel=state["sel"])
            if targets:
                hint.pack_forget()
            else:
                hint.configure(text="还没有服务器\n点 + 新建 添加第一台")
                hint.pack(fill="x", padx=10, pady=(8, 0), before=slist)

        def refresh_meta():
            t = current()
            if t is None:
                v_auth_meta.set("还没有选中服务器")
                v_refs.set("还没有选中服务器")
                v_check.set("—")
                return
            v_auth_meta.set(_auth_meta_text(t))
            rs = refs_of(t.get("id"))
            if rs:
                shown = "、".join(f"{a}/{b}" for a, b in rs[:3])
                more = f" 等 {len(rs)} 个" if len(rs) > 3 else ""
                v_refs.set(f"被 {len(rs)} 个脚本配置引用:{shown}{more}")
            else:
                v_refs.set("被 0 个脚本配置引用")
            lc = t.get("last_check") or {}
            if not target_host(t):
                # 没填主机:旧结论一律作废,别显示"上次测试通过"
                v_check.set("还没填主机,连不上")
            elif lc:
                v_check.set(f"上次测试 {lc.get('time') or '?'} "
                            f"{'通过' if lc.get('ok') else '失败'}"
                            f"({(lc.get('ms') or 0) / 1000:.1f}s)")
            else:
                v_check.set("上次测试:还没测过")

        def fill(t):
            v_name.set((t or {}).get("name") or "")
            # 旧数据(ssh 含 user@)自动拆成 主机 + 用户名 两个框
            v_host.set(target_host(t or {}))
            v_user.set(target_user(t or {}))
            v_port.set(str((t or {}).get("port") or 22))
            v_auth.set(AUTH_LABELS[target_auth(t or {})])
            v_pw.set(target_password(t or {}))
            refresh_preview()
            sync_pw_rows()

        def commit() -> bool:
            """失焦自动保存:把表单写回当前 target(所以没有保存按钮)。"""
            name = v_name.get().strip()
            ssh = (v_host.get() or "").strip()
            user = (v_user.get() or "").strip() or DEFAULT_SSH_USER
            auth = cur_auth()
            try:
                port = int(v_port.get().strip() or 22)
                if not (1 <= port <= 65535):
                    raise ValueError
            except ValueError:
                port = 22
                v_port.set("22")
            if state["mode"] == "new":
                if not (name or ssh):
                    return False        # 空表单:不创建
                tid = new_target_id(targets)
                t = {"id": tid, "name": name or tid, "ssh": ssh,
                     "user": user, "port": port}
                if auth == AUTH_PW:
                    pw = v_pw.get()
                    if pw.strip():
                        t["password"] = pw
                else:
                    t["auth"] = "key"
                targets.append(t)
                state["sel"] = tid
                state["mode"] = "edit"
                save_all()
                refresh_list()
                refresh_meta()
                sync_enabled()
                return True
            t = current()
            if t is None:
                return False
            pw = v_pw.get()
            changed = ((t.get("name") or t.get("id")) != (name or t.get("id"))
                       or (t.get("ssh") or "") != ssh
                       or str(t.get("user") or DEFAULT_SSH_USER) != user
                       or int(t.get("port") or 22) != port
                       or target_password(t) != pw
                       or target_auth(t) != auth)
            if not changed:
                return False
            # 连接参数变了 → 旧的 last_check(上次测通的结果)不再作废,
            # 留着会让状态点显示过期的绿灯(甚至 host 被清空也还是绿的)。
            conn_changed = ((t.get("ssh") or "") != ssh
                            or str(t.get("user") or DEFAULT_SSH_USER) != user
                            or int(t.get("port") or 22) != port
                            or target_password(t) != pw
                            or target_auth(t) != auth)
            t["name"] = name or t.get("id")
            t["ssh"] = ssh
            t["user"] = user
            t["port"] = port
            if auth == AUTH_PW:
                if pw.strip():
                    t["password"] = pw
                else:
                    t.pop("password", None)
                t.pop("auth", None)
            else:
                # 显式「密钥」:密码留在 json 里(用户可能临时切回来),用 auth 标记
                t["auth"] = "key"
            if conn_changed:
                t.pop("last_check", None)     # 旧的连通性结论作废
            save_all()
            refresh_list()
            refresh_meta()
            sync_pw_rows()
            return True

        def select(tid):
            commit()
            state["sel"] = tid
            state["mode"] = "edit"
            fill(current())
            refresh_list()
            refresh_meta()
            sync_enabled()

        def start_new():
            commit()
            state["mode"] = "new"
            state["sel"] = None
            fill(None)
            refresh_list()
            v_refs.set("新建:填好名称/SSH 后点到别处即自动保存")
            v_check.set("—")
            sync_enabled()
            try:
                ents["name"].focus_set()
            except tk.TclError:
                pass

        def do_delete():
            t = current()
            if t is None:
                self.log("⚠️ 先选中一台服务器再删除")
                return
            tid = t.get("id")
            rs = refs_of(tid)
            if rs:
                lst = "\n".join(f"  · {a}/{b}" for a, b in rs)
                if not messagebox.askyesno(
                        "删除服务器",
                        f"「{t.get('name')}」被 {len(rs)} 个脚本配置引用:\n{lst}\n\n"
                        f"删除后这些发布配置会一并移除(清单会写进日志),继续?",
                        parent=dlg):
                    return
                self.log(f"🗑 删除服务器「{t.get('name')}」,一并移除 {len(rs)} 个环境配置:"
                         + "、".join(f"{a}/{b}" for a, b in rs))
                for ent in self.entries:
                    evs = ent.get("envs")
                    if isinstance(evs, list):
                        ent["envs"] = [x for x in evs
                                       if not (isinstance(x, dict)
                                               and x.get("target") == tid)]
                    dp = ent.get("deploy")
                    if not isinstance(dp, dict):
                        continue
                    for sc in list(dp):
                        kept = [it for it in (dp.get(sc) or [])
                                if not (isinstance(it, dict) and it.get("target") == tid)]
                        if kept:
                            dp[sc] = kept
                        else:
                            dp.pop(sc, None)
                    if not dp:
                        ent.pop("deploy", None)
            elif not messagebox.askyesno(
                    "删除服务器", f"确定删除「{t.get('name')}」({target_ssh(t)})?",
                    parent=dlg):
                return
            # 删服务器 → secrets.json 的 targets 段会在下次保存时自动清掉
            # (extract_secrets 只收「还在注册表里」的对象),无需额外清理
            targets.remove(t)
            state["sel"] = targets[0].get("id") if targets else None
            state["mode"] = "edit"
            save_all()
            fill(current())
            refresh_list()
            refresh_meta()
            sync_enabled()

        def test_one(tid=None):
            t = current() if tid is None else tgt(tid)
            if t is None:
                self.log("⚠️ 先选中一台服务器再测试连接")
                return
            self.log(f"🔍 测试连接 {t.get('name')} ({target_ssh(t)}) …")

            def job():
                dq.put(("check", t.get("id"), check_target(t)))

            threading.Thread(target=job, daemon=True).start()

        def test_all():
            items_all = list(targets)
            if not items_all:
                self.log("⚠️ 还没有服务器可测")
                return
            dq.put(("log", f"🔍 串行测试 {len(items_all)} 台服务器(并发 SSH 容易卡死)…"))

            def job():
                for t in items_all:
                    dq.put(("check", t.get("id"), check_target(t)))

            threading.Thread(target=job, daemon=True).start()

        def on_close():
            commit()
            save_all()
            try:
                dlg.grab_release()
                dlg.destroy()
            except tk.TclError:
                pass
            self._srv_dlg = None
            # 服务器删改会影响详情区的环境配置(被删的目标不能再发)
            self._env_tab.pop(self.sel_id, None)
            self.select(self.sel_id)

        def handle(m):
            kind = m[0]
            if kind == "log":
                self.log(m[1])
            elif kind == "check":
                _, tid, r = m
                t = tgt(tid)
                if t is None:
                    return
                t["last_check"] = {"ok": bool(r.get("ok")),
                                   "time": r.get("time") or "",
                                   "ms": int(r.get("ms") or 0)}
                save_all()                  # 单台测试也写盘,重启后仍看得到
                refresh_list()
                refresh_meta()
                self.log(f"{'✅' if r.get('ok') else '❌'} {t.get('name')} "
                         f"{target_ssh(t)} {t['last_check']['ms']}ms "
                         f"{str(r.get('hint') or r.get('out') or '')[:140]}")

        def poll():
            if not dlg.winfo_exists():
                return
            try:
                while True:
                    m = dq.get_nowait()
                    try:
                        handle(m)
                    except Exception as ex:  # noqa: BLE001 — 单条消息异常不能停轮询
                        self.log(f"❌ 服务器面板消息处理失败(已跳过): {ex}")
            except queue.Empty:
                pass
            dlg.after(120, poll)

        # v2.9:按钮重新分组 —— 原来「删除」(红色危险)夹在「+新建」和「测试连接」
        # 中间,一排按钮里最扎眼的红点正好在「新建」旁边,容易误点。
        # 现在左侧只放「新建 + 测试」这类常规动作,「删除」挪到右侧独立分组,
        # 视觉上与「关闭」相邻但仍分开。
        self._btn(brow, "+ 新建", start_new).pack(side="left")
        self._btn(brow, "测试连接", lambda: test_one()).pack(side="left", padx=6)
        self._btn(brow, "全部测试", test_all).pack(side="left", padx=6)
        self._btn(brow, "关闭", on_close, primary=True).pack(side="right")
        self._btn(brow, "删除", do_delete, danger=True).pack(side="right", padx=(0, 8))

        slist.on_select = select
        dlg.protocol("WM_DELETE_WINDOW", on_close)
        dlg._start_new = start_new       # 供「+ 新建服务器…」复用已开着的窗口

        refresh_list()
        if open_new or not targets:
            start_new()
        else:
            fill(current())
            refresh_meta()
            sync_enabled()

        # 所有控件(含贴底的按钮行 + 初始元信息)都建好之后,按实际需要的高度定尺寸:
        # 内容再高也不裁(顶到屏幕 0.9 高为止,更高的部分靠纵向拉伸自己拖)。
        dlg.update_idletasks()
        need = dlg.winfo_reqheight()
        max_h = int(dlg.winfo_screenheight() * 0.9)
        self._center(dlg, 720, max(560, min(need, max_h)))
        dlg.after(120, poll)
