# 阶段 3：自己写 Agent Host（最核心）

**目标**：不依赖 Cursor 等现成客户端，自己实现一个 Host：同时连接多个 MCP server，把它们的工具交给大模型调度。

## 运行

```bash
python -m stage3_agent.agent "销量最高的 3 个商品是什么？保存成报告"
```

```bash
python -m stage3_agent.agent
```

第二条命令进入多轮对话，可以输入 `/tools`、`/prompts`、`/prompt analyze_sales 2026-08`、`/reset`。

## 读代码的顺序（`agent.py`）

1. `servers.json` 和 `load_config()`：格式和 Claude Desktop、Cursor 的 `mcpServers` 配置基本一样，支持 stdio（`command`）和远程 HTTP（`url`）两种写法。
2. `MCPAgent.__aenter__`：
   - 对每个 server 创建一个 `Client`（**一个 client 对应一个 server**），用 `AsyncExitStack` 统一管理连接的生命周期；
   - `list_tools()` 拿到工具 → `to_openai_tool()` 转换格式 → 工具名加上前缀 `shop_db__run_sql`，避免不同 server 的工具重名；
   - 读取 `client.instructions` 和配置里的 `context_resources`，拼进系统提示词（**Host 决定读哪些 Resource**）。
3. `ask()`：Agent 循环，和阶段 1 几乎一样，只是执行工具变成了 `client.call_tool()`。
4. `call_tool()`：处理四种情况：参数不是合法 JSON、工具不存在、`is_error`（工具执行失败）、`MCPError`（协议错误）。
5. `format_result()`：把结果转成文本给模型，**太长就截断**，防止撑爆上下文。阶段 5 会重写这个方法。
6. `render_prompt()`：用户选择一个提示词模板 → `get_prompt()` → 把结果当作用户消息发出去。
7. `report_server.py`：第二个 server，有一个故意留下的路径穿越漏洞（阶段 5 修复）。

## 关键概念

- **Host / Client / Server 三个角色**：`MCPAgent` 是 Host，`self.clients` 里每个 `Client` 对应一个 Server。
- **连接的生命周期**：进入 `async with Client(...)` 时完成连接和能力协商（新版协议用 `server/discover`，旧版用 `initialize` 握手），退出时断开；stdio 的子进程也会随之结束。
- **stdio 子进程不继承环境变量**：只会继承 `HOME`、`PATH` 等少数几个。需要传给 server 的变量要写在配置的 `env` 里（`tests/` 就是这么给 server 传临时数据库路径的）。

## 动手练习

1. 在 `servers.json` 里加一个你自己写的 server（比如一个天气 server），不改任何 Python 代码就能被 Agent 使用。
2. 让 `ask()` 并发执行同一轮里的多个工具调用（`asyncio.gather`）。
3. 实现简单的"工具筛选"：工具超过 N 个时，先让模型挑出相关的 server，只把这些 server 的工具交给它。
4. 配置真实模型后，故意问一个需要先查表结构的问题，观察模型会不会因为 SQL 写错而自己重试。

## 面试题

- **Host、Client、Server 分别是什么？** 见上面的关键概念，对照 `MCPAgent` 讲。
- **MCP 工具怎么交给模型？** `list_tools` 得到工具定义 → 转换成 Function Calling 的格式（`input_schema` 就是 JSON Schema，可以直接用）→ 模型决定调用后，再路由回对应的 client 执行。
- **工具太多怎么办？** 按任务筛选（见练习 3）、做工具检索、合并相近的工具、把描述写清楚；另外工具结果要截断（`max_result_chars`）。
- **一个连接的生命周期？** 能力协商 → 正常请求 → 关闭。新旧两版协议的区别见 `docs/protocol_notes.md`。
