"""Flask API 入口 —— 前后端分离架构.

启动: python backend/app.py  (端口 5001)
"""

import os, sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from flask_cors import CORS
from backend.web.school_market_web import app
from backend.config import Config

app.config.from_object(Config)

CORS(app, supports_credentials=True, origins=[
    "http://localhost:5173", "http://127.0.0.1:5173",
    "http://localhost:5000", "http://127.0.0.1:5000",
])

if __name__ == "__main__":
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    port = int(os.environ.get("FLASK_PORT", "5001"))
    app.run(host=host, port=port, debug=False, use_reloader=False)
