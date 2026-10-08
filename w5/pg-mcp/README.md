# PostgreSQL MCP 服务器

一个生产级的 [Model Context Protocol (MCP)](https://modelcontextprotocol.io) 服务器，使用户能够通过自然语言与 PostgreSQL 数据库进行交互。该服务器基于 FastMCP 构建，将自然语言问题转换为安全的 SQL 查询，执行查询并验证结果。一些参考文档：

- Python Postgres MCP 需求研究
: <https://gemini.google.com/share/c87a73f0969b>
- SQLGlot 深度研究方案
: <https://gemini.google.com/share/cc5e45c76c8f>

## 功能特性

- **自然语言转 SQL**：使用 GPT-5.2-mini 将普通英文问题转换为优化的 PostgreSQL 查询
- **多数据库支持**：通过 `DATABASES` JSON 配置多个数据库，请求按名称路由到对应执行器；单库配置完全向后兼容
- **安全至上**：只读强制执行、表/列黑名单、危险函数黑名单、EXPLAIN 策略（ANALYZE 恒拒）、SQL 注入防护、查询超时控制
- **结果验证**：基于 AI 的结果验证与置信度评分；不可接受的结果被拒绝（`low_confidence`），低置信度结果附带警告
- **Schema 智能化**：自动 Schema 缓存，基于 TTL 的刷新机制与 LRU 容量上限
- **弹性防护**：查询/LLM 双维度并发限流（超时返回 `rate_limit_exceeded`）、LLM 熔断器、数据库瞬时错误指数退避重试
- **可观测性**：Prometheus 指标全链路埋点、request_id 贯穿日志与四个阶段 span、`/health` 健康端点
- **MCP 兼容**：支持 Claude Desktop 和任何 MCP 兼容客户端

## 快速开始

### 前置条件

- Python 3.14+
- PostgreSQL 12+
- OpenAI API 密钥（用于 GPT-5.2-mini）
- UV 包管理器（推荐）或 pip

### 安装

#### 使用 UV（推荐）

```bash
# 克隆仓库
git clone <repository-url>
cd pg-mcp

# 安装依赖
uv sync

# 复制环境配置模板
cp .env.example .env

# 编辑 .env 并配置参数
vi .env
```

#### 使用 pip

```bash
# 克隆仓库
git clone <repository-url>
cd pg-mcp

# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # Windows 系统: .venv\Scripts\activate

# 安装依赖
pip install -e .

# 复制环境配置模板
cp .env.example .env

# 编辑 .env 并配置参数
vi .env
```

### 配置

编辑 `.env` 文件以配置您的设置：

```bash
# 数据库配置（单库模式；多库见下方"多数据库配置"）
DATABASE_HOST=localhost
DATABASE_PORT=5432
DATABASE_NAME=your_database
DATABASE_USER=your_user
DATABASE_PASSWORD=your_password

# OpenAI 配置
OPENAI_API_KEY=sk-your-api-key-here
OPENAI_MODEL=gpt-5.2-mini

# 安全设置（可选，显示默认值）
SECURITY_BLOCKED_TABLES=secret_data
SECURITY_BLOCKED_COLUMNS=password_hash,users.api_key
SECURITY_ALLOW_EXPLAIN=false
SECURITY_MAX_ROWS=10000
SECURITY_MAX_EXECUTION_TIME=30
```

完整的配置选项请参考 `.env.example`。

### 多数据库配置

设置 `DATABASES` 环境变量（JSON 列表）即可启用多数据库。每个条目与 `DatabaseConfig`
字段一致，未指定的字段继承上方 `DATABASE_*` 默认值：

```bash
DATABASES=[{"name":"prod","host":"db.example.com","user":"readonly","password":"secret1"},{"name":"analytics","host":"db2.example.com","port":5433,"user":"readonly","password":"secret2"}]
```

行为要点：

- 配置了**多个**数据库时，`query` 工具的 `database` 参数变为**必填**；
  未指定时返回错误并列出可用数据库；只配置一个数据库时自动选择。
- 每个数据库拥有独立连接池与执行器，请求严格路由到指定库——
  指定 B 库的请求绝不会在 A 库上执行。
- 所有池在启动时创建；任一数据库不可达则启动失败（fail-fast）。
- 表/列黑名单与 EXPLAIN 策略对全部数据库统一生效。
- 未设置 `DATABASES` 时回退到单库 `DATABASE_*` 配置，旧配置零改动。

### 运行服务器

#### 独立模式

```bash
# 使用 UV
uv run python main.py

# 或使用 pip
python main.py
```

#### 与 Claude Desktop 集成

添加以下配置到 Claude Desktop MCP 设置文件：

**macOS/Linux**: `~/Library/Application Support/Claude/claude_desktop_config.json`

**Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "postgres": {
      "command": "uv",
      "args": [
        "--directory",
        "/absolute/path/to/pg-mcp",
        "run",
        "python",
        "main.py"
      ],
      "env": {
        "DATABASE_HOST": "localhost",
        "DATABASE_NAME": "your_database",
        "DATABASE_USER": "your_user",
        "DATABASE_PASSWORD": "your_password",
        "OPENAI_API_KEY": "sk-your-api-key-here"
      }
    }
  }
}
```

详细配置说明请参阅 [Claude Desktop 配置](#claude-desktop-配置)。

## 使用方法

### 示例查询

通过 Claude Desktop 或其他 MCP 客户端连接后，您可以提出自然语言问题：

#### 简单查询

```
How many tables are in the database?
→ SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public'

