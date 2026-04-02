#!/usr/bin/env python3
import os
from fastmcp import FastMCP
import subprocess
import sys
import requests
import json
from dotenv import load_dotenv

from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext

mcp = FastMCP("TopologyTalk")

load_dotenv()
RYU_OFCTL_REST_URL = os.getenv("RYU_OFCTL_REST_URL")
RYU_API_VERSION = os.getenv("RYU_API_VERSION", "v1.0")
MCP_HOST = os.getenv("MCP_HOST")
MCP_PORT = int(os.getenv("MCP_PORT"))

# Pydantic Model for tool call verification
class ToolCallVerification(BaseModel):
    is_valid: bool = Field(description="Whether the tool call is valid and safe")
    reason: str = Field(description="The reason for the validation result")
    recommended_action: str = Field(description="Recommended next step if not valid")

# PydanticAI Agent for checking tool calls
# TODO: Obtain an appropriate LLM provider (e.g. OpenAI) API key and set in .env. Probably make a better prompt as well.
checker_agent = Agent(
    'openai:gpt-4o', 
    result_type=ToolCallVerification,
    system_prompt="You are a network security auditor for an SDN controller. Your job is to verify tool calls intended for the network."
)

## MCP Tools
@mcp.tool(description="Verify if a tool call to the network is safe and valid using PydanticAI")
async def verify_tool_call(tool_name: str, arguments: dict) -> dict:
    """
    Uses PydanticAI to audit a tool call before it's executed against the network.
    """
    try:
        result = await checker_agent.run(
            f"Audit the following tool call: Tool={tool_name}, Arguments={json.dumps(arguments)}"
        )
        return result.data.model_dump()
    except Exception as e:
        return {"is_valid": False, "reason": f"Verification error: {str(e)}", "recommended_action": "Retry or manual check"}

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

@mcp.tool(description="")
def get_network_topology() -> str:
    """
    Fetches the current network topology, including all switches and links 
    discovered by the Ryu controller.
    """
    BASE_URL = f"{RYU_OFCTL_REST_URL}/{RYU_API_VERSION}/topology"
    
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

  subprocess.Popen([sys.executable, "run_tunnel.py", "--host", MCP_HOST, "--port", str(MCP_PORT)])

  print(f"Starting FastMCP server on {MCP_HOST}:{MCP_PORT}")
  mcp.run(
    transport="http",
    host=MCP_HOST,
    port=MCP_PORT,
    stateless_http=True
  )
