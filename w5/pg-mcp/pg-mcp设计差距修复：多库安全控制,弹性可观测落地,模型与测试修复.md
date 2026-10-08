# pg-mcp 设计差距修复：多库安全控制 / 弹性可观测落地 / 模型与测试修复

## Context

`E:\ai_lessons\geektime-bootcamp-ai-lesson\w5\pg-mcp` 是设计文档驱动开发的 PostgreSQL MCP 服务器（`specs/w5` 0001–0008 共 8 篇文档）。0006 代码评审确认三大差距，本计划将其落地：

1. **多数据库与安全控制未启用**：`server.py:101` 只建单池，`server.py:197` 注入单一执行器（`orchestrator.py:198` 固定用它执行，与 `_resolve_database` 解析结果脱节——指定 B 库实际跑在 A 库）；`create_pools`（pool.py:50-79）零调用；表/列黑名单校验代码存在（sql_validator.py:236-283）但 server.py:155-157 硬编码传 `None`，且 settings.py 无对应配置字段；`allow_explain` 同样是构造参数硬编码 False。
2. **弹性/可观测"装饰性接线"**：`MultiRateLimiter` 实例化后零调用；`retry_delay`/`backoff_factor` 死配置、无 DB 瞬时错误重试；`MetricsCollector` 九个指标零埋点（/metrics 恒 0）；tracing.py 整模块无引用（request_id 用局部 uuid，不进 contextvar）；server.py:181 还有个死熔断器实例（真身在 orchestrator.py:99）；`RateLimitExceededError`/`LOW_CONFIDENCE` 错误码永不会被抛出。
3. **模型缺陷与测试缺口**：`QueryResponse.to_dict` 重复定义（query.py:160 vs 214，后者覆盖前者，tokens_used 兜底失效，靠 server.py:359-361 外层补救）；`ErrorDetail` 在 errors.py:39 与 query.py:139 同名冲突；10 个配置字段/属性定义未消费；tokens_used 恒 None（未从 OpenAI usage 提取）；e2e/integration 直连真实 PG+OpenAI 且断言非确定（`if success` 才断言），ResultValidator/observability/多库路由零覆盖。

**已确认取舍**（用户选定）：多库配置用 JSON 环境变量（保留 `DATABASE_*` 单库回退）；追踪接线自研 tracing.py（不引 OTel）；配置字段区分处理（有语义的接线、无依据的删除）；测试为新功能全测 + 存量加标记 skip。

**记录在案的设计偏差**（写进 README）：多库载体 JSON env 替代 PRD 的 YAML（避免 pyyaml 依赖）；追踪用自研 contextvar 方案替代 OTel 导出（保留 request_id 贯穿承诺）；限流为信号量并发控制，队列满语义由 `RATE_LIMITED` 错误码承载（MCP 工具返回 dict，无 HTTP 429）。

---

## 一、多数据库与安全控制

### 1.1 配置层 `src/pg_mcp/config/settings.py`

- `Settings` 新增 `databases: list[DatabaseConfig]`：env 名 `DATABASES`，pydantic-settings 自动按 JSON 解析（如 `DATABASES='[{"name":"prod","host":"..."},...]'`）。
- 兼容回退：model_validator 中，`DATABASES` 未设置时 `databases = [self.database]`（沿用现有 `DATABASE_*` 前缀字段，单库用户零改动）。
- 校验（对齐设计 0002 §4.1）：非空；name 必须唯一；单库时 name 允许与 `database.name` 一致。
- `SecurityConfig` 新增三个字段（设计 0002 §4.1 原文字段）：
  - `blocked_tables: list[str] = []`（env `SECURITY_BLOCKED_TABLES`，JSON 列表）
  - `blocked_columns: list[str] = []`（`SECURITY_BLOCKED_COLUMNS`）
  - `allow_explain: bool = False`（`SECURITY_ALLOW_EXPLAIN`）
- `ResilienceConfig` 新增：`max_concurrent_queries: int = 10`、`max_concurrent_llm_calls: int = 5`（对齐设计 0002 §4.1；消除 server.py:188-189 硬编码）、`db_retry_attempts: int = 3`（设计承诺的 DB 瞬时错误重试次数）、`rate_limit_timeout: float = 5.0`（等待限流槽位的超时，超时返回 RATE_LIMITED；等价设计 0002 §7.2 的快速失败语义但给足等待窗口）。
- 删除（按"区分处理"决策）：`SecurityConfig.allow_write_operations`、`DatabaseConfig.dsn`、`DatabaseConfig.safe_dsn`、`Settings.is_production`、`Settings.is_development`；同步删 test_config.py 中对应断言。

