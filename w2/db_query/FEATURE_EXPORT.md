# FEATURE_EXPORT — 查询结果导出功能设计文档

> 版本：1.0 ｜ 日期：2026-08-16
> 涉及仓库路径：`backend/`、`frontend/`、`.claude/commands/`

## 1. 背景与目标

"智能数据库查询工具" 已支持自然语言转 SQL、SQL 执行、结果表格展示与查询历史，但查询结果只停留在页面上，无法落盘留存。本次迭代补齐 **结果导出** 能力，目标有三：

1. **多格式导出**：查询结果可导出为 **CSV** 与 **JSON** 两种格式（满足"至少两种"的要求，且架构上可继续扩展）。
2. **一键自动化**：借助 Claude Code 的自定义 Command（`.claude/commands/export-query.md`），把"执行查询 → 导出结果"两个动作合并为一条 `/export-query` 命令触发，中间还可以自然语言生成 SQL。
3. **主动式交互**：查询成功后，前端主动询问用户是否导出（"Export this result as a CSV or JSON file?"），把功能"送到手边"而不是等用户去找。

## 2. 总体设计

```
用户触发导出（三种入口，殊途同归）
│
├─ ① 前端通知里的按钮（查询成功后主动弹出）
├─ ② Query Results 卡片右上角的常驻 Export CSV / Export JSON 按钮
└─ ③ Claude Code 命令 /export-query <连接名> <自然语言或SQL> [csv|json]
        │
        ▼
前端 handleExport(format) ──POST /api/v1/dbs/{name}/query/export?format=csv|json
（③ 由 Claude 用 curl 直接调同一端点）        │
                                             ▼
                        ┌─────────────────────────────────────┐
                        │ 复用既有执行管线 execute_query_with_service │
                        │ （SQL 校验 → 执行 → 写入查询历史）        │
                        └─────────────────────────────────────┘
                                             │
                                             ▼
                        services/export.py 按格式序列化（stdlib csv / json）
                                             │
                                             ▼
                        Response(Content-Disposition: attachment) → 浏览器下载 / curl -o 落盘
```

### 关键决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 导出在前端拼装 or 后端生成？ | **后端生成** | 与展示解耦；格式、编码、文件名规则只有一份实现；命令行/脚本可复用同一端点 |
| 导出时重新执行 or 导出内存里的结果？ | **重新执行** | 复用既有校验与历史管线（导出行为也进历史，可审计）；避免大结果集驻留内存；上限仍受 1000 行限制保护 |
| 新增依赖？ | **零新增** | 仅用标准库 `csv` / `json`，遵循工程"最小依赖"原则 |
| 文件编码 | CSV 用 `utf-8-sig`（带 BOM），JSON 用 `utf-8` | Excel 直接打开含中文的 CSV 不乱码；JSON 保持无 BOM 标准格式 |

## 3. 后端设计

### 3.1 新增文件：`backend/app/services/export.py`

- `ExportFormat`（Enum）：`csv` / `json`，新格式的扩展点。
- `export_to_csv(result) -> str`：表头取自 `columns`；`None` 落为空单元格；逗号/引号/换行由 `csv` 模块自动转义。
- `export_to_json(result) -> str`：**自描述文档**结构，不止裸行数组——
  ```json
  {
    "columns": [{"name": "id", "dataType": "integer"}],
    "rowCount": 2,
    "executionTimeMs": 12,
    "sql": "SELECT ...",
    "rows": [{"id": 1, "name": null}]
  }
  ```
  保留 `null`（与 CSV 的空单元格语义区分），`ensure_ascii=False` 保证中文可读。
- `_sanitize_cell()`：统一值清洗——`datetime/date → ISO-8601`、`Decimal → 字符串（保精度）`、`bytes → hex`。
- `build_export_filename(db, fmt, now)`：`query_<连接名>_<时间戳>.<ext>`，连接名中非安全字符替换为 `_`。
- `build_content_disposition()`：同时输出 `filename` 与 `filename*=UTF-8''`（RFC 5987），兼容非 ASCII 文件名。

### 3.2 新增端点：`POST /api/v1/dbs/{name}/query/export?format=csv|json`

- 请求体与执行端点一致：`{"sql": "SELECT ..."}`，`format` 缺省 `csv`，非法值返回 422。
- 内部调用 `execute_query_with_service`，**SQL 校验失败 400 / 连接不存在 404**，语义与执行端点对齐。
- 响应：`Content-Disposition: attachment` + 对应 MIME（`text/csv; charset=utf-8` / `application/json; charset=utf-8`），CSV 内容带 BOM。

### 3.3 API 一览（新增部分）

| Method | Path | 说明 |
|---|---|---|
| POST | `/api/v1/dbs/{name}/query/export?format=csv` | 执行并以 CSV 下载 |
| POST | `/api/v1/dbs/{name}/query/export?format=json` | 执行并以 JSON 下载 |

## 4. 前端设计

> 注意：应用**没有挂路由**，`App.tsx` 直接渲染 `Home` 一体化页面（侧边栏选库 → 元数据树 → 查询编辑器 → 结果区）。`pages/queries/execute.tsx`、`pages/databases/*` 是未接入路由的历史页面。**主改动在 `Home.tsx`**；`execute.tsx` 同步做了相同改造以保持一致，防止未来接入路由后行为缺失。

### 4.1 两个入口（均在 Home.tsx）

