from pathlib import Path
import requests
import os
import math
import json


def _load_dotenv():
    """启动时读项目根目录的 .env 文件，把 KEY=VALUE 加载进环境变量。"""
    env_file = Path(__file__).resolve().parents[2] / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

EMBEDDING_API_KEY = os.environ.get("EMBEDDING_API_KEY", "")

# DeepSeek（生成答案用）
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")

# 知识库目录与向量索引文件
KB_DIR = Path(__file__).resolve().parents[2] / "data" / "knowledge_base"
VECTOR_FILE = KB_DIR / "vectors.json"


def read_knowledge_file(path: str) -> str:
    with open(path,'r', encoding='utf-8') as f:
        return f.read()
    
def chunk_text(text:str,chunk_size:int = 400,overlap:int = 60) -> list[str]:
    text=text.strip()
    chunk=[]
    start = 0
    while start < len(text):
        end = start+ chunk_size
        chunk.append(text[start:end])
        if end >=len(text):
            break
        start=end - overlap
    return chunk

def embed_texts(texts: list[str]) -> list[list[float]]:
    response = requests.post(
        "https://api.siliconflow.cn/v1/embeddings",
        headers={"Authorization": f"Bearer {EMBEDDING_API_KEY}"},
        json={
            "model":"BAAI/bge-large-zh-v1.5",
            "input": texts,
            "encoding_format":"float"
            },
            timeout=30, 
        )
    result = response.json()
    data = result["data"]
    data.sort(key=lambda item: item["index"])
    return [item["embedding"] for item in data]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """余弦相似度：衡量两个向量的方向是否接近，值越接近 1 越相似。"""
    # 1. 点积：对应位置相乘再相加
    dot = sum(x * y for x, y in zip(a, b))
    # 2. 模（长度）：每个数平方、求和、开根号
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    # 3. 防止除以 0
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class VectorStore:
    """极简向量库：把文本片段 + 向量存成 JSON 文件，检索时算余弦相似度取 top-k。"""

    def __init__(self, vector_file: Path = VECTOR_FILE):
        self.vector_file = vector_file
        self.records: list[dict] = []

    def build(self, docs: list[dict]) -> None:
        """从文档列表构建索引。docs = [{"source": 文件名, "content": 内容}]"""
        self.records = []
        for doc in docs:
            for chunk in chunk_text(doc["content"]):
                self.records.append({"source": doc["source"], "text": chunk})
        if not self.records:
            return
        texts = [record["text"] for record in self.records]
        vectors = embed_texts(texts)   # 一次性把全部片段向量化
        for record, vec in zip(self.records, vectors):
            record["vector"] = vec
        self.save()

    def save(self) -> None:
        self.vector_file.parent.mkdir(parents=True, exist_ok=True)
        self.vector_file.write_text(
            json.dumps(self.records, ensure_ascii=False), encoding="utf-8"
        )

    def load(self) -> None:
        if self.vector_file.exists():
            self.records = json.loads(self.vector_file.read_text(encoding="utf-8"))

    def search(self, query: str, top_k: int = 3) -> list[dict]:
        """检索：把问题向量化，返回最相似的 top_k 个片段。"""
        if not self.records:
            return []
        query_vec = embed_texts([query])[0]
        scored = []
        for record in self.records:
            score = cosine_similarity(query_vec, record["vector"])
            scored.append((score, record))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [
            {"score": round(score, 4), "source": record["source"], "text": record["text"]}
            for score, record in scored[:top_k]
        ]


def generate_answer(question: str, context: list[dict], feedback: str = "") -> str:
    """把检索到的上下文 + 问题交给 DeepSeek 生成答案（feedback 用于重试纠错）。"""
    if not DEEPSEEK_API_KEY:
        return "未配置 DEEPSEEK_API_KEY，无法生成答案。"
    context_text = "\n\n".join(
        f"[资料{i + 1}]\n{item['text']}" for i, item in enumerate(context)
    )
    system_prompt = (
        "你是电脑装机领域的专业助手。请严格遵守以下规则：\n"
        "1. 只能依据提供的资料回答，禁止编造或补充资料之外的知识。\n"
        "2. 先给结论，再分点说明依据，语言简洁。\n"
        "3. 如果资料不足以回答，只回复「知识库暂无相关资料」，不要展开。\n"
        "4. 回答结尾必须标注引用来源，格式：「参考：资料1、资料2」。"
    )
    extra = f"\n\n【改进要求】{feedback}" if feedback else ""
    user_prompt = f"参考资料：\n{context_text}\n\n用户问题：{question}{extra}"
    resp = requests.post(
        DEEPSEEK_API_URL,
        headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
        json={
            "model": DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def validate_answer(answer: str, context: list[dict]) -> tuple[bool, str]:
    """验证答案是否达标（纯字符串检查，0 成本）。返回 (是否通过, 失败原因)。"""
    if len(answer.strip()) < 10:
        return False, "答案太短，请更详细地分点说明"
    if "参考：" not in answer:
        return False, "回答结尾忘记标注引用来源，请补上「参考：资料X」"
    if not context and "知识库暂无相关资料" not in answer:
        return False, "资料不足时应只回复固定话术「知识库暂无相关资料」"
    return True, ""


def ask(question: str, top_k: int = 3, max_retries: int = 2) -> dict:
    """完整 RAG 问答：检索 + 生成 + 验证 + 不通过则带反馈重试。"""
    store = VectorStore()
    store.load()
    if not store.records:
        return {"answer": "知识库尚未构建，请先运行构建脚本。", "context": [], "retries": 0}
    context = store.search(question, top_k=top_k)

    feedback = ""
    for attempt in range(max_retries + 1):
        answer = generate_answer(question, context, feedback)
        ok, fail_reason = validate_answer(answer, context)
        if ok:
            return {"answer": answer, "context": context, "retries": attempt}
        feedback = fail_reason

    return {
        "answer": "抱歉，暂时无法生成可靠答案，请稍后再试。",
        "context": context,
        "retries": max_retries,
    }


if __name__=='__main__':
    base = Path(__file__).resolve().parents[2]   # 项目根目录
    data = read_knowledge_file(str(base / 'data' / 'knowledge_base' / '兼容性规则.md'))
    chunks = chunk_text(data)
    print(f'共分为{len(chunks)}块,每块大约{len(chunks[0])}个字符')
    #测试
    print("key 前8位:", EMBEDDING_API_KEY[:8], "长度:", len(EMBEDDING_API_KEY))
    vectors = embed_texts(chunks[:2])
    print(f'向量化成功，共 {len(vectors)} 个向量，每个长度 {len(vectors[0])}')
        # 验证相似度：相近的两句应该高，无关的两句应该低
        # 完整 RAG 问答测试
    result = ask("7500F 配什么主板？")
    print("\n=== 答案 ===")
    print(result["answer"])
    print("\n=== 命中的参考资料 ===")
    for c in result["context"]:
        print(f"[{c['source']}] (相似度 {c['score']}) {c['text'][:40]}...")