Show me all users
→ SELECT * FROM users LIMIT 10000

What are the column names in the products table?
→ SELECT column_name, data_type FROM information_schema.columns
  WHERE table_name = 'products'
```

#### 分析查询

```
What are the top 10 products by sales?
→ SELECT product_name, SUM(quantity * price) as total_sales
  FROM orders
  GROUP BY product_name
  ORDER BY total_sales DESC
  LIMIT 10

How many users registered in the last 30 days?
→ SELECT COUNT(*) FROM users
  WHERE created_at > CURRENT_DATE - INTERVAL '30 days'
```

#### 仅 SQL 模式

您也可以只请求 SQL 而不执行：

```
Generate SQL to find duplicate emails
Return Type: sql
→ Returns: SELECT email, COUNT(*) FROM users GROUP BY email HAVING COUNT(*) > 1
```

### 返回类型

服务器支持两种返回类型：

- **`result`**（默认）：执行查询并返回结果
- **`sql`**：生成并验证 SQL，但不执行

### 响应格式

#### 成功查询响应

```json
{
  "success": true,
  "request_id": "9b2f0c3a-1d4e-4f5a-8b6c-7d8e9f0a1b2c",
  "generated_sql": "SELECT COUNT(*) FROM users",
  "data": {
    "columns": ["count"],
    "rows": [{"count": 1523}],
    "row_count": 1,
    "execution_time_ms": 23.0
  },
  "confidence": 95,
  "tokens_used": 234
}
```

字段说明：`request_id` 用于问题定位与日志关联；`tokens_used` 来自 OpenAI API 的
usage 统计，无法获取时按 `exclude_none` 语义整体省略（不会伪造 0）；
低置信度结果（低于 `VALIDATION_MIN_CONFIDENCE_SCORE`）会附带 `warning` 字段，
内容含置信度百分比、解释与建议。

#### 错误响应

```json
{
  "success": false,
  "request_id": "9b2f0c3a-1d4e-4f5a-8b6c-7d8e9f0a1b2c",
  "error": {
    "code": "security_violation",
    "message": "Access to table 'secret_data' is not allowed",
    "details": {}
  }
}
```

错误码全集见 `src/pg_mcp/models/errors.py` 的 `ErrorCode`，包括：
`security_violation`（黑名单/危险操作）、`sql_parse_error`、`rate_limit_exceeded`
（并发限流）、`low_confidence`（结果校验不可接受）、`llm_unavailable`（熔断）、
`database_connection_error`（指定库不可用）、`question_too_long` 等。

## 与设计文档的偏差记录

实现相对 `specs/w5/` 设计文档有以下有意偏差，均有明确理由：

| 偏差点 | 设计原文 | 实际实现 | 原因 |
|---|---|---|---|
| 多数据库配置载体 | PRD §3.4：YAML 列表 | `DATABASES` JSON 环境变量，未设置时回退 `DATABASE_*` 单库 | 避免引入 pyyaml 依赖；与现有 pydantic-settings env 体系统一 |
| 链路追踪 | PRD §3.3：OpenTelemetry 标准，导出 Jaeger/Zipkin | 自研 ContextVar 方案：request_id 贯穿 + 四阶段 span 结构化日志 | 零新增依赖；request_id 关联承诺完整兑现；OTel 导出可后续按需引入 |
| 限流语义 | PRD §3.1：队列满返回 HTTP 429 | 信号量并发限流，等待超限返回 `rate_limit_exceeded` 错误码 | MCP 工具返回 dict，无 HTTP 层；错误码承载等价语义 |
| EXPLAIN | 设计仅布尔开关 `allow_explain` | 同一开关 + **EXPLAIN ANALYZE 恒拒** | ANALYZE 会真实执行内层语句；与测试计划 0007 §3.1 期望一致 |
| 错误码命名 | PRD §6.1：`RATE_LIMITED`/`DB_CONNECTION_ERROR` 等 | `rate_limit_exceeded`/`database_connection_error` 等 | 与实现既有 `ErrorCode` 表保持一致，语义等价 |
| `EXPLAIN DELETE` 计划查看 | 无明确禁令 | 允许（仅计划展示不执行） | EXPLAIN 本身只产出计划，无副作用 |

## 架构

### 核心组件

```
┌─────────────────────────────────────────────────────────────┐
│                      MCP Server (FastMCP)                   │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    Query Orchestrator                       │
│  - Coordinates all components                               │
│  - Manages retry logic                                      │
│  - Handles error recovery                                   │
└─────────────────────────────────────────────────────────────┘
           │                  │                  │
           ▼                  ▼                  ▼
    ┌───────────┐     ┌────────────┐     ┌──────────────┐
    │   SQL     │     │    SQL     │     │     SQL      │
    │ Generator │────▶│ Validator  │────▶│  Executor    │
    │ (LLM)     │     │ (Security) │     │ (Database)   │
    └───────────┘     └────────────┘     └──────────────┘
           │                                      │
           ▼                                      ▼
    ┌───────────┐                          ┌──────────────┐
    │  Schema   │                          │   Result     │
    │  Cache    │                          │  Validator   │
    └───────────┘                          │  (LLM)       │
                                           └──────────────┘
