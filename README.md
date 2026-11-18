# dsh-superagent-test

> 用于验证 [dsh-superagent](https://github.com/ClumsyWu/dsh-superagent) 的独立测试仓库。

## 目录

- [项目介绍](#项目介绍)
- [pi-sdk Agent Demo](#pi-sdk-agent-demo)
  - [前置条件](#前置条件)
  - [安装](#安装)
  - [使用说明](#使用说明)
  - [探活服务](#探活服务)
  - [自定义配置](#自定义配置)
  - [SDK 功能概览](#sdk-功能概览)

---

## 项目介绍

本项目包含两大模块：

| 模块 | 说明 |
|------|------|
| **dsh-superagent 集成测试** | 验证 agent 框架在不同场景下的行为 |
| **pi-sdk Agent Demo** | 使用 `deepseek-harness-sdk` 搭建的 agent 示例，展示 SDK 的核心功能 |

---

## pi-sdk Agent Demo

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

### 项目结构

```
├── demo/
│   ├── __init__.py                  # 包初始化
│   ├── simple_agent.py              # 简单 agent 单次调用 demo
│   ├── conversational_agent.py      # 多轮对话 agent demo（含用户问题处理）
│   ├── health_server.py             # 探活 HTTP 服务（/health、/metrics、/ready）
│   └── test_health_server.py        # 探活接口单元测试
├── requirements.txt                 # Python 依赖
└── README.md
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

### 探活服务

使用内置 `http.server` 模块提供标准探活端点，无需额外依赖：

```bash
# 启动服务（默认端口 8080）
python -m demo.health_server

# 指定端口
python -m demo.health_server --port 9090
```

#### 端点说明

| 端点 | 用途 | 预期状态 |
|------|------|----------|
| `GET /health` | 总体健康检查，服务存活时返回 200 | `{"status": "ok"}` |
| `GET /metrics` | 基本运行指标（运行时间、请求计数） | `{"uptime_seconds": ..., "total_requests": ..., "status": "ok"}` |
| `GET /ready` | 就绪检查，接受流量时返回 200 | `{"status": "ready"}` |

#### 运行测试

```bash
python -m unittest demo.test_health_server -v
```

### 自定义配置

| 参数 | 说明 |
|------|------|
| `--dsh-home` | DSH 主目录路径（必需） |
| `--workspace` | 工作目录（默认：当前目录） |
| `--model` | 模型名称（默认：`DSH_MODEL` 环境变量或 `deepseek-v4-flash`） |
| `--provider` | LLM 提供商（默认：`deepseek-official`） |
| `--dsh-bin` | dsh 二进制文件路径（自动检测） |
| `--session-id` | 会话 ID（用于恢复上下文） |

### SDK 功能概览

`deepseek-harness-sdk` 提供的主要能力：

| 组件 | 说明 |
|------|------|
| **DeepSeekHarness** | 同步 SDK 客户端，管理 dsh 子进程生命周期 |
| **Session** | 会话管理，支持上下文保持和恢复 |
| **RunResult** | 包含最终响应、结束原因、事件和通知 |
| **UserQuestionHandler** | 处理 agent 运行时提出的用户问题（如审批请求） |
| **NotificationSubscription** | 订阅会话事件和通知 |