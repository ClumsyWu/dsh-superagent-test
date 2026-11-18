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
│   └── test_health_server.py        # 探活服务单元测试
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