```

### 安全特性

1. **只读强制执行**：默认仅允许 SELECT 查询（语句类型白名单，写操作一律拒绝）
2. **表/列黑名单**：`SECURITY_BLOCKED_TABLES` / `SECURITY_BLOCKED_COLUMNS` 配置敏感对象保护；
   表名同时匹配裸名与 `schema.table` 全名，列名支持 `table.column` 限定
3. **EXPLAIN 策略**：`SECURITY_ALLOW_EXPLAIN`（默认 `false`）控制普通 EXPLAIN；
   **EXPLAIN ANALYZE 恒拒**——ANALYZE 会真实执行内层语句
4. **阻止危险函数**：内置 19 个危险函数黑名单（pg_sleep、文件 I/O、dblink 等）+ 配置追加
5. **SQL 解析**：使用 sqlglot 进行准确的 SQL 结构验证（含子查询安全检查）
6. **注入防护**：search_path 锁定（`SECURITY_SAFE_SEARCH_PATH`）与可选只读角色切换
   （`SECURITY_READONLY_ROLE`）
7. **资源限制**：行数限制（默认 10,000）、查询超时（默认 30 秒）、连接池管理
8. **事务隔离**：所有查询在只读事务中运行

### 弹性特性

- **并发限流**：查询维度（整个流水线）与 LLM 维度（生成/结果校验）双信号量限流；
  等待超过 `RESILIENCE_RATE_LIMIT_TIMEOUT` 秒返回 `rate_limit_exceeded`
- **熔断器**：LLM 连续失败达到阈值后熔断，快速失败并自动探测恢复
- **重试与退避**：LLM 校验失败带错误反馈重新生成（指数退避间隔）；
  数据库瞬时错误（连接中断等）自动重试 `RESILIENCE_DB_RETRY_ATTEMPTS` 次
- **连接池**：每数据库独立连接池，优雅关闭带超时兜底
- **Schema 缓存**：TTL 过期 + `CACHE_MAX_SIZE` LRU 逐出

### 可观测性

- **指标**：Prometheus 全链路埋点（见"监控"一节）
- **追踪**：每个请求生成 `request_id`，经 ContextVar 贯穿四个阶段 span
  （`sql_generation` / `sql_validation` / `sql_execution` / `result_validation`），
  日志 JSON 每行自动附带 `request_id`；响应体同样返回 `request_id` 便于关联
- **健康检查**：`/health` HTTP 端点（HTTP/SSE transport 下可用）报告各库池状态、
  缓存年龄、熔断器状态与运行时长

## 配置参考

### 数据库设置

| 变量                       | 描述            | 默认值      |
|----------------------------|-----------------|-------------|
| `DATABASE_HOST`            | PostgreSQL 主机 | `localhost` |
| `DATABASE_PORT`            | PostgreSQL 端口 | `5432`      |
| `DATABASE_NAME`            | 数据库名称      | 必需        |
| `DATABASE_USER`            | 数据库用户      | 必需        |
| `DATABASE_PASSWORD`        | 数据库密码      | 必需        |
| `DATABASE_MIN_POOL_SIZE`   | 池中最小连接数  | `5`         |
| `DATABASE_MAX_POOL_SIZE`   | 池中最大连接数  | `20`        |
| `DATABASE_COMMAND_TIMEOUT` | 查询超时（秒）    | `30`        |

### OpenAI 设置

| 变量                 | 描述                    | 默认值         |
|----------------------|-------------------------|----------------|
| `OPENAI_API_KEY`     | OpenAI API 密钥         | 必需           |
| `OPENAI_MODEL`       | 使用的模型              | `gpt-5.2-mini` |
| `OPENAI_MAX_TOKENS`  | 每次请求的最大 token 数 | `32000`        |
| `OPENAI_TEMPERATURE` | 模型温度                | `0.0`          |
| `OPENAI_TIMEOUT`     | API 超时（秒）            | `30`           |

### 安全设置

| 变量                              | 描述                      | 默认值            |
|-----------------------------------|---------------------------|-------------------|
| `SECURITY_BLOCKED_TABLES`         | 禁止访问的表（逗号分隔，支持 `schema.table`） | 空 |
| `SECURITY_BLOCKED_COLUMNS`        | 禁止访问的列（逗号分隔，支持 `table.column`） | 空 |
| `SECURITY_ALLOW_EXPLAIN`          | 允许普通 EXPLAIN（ANALYZE 恒拒） | `false` |
| `SECURITY_BLOCKED_FUNCTIONS`      | 逗号分隔的函数黑名单      | 参考 .env.example |
| `SECURITY_MAX_ROWS`               | 每个查询的最大行数        | `10000`           |
| `SECURITY_MAX_EXECUTION_TIME`     | 查询超时（秒）              | `30`              |
| `SECURITY_READONLY_ROLE`          | 查询会话切换到的只读角色  | 空 |
| `SECURITY_SAFE_SEARCH_PATH`       | 会话 search_path 锁定     | `public`          |

### 缓存设置

| 变量               | 描述                | 默认值 |
|--------------------|---------------------|--------|
| `CACHE_ENABLED`    | 启用 Schema 缓存    | `true` |
| `CACHE_SCHEMA_TTL` | Schema 缓存 TTL（秒） | `3600` |
| `CACHE_MAX_SIZE`   | 最大缓存 Schema 数（LRU 逐出）  | `100`  |

### 弹性设置

| 变量                                   | 描述             | 默认值 |
|----------------------------------------|------------------|--------|
| `RESILIENCE_MAX_CONCURRENT_QUERIES`    | 最大并发查询数   | `10`   |
| `RESILIENCE_MAX_CONCURRENT_LLM_CALLS`  | 最大并发 LLM 调用数 | `5` |
| `RESILIENCE_RATE_LIMIT_TIMEOUT`        | 限流槽位等待超时（秒） | `5.0` |
| `RESILIENCE_MAX_RETRIES`               | LLM 校验重试次数 | `3`    |
| `RESILIENCE_RETRY_DELAY`               | 初始重试延迟（秒） | `1.0`  |
| `RESILIENCE_BACKOFF_FACTOR`            | 指数退避倍数     | `2.0`  |
| `RESILIENCE_DB_RETRY_ATTEMPTS`         | 数据库瞬时错误重试次数 | `3` |
| `RESILIENCE_CIRCUIT_BREAKER_THRESHOLD` | 熔断前的失败数   | `5`    |
| `RESILIENCE_CIRCUIT_BREAKER_TIMEOUT`   | 熔断器超时（秒）   | `60`   |

### 可观测性设置

| 变量                            | 描述                 | 默认值 |
|---------------------------------|----------------------|--------|
| `OBSERVABILITY_METRICS_ENABLED` | 启用 Prometheus 指标 | `true` |
| `OBSERVABILITY_METRICS_PORT`    | 指标 HTTP 端口       | `9090` |
| `OBSERVABILITY_LOG_LEVEL`       | 日志级别             | `INFO` |
| `OBSERVABILITY_LOG_FORMAT`      | 日志格式（json/text）  | `json` |

## 开发

### 设置开发环境

```bash
# 安装开发依赖
uv sync --all-extras

