#!/bin/bash
# ═══════════════════════════════════════════════════════════
#  🎌 日语学习 App — 启动脚本
#  由桌面图标自动调用，也可直接双击运行
# ═══════════════════════════════════════════════════════════

PROJECT_DIR="/Users/wang/japanese-news-reader"

# ── 彩色输出 ──────────────────────────────────────────────
G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; B='\033[0;34m'; N='\033[0m'
log()  { echo -e "${G}[✓]${N} $1"; }
warn() { echo -e "${Y}[!]${N} $1"; }
err()  { echo -e "${R}[✗]${N} $1"; }
info() { echo -e "${B}[→]${N} $1"; }

# ── 退出时清理 Flask 进程 ─────────────────────────────────
FLASK_PID=""
cleanup() {
    if [ -n "$FLASK_PID" ] && kill -0 "$FLASK_PID" 2>/dev/null; then
        warn "正在停止 Flask 服务（PID $FLASK_PID）..."
        kill "$FLASK_PID" 2>/dev/null
        sleep 0.5
    fi
}
trap cleanup EXIT INT TERM

# ── 检查某端口上是否已有我们的 app 在响应 ─────────────────
check_app_running() {
    local port=$1
    # 先检查端口是否被监听
    lsof -ti:$port &>/dev/null || return 1
    # 再检查 HTTP 是否能响应
    curl -sf --max-time 1 "http://localhost:$port/" &>/dev/null
}

# ── 端口选择逻辑 ──────────────────────────────────────────
PORT=5000

if check_app_running 5000; then
    log "检测到应用已在端口 5000 运行"
    info "直接打开浏览器..."
    sleep 0.3
    open "http://localhost:5000"
    echo ""
    warn "提示：另一个实例正在运行，关闭此窗口不会停止服务。"
    read -rp "  按 Enter 关闭此窗口 › "
    exit 0

elif lsof -ti:5000 &>/dev/null; then
    warn "端口 5000 被其他程序占用，尝试端口 5001..."
    PORT=5001

    if check_app_running 5001; then
        log "检测到应用已在端口 5001 运行"
        sleep 0.3
        open "http://localhost:5001"
        read -rp "  按 Enter 关闭此窗口 › "
        exit 0
    elif lsof -ti:5001 &>/dev/null; then
        err "端口 5000 和 5001 均被占用，无法启动！"
        err "请先关闭占用这些端口的程序，或重启电脑后重试。"
        read -rp "  按 Enter 关闭 › "
        exit 1
    fi
fi

# ── 进入项目目录 ──────────────────────────────────────────
cd "$PROJECT_DIR" || {
    err "找不到项目目录：$PROJECT_DIR"
    read -rp "  按 Enter 关闭 › "
    exit 1
}

# ── 虚拟环境检查 / 自动创建 ───────────────────────────────
if [ ! -f ".venv/bin/activate" ]; then
    warn "未找到虚拟环境，正在自动创建..."
    python3 -m venv .venv
    source .venv/bin/activate
    info "安装依赖（首次运行约需 1 分钟）..."
    pip install -q -r requirements.txt
    log "依赖安装完成"
else
    source .venv/bin/activate
fi

# ── 打印启动横幅 ──────────────────────────────────────────
clear
echo ""
echo -e "  ${G}🎌  日语学习 App${N}"
echo "  ─────────────────────────────────"
echo -e "  地址：${B}http://localhost:$PORT${N}"
echo "  关闭：直接关闭此终端窗口"
echo "  ─────────────────────────────────"
echo ""

# ── 启动 Flask ────────────────────────────────────────────
info "启动服务器（端口 $PORT）..."
FLASK_PORT=$PORT python app.py &
FLASK_PID=$!

# ── 等待服务就绪（最多 10 秒）────────────────────────────
READY=0
for ((i=0; i<20; i++)); do
    sleep 0.5
    # 先检查进程是否还活着
    if ! kill -0 "$FLASK_PID" 2>/dev/null; then
        err "Flask 进程意外退出，请检查 app.py 配置"
        err "提示：确认 DEEPSEEK_API_KEY 已填写，依赖已安装"
        read -rp "  按 Enter 关闭 › "
        exit 1
    fi
    if check_app_running $PORT; then
        READY=1; break
    fi
done

if [ $READY -eq 0 ]; then
    warn "服务器启动超时，强制打开浏览器..."
fi

# ── 打开浏览器 ────────────────────────────────────────────
log "浏览器已打开 → http://localhost:$PORT"
open "http://localhost:$PORT"

echo ""
log "服务运行中 (PID: $FLASK_PID) — 关闭此窗口即可停止"
echo ""

# ── 阻塞等待，保持窗口开启 ───────────────────────────────
wait "$FLASK_PID"
echo ""
warn "服务已停止。"
sleep 1
