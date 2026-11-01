# MCP 新旧两版协议对比（面试时很容易被问到）

MCP 规范一直在更新。网上大多数中文资料讲的是**旧版**（2025-11-25 及以前），而这个项目用的 SDK `mcp==2.3.0` 默认使用**新版**（2026-07-28），同时兼容旧版。
面试时两版都要能讲：说清楚旧版是怎么做的、新版改了什么、为什么要改，会很加分。

| 方面 | 旧版（≤ 2025-11-25） | 新版（2026-07-28） | 项目里在哪能看到 |
|---|---|---|---|
| 建立连接 | `initialize` 握手：client 提议版本 → server 回复 → client 发 `initialized` 通知 | 一次 `server/discover` 请求就拿到全部信息，没有握手 | `stage2_mcp_server/try_client.py` 打印的协议版本；`client_remote.py` 第 5 步 |
| 会话 | HTTP 下用 `Mcp-Session-Id` 标识会话，多实例部署要会话粘性；`stateless_http=True` 可以关掉 | **没有会话**，每个请求独立 | `client_remote.py` 第 6 步 |
| server 向用户提问（elicitation） | server 在工具执行中途主动向 client 发 `elicitation/create` 请求 | server 返回 `InputRequiredResult`，client 回答后重新调用同一个工具（多轮往返） | `server_secure.py` 的 `update_product_price`；测试 `test_price_update_requires_user_confirmation` 两版都跑了一遍 |
| sampling / roots | server 主动向 client 发 `sampling/createMessage`、`roots/list` | 这两个独立的请求被移除，同样的内容放在 `InputRequiredResult` 里 | — |
| 协议层日志 | `notifications/message` + `logging/setLevel` | 已废弃 | — |
| HTTP 传输 | Streamable HTTP（2025-03-26 起取代 HTTP+SSE） | Streamable HTTP | `stage4_remote/` |

## 为什么要这样改（回答"为什么"能体现理解深度）

- **去掉会话**：有状态的长连接很难横向扩展，负载均衡时必须做会话粘性。无状态之后，任何一个实例都能处理任何一个请求。
- **去掉 server 主动发给 client 的请求**：这种请求要求 HTTP 连接保持打开（SSE 流），中间层（代理、serverless）很难支持。改成"server 返回问题、client 带着答案重试"之后，所有请求都是 client 发给 server 的普通请求。代价是 server 要把中间状态放进 `requestState` 交给 client 保存（SDK 默认会加密并校验，防止被篡改）。

## SDK 层面

- `Client(..., mode="auto")`（默认）：先试新版，server 不支持就自动退回旧版握手。
- `Client(..., mode="legacy")`：强制使用旧版。
- server 端无需任何配置，同一个 `streamable_http_app()` 两版都支持（按请求头 `MCP-Protocol-Version` 分流）。
- SDK v1 → v2 的代码变化：`FastMCP` 改名为 `MCPServer`（`from mcp.server import MCPServer`）；HTTP 客户端从 `httpx` 换成了 `httpx2`；`streamable_http_client` 不再接受 `headers=`，要通过你自己创建的 `httpx2.AsyncClient` 传入。