# 安装 pre-commit 钩子（可选）
pre-commit install
```

### 运行测试

```bash
# 运行默认测试套件（单元 + mock 驱动，无需外部服务；CI 友好）
uv run pytest -m "not integration"

# 运行并生成覆盖率报告（门槛 80%）
uv run pytest -m "not integration" --cov=src --cov-report=html

# 运行需要真实 PostgreSQL + OpenAI 的集成/端到端测试
# （未配置环境时这些用例会自动跳过）
DATABASE_HOST=localhost DATABASE_NAME=testdb OPENAI_API_KEY=sk-xxx \
  uv run pytest -m integration

# 运行特定测试类别
uv run pytest tests/unit/          # 仅单元测试
uv run pytest tests/integration/   # 集成测试（需真实服务）
uv run pytest tests/e2e/           # 端到端测试（需真实服务）
```

> 仓库根目录的 `test_shutdown.py` 是手动验证脚本（需人工 Ctrl+C 触发），
> 不在 pytest 默认收集范围内。

### 代码质量

```bash
# 类型检查
uv run mypy src

# Lint 和格式化
uv run ruff check --fix .
uv run ruff format .

# 运行所有质量检查
uv run pytest --cov=src --cov-fail-under=80
uv run mypy src
uv run ruff check .
```

### 项目结构

```
pg-mcp/
├── src/pg_mcp/
│   ├── cache/              # Schema 缓存
│   ├── config/             # 配置管理
│   ├── db/                 # 数据库连接池
│   ├── models/             # 数据模型
│   ├── observability/      # 日志、指标、追踪
│   ├── prompts/            # LLM Prompt 模板
│   ├── resilience/         # 熔断器、限流器
│   ├── services/           # 核心业务逻辑
│   │   ├── orchestrator.py      # 查询协调
│   │   ├── sql_generator.py     # 基于 LLM 的 SQL 生成
│   │   ├── sql_validator.py     # 安全验证
│   │   ├── sql_executor.py      # 查询执行
│   │   └── result_validator.py  # 结果验证
│   └── server.py           # FastMCP 服务器
├── tests/
│   ├── unit/               # 单元测试
│   ├── integration/        # 集成测试
│   └── e2e/                # 端到端测试
├── fixtures/               # 测试数据库 fixture
├── .env.example            # 环境模板
├── pyproject.toml          # 项目配置
└── main.py                 # 入口点
```

## Docker 部署

### 构建镜像

```bash
docker build -t pg-mcp:latest .
```

### 运行容器

```bash
docker run -d \
  --name pg-mcp \
  -e DATABASE_HOST=your-db-host \
  -e DATABASE_NAME=your-db \
  -e DATABASE_USER=your-user \
  -e DATABASE_PASSWORD=your-password \
  -e OPENAI_API_KEY=sk-your-key \
  -p 9090:9090 \
  pg-mcp:latest
