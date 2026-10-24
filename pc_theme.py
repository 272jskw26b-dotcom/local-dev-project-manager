#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pc_theme —— Project Console 主题层

双主题配色字典(玻璃拟态外观语言)与主题偏好持久化路径。
纯数据模块:不 import tkinter,不依赖其他模块。
"""

import os

# 主题配置文件:记录上次选择的主题(dark/light),重启后记住。
THEME_FILE = os.path.expanduser("~/.project_console_theme")

# 双主题配色。玻璃拟态外观语言:分层底色 + 细描边 + 圆角 + 克制的色差,
# 而非真正的半透明毛玻璃模糊(Tkinter 不支持 backdrop blur)。
THEMES = {
    "dark": {
        "bg": "#1b1d22",          # 窗口底色
        "panel": "#22242b",       # 左栏/日志面板
        "card": "#262932",        # 详情卡
        "card_hi": "#2d303b",     # 详情卡高亮/分区头
        "fg": "#e8e9ee",          # 主文字
        "mut": "#8f93a0",         # 次要文字
        "line": "#3a3d49",        # 分隔线/描边
        "accent": "#5b8cff",      # 强调蓝(主按钮/选中)
        "accent_hover": "#6f9aff",
        "green": "#34c759", "red": "#ff6b6b", "warn": "#ffd60a",
        "btn": "#343744", "btn_hover": "#3f4350",   # 玻璃底按钮
        "btn_primary": "#5b8cff", "btn_danger": "#e14b44",
        "btn_fg": "#ffffff",      # 实底按钮文字(primary/danger 白字)
        "log_bg": "#16171c", "log_fg": "#c9cad2",
        "dot_on": "#34c759", "dot_off": "#ff5f57",
        "tab_active_bg": "#262932",
        "selected_bg": "#2d3140",
        "list_sel_fg": "#ffffff",
        # 渐变胶囊按钮所需色
        "grad_a": "#4f8cff", "grad_b": "#7a5cff",      # primary 蓝→紫
        "grad_a_hover": "#6f9aff", "grad_b_hover": "#9077ff",
        "danger_a": "#ff5f57", "danger_b": "#ff7a3c",  # danger 红→橙红
        "danger_a_hover": "#ff7a70", "danger_b_hover": "#ff9160",
        "card_line": "#3a3d49",                          # 卡片描边
        "top_a": "#5b8cff", "top_b": "#8a5cff",          # 顶栏标题渐变
        "highlight": "#ffffff",                          # 按钮顶部高光
        # v2.1 发布相关
        "dot_none": "#6b6f7c",                           # 灰点:还没测过
        "gear": "#8f93a0",                               # 脚本行齿轮图标
        "scroll_thumb": "#4a4e5c",                       # 自绘列表滚动条滑块
        "scroll_trough": "#1b1d22",                      # 滚动条槽
        "deploy_bg": "#20222a",                          # 发布卡片内底色
    },
    "light": {
        "bg": "#f4f5f7",
        "panel": "#eceef2",
        "card": "#ffffff",
        "card_hi": "#f7f8fa",
        "fg": "#1f2329",
        "mut": "#7a7f8a",
        "line": "#e2e5ea",
        "accent": "#3b74f0",
        "accent_hover": "#2f63d8",
        "green": "#16a34a", "red": "#dc2626", "warn": "#c9930a",
        "btn": "#e8eaf0", "btn_hover": "#dde1e8",
        "btn_primary": "#3b74f0", "btn_danger": "#dc2626",
        "btn_fg": "#ffffff",
        "log_bg": "#ffffff", "log_fg": "#3a3f4a",
        "dot_on": "#16a34a", "dot_off": "#dc2626",
        "tab_active_bg": "#ffffff",
        "selected_bg": "#e6edff",
        "list_sel_fg": "#1f2329",
        "grad_a": "#3b74f0", "grad_b": "#7a4ff0",
        "grad_a_hover": "#4f84f3", "grad_b_hover": "#8b66f3",
        "danger_a": "#dc2626", "danger_b": "#f97316",
        "danger_a_hover": "#e84848", "danger_b_hover": "#fb8a3a",
        "card_line": "#e2e5ea",
        "top_a": "#3b74f0", "top_b": "#7a4ff0",
        "highlight": "#ffffff",
        # v2.1 发布相关
        "dot_none": "#a0a5b0",
        "gear": "#7a7f8a",
        "scroll_thumb": "#c6cbd4",
        "scroll_trough": "#f4f5f7",
        "deploy_bg": "#f7f8fa",
    },
}
