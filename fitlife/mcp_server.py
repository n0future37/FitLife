"""MCP 只包装工具接口，业务规则留在 tools.py。"""

from mcp.server.fastmcp import FastMCP

from fitlife.tools import Category, get_activity_catalog

mcp = FastMCP("fitlife-readonly")


@mcp.tool(name="get_activity_catalog")
def catalog_tool(category: Category = "all") -> dict:
    """查询FitLife支持的运动项目。all查全部，aerobic查有氧，mobility查关节活动。只读。"""
    return get_activity_catalog(category)


if __name__ == "__main__":
    mcp.run(transport="stdio")
