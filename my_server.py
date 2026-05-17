#!/usr/bin/env python3
"""
TopologyTalk: MCP server that sits between an LLM client and Ryu's REST API.

Architecture (high level):
  1. Ryu runs with apps that expose HTTP endpoints (topology, stats, flow add/delete).
  2. This process runs FastMCP, which registers Python functions as "tools" the LLM can call.
  3. Each tool maps to one or more Ryu REST calls. Responses are JSON strings suitable for the model.
  4. Mutating tools (install/delete flows) run through validate_sdn_request() so changes are
     checked against safety rules before hitting the controller.

Cookie tagging:
  Flows we install are stamped with LLM_COOKIE so we can later delete "our" flows without
  wiping the whole switch table. The mask keeps only the prefix bits that identify the source.
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
    ForwardingFlowRequest, FlowMatch, 
    DeleteFlowRequest
)
from validator import validate_sdn_request

load_dotenv()

# Ryu's ofctl_rest (or similar) base URL — all paths below are appended here.
RYU_BASE_URL = os.getenv("RYU_BASE_URL", "http://localhost:8080")
# Bind the MCP HTTP transport so external clients (or a tunnel) can reach the tools.
MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))

# OpenFlow cookie + mask: first three bytes spell "llm" (0x6c6c6d); rest can vary per flow if needed.
# delete uses the mask so Ryu matches any flow whose cookie shares that prefix.
LLM_COOKIE = 0x6c6c6d0000000000
LLM_COOKIE_MASK = 0xffffff0000000000

mcp = FastMCP("TopologyTalk")

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _log_tool_call(tool_name: str, params: Any):
    """Simple trace so lab demos / debugging show which MCP tool ran and with what args."""
    print(f"[TOOL_CALL] {tool_name} with params: {json.dumps(params)}")

def _json(data: Any) -> str:
    """Pretty-print JSON for human/LLM readability in tool responses."""
    return json.dumps(data, indent=2, sort_keys=False)

def _request(method: str, path: str, **kwargs: Any) -> Any:
    """Thin synchronous wrapper around Ryu REST: one place for URL join, timeout, and errors."""
    url = f"{RYU_BASE_URL}{path}"
    response = requests.request(method, url, timeout=10, **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(f"Ryu Error: {response.status_code} - {response.text}")
    return response.json() if response.text else None

def _dpid_to_int(dpid: str | int) -> int:
    """Normalize switch id: Ryu sometimes returns DPID as int or hex string; stats URLs need int."""
    if isinstance(dpid, int): return dpid
    return int(str(dpid), 16) if str(dpid).startswith("0x") else int(str(dpid), 16 if len(str(dpid)) > 10 else 10)

def _dpid_to_16hex(dpid: str | int) -> str:
    """16-digit lowercase hex DPID for consistent display across topology summaries."""
    val = _dpid_to_int(dpid)
    return f"{val:016x}"

# -----------------------------------------------------------------------------
# Discovery & Observability Tools
# -----------------------------------------------------------------------------
# Read-only wrappers around Ryu's topology REST (/v1.0/topology/*) and OpenFlow stats (/stats/*).
# Each @mcp.tool becomes a callable capability exposed to the LLM over MCP.

@mcp.tool()
async def get_topology() -> str:
    """Return a structured summary of switches, links, and hosts for the LLM."""
    _log_tool_call("get_topology", {})
    try:
        # Three REST reads are merged into one object so the model sees the whole graph at once.
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
        # Ryu stats module returns a dict keyed by stringified DPID -> list of flow tables/entries.
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

# -----------------------------------------------------------------------------
# Flow Tools
# -----------------------------------------------------------------------------
# Write path: validate first, then POST flowentry add/delete. Cookies scope cleanup to our flows.

@mcp.tool()
async def install_forwarding_flow(switch_id: str, match: Dict[str, Any], out_port: int, priority: int = 100) -> str:
    """Install a basic forwarding flow (OUTPUT to port only)."""
    _log_tool_call("install_forwarding_flow", {"switch_id": switch_id, "match": match, "out_port": out_port, "priority": priority})
    
    # Pydantic parses/validates match fields; then LLM/rule validator decides if the intent is safe.
    req = ForwardingFlowRequest(switch_id=switch_id, match=FlowMatch(**match), out_port=out_port, priority=priority)
    val = await validate_sdn_request("Install forwarding flow", req)
    if not val.is_safe:
        return f"Validation Failed: {val.reason}. Suggested: {val.suggested_action}"

    dpid = _dpid_to_int(switch_id)
    # Payload shape follows Ryu's flow entry JSON: match + single OUTPUT action only (by design).
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
    
    dpid = _dpid_to_int(switch_id)
    # Ryu deletes by cookie (and optional match). If flow_id is omitted, we target our LLM_COOKIE
    # with LLM_COOKIE_MASK; if flow_id is set, it is passed as the exact cookie with full mask.
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
    # Same cookie+mask as install: removes every flow tagged with the LLM prefix on this DPID.
    flow_to_del = {
        "dpid": dpid,
        "cookie": LLM_COOKIE,
        "cookie_mask": LLM_COOKIE_MASK
    }
    _request("POST", "/stats/flowentry/delete", json=flow_to_del)
    return f"Cleared all LLM-installed flows on {switch_id}"

# -----------------------------------------------------------------------------
# Entrypoint
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    # Expose the local MCP HTTP port through PokeTunnel (see run_tunnel.py) so a remote Cursor
    # / cloud agent can call tools without LAN access to the lab machine.
    subprocess.Popen([sys.executable, "run_tunnel.py", "--host", MCP_HOST, "--port", str(MCP_PORT)])
    print("PokeTunnel enabled and starting...")
    print(f"TopologyTalk Constrained MCP starting on {MCP_HOST}:{MCP_PORT}...")
    print(f"Ryu URL: {RYU_BASE_URL}")
    print(f"LLM Cookie: {hex(LLM_COOKIE)}")
    # stateless_http=True: each MCP request is independent (typical for HTTP bridging).
    mcp.run(
        transport="http",
        host=MCP_HOST,
        port=MCP_PORT,
        stateless_http=True,
    )
