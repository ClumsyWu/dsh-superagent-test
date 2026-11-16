# dsh-superagent-test

用于验证 [dsh-superagent](https://github.com/ClumsyWu/dsh-superagent) 的独立测试仓库。

## 项目介绍

本项目主要包含两方面内容：

1. **dsh-superagent 集成测试** — 验证 agent 框架在不同场景下的行为
2. **pi-sdk Agent Demo** — 使用 `deepseek-harness-sdk` 搭建的 agent 示例，展示 SDK 的核心功能

## pi-sdk Agent Demo

### 项目结构

```
├── demo/
│   ├── __init__.py                  # 包初始化
│   ├── mcp.py                       # MCP 服务器配置与补丁生成工具
│   ├── simple_agent.py              # 简单 agent 单次调用 demo
│   └── conversational_agent.py      # 多轮对话 agent demo（含用户问题处理）
├── requirements.txt                 # Python 依赖
└── README.md
```

### 前置条件

1. **Python 3.10+** 和 pip
2. **DEEPSEEK_API_KEY** 或等效的模型凭证
3. **dsh CLI** — 由 `deepseek-harness-runtime-bin` 包提供，或指定自定义 `dsh_bin` 路径
4. **DSH_HOME** 目录 — 用于存储配置和会话日志

### 安装

```bash
# 安装 deepseek-harness-sdk
pip install deepseek-harness-sdk

# 设置 API 凭证
export DEEPSEEK_API_KEY="sk-your-key-here"
```

### 使用说明

#### 简单 agent 调用

单次任务调用，适合快速测试：

```bash
# 运行一次简单的 agent 任务
python -m demo.simple_agent \
  --dsh-home /path/to/dsh-home \
  "What is the capital of France?"
```

支持自定义 prompt，默认为一个问候任务。

#### 使用 MCP 工具的 agent 调用

[MCP (Model Context Protocol)](https://modelcontextprotocol.io) 是一种开放标准，
允许 agent 通过标准化接口调用外部工具。本例中，通过 `--mcp-stdio` 参数配置
stdio MCP 服务器：

```bash
# 使用 MCP 文件系统工具列出目录内容
python -m demo.simple_agent \
  --dsh-home /path/to/dsh-home \
  --mcp-stdio '{"name":"filesystem","command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","/tmp"]}' \
  "Show me what's currently in /tmp"
```

也可使用 Streamable HTTP 传输：

```bash
python -m demo.simple_agent \
  --dsh-home /path/to/dsh-home \
  --mcp-http '{"name":"my-api","url":"http://localhost:8080/mcp"}' \
  "Use the my-api MCP tools to... "
```

可通过预写的补丁文件也可配置 MCP 服务器：

```bash
python -m demo.simple_agent \
  --dsh-home /path/to/dsh-home \
  --mcp-patch /path/to/mcp-servers.patch.yml \
  "Run a task with pre-configured MCP tools"
```

#### 多轮对话 demo

展示上下文保持能力的多轮对话示例。内置三次对话回合：

1. 询问 Python 语言的特点
2. 基于第一轮回答进行追问
3. 要求编写具体的 Python 函数

```bash
# 运行多轮对话 demo
python -m demo.conversational_agent \
  --dsh-home /path/to/dsh-home \
  --session-id my-demo-session
```

该 demo 还演示了：
- **会话持久化**：使用 `session-id` 保存和恢复对话上下文
- **用户问题处理**：通过 `UserQuestionHandler` 处理 agent 在运行中提出的问题（如审批请求）
- **多轮上下文保持**：后续问题可引用前一轮的回答
- **MCP 工具集成**：可通过 `--mcp-stdio` / `--mcp-http` 配置外部工具服务器

### 自定义配置

| 参数 | 说明 |
|------|------|
| `--dsh-home` | DSH 主目录路径（必需） |
| `--workspace` | 工作目录（默认：当前目录） |
| `--model` | 模型名称（默认：`DSH_MODEL` 环境变量或 `deepseek-v4-flash`） |
| `--provider` | LLM 提供商（默认：`deepseek-official`） |
| `--dsh-bin` | dsh 二进制文件路径（自动检测） |
| `--session-id` | 会话 ID（用于恢复上下文） |
| `--mcp-stdio` | 配置 stdio MCP 服务器（JSON 格式，可多次指定） |
| `--mcp-http` | 配置 Streamable HTTP MCP 服务器（JSON 格式，可多次指定） |
| `--mcp-patch` | 预写 MCP 补丁 YAML 文件路径 |

### MCP 服务器配置

MCP 服务器通过 `demo/mcp.py` 模块管理。支持的服务器类型：

| 传输方式 | 配置参数（JSON） |
|----------|-----------------|
| stdio | `name`（必需）、`command`（必需）、`args`、`env`、`cwd`、`toolCallTimeoutMs`、`startupTimeoutMs`、`failOnStartupError` |
| streamable-http | `name`（必需）、`url`（必需）、`headers`、`toolCallTimeoutMs`、`startupTimeoutMs`、`failOnStartupError` |

配置参数也可通过程序化 API 使用：

```python
from demo.mcp import StdioServer, generate_yaml

# 创建服务器配置
server = StdioServer(
    name="filesystem",
    command="npx",
    args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
)

# 生成补丁 YAML
yaml_output = generate_yaml([server])
print(yaml_output)
```

### SDK 功能概览

`deepseek-harness-sdk` 提供的主要能力：

- **DeepSeekHarness**: 同步 SDK 客户端，管理 dsh 子进程生命周期
- **Session**: 会话管理，支持上下文保持和恢复
- **RunResult**: 包含最终响应、结束原因、事件和通知
- **UserQuestionHandler**: 处理 agent 运行时提出的用户问题（如审批请求）
- **NotificationSubscription**: 订阅会话事件和通知