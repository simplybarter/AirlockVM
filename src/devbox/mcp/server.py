"""DevBox MCP Server implementation."""

from mcp.server.mcpserver import MCPServer

server = MCPServer("devbox")


@server.tool()
def devbox_ping() -> str:
    """Check DevBox MCP server status."""
    return "DevBox MCP Server is online (Python 3.14)"


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
