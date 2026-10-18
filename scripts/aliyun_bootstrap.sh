#!/usr/bin/env bash
#
# 阿里云 ECS 一键部署脚本（Ubuntu 22.04/24.04 LTS）
#
# 用途：在服务器上完成环境初始化 -> 转码并导入全量数据 -> 以 systemd 服务启动 Flask 应用。
# 前置：项目代码已上传到服务器（scp 或 git clone），本脚本位于项目内 scripts/ 目录。
# 用法：sudo bash scripts/aliyun_bootstrap.sh
#
set -euo pipefail

# ---------- 可配置项 ----------
FLASK_PORT="${FLASK_PORT:-5000}"          # 对外 Web 端口，需在安全组放行
MYSQL_ROOT_PASSWORD="root123"             # 与 docker-compose.yml 保持一致
DB_NAME="school_market"
FULL_DUMP="migration/school_market_full.sql"
SQL_UTF8="/tmp/school_market_full_utf8.sql"

# 自动定位项目根目录（本脚本位于 <项目>/scripts/ 下）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$APP_DIR/pa.venv"

log()  { echo -e "\033[1;32m[deploy]\033[0m $*"; }
warn() { echo -e "\033[1;33m[deploy]\033[0m $*"; }
die()  { echo -e "\033[1;31m[error]\033[0m $*" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || die "请以 root 运行：sudo bash scripts/aliyun_bootstrap.sh"

log "项目目录：$APP_DIR"
cd "$APP_DIR"

# ---------- 1. 安装依赖 ----------
log "检查/安装 docker、python3-venv ..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y ca-certificates curl gnupg python3 python3-venv python3-pip

if ! command -v docker >/dev/null 2>&1; then
  log "通过阿里云镜像安装 Docker（国内网络）..."
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://mirrors.aliyun.com/docker-ce/linux/ubuntu/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://mirrors.aliyun.com/docker-ce/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
fi
docker compose version >/dev/null 2>&1 || die "docker compose 插件缺失"

# ---------- 2. Python 虚拟环境 ----------
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  log "创建 venv ..."
  python3 -m venv "$VENV_DIR"
fi
PIP_MIRROR="https://mirrors.aliyun.com/pypi/simple/"
"$VENV_DIR/bin/pip" install -q -i "$PIP_MIRROR" --upgrade pip
log "安装 requirements.txt（首次较久，请耐心等待）..."
"$VENV_DIR/bin/pip" install -q -i "$PIP_MIRROR" -r requirements.txt

# ---------- 3. 转码全量 SQL（UTF-16LE -> UTF-8）----------
if [[ -f "$FULL_DUMP" ]]; then
  log "转码全量 SQL 到 UTF-8 ..."
  iconv -f UTF-16 -t UTF-8 "$FULL_DUMP" > "$SQL_UTF8"
else
  warn "未找到 $FULL_DUMP，跳过数据导入"
fi

# ---------- 4. 启动 MySQL 容器 ----------
log "启动 MySQL 容器 ..."
docker compose up -d mysql

# ---------- 5. 等待 MySQL 就绪 ----------
log "等待 MySQL 就绪 ..."
READY=0
for _ in $(seq 1 60); do
  if docker compose exec -T mysql mysqladmin ping -h 127.0.0.1 -p"$MYSQL_ROOT_PASSWORD" --silent >/dev/null 2>&1; then
    READY=1; break
  fi
  sleep 2
done
[[ "$READY" -eq 1 ]] || die "MySQL 未在预期时间内就绪"

# ---------- 6. 导入全量数据（幂等：库内已有 app_user 表则跳过）----------
HAS_DATA=$(docker compose exec -T mysql mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -N \
  -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='$DB_NAME' AND table_name='app_user';" 2>/dev/null || echo "0")
if [[ -f "$SQL_UTF8" && "$HAS_DATA" != "1" ]]; then
  log "导入全量数据 ..."
  docker compose exec -T mysql mysql -uroot -p"$MYSQL_ROOT_PASSWORD" --default-character-set=utf8mb4 < "$SQL_UTF8"
  log "数据导入完成"
else
  log "跳过数据导入（已存在或缺少 dump 文件）"
fi

# ---------- 7. 安装并启动 systemd 服务 ----------
log "安装 systemd 服务 ..."
SERVICE_DST="/etc/systemd/system/aliyun_app.service"
sed "s|@APP_DIR@|$APP_DIR|g; s|@FLASK_PORT@|$FLASK_PORT|g" "$SCRIPT_DIR/aliyun_app.service" > "$SERVICE_DST"
systemctl daemon-reload
systemctl enable --now aliyun_app
sleep 2
systemctl --no-pager --lines=15 status aliyun_app || true

# ---------- 8. 完成 ----------
PUBLIC_IP="${PUBLIC_IP:-$(curl -fsSL --max-time 5 ifconfig.me 2>/dev/null || echo '<公网IP>')}"
log "部署完成！"
log "访问地址：http://$PUBLIC_IP:$FLASK_PORT"
log "查看日志：journalctl -u aliyun_app -f"
