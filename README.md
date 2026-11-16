# dsh-superagent-test

用于验证 dsh-superagent 的独立测试仓库。

## pi-sdk Agent Demo

本项目包含一个使用 **pi-sdk** (`deepseek-harness-sdk`) 搭建的简单 agent demo。

### 项目结构

```
├── demo/
│   ├── __init__.py
│   ├── simple_agent.py              # 简单 agent 单次调用 demo
│   └── conversational_agent.py      # 多轮对话 agent demo
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
# 安装 pi-sdk（即 deepseek-harness-sdk）
pip install deepseek-harness-sdk

# 设置 API 凭证
export DEEPSEEK_API_KEY="sk-your-key-here"
```

### 使用方法

#### 简单 agent 调用

```bash
# 运行一次简单的 agent 任务
python -m demo.simple_agent \
  --dsh-home /path/to/dsh-home \
  "What is the capital of France?"
```

#### 多轮对话 demo

```bash
# 运行多轮对话 demo，展示上下文保持能力
python -m demo.conversational_agent \
  --dsh-home /path/to/dsh-home \
  --session-id my-demo-session
```

### 自定义配置

| 参数 | 说明 |
|------|------|
| `--dsh-home` | DSH 主目录路径（必需） |
| `--workspace` | 工作目录（默认：当前目录） |
| `--model` | 模型名称（默认：deepseek-v4-flash） |
| `--provider` | LLM 提供商（默认：deepseek-official） |
| `--dsh-bin` | dsh 二进制文件路径（自动检测） |
| `--session-id` | 会话 ID（用于恢复上下文） |

### pi-sdk 功能概述

`deepseek-harness-sdk`（pi-sdk）提供的主要功能：

- **DeepSeekHarness**: 同步 SDK 客户端，管理子进程生命周期
- **Session**: 会话管理，支持上下文保持和恢复
- **RunResult**: 包含最终响应、结束原因、事件和通知
- **UserQuestionHandler**: 处理 agent 的用户问题（如审批请求）
- **NotificationSubscription**: 订阅会话事件和通知