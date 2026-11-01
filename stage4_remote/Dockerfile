# 构建（在项目根目录执行）：docker build -f stage4_remote/Dockerfile -t shop-db-mcp .
# 运行：docker run --rm -p 8000:8000 -e MCP_API_TOKEN=换成你自己的token shop-db-mcp
FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml ./
COPY common ./common
COPY data ./data
COPY stage1_function_calling ./stage1_function_calling
COPY stage2_mcp_server ./stage2_mcp_server
COPY stage3_agent ./stage3_agent
COPY stage4_remote ./stage4_remote
COPY stage5_security_eval ./stage5_security_eval

# 国内网络慢可以加镜像：pip install -i https://pypi.tuna.tsinghua.edu.cn/simple ...
# 只装依赖；项目代码在 /app 下，用 python -m 从 /app 启动就能找到
RUN pip install --no-cache-dir "mcp[cli]==2.3.0" "openai>=1.40" "python-dotenv>=1.0" \
    && python -m data.init_db

# 容器里必须监听 0.0.0.0，否则宿主机访问不到
ENV MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    MCP_PUBLIC_URL=http://127.0.0.1:8000 \
    MCP_ALLOWED_HOSTS=127.0.0.1:*,localhost:*

EXPOSE 8000
# 不要用 root 跑服务
RUN useradd --create-home app && chown -R app /app
USER app
CMD ["python", "-m", "stage4_remote.server_http"]