1. **主动询问通知（需求 3 的核心）**：点击 EXECUTE 且返回行数 > 0 时，右下角弹出通知：
   - 标题 `Query executed successfully`，描述 `N rows returned. Export this result as a CSV or JSON file?`
   - 三个操作：`Not now` / `Export CSV` / `Export JSON`（主按钮）。
   - 固定 key（`export-prompt`）：重复查询自动替换旧通知；重新执行或导出完成后自动关闭。
2. **常驻按钮**：RESULTS 卡片右上角始终提供 `EXPORT CSV` / `EXPORT JSON`，通知关闭后仍可导出。
   - 原实现为前端内存拼装 CSV/JSON，本次统一改为走后端导出端点（编码、文件名、审计一份逻辑）。

### 4.2 下载实现（`handleExport`）

- `axios` 以 `responseType: "blob"` POST 导出端点；
- 优先采用服务端 `Content-Disposition` 中的文件名（解析 `filename*=UTF-8''`，退回 `filename`），否则本地拼一个；
- `URL.createObjectURL` + 隐形 `<a download>` 触发浏览器下载，随后释放；
- 失败分支：错误体是 blob，`await blob.text()` 解析出后端 `detail` 再提示（blob 响应下 axios 不会自动解 JSON）。

### 4.3 类型

`frontend/src/types/query.ts` 新增 `export type ExportFormat = "csv" | "json"`。

## 5. Claude Code 自动化设计（需求 2）

新增项目级自定义 Command：`.claude/commands/export-query.md`，在该项目目录下启动 Claude Code 即可用：

```
/export-query interview_db 查询每个城市的候选人数量 csv
/export-query interview_db "SELECT * FROM candidates LIMIT 100" json
```

命令内部编排（一条命令串起完整链路）：

1. **参数解析**：连接名 / 查询内容 / 格式（默认 csv）；缺失时用 AskUserQuestion 补齐，可 `GET /api/v1/dbs` 列出连接。
2. **拿 SQL**：输入以 `SELECT/WITH` 开头直接用；自然语言则先调 `POST /{name}/query/natural` 生成并展示 SQL。
3. **执行确认**：`POST /{name}/query` 报告行数、耗时、预览。
4. **导出落盘**：`curl -o exports/query_..._<时间戳>.<格式>` 调导出端点。
5. **验证汇报**：读回文件头几行确认非空，汇报绝对路径。

**Windows 编码防坑**：命令明确要求用 Write 工具把请求体写成 UTF-8 文件后 `curl --data-binary @file` 发送，规避控制台按 GBK 发送、后端按 UTF-8 解析失败的坑。

为什么选自定义 Command 而不是 Agent：链路是**确定性**的（固定的 API 顺序调用 + 校验），Command 模板即文档、即参数说明，开销与不确定性都最小；Agent 适合探索性任务，此处无必要。

## 6. 测试

新增 `backend/tests/unit/test_export.py`（13 个用例，全部通过）：

- **服务层**：CSV 基本内容与 `None→空`、特殊字符（逗号/引号/换行/中文）转义、JSON 文档结构与 `null` 保留、中文不转义、datetime 序列化、文件名清洗、Content-Disposition 编码、格式分发与非法格式抛错。
- **API 层**（mock 执行管线，复用既有 TestClient 基建）：CSV 200 + BOM + 附件头、JSON 200、缺省格式为 CSV、非法格式 422、未知连接 404。

验证结果：`pytest tests/unit/test_export.py` **13 passed**；前端 `tsc --noEmit` 无错误、`vite build` 成功。
（注：`test_query.py` 等旧文件存在 20 个与本次改动无关的历史失败——其 mock 的 `get_connection_pool` 在适配器重构后已不存在，stash 验证过与本次改动无关。）

## 7. 变更清单

| 文件 | 变更 |
|---|---|
| `backend/app/services/export.py` | 新增：CSV/JSON 序列化与文件名/响应头工具 |
| `backend/app/api/v1/queries.py` | 新增 `POST /{name}/query/export` 端点 |
| `backend/tests/unit/test_export.py` | 新增：13 个单元测试 |
| `frontend/src/types/query.ts` | 新增 `ExportFormat` 类型 |
| `frontend/src/pages/Home.tsx` | **主 UI**：新增主动询问通知；EXPORT 按钮改走后端导出端点（替换原前端内存拼装实现） |
| `frontend/src/pages/queries/execute.tsx` | 历史页面（未挂路由）：同步加入相同的导出交互，保持一致 |
| `.claude/commands/export-query.md` | 新增：一键"查询+导出"自定义命令 |

另修复一个验证过程中暴露的工程 bug：`backend/app/adapters/registry.py` 的适配器实例按 `db_type:name` 缓存且不感知 URL 变化，导致"更新连接 URL 后测试连接仍用旧凭据"；现已在 URL 变化时重建实例。

## 8. 后续可扩展方向

- 更多格式：`ExportFormat` 加枚举值 + 一个序列化函数即可（Excel/NDJSON/Markdown 表格）。
- 大结果集：改为数据库游标流式写入 `StreamingResponse`，绕开 1000 行上限。
- 历史重放导出：`GET /{name}/history` 选中某条历史 SQL 直接导出（命令文档已预留该交互）。
- 权限与脱敏：导出前套用列级脱敏规则、记录导出审计日志。
