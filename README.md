# 电脑配件市场数据分析与智能装机系统

一个集 **数据采集、市场分析、AI 智能装机推荐、RAG 知识库问答** 于一体的 Web 应用，已部署上线阿里云。

## ✨ 核心功能

- **市场数据分析**：价格趋势、涨跌告警、关键词 / 店铺多维统计
- **AI 智能装机**：预算动态分配 + Apriori 关联规则挖掘 + 六维评分，接入 DeepSeek 大模型实现对话式装机推荐
- **RAG 知识库问答**：从零手写向量检索（文档切分 → 向量化 → 余弦相似度 → 检索），结合 DeepSeek 生成**可溯源**的答案
- **用户系统**：注册登录、社区分享、收藏

## 🛠 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python Flask |
| 前端 | Jinja2 服务端渲染 + 原生 JavaScript |
| 数据库 | MySQL 8.4（Docker 容器） |
| AI | DeepSeek API（对话生成）、SiliconFlow BGE（向量化） |
| 数据管道 | Flink SQL（批量 ETL） |
| 部署 | Docker + 阿里云 ECS + systemd |

## 🏗 架构

```
浏览器
  │
  ▼
Flask 应用（宿主机 / systemd 托管）
  ├── 市场分析（读 MySQL）
  ├── 智能装机（DeepSeek 对话 + Apriori + 六维评分）
  ├── RAG 知识库（向量检索 + DeepSeek 生成 + 验证重试）
  └── 用户系统
  │
  ▼
MySQL 8.4（Docker 容器，数据持久化到命名卷）
```

## 📁 目录结构

```
├── backend/            # 后端代码
│   ├── web/            # 路由与蓝图
│   └── services/       # 业务逻辑（装机引擎、RAG 引擎等）
├── frontend/           # 前端模板 + 静态资源
├── collectors/         # 数据采集
├── data/               # 数据与知识库
│   └── knowledge_base/ # RAG 知识文档 + 向量索引
├── migration/          # 数据库全量备份
├── scripts/            # 部署与工具脚本
├── docker-compose.yml  # 容器编排
└── requirements.txt    # Python 依赖
```

## 🧠 RAG 知识库（从零手写，无第三方框架）

1. **文档切分**：固定字数 + 重叠切分
2. **向量化**：SiliconFlow BGE 中文向量模型
3. **检索**：余弦相似度取 top-k
4. **生成**：DeepSeek 依据检索结果生成答案
5. **质量保障**：答案验证 + 带反馈重试机制

## 🚀 快速开始

1. 安装 Docker 与 Python 3.10+
2. 复制 `.env.example` 为 `.env`，填入 API Key
3. 启动数据库：`docker compose up -d mysql`
4. 安装依赖：`pip install -r requirements.txt`
5. 导入数据：`docker compose exec -T mysql mysql -uroot -p < migration/school_market_full.sql`
6. 构建 RAG 索引：`python scripts/build_rag_index.py`
7. 启动应用：`python backend/app.py`

## 📌 在线体验

- 系统地址：`http://118.31.60.103:5000`（演示环境，可能随时关闭）
- RAG 知识库问答：`http://118.31.60.103:5000/rag`

## 📄 许可证

[Apache License 2.0](LICENSE)
