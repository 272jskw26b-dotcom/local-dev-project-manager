#!/bin/bash
# ============================================================
#  项目管理工具 —— 构建 + 部署脚本
#
#  用法：
#     ./build.sh          构建并部署到桌面（默认）
#     ./build.sh build    只构建，产物在 dist/
#     ./build.sh deploy   只把已有产物复制到桌面
#     ./build.sh debug    构建带终端窗口的调试版（排查启动失败用）
#     ./build.sh run      源码方式运行（改代码后快速验证）
#     ./build.sh clean    清理构建产物与缓存
#
#  ⚠️ 本脚本必须在项目根目录执行（它用相对路径定位 app_build.spec）
# ============================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

VENV=".venv-desktop"
PY="$VENV/bin/python"
PYINSTALLER="$VENV/bin/pyinstaller"
APP_NAME="项目管理工具.app"
EXE_NAME="ProjectManager"
DESKTOP="$HOME/Desktop/$APP_NAME"
# 基础解释器(用于创建 venv):PC_PYTHON 环境变量 > 常见路径逐一探测。
# 探测标准:能执行且自带 tkinter(本项目 UI 依赖)。
RUNTIME_PY="${PC_PYTHON:-}"
if [ -z "$RUNTIME_PY" ]; then
    for cand in /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3 \
                /usr/local/bin/python3 /usr/bin/python3 python3; do
        if command -v "$cand" >/dev/null 2>&1 \
           && "$cand" -c "import tkinter" 2>/dev/null; then
            RUNTIME_PY="$(command -v "$cand")"
            break
        fi
    done
fi
[ -n "$RUNTIME_PY" ] \
    || die "未找到带 tkinter 的 Python 3。请安装,或用 PC_PYTHON=/path/to/python3 ./build.sh 指定"

# PyInstaller 会把裁剪/签名后的 libpython 缓存起来。放在项目内:
#   1. 构建完全自包含，换机器/换会话可直接 ./build.sh clean 彻底清理；
#   2. 在受限环境（沙箱、CI）里不必去写用户 Library 目录，避免被拦。
export PYINSTALLER_CONFIG_DIR="$PROJECT_DIR/.pyi-cache"

c_ok()   { printf '\033[32m%s\033[0m\n' "$*"; }
c_warn() { printf '\033[33m%s\033[0m\n' "$*"; }
c_err()  { printf '\033[31m%s\033[0m\n' "$*"; }
die()    { c_err "✗ $1"; exit 1; }

ensure_venv() {
    if [ ! -x "$PY" ]; then
        c_warn "未找到虚拟环境，正在创建…"
        [ -x "$RUNTIME_PY" ] || die "找不到 Python 3.13，请手动指定解释器"
        "$RUNTIME_PY" -m venv "$VENV"
        c_ok "虚拟环境已创建"
    fi
    # 本项目只用标准库(tkinter/subprocess等),仅需 pyinstaller
    if ! "$PY" -c "import PyInstaller" 2>/dev/null; then
        c_warn "正在安装依赖…"
        "$PY" -m pip install --quiet --upgrade pip
        "$PY" -m pip install --quiet pyinstaller
        c_ok "依赖安装完成"
    fi
}

do_build() {
    local spec="app_build.spec"
    if [ "${1:-}" = "debug" ]; then
        # 调试版：console=True，启动失败时能看到 traceback
        c_warn "构建调试版（带终端窗口）…"
        sed "s/name='$EXE_NAME'/name='${EXE_NAME}Debug'/; s/console=False/console=True/" \
            "$spec" > /tmp/_pc_debug.spec
        cp app_build.spec /tmp/_pc_debug.bak
        cp /tmp/_pc_debug.spec "$spec"
        "$PYINSTALLER" --noconfirm --clean --distpath dist --workpath build "$spec"
        cp /tmp/_pc_debug.bak "$spec"
        rm -f /tmp/_pc_debug.spec /tmp/_pc_debug.bak
        c_ok "调试版已生成：dist/${EXE_NAME}Debug"
        c_warn "运行它并查看终端输出即可定位启动问题"
        return
    fi
    c_warn "正在打包（约 20-30 秒）…"
    "$PYINSTALLER" --noconfirm --clean --distpath dist --workpath build "$spec"
    [ -d "dist/$APP_NAME" ] || die "打包失败：未生成 dist/$APP_NAME"
    c_ok "构建完成：dist/$APP_NAME"
}

do_deploy() {
    [ -d "dist/$APP_NAME" ] || die "没有可部署的产物，请先执行 ./build.sh build"
    # 关闭正在运行的实例（注意匹配完整路径，避免误杀其他进程）
    pkill -f "Desktop/$APP_NAME" 2>/dev/null || true
    sleep 1
    rm -rf "$DESKTOP"
    cp -R "dist/$APP_NAME" "$DESKTOP"
    codesign -v "$DESKTOP" 2>/dev/null && c_ok "签名校验通过" \
             || c_warn "签名校验未通过（adhoc 签名通常可正常运行）"
    c_ok "已部署到：$DESKTOP"
    c_warn "验证启动时请用： pgrep -f \"Desktop/$APP_NAME\""
    c_warn "切勿用 pkill -f \"MacOS/$EXE_NAME\" 判断启动结果——它会连带杀掉刚启动的实例。"
}

do_clean() {
    rm -rf build dist __pycache__ .pyi-cache
    find . -name "*.pyc" -delete 2>/dev/null || true
    c_ok "构建产物与缓存已清理"
}

case "${1:-all}" in
    build)  ensure_venv; do_build ;;
    deploy) do_deploy ;;
    debug)  ensure_venv; do_build debug ;;
    run)    ensure_venv; exec "$PY" project_console.py ;;
    clean)  do_clean ;;
    all)    ensure_venv; do_build; do_deploy ;;
    *)      die "未知参数「$1」。可用：build | deploy | debug | run | clean | all" ;;
esac
