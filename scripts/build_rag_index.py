"""构建 RAG 知识库向量索引。

用法：python scripts/build_rag_index.py
它会读取 data/knowledge_base/ 下所有 .md/.txt 文档，
切分 -> 向量化 -> 保存为 data/knowledge_base/vectors.json。
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from backend.services.rag_engine import KB_DIR, VectorStore  # noqa: E402


def main() -> None:
    docs: list[dict] = []
    for path in sorted(KB_DIR.iterdir()):
        if path.suffix.lower() not in (".md", ".txt"):
            continue
        content = path.read_text(encoding="utf-8")
        docs.append({"source": path.name, "content": content})
        print(f"已加载：{path.name}（{len(content)} 字）")

    if not docs:
        print("知识库目录下没有 .md/.txt 文档，请先放入知识文档。")
        return

    print(f"\n开始向量化，共 {len(docs)} 个文档 ...")
    store = VectorStore()
    store.build(docs)
    print(f"完成！索引已保存到 {store.vector_file}，共 {len(store.records)} 个片段。")


if __name__ == "__main__":
    main()
