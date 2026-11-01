"""阶段 5：修复了路径穿越的报告 server。和 stage3_agent/report_server.py 对比着看。

运行：python -m stage5_security_eval.report_server_secure（stdio）
"""

import re
from pathlib import Path

from pydantic import Field

from common.config import REPORT_DIR
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

SAFE_NAME = re.compile(r"^[\w\-. ]{1,80}\.(md|txt)$")  # 只允许简单文件名 + 白名单扩展名
MAX_BYTES = 200_000


def safe_report_path(report_dir: Path, filename: str) -> Path:
    if not SAFE_NAME.match(filename) or filename.startswith("."):
        raise ToolError(f"文件名 {filename!r} 不合法：只能用字母、数字、中文、-_. 和空格，扩展名 .md 或 .txt")
    path = (report_dir / filename).resolve()
    # 第二道保险：解析符号链接和 .. 之后，结果必须仍然在 report_dir 里面
    if not path.is_relative_to(report_dir.resolve()):
        raise ToolError("不允许写到报告目录之外")
    return path


def build_server(report_dir: Path = REPORT_DIR) -> MCPServer:
    mcp = MCPServer("reports-secure", instructions="把文本报告保存到本地 reports/ 目录，或列出已保存的报告。")

    @mcp.tool()
    def save_report(
        filename: str = Field(description="文件名，例如 2026-08-sales.md（只能是 .md 或 .txt）"),
        content: str = Field(description="报告内容（Markdown）"),
    ) -> str:
        """把一份报告保存成文件，返回保存的文件名。"""
        if len(content.encode()) > MAX_BYTES:
            raise ToolError(f"报告太大（超过 {MAX_BYTES} 字节）")
        report_dir.mkdir(parents=True, exist_ok=True)
        path = safe_report_path(report_dir, filename)
        path.write_text(content, encoding="utf-8")
        return f"已保存为 {path.name}"  # 不把服务器上的绝对路径暴露给模型

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
