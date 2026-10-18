"""RAG 知识库问答的路由。"""
from flask import Blueprint, jsonify, render_template, request

from backend.services.rag_engine import ask
from backend.web.rate_limit import rate_limit

rag_bp = Blueprint("rag", __name__)


@rag_bp.route("/rag")
def rag_page():
    """知识库问答页面。"""
    return render_template("rag.html")


@rag_bp.route("/api/rag/ask", methods=["POST"])
@rate_limit(limit=10, window=60)
def rag_ask():
    """接收问题，返回 RAG 答案 + 命中的参考资料。"""
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"ok": False, "error": "问题不能为空"}), 400
    try:
        result = ask(question)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "data": result})