### 1.2 池与执行器装配 `src/pg_mcp/server.py` + `src/pg_mcp/services/orchestrator.py`

- lifespan 改用 `pools = await create_pools(_settings.databases)`（复用 pool.py:50-79，含失败聚合语义，实现时核对其中逐库异常处理）。
- per-db executor 循环保留，但修 `server.py:166` 的 bug：`db_config` 传**各库自己的** `DatabaseConfig`（当前统一传 `_settings.database`，导致 readonly_role/search_path 等按错误库的配置执行）。
- `QueryOrchestrator.__init__`（orchestrator.py:66-102）：`sql_executor: SQLExecutor` 参数改为 `executors: dict[str, SQLExecutor]`。
- `execute_query` 执行步骤（orchestrator.py:198）改为 `executor = self.executors.get(database_name)`，取不到抛 `DatabaseError(DB_CONNECTION_ERROR)`（对齐设计 0002 §4.7 的 `self.executors.get(db_name)`）。
- `_resolve_database`（orchestrator.py:280-325）逻辑已正确，无需改：指定库校验存在性、单库自动选、多库必填。
- `close_pools`（server.py:240）保持。

### 1.3 安全校验接线 `src/pg_mcp/server.py` + `src/pg_mcp/services/sql_validator.py`

- server.py:153-158 改为传配置：`blocked_tables=_settings.security.blocked_tables, blocked_columns=_settings.security.blocked_columns, allow_explain=_settings.security.allow_explain`（validator 内部检查逻辑 _check_blocked_tables/_check_blocked_columns 已存在，仅需接线）。
- **EXPLAIN 策略**（sql_validator.py:152-168 现有分支上增强）：`allow_explain=False` 时 EXPLAIN/EXPLAIN ANALYZE 全拒（现状保持）；`allow_explain=True` 时**仅放行普通 EXPLAIN**，`EXPLAIN ANALYZE` 一律拒绝（ANALYZE 会真实执行语句——与 0007 测试计划 `test_explain_analyze_blocked_by_default` 语义一致）。实现：解析 command 文本含 `ANALYZE`/`ANALYSE` 后缀即拒。
- 黑名单匹配语义不变：表同时匹配裸名与 `schema.table` 全名；列支持 `table.column` 限定名与裸列名。

### 1.4 confidence 与 question 长度接线

- `ValidationConfig.max_question_length`：server.py query 工具在构造 `QueryRequest` 前校验 `len(question)`，超限返回 `BAD_REQUEST`（`QueryRequest` 模型内硬编码的 `max_length=10000` 改为从 settings 传入的宽松上限或保留双保险，以 settings 校验为准并返回明确 message）。
- `ValidationConfig.min_confidence_score`：orchestrator 组装响应前（orchestrator.py:220-235 处）：
  - 结果校验返回 `is_acceptable=False` → 抛/构造 `LOW_CONFIDENCE` 错误（启用现有 `ErrorCode.LOW_CONFIDENCE`，design 0001 §6.1）；
  - `0 < confidence < min_confidence_score` → 按设计 0001 §6.2 在 response 附 `warning`（"置信度较低 (X%): explanation 建议: suggestion"），不失败。

---

## 二、弹性与可观测性整合进请求链路

### 2.1 限流接线

- server.py：`MultiRateLimiter(query_limit=_settings.resilience.max_concurrent_queries, llm_limit=_settings.resilience.max_concurrent_llm_calls)`，实例注入 orchestrator（新构造参数 `rate_limiter`），删除硬编码 10/5。
- **查询维度**：server.py query 工具内，调用 orchestrator 前后包 `await rate_limiter.query_limiter.acquire(timeout=_settings.resilience.rate_limit_timeout)` / `release()`；acquire 失败 → 返回 `RATE_LIMITED` 错误响应（复用错误响应构造路径，message 带 suggestion"请稍后重试"）。
- **LLM 维度**：orchestrator `_generate_sql_with_retry`（sql_generator.generate 外）与 `_validate_results_safely`（result_validator.validate 外）分别用 `async with rate_limiter.for_llm(timeout=...)` 包裹；超时同样映射 `RateLimitExceededError`。
- 注意死锁顺序：query 槽先取、LLM 槽后取，统一顺序无循环等待。

### 2.2 重试/退避：新建 `src/pg_mcp/resilience/retry.py`

