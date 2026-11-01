# MCP 面试题 × 本项目代码对照

**用法**：先自己回答，再对照答案要点，然后打开右侧列出的代码，确认自己能指着代码讲出来。
回答时尽量用"概念 + 本项目里的例子 + 踩过的坑"这个结构。

## 一、基础概念

**1. MCP 是什么？解决了什么问题？** → `stage1_function_calling/README.md` 的"三个痛点"
- 它是一个开放协议，统一 AI 应用和外部工具、数据之间的连接方式；
- 把 M×N 套对接代码变成 M+N：应用实现一次 client，工具实现一次 server；
- 例子：阶段 2 的 shop-db server 写一次，就能被 Cursor、Claude Desktop 和阶段 3 的 Agent 同时使用。

**2. MCP 和 Function Calling 的区别？** ⭐ → 对比 `stage1_function_calling/agent.py` 和 `stage3_agent/agent.py`
- Function Calling 是模型的能力：决定调用哪个工具、参数是什么；
- MCP 是应用和工具之间的协议：工具怎么被发现、怎么调用、结果怎么返回；
- 两者配合使用：`to_openai_tool()` 把 MCP 工具转换成 Function Calling 的格式，模型决定调用后，`call_tool()` 再通过 MCP 去执行。

**3. 架构里有哪些角色？** → `MCPAgent`（Host）、`self.clients`（Client）、`stage2_mcp_server/server.py`（Server）
- Host 是用户使用的应用；Client 在 Host 内部，和 Server 一对一连接；Server 提供能力；
- 通信格式是 JSON-RPC 2.0。

**4. 三种原语？** ⭐ → `stage2_mcp_server/server.py`
- Tools 由模型控制（`run_sql`）；Resources 由应用控制（Agent 启动时读取 `schema://tables`）；Prompts 由用户控制（`/prompt analyze_sales`）。

**5. Client 能为 Server 提供什么？** → `stage5_security_eval/secure_agent.py` 的 `ask_user_in_terminal`
- Elicitation（向用户提问）、Sampling（借用 client 的大模型）、Roots（告知 server 可以访问的目录）；
- 注册了对应的回调，才算声明了这项能力；没声明时 server 发起相关请求会直接失败；
- 新版协议里，这三类请求都改成通过 `InputRequiredResult` 的多轮往返来完成（见 `docs/protocol_notes.md`）。

## 二、传输与协议

**6. stdio 和 Streamable HTTP 怎么选？** → `stage2`（stdio）、`stage4_remote`（HTTP）
- stdio：本地子进程，简单、不需要鉴权；**stdout 就是协议通道，日志必须写 stderr**；子进程不继承环境变量；
- HTTP：远程部署、多人共用，需要鉴权、Host 白名单和 TLS；
- HTTP+SSE 已经被 Streamable HTTP 取代。

**7. 连接的生命周期？** → `docs/protocol_notes.md`
- 旧版：`initialize` → server 回复能力 → `initialized` 通知 → 正常请求 → 关闭；
- 新版：一次 `server/discover` 拿到全部信息，没有握手；
- SDK 的 `Client(mode="auto")` 会自动协商，项目里两种都跑过（`test_stage4_...`）。

**8. 有状态和无状态？** → `stage4_remote/client_remote.py` 第 6 步
- 旧版用 `Mcp-Session-Id`，多实例时要会话粘性，`stateless_http=True` 可以关掉；
- 新版没有会话，天然适合横向扩展；
- 多实例部署时还要配置相同的 `requestState` 密钥。

**9. 工具执行失败和协议错误？** → `stage2_mcp_server/server.py` 的 `ToolError`；`stage3_agent/agent.py` 的 `call_tool()`
- `ToolError` → `is_error=True`，模型能看到错误并自己改正（比如 SQL 写错后改正重试）；
- `MCPError` → JSON-RPC 错误，由程序处理（比如 client 没有声明 elicitation 能力时的报错）。

## 三、安全

