#!/usr/bin/env python3
import os
from fastmcp import FastMCP
import subprocess
import sys
import requests
import json
from dotenv import load_dotenv
from models import SetQueueRequest, AddQoSRuleRequest, DeleteQoSRuleRequest, QueueConfig, QoSRuleMatch, QoSRuleActions
from validator import validate_qos_action

load_dotenv()
RYU_BASE_URL = os.getenv("RYU_BASE_URL", "http://localhost:8080")
MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))

mcp = FastMCP("TopologyTalk")

## Helper functions
def ryu_get(path):
    response = requests.get(f"{RYU_BASE_URL}{path}")
    response.raise_for_status()
    return response.json()

def ryu_post(path, data):
    response = requests.post(f"{RYU_BASE_URL}{path}", json=data)
    response.raise_for_status()
    return response.json()

def ryu_delete(path, data=None):
    if data:
        response = requests.delete(f"{RYU_BASE_URL}{path}", json=data)
    else:
        response = requests.delete(f"{RYU_BASE_URL}{path}")
    response.raise_for_status()
    return response.json()

def ryu_put(path: str, payload):
    resp = requests.put(
        f"{RYU_BASE_URL}{path}",
        json=payload,
        timeout=5,
    )
    resp.raise_for_status()

    try:
        return resp.json()
    except Exception:
        return resp.text

## MCP Tools

@mcp.tool(description="Get information about the MCP server")
def get_server_info() -> dict:
    return {
        "server_name": "TopologyTalk",
        "version": "1.1.0",
        "environment": os.environ.get("ENVIRONMENT", "development"),
        "python_version": os.sys.version.split()[0],
        "ryu_base_url": RYU_BASE_URL
    }

@mcp.tool()
def get_network_topology() -> str:
    """
    Fetches the current network topology, including all switches and links 
    discovered by the Ryu controller.
    """
    try:
        switches = ryu_get("/v1.0/topology/switches")
        links = ryu_get("/v1.0/topology/links")
        
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

@mcp.tool()
def bind_ovsdb_bridges(ovsdb_addr: str = "tcp:127.0.0.1:6640") -> str:
    """
    Binds every switch DPID discovered by Ryu to the given OVSDB address.

    This fixes Ryu QoS errors like:
    "ovs_bridge is not exists"

    Requires:
    - ryu.app.rest_conf_switch loaded
    - OVSDB listening, usually from:
      sudo ovs-vsctl set-manager ptcp:6640
    """
    try:
        switches = ryu_get("/v1.0/topology/switches")

        results = []

        for switch in switches:
            dpid = switch["dpid"]
            path = f"/v1.0/conf/switches/{dpid}/ovsdb_addr"

            # Important: rest_conf_switch expects the body to be a JSON string,
            # so requests' json=ovsdb_addr is correct.
            response = ryu_put(path, ovsdb_addr)

            results.append({
                "dpid": dpid,
                "ovsdb_addr": ovsdb_addr,
                "result": response,
            })

        return json.dumps({
            "bound_count": len(results),
            "bindings": results,
        }, indent=2)

    except Exception as e:
        return f"Error binding OVSDB bridges: {str(e)}"

@mcp.tool()
def get_qos_queues(switch_id: str = "all") -> str:
    """
    Get the current queue configurations for the specified switch.
    """
    try:
        data = ryu_get(f"/qos/queue/{switch_id}")
        return json.dumps(data, indent=2)
    except Exception as e:
        return f"Error fetching QoS queues: {str(e)}"

@mcp.tool()
def get_qos_rules(switch_id: str = "all", vlan_id: str = "all") -> str:
    """
    Get the current QoS rules for the specified switch and VLAN.
    """
    try:
        if vlan_id == "all":
            data = ryu_get(f"/qos/rules/{switch_id}")
        else:
            data = ryu_get(f"/qos/rules/{switch_id}/{vlan_id}")
        return json.dumps(data, indent=2)
    except Exception as e:
        return f"Error fetching QoS rules: {str(e)}"

@mcp.tool()
def set_qos_queues(request: SetQueueRequest) -> str:
    """
    Configure queues on a switch.
    Example: set_qos_queues(SetQueueRequest(switch_id="0000000000000001", max_rate=10000000, queues=[QueueConfig(max_rate=1000000)]))
    """
    try:
        # Convert Pydantic model to dict, filtering out None values
        data = request.dict(exclude_none=True)
        # switch_id is in the URL, not the body for this API
        switch_id = data.pop("switch_id")
        
        result = ryu_post(f"/qos/queue/{switch_id}", data)
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Error setting QoS queues: {str(e)}"

@mcp.tool()
def add_qos_rule(request: AddQoSRuleRequest) -> str:
    """
    Add a QoS rule to a switch.
    """
    try:
        data = request.dict(exclude_none=True)
        switch_id = data.pop("switch_id")
        vlan_id = data.pop("vlan_id", None)
        
        if vlan_id and vlan_id != "all":
            path = f"/qos/{switch_id}/{vlan_id}"
        else:
            path = f"/qos/{switch_id}"
            
        result = ryu_post(path, data)
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Error adding QoS rule: {str(e)}"

@mcp.tool()
def delete_qos_rule(request: DeleteQoSRuleRequest) -> str:
    """
    Delete a QoS rule from a switch.
    """
    try:
        data = request.dict(exclude_none=True)
        switch_id = data.pop("switch_id")
        vlan_id = data.pop("vlan_id", None)
        
        if vlan_id and vlan_id != "all":
            path = f"/qos/rules/{switch_id}/{vlan_id}"
        else:
            path = f"/qos/rules/{switch_id}"
            
        result = ryu_delete(path, data)
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"Error deleting QoS rule: {str(e)}"

@mcp.tool()
def get_qos_status(switch_id: str = "all") -> str:
    """
    Get the status of queues on a switch.
    """
    try:
        data = ryu_get(f"/qos/queue/status/{switch_id}")
        return json.dumps(data, indent=2)
    except Exception as e:
        return f"Error fetching QoS status: {str(e)}"

@mcp.tool()
async def safe_set_qos_queues(request: SetQueueRequest, intent: str) -> str:
    """
    Configure queues on a switch with a safety check against the user's intent.
    """
    validation = await validate_qos_action(intent, request.dict(exclude_none=True))
    if not validation.is_safe:
        return json.dumps({
            "status": "REJECTED",
            "reason": validation.reason,
            "suggested_action": validation.suggested_action
        }, indent=2)
    
    return set_qos_queues(request)

@mcp.tool()
async def safe_add_qos_rule(request: AddQoSRuleRequest, intent: str) -> str:
    """
    Add a QoS rule to a switch with a safety check against the user's intent.
    """
    validation = await validate_qos_action(intent, request.dict(exclude_none=True))
    if not validation.is_safe:
        return json.dumps({
            "status": "REJECTED",
            "reason": validation.reason,
            "suggested_action": validation.suggested_action
        }, indent=2)
    
    return add_qos_rule(request)

if __name__ == "__main__":
    if os.getenv("POKE_TUNNEL_ENABLED") == "true":
        # Start the tunnel in the background
        # Note: run_tunnel.py expects MCP_HOST and MCP_PORT
        subprocess.Popen([sys.executable, "run_tunnel.py", "--host", MCP_HOST, "--port", str(MCP_PORT)])
        print("PokeTunnel enabled and starting...")
    else:
        print("PokeTunnel disabled. Set POKE_TUNNEL_ENABLED=true to enable.")
    mcp.run(
        transport="http",
        host=MCP_HOST,
        port=MCP_PORT,
        stateless_http=True
    )
