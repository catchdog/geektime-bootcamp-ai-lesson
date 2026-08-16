---
description: 执行 SQL 查询（支持自然语言）并将结果一键导出为 CSV 或 JSON 文件
argument-hint: <数据库连接名> <自然语言或SQL> [csv|json]
---

# 查询并导出（Query & Export）

你的任务：对 db_query 后端（默认 `http://localhost:8000`）执行一次查询，并把结果导出为文件。
后端未启动时，先提示用户运行 `make backend` 或 `uvicorn app.main:app --reload`（在 backend/ 目录下），不要自行猜测连接。

## 参数解析

用户输入：$ARGUMENTS

按以下规则解析（缺少参数时用 AskUserQuestion 向用户补齐，不要猜）：

1. **数据库连接名**（必填）：第一个词。可用 `curl -s http://localhost:8000/api/v1/dbs` 列出已有连接让用户选。
2. **查询内容**（必填）：中间部分，可能是自然语言（如"查询用户总数"）或 SQL（以 SELECT / WITH 开头）。
3. **导出格式**（可选）：末尾的 `csv` 或 `json`，默认 `csv`。

## 执行步骤

### 第 1 步：拿到 SQL

- 输入是 SQL（以 SELECT 或 WITH 开头，忽略大小写）→ 直接使用。
- 输入是自然语言 → 先生成 SQL：

```bash
curl -s -X POST "http://localhost:8000/api/v1/dbs/<连接名>/query/natural" \
  -H "Content-Type: application/json" \
  --data-binary @payload.json
```

其中 `payload.json` 用 Write 工具写到临时文件（内容为 `{"prompt": "<用户自然语言>"}`），
**必须用 `--data-binary @文件` 方式发送**，避免 Windows 控制台按 GBK 编码发送导致后端 UTF-8 解析失败。
把返回的 `sql` 字段展示给用户，然后继续。

### 第 2 步：执行查询并确认

```bash
curl -s -X POST "http://localhost:8000/api/v1/dbs/<连接名>/query" \
  -H "Content-Type: application/json" \
  --data-binary @query.json
```

`query.json` 内容为 `{"sql": "<SQL语句>"}`（同样用 Write 写 UTF-8 文件）。
向用户报告：行数、耗时、前几行数据预览。

### 第 3 步：导出结果

```bash
mkdir -p exports
curl -s -X POST "http://localhost:8000/api/v1/dbs/<连接名>/query/export?format=<csv|json>" \
  -H "Content-Type: application/json" \
  --data-binary @query.json \
  -o "exports/query_<连接名>_$(date +%Y%m%d_%H%M%S).<格式>"
```

### 第 4 步：验证与汇报

- 用 Read 工具读取导出文件的前几行，确认内容非空且编码正确。
- 清理临时 payload 文件。
- 向用户汇报：SQL、行数、导出格式、**文件的绝对路径**、内容预览。

## 注意事项

- SQL 校验失败（400）或连接不存在（404）时，把后端返回的 `detail` 原样转述给用户并停止。
- 导出文件统一放在项目根目录 `exports/` 下。
- 若用户只说了"导出"，没有给新查询，可询问是否复用查询历史（GET `/api/v1/dbs/<连接名>/history`）中最近一条成功的 SQL。
