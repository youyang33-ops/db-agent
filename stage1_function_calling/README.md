# 阶段 1：手写 Function Calling（先不用 MCP）

**目标**：搞懂 Agent 的本质 —— 一个"模型 → 工具 → 模型"的循环；再亲身体会没有 MCP 时哪里麻烦。

## 运行

```bash
python -m stage1_function_calling.agent "销量最高的 3 个商品是什么？"
```

不带参数运行会进入多轮对话。没有配置 API Key 时用的是 MockLLM。

## 读代码的顺序（`agent.py`）

1. `TOOLS`：发给模型的工具说明书（JSON Schema）。模型就是靠 `description` 判断什么时候该用哪个工具。
2. `run_agent()`：整个 Agent 循环。
   - 调用模型，把回复追加进 `messages`；
   - 回复里没有 `tool_calls` → 模型认为可以回答了，循环结束；
   - 有 `tool_calls` → 逐个执行，结果以 `role: "tool"` 消息放回对话，**必须带上 `tool_call_id`**；
   - `max_steps` 限制最大步数，防止死循环。
3. `execute_tool()`：工具出错时**不让程序崩溃**，而是把错误信息返回给模型，让它自己改正。

## 关键概念

- **Function Calling 是模型的能力**：模型只会"说"自己想调用哪个函数、参数是什么，真正执行的是你的代码。
- **messages 就是 Agent 的记忆**：每一步的工具调用和结果都要放回去，模型才知道之前做过什么。

## 没有 MCP 时的三个痛点（阶段 2 会解决）

1. Schema 是手写的：函数参数一改，`TOOLS` 也得跟着改，很容易对不上。
2. 工具和 Agent 绑死在同一个进程里，Cursor、Claude Desktop 等其它应用没法复用。
3. 每接一个新应用，都要再写一遍对接代码（M 个应用 × N 个工具 = M×N 套代码）。

## 动手练习

1. 把 `max_steps` 改成 1，问一个需要两步才能完成的问题，观察会发生什么。
2. 去掉 `execute_tool` 里的 `try/except`，故意让模型写错 SQL，看程序怎么崩溃。
3. 加一个新工具 `count_rows(table)`，体会一下要改几个地方。
4. 配置真实模型后，在 `run_agent` 里打印每一轮的 `messages`，看清楚消息格式。

## 面试题

- **MCP 和 Function Calling 有什么区别？**
  Function Calling 是模型的能力（决定调用哪个工具、参数是什么）；MCP 是应用和工具之间的通信协议（工具怎么被发现、怎么调用、结果怎么传回）。两者是配合关系：MCP 工具会被转换成 Function Calling 的格式交给模型。对照本阶段和阶段 3 的代码就能讲清楚。
- **Agent 循环怎么防止死循环？** 限制最大步数；检测连续重复的调用；设置总超时和总 token 预算。