- 实现 `async def async_retry(operation, *, attempts, delay, backoff_factor, retry_on: tuple[type[Exception], ...], sleep=asyncio.sleep) -> T`：指数退避 `delay * backoff_factor**attempt`；`sleep` 参数注入便于测试免真实等待。
- **DB 瞬时错误**（`services/sql_executor.py`）：`execute` 的 `conn.fetch` 外包 async_retry，`retry_on=(asyncpg.exceptions.ConnectionDoesNotExistError, asyncpg.exceptions.ConnectionFailureError, asyncpg.InterfaceError, OSError)`，`attempts=resilience.db_retry_attempts, delay=resilience.retry_delay, backoff_factor=resilience.backoff_factor`；executor 构造函数需新增 resilience_config 注入（server.py 装配处传入）。非瞬时错误（如 SecurityViolation、语法错误）不重试，直接抛。
- **LLM 校验重试**（orchestrator.py:377 循环）：现有重试环（带 error_feedback 重新生成）保持，两次尝试之间 `await asyncio.sleep(resilience.retry_delay * backoff_factor**attempt)`，消费两个死配置字段。
- 熔断器保持现状（orchestrator 内实例工作正常），删除 server.py:181-184 死实例及对应 import/global。

### 2.3 指标埋点（MetricsCollector 已有 9 个指标，只接线不改定义）

| 埋点 | 位置 | 指标 |
|---|---|---|
| 请求计数 | server query 工具收尾（成功/各错误 status + resolved database） | `increment_query_request(status, database)` |
| 请求耗时 | query 工具整体计时 | `query_duration.time()` |
| LLM 调用/延迟/token | sql_generator.generate（operation="generate_sql"）、result_validator.validate（operation="validate_result"）；token 见 3.4 | `increment_llm_call` / `observe_llm_latency` / `increment_llm_tokens` |
| SQL 拒绝 | orchestrator 捕获 `SecurityViolationError`/`SQLParseError` 处，reason=异常类别 | `increment_sql_rejected(reason)` |
| DB 耗时 | sql_executor.execute 计时 | `observe_db_query_duration` |
| 活跃连接 | query 工具收尾读 `pool.get_size()` | `set_db_connections_active(db, n)` |
| 缓存年龄 | orchestrator 取 schema 后读缓存时间戳 | `set_schema_cache_age(db, age)` |

- 注入方式：`MetricsCollector` 是模块级单例（metrics.py:198 `metrics`），服务层直接 `from pg_mcp.observability.metrics import metrics` 使用（与设计 0002 §4.8 的构造注入不同，但避免 6 处构造签名膨胀；测试隔离见 4.1）。`metrics_enabled=False` 时 server 不启 HTTP 端口（现状保持），埋点调用用 `observability.metrics_enabled` 判断包一层轻量开关或直接容忍（Prometheus Counter 开销极低，倾向不分支，实现时定）。
- metrics.py 单例 `__new__` + `reset_all_metrics` 直接重挂 `_initialize_metrics` 在测试里会造成新旧对象脱节——测试改为通过 `REGISTRY.get_sample_value(...)` 断言（见 4.1），必要时给 MetricsCollector 加 `@classmethod from_registry` 支持测试隔离注册表。**注意**：`prometheus_client` 默认注册表全局唯一，多个测试进程内重复实例化同一指标名会报 `Duplicated timeseries`——单例已规避，测试中不得再 new。

### 2.4 追踪接线（自研 tracing.py）

- orchestrator `execute_query` 整体包 `async with request_context(generate_request_id()) as request_id:`，替换现有局部 `str(uuid.uuid4())`（orchestrator.py:130）；响应体 `request_id` 字段取 contextvar 值（语义不变，仍是响应里的 uuid）。
- 四个关键 span（设计 0001 §3.3）：`sql_generation`（_generate_sql_with_retry 整体）、`sql_validation`（validate_or_raise 调用点）、`sql_execution`（executor.execute 调用点）、`result_validation`（_validate_results_safely 调用点），各用 `trace_async(operation=...)` 装饰或手动计时 + 结构化日志（含耗时 ms）。
- request_id 进日志：现 trace_async 用全局 LogRecordFactory 有并发覆盖问题（同时多请求时 factory 相互覆盖）——**修正为** JSONFormatter/TextFormatter（logging.py）直接从 `get_request_id()` contextvar 取值附加字段，不依赖 record factory；trace_async 简化为纯 span 计时（或仅保留 contextvar 传播）。
- 明确不引入 OTel（用户已选），README 记录偏差。

