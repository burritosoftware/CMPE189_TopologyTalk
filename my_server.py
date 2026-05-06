#!/usr/bin/env python3
"""
TopologyTalk Constrained MCP Server.
Implements specific flow and group management tools with validation and logging.
"""

from __future__ import annotations

import subprocess
import sys
import json
import os
import requests
import asyncio
from typing import Any, Dict, List, Optional, Union
from dotenv import load_dotenv
from fastmcp import FastMCP

from models import (
    ForwardingFlowRequest, FlowMatch, SelectGroupRequest, 
    FastFailoverGroupRequest, FlowToGroupRequest, 
    DeleteFlowRequest, DeleteGroupRequest
)
from validator import validate_sdn_request

load_dotenv()

RYU_BASE_URL = os.getenv("RYU_BASE_URL", "http://localhost:8080")
MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))

# Dedicated cookie for LLM-installed flows: "llm" in hex prefix
# Use in combination with group IDs > 10000 to identify LLM installations
LLM_COOKIE = 0x6c6c6d0000000000
LLM_COOKIE_MASK = 0xffffff0000000000

mcp = FastMCP("TopologyTalk")

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _log_tool_call(tool_name: str, params: Any):
    print(f"[TOOL_CALL] {tool_name} with params: {params}")

def _json(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=False)

