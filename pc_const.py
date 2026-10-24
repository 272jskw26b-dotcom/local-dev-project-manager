#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pc_const —— 全局常量(纯数据,不 import 任何本项目模块)

放这里的东西必须满足两条:① 被两个以上模块用;② 不含任何可执行逻辑。
`project_console.py` 会把这些名字再导出一次,所以 `import project_console as pc`
之后 `pc.GROUPS` / `pc.SYSTEM_TAB` 这类老写法仍然有效。
"""

# ---------------------------------------------------------------- 常量
APP_TITLE = "Local Dev Project Manager · 项目控制台"

DOT = {"on": "●", "off": "○", "warn": "◐"}
AUTO_REFRESH_MS = 15000   # 本地端口自动轮询周期
START_TIMEOUT_DEFAULT = 90   # 启动超时秒数(P0-1);条目可用 start_timeout 覆盖

# 条目分组(tab 页)。注册表条目缺 group 字段时归入 personal。
GROUPS = [("work", "💼 工作"), ("personal", "👤 个人")]

# v2.1 发布:脚本名以此前缀开头的才画齿轮(只有 build:* 能发)
BUILD_PREFIX = "build"

# v2.2 日志 tab 化:系统 tab 固定存在且不可关闭,其余每个项目一个 tab
SYSTEM_TAB = "system"
LOG_MAX_LINES = 4000      # 单个 tab 最多保留行数(长跑项目不能把内存吃满)
LOG_TITLE_MAX = 20        # tab 标题最长字符数(太短会让 ai-platform-api/web 撞名)

# 服务器认证方式标记。密码本体在内存 cfg 里,落盘时由 save_config() 抽到
# 同目录 secrets.json(v3.0);auth="key" 表示用户显式选「密钥」压过密码。
AUTH_KEY = "key"
AUTH_PW = "password"
AUTH_LABELS = {AUTH_KEY: "密钥(免密登录)", AUTH_PW: "密码"}
ACCOUNT_COLS = ("user", "password", "note")   # 网站账号三列(仅备忘,不参与发布)