### 2.5 健康检查

- server.py 增加 `@mcp.custom_route("/health", methods=["GET"])`（FastMCP 2.x 支持；仅 http/sse transport 生效，stdio 下无 HTTP——README 注明）：返回 `{"status":"ok","databases":{name:{"pool_size":n,"cache_age":s}},"circuit_breaker":state,"uptime_seconds":t}`。
- 实现前先验证 `fastmcp==2.14.1` 的 `mcp.custom_route` API（`uv run python -c "from pg_mcp.server import mcp; print(hasattr(mcp,'custom_route'))"`）；若不可用则降级为：lifespan 里用 prometheus 的 `start_http_server` 之外再起一个 `asyncio` 原生最小 handler 不可行时，退化为仅 metrics 端口 + 日志健康行，并在计划执行时如实记录。

---

## 三、模型/响应缺陷修复

### 3.1 `src/pg_mcp/models/query.py`

- 删除 `QueryResponse.to_dict` 第一处重复定义（160-173，含 tokens_used None→0 兜底），保留 214 的 `exclude_none=True` 版本为唯一定义。
- server.py:359-361 的 `tokens_used` 外层补丁删除（配合 3.4 后字段有真实值；无值时按 exclude_none 语义省略键）。

### 3.2 `src/pg_mcp/models/errors.py` ErrorDetail 冲突

- 以 query.py 的 pydantic `ErrorDetail` 为唯一实现；errors.py 的 plain `ErrorDetail`（39-76）删除，`PgMcpError.to_error_detail()`（errors.py:106-112）改为构造 pydantic 版；`models/__init__.py` 导出统一指向 pydantic 版；同步修 test_models.py:442 等引用。

### 3.3 tokens 真实提取

- `sql_generator.py` / `result_validator.py`：从 OpenAI 响应 `response.usage.total_tokens` 提取（生成器现有 `tokens_used=None` 占位处，orchestrator.py:375/396-397）→ 传入 `QueryResult.tokens_used`；同时喂 `increment_llm_tokens`。

### 3.4 `src/pg_mcp/cache/schema_cache.py` max_size 接线

- 缓存 dict 改 `OrderedDict`：`load` 成功后 move_to_end，超 `cache.max_size` 时 `popitem(last=False)` 逐出最旧库；其余逻辑不动。

---

## 四、测试

### 4.1 新增/修改单测（全部 mock 驱动、确定性的，默认集必须绿）

- **test_config.py**：`DATABASES` JSON 解析、未设置回退 `[database]`、重名报错、空列表报错；Security/Resilience 新字段默认值与 env 注入；删除字段的断言清理。
- **test_orchestrator.py**（现有 21 个适配新构造签名 + 新增）：多库路由——`database="analytics"` 时调用 `executors["analytics"]`（用两个 mock executor 断言各自被调）；未知库名 → DB_CONNECTION_ERROR；多库未指定 → BAD_REQUEST；限流 acquire 失败 → RATE_LIMITED；`is_acceptable=False` → LOW_CONFIDENCE；低置信度 → response.warning；request_id 贯穿（contextvar 在 span 内可读、响应含同值）。
- **test_sql_validator.py**（现有 56 个保持）：新增黑名单矩阵——`blocked_tables=["secret_data"]` 拒 `SELECT * FROM secret_data` 与 `s.secret_data`；`blocked_columns=["users.password_hash"]` 限定名拒、裸名按策略；EXPLAIN 三态：默认拒 / allow_explain=True 放行 EXPLAIN / EXPLAIN ANALYZE 始终拒。
- **test_retry.py**（新）：退避时序（注入 fake sleep 记录 delay 序列 `[1.0, 2.0, 4.0]`）、瞬时错误第 3 次成功、次数耗尽抛原异常、非 retry_on 异常立即抛不重试。
- **test_sql_executor.py**（现有 19 个适配 resilience 注入）：瞬时错误重试路径、非瞬时错误不重试。
- **test_models.py**：to_dict 唯一定义回归（无 tokens_used 伪造 0；None 键省略）；ErrorDetail 统一后 to_error_detail 结构。
- **metrics 断言**：新建 `tests/unit/test_metrics_wiring.py`——调用 orchestrator（全 mock）后用 `prometheus_client.REGISTRY.get_sample_value("pg_mcp_query_requests_total", {...})` 断言计数变化；限流拒绝路径断言 status="rate_limited" 样本。conftest 的 `disable_metrics_for_tests` 保留（不开 HTTP 端口），埋点本身照常发生。
- **test_health / server 装配**：lifespan 在 mock create_pools 下装配 N 库 executor；/health 路由函数直调返回结构断言。

