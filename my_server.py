#!/usr/bin/env python3
import os
import asyncio
from fastmcp import FastMCP
import subprocess
import sys

mcp = FastMCP("TopologyTalk")

@mcp.tool(description="Greet a user by name with a welcome message from the MCP server")
def greet(name: str) -> str:
    return f"Hello, {name}! Welcome to our sample MCP server!"

@mcp.tool(description="Get information about the MCP server including name, version, environment, and Python version")
def get_server_info() -> dict:
    return {
        "server_name": "TopologyTalk",
        "version": "1.0.0",
        "environment": os.environ.get("ENVIRONMENT", "development"),
        "python_version": os.sys.version.split()[0]
    }

if __name__ == "__main__":
  port = int(os.environ.get("PORT", 8000))
  host = "localhost"

  subprocess.Popen([sys.executable, "run_tunnel.py"])

  print(f"Starting FastMCP server on {host}:{port}")  
  mcp.run(
    transport="http",
    host=host,
    port=port,
    stateless_http=True
  )