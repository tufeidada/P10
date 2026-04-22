# P10-AlphaRadar

## 项目概述
A股+美股多维投研分析系统。核心能力：候选池管理、多维分析（技术面/基本面/资金面/情绪面）、盘中买卖点信号、判断追踪与自我进化。

## 架构文档
完整技术架构见 `docs/P10-AlphaRadar-Architecture.md`，开发前必须通读。

## 技术栈
- **语言**: Python 3.11+
- **数据库**: PostgreSQL 15 + TimescaleDB + pgvector
- **Web 框架**: FastAPI
- **调度**: APScheduler
- **Telegram**: python-telegram-bot v20+
- **数据处理**: pandas, numpy, ta-lib
- **LLM**: DeepSeek V3 (主力分析), Qwen3-Mini (轻量任务), text-embedding-v4 (嵌入)
- **ORM**: 不用 ORM，直接 asyncpg + raw SQL（性能优先）
- **配置**: pydantic-settings + YAML
- **前端**: React（Phase 6，暂不开发）

## 开发规范

### 代码风格
- 类型注解：所有函数必须有完整的 type hints
- docstring：所有公开函数用 Google style docstring
- 异步优先：数据库操作、API 调用、Telegram 交互全部用 async/await
- 错误处理：所有外部调用（API、数据库、网络）必须有 try/except + 日志
- 日志：使用 structlog，JSON 格式，包含 symbol/market/module 上下文

### 数据库
- 连接池：asyncpg.create_pool，min=5, max=20
- 所有 SQL 用参数化查询，禁止字符串拼接
- 时序表必须用 TimescaleDB hypertable
- 大批量写入用 COPY 协议（asyncpg copy_records_to_table）
- 迁移用 Alembic

### 配置管理
- 敏感信息（API keys、DB password）只从环境变量读取
- 业务参数（regime 阈值、权重）从 YAML 读取，支持热重载
- 不同环境用 .env.development / .env.production 区分

### 测试
- 核心计算模块（regime、技术分析、综合判断）必须有单元测试
- 数据管道用 mock 数据测试，不依赖真实 API
- 信号检测用历史数据做回归测试

## 目录结构
```
P10-AlphaRadar/
├── CLAUDE.md                      # 本文件
├── docs/
│   └── P10-AlphaRadar-Architecture.md
├── config/
│   ├── settings.yaml
│   ├── watchlist.yaml
│   ├── regime_params.yaml
│   └── industry_frameworks.yaml
├── core/                          # 核心业务逻辑
│   ├── regime/
│   ├── analysis/
│   ├── intraday/
│   ├── scanner/
│   ├── risk/
│   └── evolution/
├── data/                          # 数据采集与管道
│   ├── sources/
│   ├── pipeline/
│   ├── quality/
│   └── migration/
├── db/
│   ├── connection.py
│   ├── schema.sql
│   └── migrations/
├── llm/
├── bot/
│   ├── telegram_bot.py
│   └── commands/
├── api/
├── scheduler/
├── wiki/
├── scripts/
├── tests/
├── docker-compose.yml
├── requirements.txt
└── pyproject.toml
```

## 开发顺序
严格按 Phase 执行，每个 Phase 完成后验证再进入下一个：

**Phase 0**: 基础设施 → db/schema.sql, db/connection.py, docker-compose.yml, scripts/migrate_from_p6.py, bot 基础框架, data/quality/monitor.py

**Phase 1**: 核心分析 → core/regime/, core/analysis/technical.py, core/analysis/stage_detector.py, 判断记录框架, Telegram 核心命令

**Phase 2**: 分析扩展 → core/analysis/fundamental.py, core/analysis/flow.py, llm/, wiki/ 初始化

**Phase 3**: 盘中信号 → core/intraday/, 盘中 Telegram 推送

**Phase 4**: 美股+情绪 → data/sources/yfinance_client.py, data/sources/stocktwits_client.py, core/analysis/sentiment.py

**Phase 5**: 进化引擎 → core/evolution/

**Phase 6**: 前端

## 关键约束
- 所有分析结果必须带 confidence 字段和 data_freshness 标注
- 盘中信号必须关联 basis_judgment_id（基础分析判断）
- Regime 变更必须推送 Telegram 通知
- 每日推送上限 5 条（信号强度排序后取 top 5）
- 数据拉取超时统一 900 秒，超时后标注数据状态而非崩溃
- LLM 调用失败不阻塞主流程，降级为纯定量分析