```

### Docker Compose

```bash
# 启动所有服务（PostgreSQL + pg-mcp）
docker-compose up -d

# 查看日志
docker-compose logs -f pg-mcp

# 停止服务
docker-compose down
```

详细配置参考 `docker-compose.yml`。

## 监控

### 指标

服务器在端口 9090（可配置）上暴露 Prometheus 指标：

```bash
curl http://localhost:9090/metrics
```

**可用指标：**

- `pg_mcp_query_requests_total{status,database}` - 请求计数（按状态与目标库）
- `pg_mcp_query_duration_seconds` - 端到端请求耗时直方图
- `pg_mcp_llm_calls_total{operation}` - LLM 调用计数（generate_sql / validate_result）
- `pg_mcp_llm_latency_seconds{operation}` - LLM 调用延迟直方图
- `pg_mcp_llm_tokens_used{operation}` - LLM token 使用量（来自 API usage）
- `pg_mcp_sql_rejected_total{reason}` - 安全校验拒绝计数
- `pg_mcp_db_query_duration_seconds` - 数据库查询耗时直方图
- `pg_mcp_db_connections_active{database}` - 各库连接池活跃连接数
- `pg_mcp_schema_cache_age_seconds{database}` - Schema 缓存年龄

### 健康检查

```bash
# HTTP/SSE transport 下可用（stdio 模式无 HTTP 面）
curl http://localhost:8000/health
```

返回各数据库池大小、缓存年龄、LLM 熔断器状态与运行时长。

### 日志

结构化 JSON 日志（或文本格式）输出到标准输出。每行自动携带 `request_id`
（来自请求上下文），可与响应体中的 `request_id` 精确关联：

```json
{
  "timestamp": "2025-12-20T10:30:00.123Z",
  "level": "INFO",
  "logger": "pg_mcp.services.orchestrator",
  "message": "SQL executed successfully",
  "request_id": "3f2a...c1",
  "extra": {
    "operation": "sql_execution",
    "row_count": 42,
    "execution_time_ms": 23.1
  }
}
```

四个关键 span 以 `operation` 字段标记：`sql_generation`、`sql_validation`（重试环内逐次）、
`sql_execution`、`result_validation`。

## 故障排查

### 常见问题

#### 连接被拒绝

```
Error: Connection to database failed
```

**解决方案**：验证 PostgreSQL 正在运行且凭证正确：

```bash
psql -h $DATABASE_HOST -U $DATABASE_USER -d $DATABASE_NAME
```

#### OpenAI API 错误

```
Error: OpenAI API request failed
```

**解决方案**：

1. 检查 API 密钥是否有效且有额度
2. 验证网络连接
3. 如果请求超时，检查 `OPENAI_TIMEOUT` 设置

#### 查询超时

```
Error: Query execution timeout exceeded
```

**解决方案**：

1. 增加 `SECURITY_MAX_EXECUTION_TIME`
2. 优化数据库（添加索引、VACUUM）
3. 简化查询或添加过滤条件

#### Schema 缓存问题

```
Error: Schema not found in cache
```

**解决方案**：

1. 重启服务器以重新加载 Schema
2. 验证数据库用户有 Schema 读取权限
3. 检查 `CACHE_ENABLED` 是否设置为 `true`

### 调试模式

启用调试日志：

```bash
export OBSERVABILITY_LOG_LEVEL=DEBUG
uv run python main.py
```

## Claude Desktop 配置

### macOS/Linux 配置

编辑 `~/Library/Application Support/Claude/claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "postgres": {
      "command": "uv",
      "args": [
        "--directory",
        "/Users/yourname/projects/pg-mcp",
        "run",
        "python",
        "main.py"
      ],
      "env": {
        "DATABASE_HOST": "localhost",
        "DATABASE_PORT": "5432",
        "DATABASE_NAME": "mydb",
        "DATABASE_USER": "postgres",
        "DATABASE_PASSWORD": "your-password",
        "OPENAI_API_KEY": "sk-your-api-key-here",
        "OPENAI_MODEL": "gpt-5.2-mini",
        "SECURITY_MAX_ROWS": "10000",
        "CACHE_ENABLED": "true",
        "OBSERVABILITY_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

### Windows 配置

编辑 `%APPDATA%\Claude\claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "postgres": {
      "command": "uv",
      "args": [
        "--directory",
        "C:\\Users\\YourName\\projects\\pg-mcp",
        "run",
        "python",
        "main.py"
      ],
      "env": {
        "DATABASE_HOST": "localhost",
        "DATABASE_NAME": "mydb",
        "DATABASE_USER": "postgres",
        "DATABASE_PASSWORD": "your-password",
        "OPENAI_API_KEY": "sk-your-api-key-here"
      }
    }
  }
}
```

### 使用 Python Virtualenv

如果不使用 UV，请直接配置 Python：

```json
{
  "mcpServers": {
    "postgres": {
      "command": "/absolute/path/to/pg-mcp/.venv/bin/python",
      "args": ["main.py"],
      "cwd": "/absolute/path/to/pg-mcp",
      "env": {
        "DATABASE_HOST": "localhost",
        ...
      }
    }
  }
}
```

### 重启 Claude Desktop

编辑配置后：

1. 完全退出 Claude Desktop
2. 重启 Claude Desktop
3. PostgreSQL MCP 服务器将可用

## 安全考虑

### 生产环境部署

1. **使用只读数据库用户**：创建专用 PostgreSQL 用户，仅具有 SELECT 权限：

```sql
CREATE USER pg_mcp_readonly WITH PASSWORD 'secure-password';
GRANT CONNECT ON DATABASE your_database TO pg_mcp_readonly;
GRANT USAGE ON SCHEMA public TO pg_mcp_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO pg_mcp_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT ON TABLES TO pg_mcp_readonly;
```

2. **保护 API 密钥**：使用环境变量或秘密管理系统，切勿提交到版本控制

3. **网络隔离**：在隔离网络中运行服务器，通过 IP 限制数据库访问

4. **监控使用**：启用指标并为异常模式设置告警

5. **限流**：配置合适的限流参数以防止滥用

6. **日志清理**：敏感数据会自动从日志中过滤

## 许可证

[您的许可证信息]

## 贡献

欢迎贡献！请参阅 CONTRIBUTING.md 了解指南。

## 支持

如有问题和疑问：

- GitHub Issues：[repository-url]/issues
- 文档：查看 `specs/w5/` 目录获取详细设计文档

## 致谢

- 基于 [FastMCP](https://github.com/jlowin/fastmcp) 构建
- SQL 解析由 [sqlglot](https://github.com/tobymao/sqlglot) 提供
- 数据库驱动：[asyncpg](https://github.com/MagicStack/asyncpg)
