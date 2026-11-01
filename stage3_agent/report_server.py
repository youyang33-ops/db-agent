"""阶段 3：第二个 MCP Server —— 把分析报告保存成文件。

它的作用是让 Agent 同时连接两个 server：查数据用 shop-db，存结果用 reports。
真实项目里这个位置可以换成任何现成的 server，例如官方的 filesystem server（需要 Node.js）。

运行：python -m stage3_agent.report_server（stdio）
"""

from pathlib import Path

from pydantic import Field

from common.config import REPORT_DIR
from mcp.server.mcpserver import MCPServer


def build_server(report_dir: Path = REPORT_DIR) -> MCPServer:
    mcp = MCPServer("reports", instructions="把文本报告保存到本地 reports/ 目录，或列出已保存的报告。")

    @mcp.tool()
    def save_report(
        filename: str = Field(description="文件名，例如 2026-08-sales.md"),
        content: str = Field(description="报告内容（Markdown）"),
    ) -> str:
        """把一份报告保存成文件，返回保存的路径。"""
        report_dir.mkdir(parents=True, exist_ok=True)
        # ⚠️ 故意留下的漏洞（阶段 5 修复）：没有检查 filename。
        # 传入 "../../.bashrc" 这样的文件名，就能写到 reports/ 目录之外（路径穿越）。
        path = report_dir / filename
        path.write_text(content, encoding="utf-8")
        return f"已保存到 {path}"

    @mcp.tool()
    def list_reports() -> list[str]:
        """列出已经保存的报告文件名。"""
        if not report_dir.exists():
            return []
        return sorted(p.name for p in report_dir.iterdir() if p.is_file())

    return mcp


mcp = build_server()

if __name__ == "__main__":
    mcp.run()
