# 阶段 4：部署成远程服务

**目标**：把 server 从本机 stdio 子进程变成一个独立的 HTTP 服务，加上鉴权和进度通知，并理解新旧两版协议在会话上的区别。

## 运行

先在一个终端启动 server：

```bash
python -m stage4_remote.server_http
```

再在另一个终端运行演示客户端：

```bash
python -m stage4_remote.client_remote
```

演示客户端会依次展示：不带 token 返回 401、错误的 token 返回 401、正确的 token 能连上（`whoami`）、进度条、旧版协议客户端、`Mcp-Session-Id` 响应头。

让阶段 3 的 Agent 连接这个远程 server（需要 `.env` 里有 `MCP_API_TOKEN`）：

```bash
python -m stage3_agent.agent --config stage4_remote/servers_remote.json "一共有多少个客户？"
```

试一下无状态模式（先停掉之前的 server），然后再运行 `client_remote`，第 6 步就不会有 `Mcp-Session-Id` 了：

```bash
MCP_STATELESS=1 python -m stage4_remote.server_http
```

用 Docker 部署（在项目根目录执行，需要先启动 Docker Desktop；这一步开发时还没有实际验证过）：

```bash
docker build -f stage4_remote/Dockerfile -t shop-db-mcp .
```

```bash
docker run --rm -p 8000:8000 -e MCP_API_TOKEN=dev-token-change-me shop-db-mcp
```

## 读代码的顺序

1. `server_http.py`
   - `build_server(..., token_verifier=..., auth=AuthSettings(...))`：工具代码完全复用阶段 2，只是多传了鉴权参数；
   - `StaticTokenVerifier.verify_token()`：这是鉴权的**唯一接入点**：输入 token，返回 `AccessToken` 或 `None`。真实项目在这里校验 JWT 或调用 OAuth 授权服务器；
   - `whoami` 里的 `get_access_token()`：在工具里拿到"当前是谁在调用"；
   - `monthly_sales_report` 里的 `ctx.report_progress()`：长任务汇报进度。注意它内部的 SQL 用 `?` 参数化，**不能把模型传来的参数直接拼进 SQL**；
   - `mcp.run(transport="streamable-http", ..., transport_security=...)`：传输相关的参数都传给 `run()`，不传给 `MCPServer()`。
2. `client_remote.py`：用 `httpx2.AsyncClient(headers=...)` 带上 token，再交给 `streamable_http_client`；`call_tool(..., progress_callback=...)` 接收进度。
3. `Dockerfile`：容器里要监听 `0.0.0.0`，不用 root 运行，token 通过环境变量传入。

## 关键概念

- **stdio 和 Streamable HTTP**：stdio 适合本地工具（简单、没有网络开销、不需要鉴权）；HTTP 适合远程部署和多人共用（需要鉴权、TLS、限流）。旧的 HTTP+SSE 方式在 2025-03-26 版协议里已经被取代。
- **鉴权流程**：没有 token 时返回 401，`WWW-Authenticate` 头里带着 `resource_metadata` 地址（RFC 9728）。客户端顺着这个地址找到授权服务器，走 OAuth 2.1 流程拿 token。`validate_token_resource=True` 会拒绝签发给其它服务的 token。
- **DNS 重绑定防护**：默认只接受 Host 头是 localhost 的请求。部署到真实域名时，要在 `MCP_ALLOWED_HOSTS` 里加上域名，否则所有请求都会返回 421。
- **会话（重要，新旧协议不同）**：
  - 旧版协议（≤2025-11-25）：`initialize` 握手后，server 返回 `Mcp-Session-Id`，后续请求都要带上它；多实例部署时需要会话粘性。`stateless_http=True` 可以关掉会话。
  - 新版协议（2026-07-28）：**没有会话**，每个请求独立，天然方便横向扩展。`stateless_http` 只对旧版客户端起作用。
- **多实例部署时还要注意**：多轮往返请求里的 `requestState` 默认用每个进程随机生成的密钥加密，多实例时要配置相同的密钥（见 SDK 文档 Deploy 一章）。

## 动手练习

1. 支持多个 token、对应不同用户：`MCP_API_TOKENS="tokenA:alice,tokenB:bob"`，让 `whoami` 返回不同的人。
2. 增加一个 scope `shop:write`，只有带这个 scope 的 token 才能调用写操作（结合阶段 5 的 `update_product_price`）。
3. 给 `monthly_sales_report` 加上取消支持：客户端取消后停止计算。
4. 部署到云服务器，配好域名和 HTTPS，在 `MCP_ALLOWED_HOSTS` 里加上域名。

## 面试题

- **stdio 和 Streamable HTTP 怎么选？** 见关键概念。
- **有状态和无状态有什么区别？** 旧版协议用 Session ID 区分，多实例时要会话粘性；新版协议本身没有会话。
- **远程 server 怎么鉴权？** OAuth 2.1 + Bearer Token；SDK 的接入点是 `TokenVerifier`；资源元数据遵循 RFC 9728；不要把用户的 token 原样转发给下游服务。
- **长任务怎么处理？** 用进度通知汇报进度、支持取消；耗时更长的任务改成异步（先返回任务 ID，之后再查询结果）。
