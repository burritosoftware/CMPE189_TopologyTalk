import httpx
import json
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()
MCP_HOST = os.getenv("MCP_HOST", "localhost")
MCP_PORT = os.getenv("MCP_PORT", "8000")

async def call_tool(name, arguments):
    print(f"\n>>> Calling tool: {name}")
    async with httpx.AsyncClient() as client:
        url = f"http://{MCP_HOST}:{MCP_PORT}/mcp"
        headers = {
            "Accept": "application/json, text/event-stream",
        }
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": name,
                "arguments": arguments
            }
        }
        try:
            # FastMCP with stateless_http=True might return an SSE stream
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                print(f"<<< Response ({response.status_code}):")
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        data_str = line[len("data:"):].strip()
                        try:
                            result = json.loads(data_str)
                            if "error" in result:
                                print(f"Error: {json.dumps(result['error'], indent=2)}")
                            else:
                                content = result.get("result", {}).get("content", [])
                                for item in content:
                                    if item.get("type") == "text":
                                        print(item.get("text"))
                                    else:
                                        print(json.dumps(item, indent=2))
                        except json.JSONDecodeError:
                            print(f"Failed to parse line: {line}")
                    elif line.strip():
                        # Try to parse as raw JSON if not SSE
                        try:
                           result = json.loads(line)
                           # (same processing as above)
                        except:
                           pass
        except Exception as e:
            print(f"Connection failed: {e}")

async def main():
    print(f"TopologyTalk Demo Client connecting to http://{MCP_HOST}:{MCP_PORT}/mcp")
    
    # 1. Get topology
    await call_tool("get_network_topology", {})
    
    # 2. Try to set a safe QoS policy
    intent = "Limit switch 1 bandwidth to 500Mbps"
    request = {
        "switch_id": "0000000000000001",
        "max_rate": 500000000,
        "queues": [{"max_rate": 100000000}]
    }
    await call_tool("safe_set_qos_queues", {"request": request, "intent": intent})

if __name__ == "__main__":
    asyncio.run(main())
