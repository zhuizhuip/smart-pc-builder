#!/usr/bin/env bash
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

if ! command -v python3.13 >/dev/null 2>&1; then
  echo "未找到 python3.13，请先安装 Python 3.13"
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "未找到 docker，请先安装并启动 Docker Desktop"
  exit 1
fi

python3.13 -m venv pa.venv
source pa.venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

docker compose up -d mysql jobmanager taskmanager

for i in {1..60}; do
  if docker compose exec -T mysql mysqladmin ping -h 127.0.0.1 -proot123 --silent >/dev/null 2>&1; then
    break
  fi
  sleep 3
done

docker compose exec -T mysql mysql -uroot -proot123 < migration/school_market_full.sql

python school_market_web.py
