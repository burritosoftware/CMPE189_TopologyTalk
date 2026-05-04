#!/usr/bin/env python3
"""
TopologyTalk flow-only MCP server.

Scope:
- Manage OpenFlow rules only.
- Discover topology, hosts, ports, flows, and counters to help the caller decide.
- Install/delete raw OpenFlow entries through Ryu ofctl_rest.
- Install forwarding rules for an explicitly supplied path without assuming any
  particular topology.

Intentionally out of scope:
- QoS queues, OVSDB binding, set_queue, meters, TC/qdisc, Mininet bandwidth/delay.
- Controller reset/reconnect operations.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections import deque
from typing import Any, Iterable

import requests
from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv()

RYU_BASE_URL = os.getenv("RYU_BASE_URL", "http://localhost:8080")
MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))

# Used only for read-only local flow inspection. Comma-separated, e.g. s1,s2,s3,s4.
OVS_SWITCHES = [s.strip() for s in os.getenv("OVS_SWITCHES", "s1,s2,s3,s4").split(",") if s.strip()]

mcp = FastMCP("TopologyTalk")


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _json(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=False)


def _request(method: str, path: str, **kwargs: Any) -> Any:
    """HTTP helper that preserves useful Ryu error bodies."""
    timeout = kwargs.pop("timeout", 8)
    url = f"{RYU_BASE_URL}{path}"
    response = requests.request(method, url, timeout=timeout, **kwargs)

    if response.status_code >= 400:
        raise RuntimeError(
            f"{method} {url} failed: HTTP {response.status_code}\n"
            f"Response body: {response.text}"
        )

    if not response.text:
        return None
    try:
        return response.json()
    except Exception:
        return response.text


def ryu_get(path: str) -> Any:
    return _request("GET", path)


def ryu_post(path: str, data: Any) -> Any:
    return _request("POST", path, json=data)


def ryu_delete(path: str, data: Any | None = None) -> Any:
    if data is None:
        return _request("DELETE", path)
    return _request("DELETE", path, json=data)


def _run_cmd(args: list[str], timeout: int = 10) -> dict[str, Any]:
    """Run a narrow read-only local OVS command and return structured output."""
    try:
        completed = subprocess.run(
            args,
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        return {
            "command": args,
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
        }
    except Exception as exc:
        return {"command": args, "error": str(exc)}


def _dpid_to_int(dpid: str | int) -> int:
    """Normalize DPID for Ryu ofctl_rest flowentry endpoints."""
    if isinstance(dpid, int):
        return dpid
    value = str(dpid).strip()
    if not value:
        raise ValueError("DPID cannot be empty")
    if value.startswith("0x"):
        return int(value, 16)
    # Ryu topology APIs return 16-char hex strings, e.g. 0000000000000001.
    if len(value) == 16 and all(c in "0123456789abcdefABCDEF" for c in value):
        return int(value, 16)
    return int(value, 10)


def _dpid_to_16hex(dpid: str | int) -> str:
    return f"{_dpid_to_int(dpid):016x}"


def _extract_port_deltas(before: Any, after: Any) -> Any:
    """Best-effort delta for Ryu /stats/port responses."""
    if not isinstance(before, dict) or not isinstance(after, dict):
        return {"before": before, "after": after, "note": "Unexpected response shape; could not compute delta."}

    deltas: dict[str, list[dict[str, Any]]] = {}
    for dpid, after_ports in after.items():
        before_ports = before.get(dpid, [])
        before_by_port = {str(p.get("port_no")): p for p in before_ports if isinstance(p, dict)}
        rows: list[dict[str, Any]] = []
        for current in after_ports:
            if not isinstance(current, dict):
                continue
            port_no = str(current.get("port_no"))
            old = before_by_port.get(port_no, {})
            row: dict[str, Any] = {"port_no": port_no}
            for key in [
                "rx_packets",
                "tx_packets",
                "rx_bytes",
                "tx_bytes",
                "rx_dropped",
                "tx_dropped",
                "rx_errors",
                "tx_errors",
            ]:
                try:
                    row[f"delta_{key}"] = int(current.get(key, 0)) - int(old.get(key, 0))
                except Exception:
                    pass
            rows.append(row)
        deltas[dpid] = rows
    return deltas


def _actions_iter(actions: Any) -> Iterable[Any]:
    if actions is None:
        return []
    if isinstance(actions, list):
        return actions
    return [actions]


def _assert_flow_only_actions(flow: dict[str, Any]) -> None:
    """Reject QoS/shaping actions. This server manages forwarding flows only."""
    blocked = {"SET_QUEUE", "ENQUEUE", "METER"}
    actions = list(_actions_iter(flow.get("actions")))

    for action in actions:
        if isinstance(action, dict):
            action_type = str(action.get("type", "")).upper()
            if action_type in blocked:
                raise ValueError(f"QoS action {action_type!r} is not allowed in the flow-only server")
            # Some callers use lowercase/alternate keys.
            serialized = json.dumps(action).upper()
            for marker in blocked:
                if marker in serialized:
                    raise ValueError(f"QoS action {marker!r} is not allowed in the flow-only server")
        elif isinstance(action, str):
            upper = action.upper()
            for marker in blocked:
                if marker in upper:
                    raise ValueError(f"QoS action {marker!r} is not allowed in the flow-only server")


def _normalize_flow(flow: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(flow, dict):
        raise ValueError("Flow must be a JSON object")
    if "dpid" not in flow:
        raise ValueError("Flow must include dpid")

    normalized = dict(flow)
    normalized["dpid"] = _dpid_to_int(normalized["dpid"])

    if "table_id" in normalized:
        normalized["table_id"] = int(normalized["table_id"])
    if "priority" in normalized:
        normalized["priority"] = int(normalized["priority"])
    if "idle_timeout" in normalized:
        normalized["idle_timeout"] = int(normalized["idle_timeout"])
    if "hard_timeout" in normalized:
        normalized["hard_timeout"] = int(normalized["hard_timeout"])

    _assert_flow_only_actions(normalized)
    return normalized


def _get_topology_index() -> dict[str, Any]:
    """Build lookup maps from Ryu topology discovery."""
    switches = ryu_get("/v1.0/topology/switches")
    links = ryu_get("/v1.0/topology/links")

    port_names: dict[str, dict[int, str]] = {}
    port_numbers: dict[str, dict[str, int]] = {}
    for sw in switches:
        dpid = _dpid_to_16hex(sw.get("dpid"))
        port_names[dpid] = {}
        port_numbers[dpid] = {}
        for port in sw.get("ports", []):
            port_no = int(port.get("port_no"))
            name = str(port.get("name"))
            port_names[dpid][port_no] = name
            port_numbers[dpid][name] = port_no

    # directed_links[(src_dpid, dst_dpid)] = src_port_no
    directed_links: dict[tuple[str, str], int] = {}
    adjacency: dict[str, set[str]] = {}
    link_rows = []
    for link in links:
        src = link.get("src", {})
        dst = link.get("dst", {})
        src_dpid = _dpid_to_16hex(src.get("dpid"))
        dst_dpid = _dpid_to_16hex(dst.get("dpid"))
        src_port_no = int(src.get("port_no"))
        dst_port_no = int(dst.get("port_no"))
        directed_links[(src_dpid, dst_dpid)] = src_port_no
        adjacency.setdefault(src_dpid, set()).add(dst_dpid)
        link_rows.append({
            "src_dpid": src_dpid,
            "src_port_no": src_port_no,
            "src_port_name": port_names.get(src_dpid, {}).get(src_port_no),
            "dst_dpid": dst_dpid,
            "dst_port_no": dst_port_no,
            "dst_port_name": port_names.get(dst_dpid, {}).get(dst_port_no),
        })

    return {
        "switches": switches,
        "links": links,
        "link_rows": link_rows,
        "port_names": port_names,
        "port_numbers": port_numbers,
        "directed_links": directed_links,
        "adjacency": adjacency,
    }


def _port_to_number(index: dict[str, Any], dpid: str | int, port: str | int) -> int:
    dpid_hex = _dpid_to_16hex(dpid)
    if isinstance(port, int):
        return port
    value = str(port).strip()
    if value.isdigit():
        return int(value)
    port_numbers = index["port_numbers"].get(dpid_hex, {})
    if value not in port_numbers:
        raise ValueError(f"Port {value!r} was not found on switch {dpid_hex}")
    return int(port_numbers[value])


def _build_output_flow(
    dpid: str,
    in_port: int,
    out_port: int,
    match: dict[str, Any] | None,
    table_id: int,
    priority: int,
    idle_timeout: int,
    hard_timeout: int,
) -> dict[str, Any]:
    flow_match = dict(match or {})
    # Caller-specified in_port would make path installation ambiguous; path ports win.
    flow_match["in_port"] = in_port

    return {
        "dpid": _dpid_to_int(dpid),
        "table_id": table_id,
        "priority": priority,
        "idle_timeout": idle_timeout,
        "hard_timeout": hard_timeout,
        "match": flow_match,
        "actions": [
            {"type": "OUTPUT", "port": out_port}
        ],
    }


def _reverse_match(match: dict[str, Any] | None) -> dict[str, Any]:
    """Swap common src/dst match fields for reverse-direction rules."""
    original = dict(match or {})
    pairs = [
        ("eth_src", "eth_dst"),
        ("dl_src", "dl_dst"),
        ("ipv4_src", "ipv4_dst"),
        ("nw_src", "nw_dst"),
        ("tcp_src", "tcp_dst"),
        ("udp_src", "udp_dst"),
        ("sctp_src", "sctp_dst"),
        ("tp_src", "tp_dst"),
    ]
    for left, right in pairs:
        left_present = left in original
        right_present = right in original
        if left_present or right_present:
            original[left], original[right] = original.get(right), original.get(left)
            if original.get(left) is None:
                original.pop(left, None)
            if original.get(right) is None:
                original.pop(right, None)
    return original


def _find_shortest_switch_path(src_dpid: str, dst_dpid: str, index: dict[str, Any]) -> list[str]:
    """Shortest path over discovered switch graph. Used only when caller requests auto_path."""
    src = _dpid_to_16hex(src_dpid)
    dst = _dpid_to_16hex(dst_dpid)
    adjacency: dict[str, set[str]] = index["adjacency"]

    queue: deque[tuple[str, list[str]]] = deque([(src, [src])])
    visited = {src}
    while queue:
        node, path = queue.popleft()
        if node == dst:
            return path
        for neighbor in sorted(adjacency.get(node, set())):
            if neighbor in visited:
                continue
            visited.add(neighbor)
            queue.append((neighbor, path + [neighbor]))
    raise ValueError(f"No discovered switch path from {src} to {dst}")


# -----------------------------------------------------------------------------
# 0. Server/config sanity
# -----------------------------------------------------------------------------

@mcp.tool(description="Get MCP server and controller configuration. Use this first for sanity checks.")
def get_server_info() -> str:
    return _json({
        "server_name": "TopologyTalkFlowOnly",
        "version": "3.0.0-flow-only-general",
        "environment": os.environ.get("ENVIRONMENT", "development"),
        "python_version": os.sys.version.split()[0],
        "ryu_base_url": RYU_BASE_URL,
        "ovs_switches_for_local_dump_only": OVS_SWITCHES,
        "scope": [
            "OpenFlow rule installation/deletion",
            "Ryu topology/host/port/flow/stat inspection",
            "General explicit path forwarding installation",
        ],
        "explicitly_removed": [
            "QoS queues",
            "OVSDB binding",
            "set_queue actions",
            "meters",
            "TC/qdisc tools",
            "topology-specific helpers",
            "controller reset/reconnect tools",
        ],
    })


# -----------------------------------------------------------------------------
# 1. Topology and host/port discovery
# -----------------------------------------------------------------------------

@mcp.tool()
def get_network_topology() -> str:
    """
    Fetch Ryu-discovered switches, switch ports, and switch-to-switch links.

    Host links may require get_network_hosts().
    """
    try:
        index = _get_topology_index()
        switch_rows = []
        for sw in index["switches"]:
            switch_rows.append({
                "dpid": _dpid_to_16hex(sw.get("dpid")),
                "ports": sw.get("ports", []),
            })
        return _json({
            "switch_count": len(switch_rows),
            "link_count": len(index["link_rows"]),
            "switches": switch_rows,
            "links": index["link_rows"],
            "reasoning_notes": [
                "Use links to determine the output port from one switch to the next.",
                "Use get_network_hosts() to identify host attachment switch/port.",
                "This endpoint does not expose Mininet bw/delay.",
            ],
        })
    except Exception as exc:
        return f"Error fetching topology: {exc}"


@mcp.tool()
def get_network_hosts() -> str:
    """
    Fetch host attachment data from Ryu topology discovery.

    If hosts are missing, generate traffic first, such as ping, so Ryu can learn.
    """
    try:
        return _json(ryu_get("/v1.0/topology/hosts"))
    except Exception as exc:
        return f"Error fetching hosts: {exc}"


@mcp.tool()
def get_port_descriptions(switch_id: str = "all") -> str:
    """Get OpenFlow port descriptions from Ryu ofctl_rest."""
    try:
        return _json(ryu_get(f"/stats/portdesc/{switch_id}"))
    except Exception as exc:
        return f"Error fetching port descriptions: {exc}"


# -----------------------------------------------------------------------------
# 2. Flow and counter observability
# -----------------------------------------------------------------------------

@mcp.tool()
def get_flow_stats(switch_id: str = "all") -> str:
    """Fetch OpenFlow flow entries from Ryu ofctl_rest."""
    try:
        return _json(ryu_get(f"/stats/flow/{switch_id}"))
    except Exception as exc:
        return f"Error fetching flow stats: {exc}"


@mcp.tool()
def get_port_stats(switch_id: str = "all") -> str:
    """Fetch OpenFlow port counters."""
    try:
        return _json(ryu_get(f"/stats/port/{switch_id}"))
    except Exception as exc:
        return f"Error fetching port stats: {exc}"


@mcp.tool()
def get_port_stats_delta(interval_seconds: float = 2.0, switch_id: str = "all") -> str:
    """Measure port counter changes over a short interval."""
    try:
        interval = max(0.25, min(float(interval_seconds), 10.0))
        before = ryu_get(f"/stats/port/{switch_id}")
        time.sleep(interval)
        after = ryu_get(f"/stats/port/{switch_id}")
        return _json({
            "interval_seconds": interval,
            "deltas": _extract_port_deltas(before, after),
            "reasoning_notes": [
                "Use deltas, not cumulative counters, to infer active path.",
                "Map port_no to names using get_port_descriptions() or get_network_topology().",
            ],
        })
    except Exception as exc:
        return f"Error fetching port stat delta: {exc}"


@mcp.tool()
def dump_local_flows(switch_name: str = "all", table: str | None = None) -> str:
    """
    Dump OVS flows directly with ovs-ofctl.

    Read-only local inspection. This does not add, delete, or reset anything.
    """
    switches = OVS_SWITCHES if switch_name == "all" else [switch_name]
    results = {}
    for sw in switches:
        args = ["sudo", "ovs-ofctl", "-O", "OpenFlow13", "dump-flows", sw]
        if table is not None:
            args.append(f"table={table}")
        results[sw] = _run_cmd(args)
    return _json(results)


# -----------------------------------------------------------------------------
# 3. Raw OpenFlow management
# -----------------------------------------------------------------------------

@mcp.tool()
def add_openflow_entry(flow: dict[str, Any]) -> str:
    """
    Add one OpenFlow entry using Ryu ofctl_rest /stats/flowentry/add.

    This server rejects QoS/shaping actions such as SET_QUEUE, ENQUEUE, and METER.
    Please stop attempting to make queues and tag packets.
    """
    try:
        normalized = _normalize_flow(flow)
        return _json(ryu_post("/stats/flowentry/add", normalized))
    except Exception as exc:
        return f"Error adding OpenFlow entry: {exc}"


@mcp.tool()
def add_openflow_entries(flows: list[dict[str, Any]]) -> str:
    """Add multiple OpenFlow entries. Each entry is validated independently."""
    results = []
    for flow in flows:
        try:
            normalized = _normalize_flow(flow)
            result = ryu_post("/stats/flowentry/add", normalized)
            results.append({"status": "ok", "flow": normalized, "result": result})
        except Exception as exc:
            results.append({"status": "error", "flow": flow, "error": str(exc)})
    return _json(results)


@mcp.tool()
def delete_openflow_entry(flow: dict[str, Any]) -> str:
    """Delete one OpenFlow entry using Ryu ofctl_rest /stats/flowentry/delete."""
    try:
        normalized = _normalize_flow(flow)
        return _json(ryu_post("/stats/flowentry/delete", normalized))
    except Exception as exc:
        return f"Error deleting OpenFlow entry: {exc}"


@mcp.tool()
def delete_openflow_entries(flows: list[dict[str, Any]]) -> str:
    """Delete multiple OpenFlow entries. Each entry is validated independently."""
    results = []
    for flow in flows:
        try:
            normalized = _normalize_flow(flow)
            result = ryu_post("/stats/flowentry/delete", normalized)
            results.append({"status": "ok", "flow": normalized, "result": result})
        except Exception as exc:
            results.append({"status": "error", "flow": flow, "error": str(exc)})
    return _json(results)


@mcp.tool()
def clear_flows_on_switch(switch_id: str | int) -> str:
    """
    Clear all OpenFlow entries on one switch through Ryu ofctl_rest.

    This is flow-only cleanup. It does not reset controllers, OVSDB, QoS, queues,
    or local OVS configuration.
    """
    try:
        dpid = _dpid_to_int(switch_id)
        return _json(ryu_delete(f"/stats/flowentry/clear/{dpid}"))
    except Exception as exc:
        return f"Error clearing flows on switch: {exc}"


# -----------------------------------------------------------------------------
# 4. General path-to-flow installer
# -----------------------------------------------------------------------------

@mcp.tool()
def plan_path_forwarding_flows(request: dict[str, Any]) -> str:
    """
    Build, but do not install, forwarding flows for a caller-specified path.

    Required request fields:
    - path: ordered list of switch DPIDs, e.g. ["0000000000000001", "0000000000000002"]
    - src_port: ingress port on first switch, as a port number or port name
    - dst_port: egress port on last switch, as a port number or port name

    Optional fields:
    - match: extra match fields applied in the forward direction
    - reverse_match: explicit reverse-direction match. If omitted, common src/dst
      fields are swapped automatically from match.
    - bidirectional: default true
    - table_id: default 1
    - priority: default 100
    - idle_timeout: default 0
    - hard_timeout: default 0

    This is general: switch-to-switch output ports are looked up from the current
    Ryu-discovered topology. No topology shape is assumed.
    """
    try:
        index = _get_topology_index()
        path = request.get("path")
        if not isinstance(path, list) or len(path) < 1:
            raise ValueError("request.path must be a non-empty list of switch DPIDs")

        path_hex = [_dpid_to_16hex(dpid) for dpid in path]
        src_port = _port_to_number(index, path_hex[0], request["src_port"])
        dst_port = _port_to_number(index, path_hex[-1], request["dst_port"])
        match = request.get("match") or {}
        reverse_match = request.get("reverse_match")
        bidirectional = bool(request.get("bidirectional", True))
        table_id = int(request.get("table_id", 1))
        priority = int(request.get("priority", 100))
        idle_timeout = int(request.get("idle_timeout", 0))
        hard_timeout = int(request.get("hard_timeout", 0))

        directed_links: dict[tuple[str, str], int] = index["directed_links"]
        flows: list[dict[str, Any]] = []

        # Forward direction: src host/port -> ... -> dst host/port.
        for i, dpid in enumerate(path_hex):
            if i == 0:
                in_port = src_port
            else:
                previous_dpid = path_hex[i - 1]
                in_port = directed_links.get((dpid, previous_dpid))
                if in_port is None:
                    raise ValueError(f"No discovered link from {previous_dpid} into {dpid}")

            if i == len(path_hex) - 1:
                out_port = dst_port
            else:
                next_dpid = path_hex[i + 1]
                out_port = directed_links.get((dpid, next_dpid))
                if out_port is None:
                    raise ValueError(f"No discovered link from {dpid} to {next_dpid}")

            flows.append(_build_output_flow(dpid, in_port, out_port, match, table_id, priority, idle_timeout, hard_timeout))

        if bidirectional:
            rev_path = list(reversed(path_hex))
            rev_match = reverse_match if isinstance(reverse_match, dict) else _reverse_match(match)
            for i, dpid in enumerate(rev_path):
                if i == 0:
                    in_port = dst_port
                else:
                    previous_dpid = rev_path[i - 1]
                    in_port = directed_links.get((dpid, previous_dpid))
                    if in_port is None:
                        raise ValueError(f"No discovered link from {previous_dpid} into {dpid}")

                if i == len(rev_path) - 1:
                    out_port = src_port
                else:
                    next_dpid = rev_path[i + 1]
                    out_port = directed_links.get((dpid, next_dpid))
                    if out_port is None:
                        raise ValueError(f"No discovered link from {dpid} to {next_dpid}")

                flows.append(_build_output_flow(dpid, in_port, out_port, rev_match, table_id, priority, idle_timeout, hard_timeout))

        # Validate actions before returning the plan.
        normalized = [_normalize_flow(flow) for flow in flows]
        return _json({
            "status": "planned",
            "flow_count": len(normalized),
            "flows": normalized,
            "notes": [
                "No flows were installed by this planning call.",
                "Call install_path_forwarding_flows with the same request to install them.",
                "The caller supplied the path; this server did not assume topology shape.",
            ],
        })
    except Exception as exc:
        return f"Error planning path forwarding flows: {exc}"


@mcp.tool()
def install_path_forwarding_flows(request: dict[str, Any]) -> str:
    """
    Install forwarding flows for a caller-specified path.

    Same request schema as plan_path_forwarding_flows(). This is topology-agnostic:
    it discovers link ports from Ryu topology and installs only OUTPUT actions.
    """
    try:
        planned_raw = plan_path_forwarding_flows(request)
        if planned_raw.startswith("Error"):
            return planned_raw
        planned = json.loads(planned_raw)
        flows = planned.get("flows", [])
        results = []
        for flow in flows:
            try:
                result = ryu_post("/stats/flowentry/add", flow)
                results.append({"status": "ok", "flow": flow, "result": result})
            except Exception as exc:
                results.append({"status": "error", "flow": flow, "error": str(exc)})
        return _json({
            "status": "completed",
            "requested_path": request.get("path"),
            "installed_count": sum(1 for r in results if r.get("status") == "ok"),
            "error_count": sum(1 for r in results if r.get("status") == "error"),
            "results": results,
        })
    except Exception as exc:
        return f"Error installing path forwarding flows: {exc}"


@mcp.tool()
def plan_shortest_path_forwarding_flows(request: dict[str, Any]) -> str:
    """
    Build, but do not install, forwarding flows over the discovered shortest switch path.

    This is optional convenience when the caller explicitly asks for auto_path.
    It still does not know or use Mininet bw/delay. It only uses graph hop count.

    Required fields:
    - src_switch: source attachment switch DPID
    - src_port: source attachment port number/name on src_switch
    - dst_switch: destination attachment switch DPID
    - dst_port: destination attachment port number/name on dst_switch

    Optional fields are the same as plan_path_forwarding_flows().
    """
    try:
        index = _get_topology_index()
        src_switch = _dpid_to_16hex(request["src_switch"])
        dst_switch = _dpid_to_16hex(request["dst_switch"])
        path = _find_shortest_switch_path(src_switch, dst_switch, index)
        path_request = dict(request)
        path_request.pop("src_switch", None)
        path_request.pop("dst_switch", None)
        path_request["path"] = path
        planned_raw = plan_path_forwarding_flows(path_request)
        if planned_raw.startswith("Error"):
            return planned_raw
        planned = json.loads(planned_raw)
        planned["chosen_path"] = path
        planned["path_selection"] = "shortest_discovered_switch_path_by_hop_count"
        return _json(planned)
    except Exception as exc:
        return f"Error planning shortest path forwarding flows: {exc}"


# -----------------------------------------------------------------------------
# Entrypoint
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    if os.getenv("POKE_TUNNEL_ENABLED") == "true":
        subprocess.Popen([sys.executable, "run_tunnel.py", "--host", MCP_HOST, "--port", str(MCP_PORT)])
        print("PokeTunnel enabled and starting...")
    else:
        print("PokeTunnel disabled. Set POKE_TUNNEL_ENABLED=true to enable.")

    print("TopologyTalk starting...")
    print(f"Ryu base URL: {RYU_BASE_URL}")
    print("Scope: flow rules only; QoS/OVSDB/reset tools are intentionally absent.")
    mcp.run(
        transport="http",
        host=MCP_HOST,
        port=MCP_PORT,
        stateless_http=True,
    )