def _request(method: str, path: str, **kwargs: Any) -> Any:
    url = f"{RYU_BASE_URL}{path}"
    response = requests.request(method, url, timeout=10, **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(f"Ryu Error: {response.status_code} - {response.text}")
    return response.json() if response.text else None

def _dpid_to_int(dpid: str | int) -> int:
    if isinstance(dpid, int): return dpid
    return int(str(dpid), 16) if str(dpid).startswith("0x") else int(str(dpid), 16 if len(str(dpid)) > 10 else 10)

def _dpid_to_16hex(dpid: str | int) -> str:
    val = _dpid_to_int(dpid)
    return f"{val:016x}"

# -----------------------------------------------------------------------------
# Discovery & Observability Tools
# -----------------------------------------------------------------------------

@mcp.tool()
async def get_topology() -> str:
    """Return a structured summary of switches, links, and hosts for the LLM."""
    _log_tool_call("get_topology", {})
    try:
        switches = _request("GET", "/v1.0/topology/switches")
        links = _request("GET", "/v1.0/topology/links")
        hosts = _request("GET", "/v1.0/topology/hosts")
        
        summary = {
            "switch_count": len(switches),
            "link_count": len(links),
            "host_count": len(hosts),
            "switches": [_dpid_to_16hex(s.get("dpid")) for s in switches],
            "links": [
                {
                    "src": _dpid_to_16hex(l.get("src", {}).get("dpid")),
                    "src_port": l.get("src", {}).get("port_no"),
                    "dst": _dpid_to_16hex(l.get("dst", {}).get("dpid")),
                    "dst_port": l.get("dst", {}).get("port_no")
                } for l in links
            ],
            "hosts": [
                {
                    "mac": h.get("mac"),
                    "ipv4": h.get("ipv4"),
                    "switch": _dpid_to_16hex(h.get("port", {}).get("dpid")),
                    "port": h.get("port", {}).get("port_no")
                } for h in hosts
            ]
        }
        return _json(summary)
    except Exception as e:
        return f"Error fetching topology: {str(e)}"

@mcp.tool()
async def get_hosts() -> str:
    """Return known hosts with MAC, IP, attached switch DPID, and port."""
    _log_tool_call("get_hosts", {})
    try:
        hosts = _request("GET", "/v1.0/topology/hosts")
        return _json([{
            "mac": h.get("mac"),
            "ipv4": h.get("ipv4"),
            "ipv6": h.get("ipv6"),
            "switch": _dpid_to_16hex(h.get("port", {}).get("dpid")),
            "port": h.get("port", {}).get("port_no")
        } for h in hosts])
    except Exception as e:
        return f"Error fetching hosts: {str(e)}"

@mcp.tool()
async def get_switches() -> str:
    """Return switch DPIDs and basic metadata (port lists)."""
    _log_tool_call("get_switches", {})
    try:
        switches = _request("GET", "/v1.0/topology/switches")
        return _json([{
            "dpid": _dpid_to_16hex(s.get("dpid")),
            "ports": [
                {"port_no": p.get("port_no"), "name": p.get("name"), "hw_addr": p.get("hw_addr")}
                for p in s.get("ports", [])
            ]
        } for s in switches])
    except Exception as e:
        return f"Error fetching switches: {str(e)}"

@mcp.tool()
async def get_links() -> str:
    """Return links between switches."""
    _log_tool_call("get_links", {})
    try:
        links = _request("GET", "/v1.0/topology/links")
        return _json([{
            "src_dpid": _dpid_to_16hex(l.get("src", {}).get("dpid")),
            "src_port": l.get("src", {}).get("port_no"),
            "dst_dpid": _dpid_to_16hex(l.get("dst", {}).get("dpid")),
            "dst_port": l.get("dst", {}).get("port_no")
        } for l in links])
    except Exception as e:
        return f"Error fetching links: {str(e)}"

@mcp.tool()
async def get_flows(switch_id: str) -> str:
    """Return detailed flow entries for one switch."""
    _log_tool_call("get_flows", {"switch_id": switch_id})
    try:
        dpid = _dpid_to_int(switch_id)
        flows = _request("GET", f"/stats/flow/{dpid}")
        if not flows or str(dpid) not in flows:
            return f"No flows found on {switch_id}"
        
        return _json(flows[str(dpid)])
    except Exception as e:
        return f"Error fetching flows: {str(e)}"

@mcp.tool()
async def get_port_stats(switch_id: str) -> str:
    """Return per-port stats (packets, bytes, errors, drops) for one switch."""
    _log_tool_call("get_port_stats", {"switch_id": switch_id})
    try:
        dpid = _dpid_to_int(switch_id)
        stats = _request("GET", f"/stats/port/{dpid}")
        if not stats or str(dpid) not in stats:
            return f"No port stats found on {switch_id}"
        return _json(stats[str(dpid)])
    except Exception as e:
        return f"Error fetching port stats: {str(e)}"

@mcp.tool()
async def get_queue_stats(switch_id: str) -> str:
    """Return queue stats (tx_bytes, packets, errors) for one switch."""
    _log_tool_call("get_queue_stats", {"switch_id": switch_id})
    try:
        dpid = _dpid_to_int(switch_id)
        stats = _request("GET", f"/stats/queue/{dpid}")
        if not stats or str(dpid) not in stats:
            return f"Queue stats unavailable or none found on {switch_id}"
        return _json(stats[str(dpid)])
    except Exception as e:
        return f"Queue stats unavailable or error: {str(e)}"

@mcp.tool()
async def get_groups(switch_id: str) -> str:
    """Return group table entries (buckets, types) for one switch."""
    _log_tool_call("get_groups", {"switch_id": switch_id})
    try:
        dpid = _dpid_to_int(switch_id)
        groups = _request("GET", f"/stats/groupdesc/{dpid}")
        if not groups or str(dpid) not in groups:
            return f"No groups found on {switch_id}"
        return _json(groups[str(dpid)])
    except Exception as e:
        return f"Error fetching groups: {str(e)}"

@mcp.tool()
async def get_group_stats(switch_id: str) -> str:
    """Return group stats (packet/byte counts) for one switch."""
    _log_tool_call("get_group_stats", {"switch_id": switch_id})
    try:
        dpid = _dpid_to_int(switch_id)
        stats = _request("GET", f"/stats/group/{dpid}")
        if not stats or str(dpid) not in stats:
            return f"Group stats unavailable or none found on {switch_id}"
        return _json(stats[str(dpid)])
    except Exception as e:
        return f"Group stats unavailable or error: {str(e)}"

# -----------------------------------------------------------------------------
# Phase 1: Flow Tools
# -----------------------------------------------------------------------------

@mcp.tool()
async def install_forwarding_flow(switch_id: str, match: Dict[str, Any], out_port: int, priority: int = 100) -> str:
    """Install a basic forwarding flow (OUTPUT to port only)."""
    _log_tool_call("install_forwarding_flow", {"switch_id": switch_id, "match": match, "out_port": out_port, "priority": priority})
    
    req = ForwardingFlowRequest(switch_id=switch_id, match=FlowMatch(**match), out_port=out_port, priority=priority)
    val = await validate_sdn_request("Install forwarding flow", req)
    if not val.is_safe:
        return f"Validation Failed: {val.reason}. Suggested: {val.suggested_action}"

    dpid = _dpid_to_int(switch_id)
    flow = {
        "dpid": dpid,
        "cookie": LLM_COOKIE,
        "priority": priority,
        "match": req.match.model_dump(exclude_none=True),
        "actions": [{"type": "OUTPUT", "port": out_port}]
    }
    _request("POST", "/stats/flowentry/add", json=flow)
    return f"Flow installed on {switch_id}: {match} -> Port {out_port} (Cookie: {hex(LLM_COOKIE)})"

@mcp.tool()
async def delete_flow(switch_id: str, flow_id: Optional[int] = None, match: Optional[Dict[str, Any]] = None) -> str:
    """Delete a specific flow installed by this system."""
    _log_tool_call("delete_flow", {"switch_id": switch_id, "flow_id": flow_id, "match": match})
    
    # Check if flow has our cookie
    dpid = _dpid_to_int(switch_id)
    # Ryu delete needs match or cookie
    flow_to_del = {
        "dpid": dpid,
        "cookie": flow_id if flow_id else LLM_COOKIE,
        "cookie_mask": LLM_COOKIE_MASK if not flow_id else 0xffffffffffffffff
    }
    if match:
        flow_to_del["match"] = match
    
    _request("POST", "/stats/flowentry/delete", json=flow_to_del)
    return f"Attempted deletion of LLM flow on {switch_id}"

@mcp.tool()
async def clear_llm_installed_flows(switch_id: str) -> str:
    """Delete all flows installed by this MCP system on a switch."""
    _log_tool_call("clear_llm_installed_flows", {"switch_id": switch_id})
    dpid = _dpid_to_int(switch_id)
    flow_to_del = {
        "dpid": dpid,
        "cookie": LLM_COOKIE,
        "cookie_mask": LLM_COOKIE_MASK
    }
    _request("POST", "/stats/flowentry/delete", json=flow_to_del)
    return f"Cleared all LLM-installed flows on {switch_id}"

# -----------------------------------------------------------------------------
# Phase 2: Group Tools
# -----------------------------------------------------------------------------

@mcp.tool()
async def install_select_group(switch_id: str, group_id: int, buckets: List[Dict[str, Any]]) -> str:
    """Install a SELECT group for load balancing."""
    _log_tool_call("install_select_group", {"switch_id": switch_id, "group_id": group_id, "buckets": buckets})
    
    # LLM group IDs must be in 100000+ range
    if group_id < 100000:
        return "Error: Group ID must be >= 100000 for LLM-installed groups."

    req = SelectGroupRequest(switch_id=switch_id, group_id=group_id, buckets=[])
    # Basic bucket validation logic should be here or in validator
    for b in buckets:
        req.buckets.append(b)

    val = await validate_sdn_request("Install select group", req)
    if not val.is_safe:
        return f"Validation Failed: {val.reason}"

    dpid = _dpid_to_int(switch_id)
    group = {
        "dpid": dpid,
        "type": "SELECT",
        "group_id": group_id,
        "buckets": buckets
    }
    _request("POST", "/stats/groupentry/add", json=group)
    return f"Select Group {group_id} installed on {switch_id}"

@mcp.tool()
async def install_fast_failover_group(switch_id: str, group_id: int, buckets: List[Dict[str, Any]]) -> str:
    """Install a FAST_FAILOVER group."""
    _log_tool_call("install_fast_failover_group", {"switch_id": switch_id, "group_id": group_id, "buckets": buckets})
    
    if group_id < 100000:
        return "Error: Group ID must be >= 100000 for LLM-installed groups."

    req = FastFailoverGroupRequest(switch_id=switch_id, group_id=group_id, buckets=[])
    for b in buckets:
        req.buckets.append(b)

    val = await validate_sdn_request("Install fast failover group", req)
    if not val.is_safe:
        return f"Validation Failed: {val.reason}"

    dpid = _dpid_to_int(switch_id)
    group = {
        "dpid": dpid,
        "type": "FF",
        "group_id": group_id,
        "buckets": buckets
    }
    _request("POST", "/stats/groupentry/add", json=group)
    return f"Fast Failover Group {group_id} installed on {switch_id}"

@mcp.tool()
async def install_flow_to_group(switch_id: str, match: Dict[str, Any], group_id: int, priority: int = 100) -> str:
    """Install a flow that redirects traffic to a group."""
    _log_tool_call("install_flow_to_group", {"switch_id": switch_id, "match": match, "group_id": group_id, "priority": priority})
    
    if group_id < 100000:
        return "Rejected: Only LLM-managed groups (ID >= 100000) can be used."

    req = FlowToGroupRequest(switch_id=switch_id, match=FlowMatch(**match), group_id=group_id, priority=priority)
    val = await validate_sdn_request("Install flow to group", req)
    if not val.is_safe:
        return f"Validation Failed: {val.reason}"

    dpid = _dpid_to_int(switch_id)
    flow = {
        "dpid": dpid,
        "cookie": LLM_COOKIE,
        "priority": priority,
        "match": req.match.model_dump(exclude_none=True),
        "actions": [{"type": "GROUP", "group_id": group_id}]
    }
    _request("POST", "/stats/flowentry/add", json=flow)
    return f"Flow to Group {group_id} installed on {switch_id}"

@mcp.tool()
async def delete_group(switch_id: str, group_id: int) -> str:
    """Delete an LLM-installed group."""
    _log_tool_call("delete_group", {"switch_id": switch_id, "group_id": group_id})
    
    if group_id < 100000:
        return "Rejected: Cannot delete non-LLM groups via this tool."

    dpid = _dpid_to_int(switch_id)
    group = {
        "dpid": dpid,
        "group_id": group_id
    }
    _request("POST", "/stats/groupentry/delete", json=group)
    return f"Deleted group {group_id} on {switch_id}"

@mcp.tool()
async def clear_llm_installed_groups(switch_id: str) -> str:
    """Clear all LLM-installed groups (heuristically based on range)."""
    _log_tool_call("clear_llm_installed_groups", {"switch_id": switch_id})
    dpid = _dpid_to_int(switch_id)
    
    # We must fetch all groups first to know which ones to delete
    groups = _request("GET", f"/stats/groupdesc/{dpid}")
    if not groups or str(dpid) not in groups:
        return f"No groups found on {switch_id}"
    
    count = 0
    for g in groups[str(dpid)]:
        gid = int(g.get("group_id", 0))
        if gid >= 100000:
            _request("POST", "/stats/groupentry/delete", json={"dpid": dpid, "group_id": gid})
            count += 1
            
    return f"Cleared {count} LLM-managed groups on {switch_id}"

# -----------------------------------------------------------------------------
# Entrypoint
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    # Start the tunnel in the background
    # Note: run_tunnel.py expects MCP_HOST and MCP_PORT
    subprocess.Popen([sys.executable, "run_tunnel.py", "--host", MCP_HOST, "--port", str(MCP_PORT)])
    print("PokeTunnel enabled and starting...")
    print(f"TopologyTalk Constrained MCP starting on {MCP_HOST}:{MCP_PORT}...")
    print(f"Ryu URL: {RYU_BASE_URL}")
    print(f"LLM Cookie: {hex(LLM_COOKIE)}")
    mcp.run(
        transport="http",
        host=MCP_HOST,
        port=MCP_PORT,
        stateless_http=True,
    )
