/** Query execution page with SQL editor and result table. */

import React, { useState, useEffect } from "react";
import { useParams } from "react-router-dom";
import { Card, Button, Space, Spin, Alert, List, Typography, notification, message } from "antd";
import {
  PlayCircleOutlined,
  ReloadOutlined,
  DownloadOutlined,
  FileTextOutlined,
} from "@ant-design/icons";
import { apiClient } from "../../services/api";
import { QueryResult, QueryHistoryEntry, QueryInput, ExportFormat } from "../../types/query";
import { SqlEditor } from "../../components/SqlEditor";
import { ResultTable } from "../../components/ResultTable";

const { Text } = Typography;

/** Notification key reused for the post-query export prompt. */
const EXPORT_PROMPT_KEY = "export-prompt";

export const QueryExecute: React.FC = () => {
  const { databaseName } = useParams<{ databaseName: string }>();
  const [sql, setSql] = useState("SELECT * FROM ");
  const [result, setResult] = useState<QueryResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [history, setHistory] = useState<QueryHistoryEntry[]>([]);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [exporting, setExporting] = useState<ExportFormat | null>(null);
  // Hook-based notification instance supports destroy(key), unlike the static API
  const [notificationApi, notificationContextHolder] = notification.useNotification();

  useEffect(() => {
    if (databaseName) {
      loadHistory();
    }
  }, [databaseName]);

  const loadHistory = async () => {
    if (!databaseName) return;

    setLoadingHistory(true);
    try {
      const response = await apiClient.get<QueryHistoryEntry[]>(
        `/api/v1/dbs/${databaseName}/history`
      );
      setHistory(response.data);
    } catch (err) {
      console.error("Failed to load history:", err);
    } finally {
      setLoadingHistory(false);
    }
  };

  const handleExecute = async () => {
    if (!databaseName || !sql.trim()) {
      setError("Please enter a SQL query");
      return;
    }

    setLoading(true);
    setError(null);
    setResult(null);
    notificationApi.destroy(EXPORT_PROMPT_KEY);

    try {
      const input: QueryInput = { sql: sql.trim() };
      const response = await apiClient.post<QueryResult>(
        `/api/v1/dbs/${databaseName}/query`,
        input
      );
      setResult(response.data);
      // Proactively offer to export the fresh result
      if (response.data.rowCount > 0) {
        promptExport(response.data.rowCount);
      }
      // Reload history after successful query
      await loadHistory();
    } catch (err: any) {
      const errorMessage =
        err.response?.data?.detail || err.message || "Query execution failed";
      setError(errorMessage);
    } finally {
      setLoading(false);
    }
  };

  const handleHistoryClick = (historyItem: QueryHistoryEntry) => {
    setSql(historyItem.sqlText);
    setError(null);
    setResult(null);
    notificationApi.destroy(EXPORT_PROMPT_KEY);
  };

  /**
   * Download the current query result as a CSV or JSON file.
   * The backend re-executes the SQL through the export endpoint and streams
   * the file back, so the download always matches the SQL being exported.
   */
  const handleExport = async (format: ExportFormat) => {
    if (!databaseName || !sql.trim()) {
      setError("Please enter a SQL query");
      return;
    }

    setExporting(format);
    try {
      const response = await apiClient.post(
        `/api/v1/dbs/${databaseName}/query/export`,
        { sql: sql.trim() },
        {
          params: { format },
          responseType: "blob",
        }
      );

      // Prefer the filename proposed by the server, fall back to a local one
      const disposition: string = response.headers?.["content-disposition"] || "";
      const match = disposition.match(/filename\*=UTF-8''([^;]+)/) ||
        disposition.match(/filename="?([^";]+)"?/);
      const filename = match?.[1]
        ? decodeURIComponent(match[1])
        : `query_${databaseName}_${Date.now()}.${format}`;

      const url = window.URL.createObjectURL(new Blob([response.data]));
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      window.URL.revokeObjectURL(url);

      notificationApi.destroy(EXPORT_PROMPT_KEY);
      message.success(`Exported to ${filename}`);
    } catch (err: any) {
      // Errors arrive as a blob, so surface the server detail when parseable
      let errorMessage = "Export failed";
      if (err.response?.data instanceof Blob) {
        try {
          const text = await err.response.data.text();
          const parsed = JSON.parse(text);
          errorMessage = parsed?.detail || errorMessage;
        } catch {
          /* keep default message */
        }
      } else {
        errorMessage = err.response?.data?.detail || err.message || errorMessage;
      }
      message.error(errorMessage);
    } finally {
      setExporting(null);
    }
  };

  /** Proactively ask the user whether to export the fresh query result. */
  const promptExport = (rowCount: number) => {
    notificationApi.open({
      key: EXPORT_PROMPT_KEY,
      message: "Query executed successfully",
      description: `${rowCount} rows returned. Export this result as a CSV or JSON file?`,
      icon: <DownloadOutlined style={{ color: "#1890ff" }} />,
      btn: (
        <Space>
          <Button size="small" onClick={() => notificationApi.destroy(EXPORT_PROMPT_KEY)}>
            Not now
          </Button>
          <Button
            size="small"
            icon={<FileTextOutlined />}
            loading={exporting === "csv"}
            onClick={() => handleExport("csv")}
          >
            Export CSV
          </Button>
          <Button
            size="small"
            type="primary"
            icon={<DownloadOutlined />}
            loading={exporting === "json"}
            onClick={() => handleExport("json")}
          >
            Export JSON
          </Button>
        </Space>
      ),
      duration: null,
      placement: "bottomRight",
    });
  };

  return (
    <div style={{ padding: 24 }}>
      {notificationContextHolder}
      <Card
        title={`Execute Query - ${databaseName}`}
        extra={
          <Space>
            <Button
              type="primary"
              icon={<PlayCircleOutlined />}
              onClick={handleExecute}
              loading={loading}
            >
              Execute
            </Button>
            <Button
              icon={<ReloadOutlined />}
              onClick={loadHistory}
              loading={loadingHistory}
            >
              Refresh History
            </Button>
          </Space>
        }
      >
        <Space direction="vertical" style={{ width: "100%" }} size="large">
          <div>
            <Card title="SQL Editor" size="small">
              <SqlEditor value={sql} onChange={(val) => setSql(val || "")} height="200px" />
            </Card>
          </div>

          {error && (
            <Alert
              message="Error"
              description={error}
              type="error"
              showIcon
              closable
              onClose={() => setError(null)}
            />
          )}

          {loading && (
            <div style={{ textAlign: "center", padding: "50px" }}>
              <Spin size="large" />
            </div>
          )}

          {result && (
            <Card
              title="Query Results"
              size="small"
              extra={
                <Space>
                  <Button
                    size="small"
                    icon={<FileTextOutlined />}
                    loading={exporting === "csv"}
                    onClick={() => handleExport("csv")}
                  >
                    Export CSV
                  </Button>
                  <Button
                    size="small"
                    icon={<DownloadOutlined />}
                    loading={exporting === "json"}
                    onClick={() => handleExport("json")}
                  >
                    Export JSON
                  </Button>
                </Space>
              }
            >
              <ResultTable result={result} loading={loading} />
            </Card>
          )}
        </Space>
      </Card>

      <Card title="Query History" style={{ marginTop: 16 }}>
        {loadingHistory ? (
          <Spin />
        ) : (
          <List
            dataSource={history}
            renderItem={(item) => (
              <List.Item
                style={{
                  cursor: "pointer",
                  backgroundColor: item.success ? "transparent" : "#fff2f0",
                }}
                onClick={() => handleHistoryClick(item)}
              >
                <List.Item.Meta
                  title={
                    <Space>
                      <Text
                        code
                        style={{
                          maxWidth: "600px",
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                          display: "inline-block",
                        }}
                      >
                        {item.sqlText}
                      </Text>
                      {item.success ? (
                        <Text type="success">
                          ✓ {item.rowCount} rows in {item.executionTimeMs}ms
                        </Text>
                      ) : (
                        <Text type="danger">✗ Failed</Text>
                      )}
                    </Space>
                  }
                  description={
                    <Text type="secondary">
                      {new Date(item.executedAt).toLocaleString()}
                    </Text>
                  }
                />
              </List.Item>
            )}
          />
        )}
      </Card>
    </div>
  );
};
