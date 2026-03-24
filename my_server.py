#!/usr/bin/env python3
import os
from fastmcp import FastMCP
import subprocess
import sys
import requests
import json

mcp = FastMCP("TopologyTalk")

## MCP Tools
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

@mcp.tool()
def get_network_topology() -> str:
    """
    Fetches the current network topology, including all switches and links 
    discovered by the Ryu controller.
    """
    BASE_URL = "http://localhost:8080/v1.0/topology"
    
    try:
        # Get switches and links from Ryu's topology REST API
        switches = requests.get(f"{BASE_URL}/switches").json()
        links = requests.get(f"{BASE_URL}/links").json()
        
        topology_summary = {
            "switch_count": len(switches),
            "switches": [s['dpid'] for s in switches],
            "links": [
                f"Switch {l['src']['dpid']} (port {l['src']['port_no']}) -> "
                f"Switch {l['dst']['dpid']} (port {l['dst']['port_no']})"
                for l in links
            ]
        }
        
        return json.dumps(topology_summary, indent=2)
    
    except Exception as e:
        return f"Error fetching topology: {str(e)}"

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