**10. 有哪些安全风险？** → `stage5_security_eval/injection_demo.py`
- 提示注入（项目里的演示：恶意评价 → `DROP TABLE`）、工具投毒、Rug Pull、权限过大、路径穿越（演示 2）、SQL 注入（阶段 4 里用 `?` 参数化）。

**11. 怎么防？** → `sql_guard.py`、`server_secure.py`、`secure_agent.py`
- 工具层强制执行规则（只读连接、authorizer、脱敏、超时和行数上限）—— **最重要，因为演示里模型确实被骗了**；
- 写操作要用户确认（Elicit）；写审计日志；
- Host 层用 `<tool_result>` 标签包住结果并声明它是数据（只能降低风险）；
- 只使用可信的 server，坚持最小权限。

**12. 远程 server 怎么鉴权？** → `stage4_remote/server_http.py`
- OAuth 2.1 + Bearer Token；SDK 的接入点是 `TokenVerifier.verify_token()`；
- 401 响应带 `WWW-Authenticate` 和资源元数据地址（RFC 9728）；`validate_token_resource` 拒绝签发给其它服务的 token；
- 不要把用户的 token 原样转发给下游服务。

## 四、工程实践

**13. 工具太多怎么办？** → `stage3_agent/README.md` 练习 3
- 按任务筛选 server 或工具、做工具检索、合并粒度、把描述写好；
- 结果截断（`max_result_chars`），把表结构这类上下文放进 Resource 一次性读入。

**14. 怎么设计好的工具？** → `describe_table` 的错误信息、`run_sql` 的 `Field` 约束、`update_product_price`
- 名称和描述写清楚"什么时候用"；参数有约束（`ge`、`le`）；错误信息告诉模型下一步怎么做；
- 返回结构化数据；写操作要确认、幂等；不暴露内部信息（加固版报告 server 不返回绝对路径）。

**15. 参数格式（Schema）从哪来？** → `stage2_mcp_server/try_client.py` 打印的 `input_schema`
- 由类型标注、默认值、`Field` 和 docstring 经 Pydantic 自动生成；
- `Context` 参数和 `Resolve(...)` 依赖不会出现在 schema 里（模型没法填）。

**16. 怎么调试？** → `try_client.py`、`tests/test_project.py`
- MCP Inspector（`mcp dev`）；用 SDK 的 Client 写脚本；
- 测试时用进程内 `Client(server 实例)`，不需要子进程和端口；
- stdio 下日志写 stderr。

**17. 长任务怎么办？** → `monthly_sales_report` 的 `ctx.report_progress()`
- 进度通知（client 在 `call_tool` 时传入 `progress_callback` 才会收到）、支持取消；
- 耗时更长的任务改成异步（先返回任务 ID，之后再查询结果）。

## 五、对比与开放题

**18. MCP 和 A2A？** MCP 连接 Agent 和工具（纵向）；A2A 连接 Agent 和 Agent（横向），两者互补。

**19. 和 LangChain Tool、插件机制相比？** 优点：跨语言、跨应用复用，生态大；缺点：多一层进程或网络开销，协议还在快速迭代（项目里就遇到了新旧两版），安全要自己把关。

**20. 讲讲你的项目** → 按下面这个顺序准备，每一点都要有具体数字或现象
1. 背景：用自然语言问业务数据，不懂 SQL 也能查；
2. 架构：Host + 2 个 MCP server，stdio 和 HTTP 两种部署方式（画出 README 里的架构图）；
3. 工具设计：为什么拆成 `list_tables`、`describe_table`、`run_sql`；为什么把表结构做成 Resource；
4. 踩过的坑：stdio 子进程不继承环境变量；DNS 重绑定防护导致 421；SQL 错误信息怎么写才能让模型自己改正；
5. 安全：提示注入演示 → 为什么要在工具层兜底；
6. 效果：评估集的正确率（用真实模型跑出来的数字）、平均步数、工具出错率，以及这种评估方式的局限。
