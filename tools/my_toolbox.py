"""我的工具箱 MCP Server —— 给 DeerFlow agent 用的实用工具"""

import json
import os
from datetime import datetime
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("我的工具箱")

# 备忘录存储路径
NOTES_FILE = os.path.expanduser("~/my-deer-flow-notes.json")


def _load_notes() -> list[dict]:
    if os.path.exists(NOTES_FILE):
        with open(NOTES_FILE, "r") as f:
            return json.load(f)
    return []


def _save_notes(notes: list[dict]):
    with open(NOTES_FILE, "w") as f:
        json.dump(notes, f, ensure_ascii=False, indent=2)


# ── 工具 1：获取当前时间 ──
@mcp.tool()
def get_current_time() -> str:
    """获取当前日期和时间，返回格式: 2026-07-08 14:30:00 星期一"""
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    now = datetime.now()
    return now.strftime(f"%Y-%m-%d %H:%M:%S 星期{weekdays[now.weekday()]}")


# ── 工具 2：添加备忘录 ──
@mcp.tool()
def add_note(title: str, content: str) -> str:
    """添加一条备忘录。参数: title 标题, content 内容"""
    notes = _load_notes()
    notes.append({
        "title": title,
        "content": content,
        "created_at": datetime.now().isoformat()
    })
    _save_notes(notes)
    return f"✅ 已添加备忘录: {title}"


# ── 工具 3：列出所有备忘录 ──
@mcp.tool()
def list_notes() -> str:
    """列出所有备忘录"""
    notes = _load_notes()
    if not notes:
        return "📝 暂无备忘录"
    
    lines = [f"📝 共 {len(notes)} 条备忘录:"]
    for i, note in enumerate(notes, 1):
        created = note["created_at"][:10]
        lines.append(f"  {i}. [{created}] {note['title']}")
    return "\n".join(lines)


# ── 工具 4：查询备忘录 ──
@mcp.tool()
def search_notes(keyword: str) -> str:
    """按关键词搜索备忘录"""
    notes = _load_notes()
    results = [n for n in notes if keyword in n["title"] or keyword in n["content"]]
    if not results:
        return f"未找到包含「{keyword}」的备忘录"
    
    lines = [f"找到 {len(results)} 条:「{keyword}」"]
    for i, note in enumerate(results, 1):
        lines.append(f"  {i}. {note['title']}: {note['content'][:100]}")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