### 4.2 存量标注（确定性改造最小化）

- `tests/e2e/test_mcp.py`、`tests/integration/test_full_flow.py`：模块级 `pytestmark = [pytest.mark.integration]` + `pytest.skip("requires live PG & OpenAI", allow_module_level=True)` 条件（探测 `DATABASE_HOST`/`OPENAI_API_KEY` 环境是否齐备）；pyproject markers 已有 `integration`。同时把 `if result.get("success")` 式条件断言改为：配置了真实服务时必须 success（保留现行为仅在标记内生效，不重写用例逻辑）。
- `tests/conftest.py`：新增 `mock_openai_client`（AsyncMock 返回 markdown 包裹 SQL）、`mock_pool/mock_connection`（fetch 返回固定行）、`orchestrator_factory(**overrides)`（组装全 mock 依赖 + 注入 mock rate_limiter/metrics 兼容的新构造签名）三个公共 fixture（对齐 0008 的工厂模式）。
- `test_shutdown.py`（仓库根）：评估并入 tests/ 或加标记，避免默认收集干扰（视其当前依赖定）。

### 4.3 覆盖率门槛

- 保持 `fail_under = 80`；新模块（retry.py、装配改动）由上述单测覆盖。

---

## 五、文档

- `.env.example`：新增 `DATABASES` JSON 示例（两库）、`SECURITY_BLOCKED_TABLES/BLOCKED_COLUMNS/ALLOW_EXPLAIN`、`RESILIENCE_MAX_CONCURRENT_QUERIES/LLM_CALLS/DB_RETRY_ATTEMPTS/RATE_LIMIT_TIMEOUT`；标注 `DATABASE_*` 为单库兼容模式。
- `README.md`：多库配置一节；安全配置一节（黑名单与 EXPLAIN 策略、EXPLAIN ANALYZE 恒拒说明）；可观测一节（metrics 端口、/health 与 transport 关系、request_id 贯穿）；设计偏差记录（JSON env≠YAML、自研 tracing≠OTel）。

---

## 实施顺序（每步可独立验证）

1. **模型与配置**：settings 新字段+回退校验、删字段；models 三处修复；同步 test_config/test_models → `uv run pytest tests/unit/test_config.py tests/unit/test_models.py` 绿。
2. **多库与安全接线**：pool/server/orchestrator 执行器路由 + validator 配置接线 + EXPLAIN ANALYZE 检查 + confidence/长度接线 → 相关单测绿。
3. **弹性**：retry.py + executor 重试 + LLM 退避 sleep + 限流接线 + 删死熔断器 → test_retry/test_orchestrator 绿。
4. **可观测**：metrics 埋点 + tracing contextvar/Formatter + /health → test_metrics_wiring 绿。
5. **测试收尾**：conftest fixtures、存量 integration 标注、test_shutdown 处置 → `uv run pytest -m "not integration"` 全绿。
6. **文档**：.env.example、README。

## 验证

1. `uv run pytest -m "not integration"` 全绿；`uv run pytest --cov -m "not integration"` 覆盖率 ≥80%；`uv run ruff check .`、`uv run mypy src` 无新告警。
2. 手动端到端（需本地 PG）：`fixtures/` 建库，设 `DATABASES='[{"name":"db_a",...},{"name":"db_b",...}]'` + `SECURITY_BLOCKED_TABLES='["secret_data"]'` 启动 server：
   - query 指定 `database="db_b"` → 结果确来自 db_b（建表差异验证）；未指定且多库 → BAD_REQUEST 提示可用库列表；
   - 问敏感表 → SECURITY_VIOLATION + `pg_mcp_sql_rejected_total` 增长；
   - `SECURITY_ALLOW_EXPLAIN=true` 时 EXPLAIN 通过、EXPLAIN ANALYZE 仍拒；默认全拒；
   - `curl localhost:9090/metrics` 各指标非零；`/health` 返回池状态；日志每行含 request_id，四阶段 span 日志齐全；
   - 断开 LLM（错误 key）连发 6 次 → 第 6 次起 LLM_UNAVAILABLE（熔断），恢复后自愈。
3. 限流压测：`max_concurrent_queries=1` 时并发两请求，后者在 rate_limit_timeout 后得 RATE_LIMITED。
