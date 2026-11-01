# 阶段 2：MCP Server

**目标**：用 `MCPServer` 把数据库变成一个标准的 MCP Server，并把三种原语（Tools、Resources、Prompts）都用一遍。

## 运行

```bash
python -m stage2_mcp_server.try_client
```

这个脚本会以 stdio 方式启动 server 子进程，把每个功能调一遍，并打印线上实际传输的内容（工具的 schema、结构化结果、错误信息、资源、提示词）。

装了 Node.js 的话，也可以用官方调试工具 Inspector：

```bash
mcp dev stage2_mcp_server/server.py
```

## 读代码的顺序（`server.py`）

1. `build_server()`：写成工厂函数，阶段 4 和阶段 5 可以直接复用（传入鉴权参数、换掉执行器）。
2. **Tools**：`list_tables`、`describe_table`、`run_sql`
   - 参数用 `Field(description=...)` 描述，返回值用 Pydantic 模型 `QueryResult` → SDK 自动生成 `input_schema` 和 `output_schema`；
   - `raise ToolError(...)` → 返回 `is_error=True` 的结果，**模型能看到错误信息**并自己改正；
   - `ToolAnnotations(read_only_hint=True)`：告诉 Host 这个工具是只读的，只是提示，不是强制约束。
3. **Resources**：`schema://tables` 是静态资源，`schema://tables/{table}` 是 URI 模板。
4. **Prompts**：`analyze_sales(month)`，客户端里通常显示成斜杠命令。
5. **lifespan**：启动时检查数据库是否存在。真实项目一般在这里创建连接池。
6. 最后一行 `mcp.run()`：默认使用 stdio 传输。

## 关键概念

| 原语 | 谁控制 | 本项目里的例子 |
|---|---|---|
| Tools | **模型**决定什么时候调用 | `run_sql` |
| Resources | **应用（Host）**决定读不读 | 阶段 3 的 Agent 启动时，把 `schema://tables` 读进系统提示词 |
| Prompts | **用户**主动选择 | 在阶段 3 的对话里输入 `/prompt analyze_sales 2026-08` |

- **input_schema 从哪来**：SDK 读取函数签名（类型标注、默认值、`Field` 约束）和 docstring，再交给 Pydantic 生成 JSON Schema。对比阶段 1 里手写的 `TOOLS`。
- **content 和 structured_content**：同一个结果有两份。`content` 是给模型看的文本，`structured_content` 是给程序用的 JSON（符合 `output_schema`）。
- **stdio 的坑**：stdout 就是协议通道。v1 里随手 `print` 一下就会破坏协议；v2 会把运行期间刷到 stdout 的输出转到 stderr，但启动前的输出仍然会污染通道。**日志一律用 `logging` 写到 stderr**。

## 动手练习

1. 给 `run_sql` 加一个参数 `explain: bool = False`，为 True 时返回 `EXPLAIN QUERY PLAN` 的结果。运行 `try_client` 看看 schema 有什么变化（不用手动改任何 schema）。
2. 加一个资源 `stats://overview`，返回各表的行数。
3. 加一个提示词模板 `compare_months(month_a, month_b)`。
4. 把 `run_sql` 的返回值从 `QueryResult` 改成 `str`，观察 `output_schema` 和 `structured_content` 怎么变。

## 面试题

- **三种原语的区别？** 关键是"谁控制"，见上表。
- **工具的 JSON Schema 是怎么来的？** 由类型标注、Field 和 docstring 自动生成，代码在 SDK 的 `mcp/server/mcpserver/tools/`。
- **工具执行失败和协议错误有什么区别？** `ToolError` 会变成 `is_error=True` 的正常结果，模型看得到；`MCPError` 会变成 JSON-RPC 错误，给程序处理。
- **怎么调试 MCP server？** 用 MCP Inspector（`mcp dev`）或 SDK 的 Client 写脚本（本阶段的 `try_client.py`）；日志写 stderr；测试时用进程内 `Client(server)`（见 `tests/`）。
