# 数据库问答 Agent（MCP 学习项目）

用中文问问题，比如"8 月销售额是多少""销量最高的 3 个商品"，Agent 自己写 SQL 查数据库，然后回答。
这个项目从零开始，一共 5 个阶段：从手写 Function Calling，到 MCP Server、自己写的 Agent、远程部署，最后做安全加固和效果评估。
每个阶段都能独立运行，也都对应着一批面试题。

- **SDK 版本**：`mcp==2.3.0`（MCP Python SDK v2）。网上很多教程是 v1 写法（`from mcp.server.fastmcp import FastMCP`），在 v2 里会报错，v2 改成了 `MCPServer`。
- **协议版本**：同时支持 `2026-07-28`（新）和 `2025-11-25`（旧）两版协议，差别见 [docs/protocol_notes.md](docs/protocol_notes.md)。
- **模型**：任何 OpenAI 兼容接口都能用（DeepSeek、通义千问等）。**不配置 API Key 也能跑**，会自动使用离线的 MockLLM。

## 整体架构

```
                         ┌──────────────────────── 你的 Agent（Host）────────────────────────┐
  用户 ──问题──>         │  大模型（DeepSeek/通义）  ←→  Agent 循环  ←→  MCP Client × N      │
                         └───────────────────────────────────────────┬──────────┬──────────┘
                                                                     │stdio     │HTTP + Bearer Token
                                                         ┌───────────▼───┐  ┌───▼──────────────┐
                                                         │ shop-db server│  │ shop-db-remote   │
                                                         │ tools/resources│  │ （阶段 4 部署版） │
                                                         │ /prompts      │  └───┬──────────────┘
                                                         └───────┬───────┘      │
                                                                 ▼              ▼
                                                            SQLite 数据库 data/shop.db
                                                         ┌───────────────┐
                                                         │ reports server│ → reports/*.md
                                                         └───────────────┘
```

## 快速开始

需要 Python 3.11 或更高版本（这台电脑上可以用 Homebrew 的 `python3.13`；conda base 是 3.10，版本不够）。

```bash
python3.13 -m venv .venv
```

```bash
source .venv/bin/activate
```

```bash
pip install -e ".[dev]"
```

```bash
python -m data.init_db
```

```bash
python -m pytest -q
```

最后一步应该显示 `11 passed`。想接入真实模型，就复制配置文件，再填上 `LLM_API_KEY`：

```bash
cp .env.example .env
```

> 如果移动了项目文件夹，`.venv` 会失效（里面写死了绝对路径），删掉后重新执行上面几步即可。

## 学习路线

| 阶段 | 做什么 | 先读哪里 | 对应面试题 |
|---|---|---|---|
| 0 | 生成示例数据 | `data/init_db.py` | — |
| [1](stage1_function_calling/README.md) | 不用 MCP，手写 Function Calling 循环 | `stage1_function_calling/agent.py` | MCP 解决什么问题；和 Function Calling 的区别 |
| [2](stage2_mcp_server/README.md) | 把数据库包装成 MCP Server | `stage2_mcp_server/server.py` | 三种原语；参数格式从哪来；怎么调试 |
| [3](stage3_agent/README.md) | 自己写 Agent，同时连接多个 Server | `stage3_agent/agent.py` | 架构角色；生命周期；两种错误；工具太多怎么办 |
| [4](stage4_remote/README.md) | HTTP 部署 + 鉴权 + 进度通知 | `stage4_remote/server_http.py` | stdio 和 HTTP 怎么选；会话；鉴权；长任务 |
| [5](stage5_security_eval/README.md) | 安全加固、攻击演示、效果评估 | `stage5_security_eval/` | 安全风险与防范；工具设计；项目深挖 |

**建议节奏**：每个阶段先运行一遍，再按 README 里"读代码的顺序"读，然后做"动手练习"，最后合上代码，用自己的话写下"面试题"的答案。
所有面试题和答案要点汇总在 [docs/interview_qa.md](docs/interview_qa.md)。

## 目录结构

```
common/                 各阶段共用：配置、数据库访问、大模型客户端（含 MockLLM）
data/init_db.py         生成示例数据库（固定随机种子，每次结果一样）
stage1_function_calling/  阶段 1：手写工具 + Agent 循环
stage2_mcp_server/      阶段 2：MCP Server（server.py）+ 调试脚本（try_client.py）
stage3_agent/           阶段 3：Agent Host（agent.py）+ 第二个 server（report_server.py）+ 配置（servers.json）
stage4_remote/          阶段 4：HTTP server、远程客户端演示、Dockerfile
stage5_security_eval/   阶段 5：SQL 防护、加固版 server/Agent、攻击演示、评估集
tests/test_project.py   自动化测试（也是"怎么测试 MCP server"的示例）
docs/                   面试题汇总、新旧协议对比
```

## 关于 MockLLM

`common/llm.py` 里的 MockLLM 是一个按关键词匹配的"假模型"，只认识几种固定问法，比如"销量最高""一共有多少个客户"。
它的作用是：没有 API Key 时，也能验证"模型 → 工具调用 → MCP → 数据库 → 回答"这整条链路能不能跑通，测试也全靠它。
**它不代表真实效果**。阶段 5 用它跑评估只得到 25% 的正确率，这是正常的；换成真实模型再评估，分数才有意义。

## 已验证 / 未验证

- ✅ 全部 11 个测试通过：五个阶段的功能、两版协议、鉴权、进度通知、用户确认、攻击演示、评分逻辑。
- ✅ 真实模型的封装（`OpenAICompatibleLLM`）用一个本地假接口测过请求和返回格式，但**没有用真实的 API Key 跑过**。
- ⚠️ Dockerfile 已经写好，但开发时本机的 Docker 没有运行，**还没有实际构建过**。
- ⚠️ `mcp dev`（MCP Inspector）需要 Node.js，开发时本机没有装；可以用 `python -m stage2_mcp_server.try_client` 代替。
