# dsh-superagent-test

> 用于验证 [dsh-superagent](https://github.com/ClumsyWu/dsh-superagent) 的独立测试仓库。

## 概览

本仓库服务于两个核心目标：

| 模块 | 说明 |
|------|------|
| **dsh-superagent 集成测试** | 验证 agent 框架在不同场景下的行为表现 |
| **pi-sdk Agent Demo** | 基于 `deepseek-harness-sdk` 搭建的 agent 示例，展示 SDK 的核心用法 |

此外，[feature-ideas.md](feature-ideas.md) 收录了基于 dsh 现有架构调研得出的功能构想，涵盖开发者体验、智能体能力增强、UI/UX、集成生态、运维及高级特性等多个方向。

---

## pi-sdk Agent Demo

### 目录结构

```
├── demo/
│   ├── __init__.py                  # 包初始化
│   ├── simple_agent.py              # 单次 agent 调用示例
│   ├── conversational_agent.py      # 多轮对话 agent 示例
│   ├── health_server.py             # 探活 HTTP 服务
│   ├── test_health_server.py        # 探活服务单元测试
│   ├── http_tool.py                 # HTTP 请求工具（CLI + 可集成到 agent）
│   ├── test_http_tool.py            # HTTP 请求工具单元测试
│   ├── read_file_tool.py            # 文件读取工具（CLI + 可集成到 agent）
│   └── test_read_file_tool.py       # 文件读取工具单元测试
├── requirements.txt                 # Python 依赖
└── README.md
```

### 前置条件

- **Python 3.10+** 及 pip
- **DEEPSEEK_API_KEY** 或等效的模型凭证
- **dsh CLI** — 由 `deepseek-harness-runtime-bin` 包提供，也可通过自定义 `dsh_bin` 路径指定
- **DSH_HOME** 目录 — 用于存储配置和会话日志

### 安装

```bash
# 安装 deepseek-harness-sdk
pip install deepseek-harness-sdk

# 设置 API 凭证
export DEEPSEEK_API_KEY="sk-your-key-here"
```

### 使用说明

#### 单次 agent 调用

快速验证 agent 任务执行：

```bash
python -m demo.simple_agent \
  --dsh-home /path/to/dsh-home \
  "What is the capital of France?"
```

不传自定义 prompt 时，默认执行一个问候任务。

#### 多轮对话

展示 agent 的上下文保持能力。内置三轮递进式对话：

1. 询问 Python 语言的特点
2. 基于第一轮回答进行追问
3. 要求编写具体的 Python 函数

```bash
python -m demo.conversational_agent \
  --dsh-home /path/to/dsh-home \
  --session-id my-demo-session
```

该示例同时演示了：
- **会话持久化**：通过 `session-id` 保存和恢复对话上下文
- **用户问题处理**：`UserQuestionHandler` 处理 agent 运行中的用户交互（如审批请求）
- **上下文保持**：后续问题可引用前一轮的回答

#### 探活服务

基于内置 `http.server` 实现，无需额外依赖：

```bash
# 启动服务（默认端口 8080）
python -m demo.health_server

# 指定端口
python -m demo.health_server --port 9090
```

##### 端点说明

| 端点 | 用途 | 响应示例 |
|------|------|----------|
| `GET /health` | 总体健康检查 | `{"status": "ok"}` |
| `GET /metrics` | 运行指标（运行时间、请求计数） | `{"uptime_seconds": ..., "total_requests": ..., "status": "ok"}` |
| `GET /ready` | 就绪检查 | `{"status": "ready"}` |

##### 运行测试

```bash
python -m unittest demo.test_health_server -v
```

#### HTTP 请求工具

零依赖的 HTTP(S) 请求工具（基于标准库 `urllib`），既可作为命令行工具直接调用，也可通过 `HttpTool` 类集成到 agent 循环中。支持 `GET`/`POST`/`PUT`/`PATCH`/`DELETE`/`HEAD`/`OPTIONS`、自定义请求头、原始或 JSON 请求体、超时与响应体截断。

```bash
# GET 请求
python -m demo.http_tool https://httpbin.org/get

# POST JSON，并附加请求头
python -m demo.http_tool https://httpbin.org/post \
  -X POST --json '{"name": "dsh"}' -H 'X-Test: demo'

# 设置超时、限制响应体大小并美化输出
python -m demo.http_tool https://example.com -t 5 --max-body-bytes 65536 --pretty
```

输出统一为 JSON：请求成功时返回 `status`、`reason`、`url`、`headers`、`body`、`truncated`、`elapsed_ms`；传输失败或参数错误时返回 `ok: false` 与 `error`。

| 参数 | 说明 |
|------|------|
| `url` | 请求地址（仅支持 `http://` / `https://`） |
| `-X, --method` | HTTP 方法（默认 `GET`） |
| `-H, --header` | 请求头，格式 `'Name: Value'`，可重复 |
| `-d, --data` | 原始请求体 |
| `--json` | JSON 请求体，与 `--data` 互斥 |
| `-t, --timeout` | 超时秒数（默认 30） |
| `--max-body-bytes` | 返回响应体的最大字节数（默认 1 MiB） |
| `--pretty` | 美化 JSON 输出 |

编程方式调用：

```python
from demo.http_tool import HttpTool

tool = HttpTool()
result = tool.invoke({"url": "https://example.com", "method": "GET", "timeout_seconds": 5})
print(result["status"], result["body"][:200])
```

运行测试：

```bash
python -m unittest demo.test_http_tool -v
```

#### 文件读取工具

零依赖的文本文件读取工具（基于标准库），既可作为命令行工具直接调用，也可通过 `ReadFileTool` 类集成到 agent 循环中。支持 1-based 行号分页（`offset` / `limit`）、自定义编码、返回内容字节上限、`~` 路径展开，以及二进制文件检测。

```bash
# 读取整个文件
python -m demo.read_file_tool README.md

# 从第 10 行开始读取 20 行
python -m demo.read_file_tool README.md --offset 10 --limit 20

# 指定编码、限制返回内容大小并美化输出
python -m demo.read_file_tool data.txt --encoding gb18030 --max-bytes 65536 --pretty
```

输出统一为 JSON：成功时返回 `path`、`size_bytes`、`encoding`、`offset`、`line_count`、`next_offset`、`has_more`、`truncated`、`truncated_by`、`partial_line` 和 `content`；文件不存在、路径是目录、无权限、疑似二进制文件或参数错误时返回 `ok: false` 与 `error_type`、`error`。

| 参数 | 说明 |
|------|------|
| `path` | 文件路径（绝对路径或相对当前目录，支持 `~`） |
| `--offset` | 起始行号，1-based（默认 `1`） |
| `--limit` | 最多返回的行数（默认 `2000`，最大 `100000`） |
| `--max-bytes` | 返回内容的最大字节数（默认 1 MiB） |
| `--encoding` | 文件编码（默认 `utf-8`） |
| `--pretty` | 美化 JSON 输出 |

`content` 保留原始换行符；`next_offset` 可传给下一次调用的 `offset` 以继续读取。当单行长度超过 `max_bytes` 时，该行会被截断并返回 `partial_line: true`，此时 `next_offset` 为 `null`，需要增大 `max_bytes` 后重试。

编程方式调用：

```python
from demo.read_file_tool import ReadFileTool

tool = ReadFileTool()
result = tool.invoke({"path": "README.md", "offset": 10, "limit": 20})
print(result["content"])
```

运行测试：

```bash
python -m unittest demo.test_read_file_tool -v
```

### 配置参数

| 参数 | 说明 |
|------|------|
| `--dsh-home` | DSH 主目录路径（必需） |
| `--workspace` | 工作目录（默认：当前目录） |
| `--model` | 模型名称（默认：`DSH_MODEL` 环境变量或 `deepseek-v4-flash`） |
| `--provider` | LLM 提供商（默认：`deepseek-official`） |
| `--dsh-bin` | dsh 二进制文件路径（自动检测） |
| `--session-id` | 会话 ID（用于恢复上下文） |

### SDK 功能速览

| 功能 | 说明 |
|------|------|
| `DeepSeekHarness` | 同步 SDK 客户端，管理 dsh 子进程生命周期 |
| `Session` | 会话管理，支持上下文保持和恢复 |
| `RunResult` | 包含最终响应、结束原因、事件和通知 |
| `UserQuestionHandler` | 处理 agent 运行时提出的用户问题 |
| `NotificationSubscription` | 订阅会话事件和通知 |