#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pc_widgets —— Project Console 自绘控件与绘图辅助

包含:
    _DotList        左栏自绘列表(Canvas 圆点状态灯 + 折叠树)
    _ScrollList     Canvas 自绘列表通用骨架(选中/悬停/按需滚动条/命中测试)
    _ServerList     _ScrollList 子类:状态点 + 名称 + ssh 串
    _ScriptList     _ScrollList 子类:脚本名 + 备注
    _make_btn_png   预渲染渐变按钮底图(纯标准库 zlib/struct 逐像素画 PNG,
                    无 PIL 依赖)
    _btn_cache      按钮底图缓存(切主题时由 App 清空)
    _hex2rgb / _rgb2hex / _lerp / _rounded_poly / _inside_rounded / _encode_png
                    绘图辅助
"""

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# ---------------------------------------------------------------- 自绘列表


class _DotList(tk.Canvas):
    """左栏自绘列表(Canvas 实现)。

    用 Listbox 做行首状态灯只能靠 emoji,尺寸不可控(用户嫌大);
    Canvas 圆点半径自定(4px,约为 emoji 一半),行内容不带小图标。
    一级仓节点行首带 ▾/▸ 折叠箭头,点箭头折叠/展开,点其余处选中。
    rows = [(eid, label, color, indent, expandable)]
    theme_getter: 取色函数(key -> 颜色),让列表跟随主题切换。
    """

    ROW_H = 26
    DOT_R = 4

    def __init__(self, master, on_select=None, on_context=None,
                 on_toggle=None, theme_getter=None, **kw):
        super().__init__(master, highlightthickness=0, bd=0, **kw)
        self._rows: list[tuple[str, str, str, bool, bool]] = []
        self._sel: str | None = None
        self._collapsed: set[str] = set()   # 已折叠的一级节点
        self.on_select = on_select
        self.on_context = on_context
        self.on_toggle = on_toggle
        self._hover: str | None = None
        self._theme_getter = theme_getter or (lambda k: "#000000")
        self.bind("<Button-1>", self._click)
        for seq in ("<Button-2>", "<Button-3>", "<Control-Button-1>"):
            self.bind(seq, self._click_ctx)
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<Configure>", lambda _ev: self.redraw())
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", self._leave)

    def _c(self, key: str) -> str:
        return self._theme_getter(key)

    def set_rows(self, rows, sel_id: str | None):
        self._rows = rows
        self._sel = sel_id
        self.redraw()

    def redraw(self):
        self.delete("all")
        y = 6
        w = max(self.winfo_width(), 1)
        for eid, label, color, indent, expandable in self._rows:
            selected = eid == self._sel
            hovered = eid == self._hover and not selected
            if selected:
                self._rounded_rect(4, y, w - 4, y + self.ROW_H, 6,
                                   fill=self._c("selected_bg"))
            elif hovered:
                self._rounded_rect(4, y, w - 4, y + self.ROW_H, 6,
                                   fill=self._c("btn_hover"))
            cx = 26 if indent else 20
            if expandable:
                # 折叠箭头单独一个可点元素
                arrow = "▾" if eid not in self._collapsed else "▸"
                t = self.create_text(8, y + self.ROW_H // 2, text=arrow,
                                     fill=self._c("mut"), font=("PingFang SC", 10),
                                     tags=(f"arrow-{eid}",))
                self.tag_bind(t, "<Button-1>",
                              lambda _ev, eid=eid: self._toggle(eid))
            r = self.DOT_R
            cy = y + self.ROW_H // 2
            self.create_oval(cx - r, cy - r, cx + r, cy + r,
                             fill=color, outline=color)
            self.create_text(cx + 12, cy, text=label, anchor="w",
                             fill=self._c("fg"),
                             font=("PingFang SC", 12))
            y += self.ROW_H
        self.configure(scrollregion=(0, 0, w, y + 8))

    def _rounded_rect(self, x1, y1, x2, y2, radius, **kw):
        """圆角矩形(玻璃拟态选中/悬停底)。

        显式 outline="" —— Tk 的 polygon 默认描系统文字色(systemTextColor),
        深色主题下选中行会凭空多出一圈白边(与 _ScrollList.rounded 同理)。
        """
        r = radius
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
               x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
               x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        kw.setdefault("outline", "")
        return self.create_polygon(pts, smooth=True, **kw)

    def _eid_at(self, ev) -> str | None:
        idx = int((self.canvasy(ev.y) - 6) // self.ROW_H)
        return self._rows[idx][0] if 0 <= idx < len(self._rows) else None

    def _motion(self, ev):
        eid = self._eid_at(ev)
        if eid != self._hover:
            self._hover = eid
            self.redraw()

    def _leave(self, _ev):
        if self._hover is not None:
            self._hover = None
            self.redraw()

    def _toggle(self, eid: str):
        if eid in self._collapsed:
            self._collapsed.discard(eid)
        else:
            self._collapsed.add(eid)
        self.redraw()
        if self.on_toggle:
            self.on_toggle(eid)

    def _click(self, ev):
        eid = self._eid_at(ev)
        if eid and self.on_select:
            self.on_select(eid)

    def _click_ctx(self, ev):
        eid = self._eid_at(ev)
        if eid and self.on_context:
            self.on_context(eid, ev)

    def _wheel(self, ev):
        self.yview_scroll(-1 if ev.delta > 0 else 1, "units")


# ---------------------------------------------------------------- 自绘列表骨架


class _ScrollList(tk.Frame):
    """Canvas 自绘列表的通用骨架(选中 / 悬停 / 主题化滚动条 / 滚轮 / 命中测试)。

    Listbox 无法在行内放按钮(build 脚本右侧的齿轮),故与 _DotList 同一套
    自绘技术。滚动条只在内容超出可视高度时出现(服务器管理面板与脚本列表
    都是短列表,常驻滚动条很丑)。
    """

    ROW_H = 26
    TOP = 4

    def __init__(self, master, theme_getter, bg, height=None, **kw):
        super().__init__(master, bg=bg, **kw)
        self._c = theme_getter
        self._bg = bg
        self.cv = tk.Canvas(self, bg=bg, bd=0, highlightthickness=0,
                            height=(height or self.ROW_H * 4))
        self.cv.pack(side="left", fill="both", expand=True)
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.cv.yview,
                                style="PC.Vertical.TScrollbar")
        self.cv.configure(yscrollcommand=self._on_yview)
        self._rows: list = []
        self._sel = None
        self._hover = None
        self._sb_on = False
        self.on_select = None       # 单击选中回调
        self.on_run = None          # 双击回调
        self.on_context = None      # 右键回调
        self.cv.bind("<Configure>", lambda _e: self.redraw())
        self.cv.bind("<Motion>", self._motion)
        self.cv.bind("<Leave>", self._leave)
        self.cv.bind("<Button-1>", self._click)
        self.cv.bind("<Double-Button-1>", self._dbl)
        self.cv.bind("<MouseWheel>", self._wheel)
        for seq in ("<Button-2>", "<Button-3>", "<Control-Button-1>"):
            self.cv.bind(seq, self._ctx)

    # ------------------------------------------------ 子类接口
    def row_key(self, row):
        """行唯一键(选中/悬停状态按它比对)。"""
        return row.get("id") if isinstance(row, dict) else row[0]

    def draw_row(self, row, y: int, w: int, selected: bool, hovered: bool):
        raise NotImplementedError

    def hit_extra(self, row, ev, w: int) -> bool:
        """行内特殊可点区域(如齿轮)。返回 True 表示这次点击已消费。"""
        return False

    # ------------------------------------------------ 公共能力
    def set_rows(self, rows, sel=None):
        self._rows = list(rows or [])
        if sel is not None or not rows:
            self._sel = sel
        self.redraw()

    def redraw(self):
        self.cv.delete("all")
        w = max(self.cv.winfo_width(), 1)
        y = self.TOP
        for row in self._rows:
            key = self.row_key(row)
            self.draw_row(row, y, w, key == self._sel,
                          key == self._hover and key != self._sel)
            y += self.ROW_H
        self.cv.configure(scrollregion=(0, 0, w, y + self.TOP))

    def rounded(self, x1, y1, x2, y2, r, **kw):
        """圆角矩形(选中/悬停底块)。显式 outline="" —— Tk 的 polygon 默认会描黑边。"""
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
               x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
               x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        kw.setdefault("outline", "")
        return self.cv.create_polygon(pts, smooth=True, **kw)

    def _row_at(self, ev):
        idx = int((self.cv.canvasy(ev.y) - self.TOP) // self.ROW_H)
        if 0 <= idx < len(self._rows):
            return self._rows[idx]
        return None

    def _on_yview(self, first, last):
        need = not (float(first) <= 0.0 and float(last) >= 1.0)
        if need and not self._sb_on:
            self.sb.pack(side="right", fill="y")
            self._sb_on = True
        elif not need and self._sb_on:
            self.sb.pack_forget()
            self._sb_on = False
        self.sb.set(first, last)

    def _motion(self, ev):
        row = self._row_at(ev)
        key = self.row_key(row) if row is not None else None
        if key != self._hover:
            self._hover = key
            self.redraw()

    def _leave(self, _ev):
        if self._hover is not None:
            self._hover = None
            self.redraw()

    def _click(self, ev):
        row = self._row_at(ev)
        if row is None:
            return
        w = max(self.cv.winfo_width(), 1)
        if self.hit_extra(row, ev, w):
            return
        key = self.row_key(row)
        self._sel = key
        self.redraw()
        if self.on_select:
            self.on_select(key)

    def _dbl(self, ev):
        row = self._row_at(ev)
        if row is not None and self.on_run:
            self.on_run(self.row_key(row))

    def _ctx(self, ev):
        row = self._row_at(ev)
        if row is not None and self.on_context:
            self.on_context(self.row_key(row), ev)

    def _wheel(self, ev):
        if not self._sb_on:          # 内容没超出:让外层(详情区)接管滚动
            return None
        self.cv.yview_scroll(-1 if ev.delta > 0 else 1, "units")
        return "break"


class _ServerList(_ScrollList):
    """服务器管理面板左栏:状态点三态 + 名称 + ssh 串(端口非 22 附在末尾)。"""

    ROW_H = 42

    def row_key(self, row):
        return row.get("id")

    def draw_row(self, row, y, w, selected, hovered):
        if selected:
            self.rounded(4, y, w - 4, y + self.ROW_H - 2, 6,
                         fill=self._c("selected_bg"))
        elif hovered:
            self.rounded(4, y, w - 4, y + self.ROW_H - 2, 6,
                         fill=self._c("btn_hover"))
        color = {"ok": self._c("dot_on"), "fail": self._c("dot_off")}.get(
            row.get("state"), self._c("dot_none"))
        cy = y + self.ROW_H // 2 - 1
        r = 4
        self.cv.create_oval(12 - r, cy - r, 12 + r, cy + r, fill=color, outline=color)
        self.cv.create_text(26, y + 13, text=row.get("name") or row.get("id") or "",
                            anchor="w", fill=self._c("fg"), font=("PingFang SC", 12))
        self.cv.create_text(26, y + 29, text=row.get("ssh") or "(未填 SSH)",
                            anchor="w", fill=self._c("mut"), font=("Menlo", 9))


class _ScriptList(_ScrollList):
    """详情区可用脚本列表(替代 Listbox):脚本名 + 备注。

    双击 = 运行(语义不变),右键 = 改备注(原 Listbox 的能力保留)。
    v2.4 起发布入口统一收到「环境配置」区,行尾齿轮已移除 ——
    行内排版按脚本名 + 备注自然铺开,不再为齿轮留白。
    """

    ROW_H = 26

    def row_key(self, row):
        return row.get("name")

    def draw_row(self, row, y, w, selected, hovered):
        if selected:
            self.rounded(4, y, w - 4, y + self.ROW_H - 2, 6,
                         fill=self._c("selected_bg"))
        elif hovered:
            self.rounded(4, y, w - 4, y + self.ROW_H - 2, 6,
                         fill=self._c("btn_hover"))
        cy = y + self.ROW_H // 2
        name = row.get("name") or ""
        self.cv.create_text(14, cy, text=name, anchor="w",
                            fill=self._c("fg"), font=("Menlo", 11))
        remark = row.get("remark") or ""
        if remark:
            x = 14 + 7 * len(name) + 12
            self.cv.create_text(min(x, max(120, w - 80)), cy,
                                text=f"— {remark}", anchor="w",
                                fill=self._c("mut"), font=("Menlo", 9))


class _StatusChip(tk.Canvas):
    """状态胶囊(圆角底 + 彩色状态点 + 文字)。

    详情区顶部的状态总览条用:一个胶囊 = 一项状态(本地端口 / 服务 / 健康)。
    为什么自绘:Label 放不下一颗独立着色的状态点,也做不出胶囊圆角底;
    `set()` 只重画不重建 —— 状态轮询每 15s 回来一次,重建控件会闪。
    """

    H = 26
    PAD_X = 10
    DOT_R = 4
    DOT_GAP = 7

    def __init__(self, master, theme_getter, bg, text="", dot=None,
                 font=("PingFang SC", 11)):
        super().__init__(master, bg=bg, highlightthickness=0, bd=0, height=self.H)
        self._c = theme_getter
        self._bg = bg
        self._font = font
        self._fontobj = tkfont.Font(font=font)
        self._dot = dot or self._c("dot_none")
        self._text = text
        self._wpx = 0          # 画布像素宽(别叫 _w:那是 Tk 的控件路径!)
        self.set(text, self._dot)

    def set(self, text: str, dot: str | None = None):
        """换文字 / 换状态点颜色(原地重画)。"""
        if dot is not None:
            self._dot = dot
        self._text = text
        tw = self._fontobj.measure(text)
        w = self.PAD_X * 2 + self.DOT_R * 2 + self.DOT_GAP + tw
        self._wpx = w
        self.configure(width=w)
        self.delete("all")
        pts = _rounded_poly(0, 0, w, self.H, 8)
        self.create_polygon(pts, smooth=True, fill=self._c("card_hi"),
                            outline=self._c("line"), width=1)
        cx = self.PAD_X + self.DOT_R
        cy = self.H // 2
        self.create_oval(cx - self.DOT_R, cy - self.DOT_R,
                         cx + self.DOT_R, cy + self.DOT_R,
                         fill=self._dot, outline=self._dot)
        self.create_text(cx + self.DOT_R + self.DOT_GAP, cy, text=self._text,
                         anchor="w", fill=self._c("fg"), font=self._font)


class _ChipFlow(tk.Frame):
    """胶囊流式容器:宽度不够时自动折行。

    `pack(side="left")` 只会横着排,窄窗口下右端胶囊直接被窗口边缘裁掉
    (v2.9 实测第三枚「健康检查」只剩半截)。这里按可用宽度把子控件
    grid 重排到下一行 —— 只挪位置,不重建控件,轮询刷新时不会闪。
    """

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self._items: list[tuple[tk.Widget, int]] = []
        self._pending: str | None = None
        self.bind("<Configure>", lambda _e: self.schedule(), add="+")

    def add(self, widget: tk.Widget, gap: int = 8):
        """放入一个胶囊,gap 是它与后一个胶囊的间距。"""
        self._items.append((widget, gap))
        widget.grid(row=0, column=len(self._items) - 1, sticky="w",
                    padx=(0, gap), pady=2)
        self.schedule()

    def schedule(self):
        """合并密集触发(窗口拖拽)后的重排请求。"""
        if self._pending is None:
            self._pending = self.after_idle(self._relayout)

    def _relayout(self):
        self._pending = None
        w = self.winfo_width()
        if w <= 1 or not self._items:
            return
        row = col = x = 0
        for widget, gap in self._items:
            try:
                if not widget.winfo_exists():
                    continue
                ww = widget.winfo_reqwidth() + gap
            except tk.TclError:
                continue
            if col and x + ww > w:   # 放不下就折行
                row += 1
                col = x = 0
            widget.grid(row=row, column=col, sticky="w",
                        padx=(0, gap), pady=2)
            x += ww
            col += 1


# ---------------------------------------------------------------- 绘图辅助


def _hex2rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _rgb2hex(c: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(v))) for v in c)


def _lerp(c1: str, c2: str, t: float) -> str:
    a, b = _hex2rgb(c1), _hex2rgb(c2)
    return _rgb2hex(tuple(a[i] + (b[i] - a[i]) * t for i in range(3)))


def _rounded_poly(x1, y1, x2, y2, r):
    """圆角矩形顶点序列(供 Canvas create_polygon smooth=True)。"""
    return [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]


def _inside_rounded(x, y, w, h, r):
    """判断 (x,y) 是否在圆角矩形内(含边缘抗锯齿带,返回 0~1 覆盖度)。"""
    # 圆心
    cx = r if x < r else (w - r if x > w - r else None)
    cy = r if y < r else (h - r if y > h - r else None)
    if cx is not None and cy is not None:
        d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
        # 圆角外
        if d > r:
            return 0.0
        # 边缘 1px 抗锯齿
        return 1.0 if d <= r - 1 else r - d
    # 在矩形主体内(但可能在圆角缺角区,由上面圆心分支处理)
    if cx is not None or cy is not None:
        # 处于某一条边的"直边延伸区",但靠近角的直边部分
        pass
    if 0 <= x < w and 0 <= y < h:
        # 主体: 判断是否落入某个圆角缺角
        # 左上角
        if x < r and y < r and ((x - r) ** 2 + (y - r) ** 2) > r * r:
            return 0.0
        if x >= w - r and y < r and ((x - (w - r)) ** 2 + (y - r) ** 2) > r * r:
            return 0.0
        if x < r and y >= h - r and ((x - r) ** 2 + (y - (h - r)) ** 2) > r * r:
            return 0.0
        if x >= w - r and y >= h - r and ((x - (w - r)) ** 2 + (y - (h - r)) ** 2) > r * r:
            return 0.0
        return 1.0
    return 0.0


def _encode_png(w, h, rgba_rows):
    """把 RGBA 像素行列表编码为 PNG 字节(标准库 zlib+struct)。"""
    import zlib
    import struct

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
    raw = b"".join(b"\x00" + bytes(row) for row in rgba_rows)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def _make_btn_png(ca, cb, w, h, radius, outline=None, glow=False):
    """预渲染一张 w*h 的圆角渐变按钮底图,返回 PNG 字节。

    逐像素(超采样 2x 抗锯齿)计算:左→右渐变 ca→cb、圆角 alpha 过渡、
    可选顶部高光(玻璃反光)与 1px 描边。无 banding、无锯齿。
    """
    ss = 2  # 超采样
    W, H = w * ss, h * ss
    R = radius * ss
    a = _hex2rgb(ca)
    b = _hex2rgb(cb)

    def sample(px, py):
        """返回 (r,g,b,alpha) 0~255。px,py 为超采样坐标。"""
        cov = _inside_rounded(px, py, W, H, R)
        if cov <= 0.0:
            return (0, 0, 0, 0)
        t = px / W
        r = a[0] + (b[0] - a[0]) * t
        g = a[1] + (b[1] - a[1]) * t
        bl = a[2] + (b[2] - a[2]) * t
        # 顶部高光: 0~45% 高度向 ca 淡出(更亮)
        if glow:
            f = max(0.0, 1.0 - py / (H * 0.5))
            light = f * 0.35
            r = r + (255 - r) * light
            g = g + (255 - g) * light
            bl = bl + (255 - bl) * light
        # 描边(在圆角边界内侧 1px)
        if outline is not None:
            # 边缘检测: 距边界 < 1px
            d_edge = _edge_dist(px, py, W, H, R)
            if d_edge < 1.0:
                oc = _hex2rgb(outline)
                k = 1.0 - d_edge
                r = r * (1 - k) + oc[0] * k
                g = g * (1 - k) + oc[1] * k
                bl = bl * (1 - k) + oc[2] * k
        return (int(r), int(g), int(bl), int(cov * 255))

    rows = []
    for y in range(h):
        row = bytearray(w * 4)
        for x in range(w):
            # 2x2 亚像素平均
            rs = gs = bs = al = 0.0
            cnt = 0
            for sy in range(ss):
                for sx in range(ss):
                    r_, g_, b_, a_ = sample(x * ss + sx, y * ss + sy)
                    rs += r_; gs += g_; bs += b_; al += a_
                    cnt += 1
            n = cnt
            row[x * 4] = int(rs / n)
            row[x * 4 + 1] = int(gs / n)
            row[x * 4 + 2] = int(bs / n)
            row[x * 4 + 3] = int(al / n)
        rows.append(row)
    return _encode_png(w, h, rows)


def _edge_dist(px, py, w, h, r):
    """返回点到圆角矩形内边界的距离(0=在边界上,负=在内部)。"""
    # 用覆盖度近似: 边界外 1px 内的点
    # 简化: 直接计算到最近圆角/直边的距离
    # 先判断四个角
    cx = r if px < r else (w - r if px > w - r else None)
    cy = r if py < r else (h - r if py > h - r else None)
    if cx is not None and cy is not None:
        return r - ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5
    # 直边
    dl = px
    dr = w - px
    dt = py
    db = h - py
    return min(dl, dr, dt, db)


# 按钮底图缓存: (ca, cb, w, h, radius, outline, glow) -> tk.PhotoImage
# 用模块级字典持有引用,防止 PhotoImage 被 GC 导致图片消失;主题切换时清空。
_btn_cache: dict = {}
