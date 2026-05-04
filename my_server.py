#!/usr/bin/env python3

import json
import os
import subprocess
import sys
import time
from typing import Any

import requests
from dotenv import load_dotenv
from fastmcp import FastMCP

from models import AddQoSRuleRequest, DeleteQoSRuleRequest, SetQueueRequest
from validator import validate_qos_action

load_dotenv()

RYU_BASE_URL = os.getenv("RYU_BASE_URL", "http://localhost:8080")
MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))

# OVS listens with ptcp:6640, but Ryu must connect with tcp:HOST:6640.
# Keep this out of the model's control; use env/config.
OVSDB_ADDR = os.getenv("OVSDB_ADDR", "tcp:127.0.0.1:6640")

# Used only for local OVS recovery/debug tools. Comma-separated, e.g. s1,s2,s3,s4.
OVS_SWITCHES = [s.strip() for s in os.getenv("OVS_SWITCHES", "s1,s2,s3,s4").split(",") if s.strip()]
OF_CONTROLLER_ADDR = os.getenv("OF_CONTROLLER_ADDR", "tcp:127.0.0.1:6633")

mcp = FastMCP("TopologyTalk")


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _json(data: Any) -> str:
    return json.dumps(data, indent=2, sort_keys=False)


def _request(method: str, path: str, **kwargs: Any) -> Any:
    timeout = kwargs.pop("timeout", 8)
    response = requests.request(method, f"{RYU_BASE_URL}{path}", timeout=timeout, **kwargs)
    response.raise_for_status()
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


def ryu_put(path: str, payload: Any) -> Any:
    # If payload is a Python string, requests sends a raw JSON string body.
    # That is exactly what /ovsdb_addr expects: "tcp:127.0.0.1:6640".
    return _request("PUT", path, json=payload)


def ryu_delete(path: str, data: Any | None = None) -> Any:
    if data is None:
        return _request("DELETE", path)
    return _request("DELETE", path, json=data)

# Dude this LLM is so stupid with this OVSDB parameter input I have to NORMALIZE it man???? Hello???
def normalize_ovsdb_addr(value: str) -> str:
    """
    Normalize the Ryu-side OVSDB address.

    OVS is configured to listen with ptcp:6640. Ryu must connect with
    tcp:127.0.0.1:6640. The LLM should not choose this value.
    """
    addr = value.strip()
    if addr.startswith("ptcp:"):
        return "tcp:127.0.0.1:" + addr.split(":", 1)[1]

    if not addr.startswith(("tcp:", "unix:")):
        raise ValueError(
            f"Invalid OVSDB address {addr!r}. Use tcp:127.0.0.1:6640 "
            "or unix:/var/run/openvswitch/db.sock. Do not use ptcp here."
        )

    return addr


def _run_cmd(args: list[str], timeout: int = 10) -> dict[str, Any]:
    """Run a narrow local OVS/OVS-ofctl command and return structured output."""
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
            row = {"port_no": port_no}
            for key in ["rx_packets", "tx_packets", "rx_bytes", "tx_bytes", "rx_dropped", "tx_dropped", "rx_errors", "tx_errors"]:
                try:
                    row[f"delta_{key}"] = int(current.get(key, 0)) - int(old.get(key, 0))
                except Exception:
                    pass
            rows.append(row)
        deltas[dpid] = rows
    return deltas


# -----------------------------------------------------------------------------
# 0. Server/config sanity
# -----------------------------------------------------------------------------

@mcp.tool(description="Get MCP server and controller configuration. Use this first for sanity checks.")
def get_server_info() -> str:
    """
    Returns MCP/Ryu configuration.

    Reasoning hint for the LLM:
    - RYU_BASE_URL is the Ryu REST API address.
    - OVSDB_ADDR is the Ryu-side address used for OVSDB binding.
    - If OVSDB_ADDR is ptcp:6640, it will be normalized before use because
      Ryu connects with tcp:127.0.0.1:6640 while OVS listens with ptcp:6640.
    """
    return _json({
        "server_name": "TopologyTalk",
        "version": "2.0.0-refactored",
        "environment": os.environ.get("ENVIRONMENT", "development"),
        "python_version": os.sys.version.split()[0],
        "ryu_base_url": RYU_BASE_URL,
        "ovsdb_addr_env": OVSDB_ADDR,
        "ovsdb_addr_normalized": normalize_ovsdb_addr(OVSDB_ADDR),
        "openflow_controller_addr": OF_CONTROLLER_ADDR,
        "ovs_switches": OVS_SWITCHES,
    })


# -----------------------------------------------------------------------------
# 1. Topology and host discovery
# -----------------------------------------------------------------------------

@mcp.tool()
def get_network_topology() -> str:
    """
    Fetch the current Ryu-discovered topology: switches, switch ports, and
    switch-to-switch links.

    Reasoning hint for the LLM:
    - This tells you the graph shape and port numbers/names.
    - It does NOT tell you Mininet link bandwidth or delay.
    - Use get_port_stats_delta() and get_flow_stats() to infer active paths.
    - Use TC/OVS inspection outside Ryu if link capacity/delay is needed.
    """
    try:
        switches = ryu_get("/v1.0/topology/switches")
        links = ryu_get("/v1.0/topology/links")

        port_names: dict[str, dict[str, str]] = {}
        switch_rows = []
        for sw in switches:
            dpid = sw.get("dpid")
            ports = sw.get("ports", [])
            port_names[dpid] = {str(p.get("port_no")): p.get("name") for p in ports}
            switch_rows.append({
                "dpid": dpid,
                "ports": ports,
            })

        link_rows = []
        for link in links:
            src = link.get("src", {})
            dst = link.get("dst", {})
            src_dpid = src.get("dpid")
            dst_dpid = dst.get("dpid")
            src_port = str(src.get("port_no"))
            dst_port = str(dst.get("port_no"))
            link_rows.append({
                "src_dpid": src_dpid,
                "src_port_no": src_port,
                "src_port_name": port_names.get(src_dpid, {}).get(src_port),
                "dst_dpid": dst_dpid,
                "dst_port_no": dst_port,
                "dst_port_name": port_names.get(dst_dpid, {}).get(dst_port),
            })

        return _json({
            "switch_count": len(switches),
            "link_count": len(links),
            "switches": switch_rows,
            "links": link_rows,
            "reasoning_notes": [
                "Topology links are switch-to-switch links discovered by LLDP.",
                "Host links may require get_network_hosts().",
                "Ryu topology does not expose Mininet bw/delay; infer via stats/tests or TC inspection.",
            ],
        })
    except Exception as exc:
        return f"Error fetching topology: {exc}"


@mcp.tool()
def get_network_hosts() -> str:
    """
    Fetch host attachment data from Ryu topology discovery.

    Reasoning hint for the LLM:
    - Use this to locate source/destination hosts before changing flows or QoS.
    - If hosts are missing, trigger traffic first with ping so Ryu can learn them.
    """
    try:
        return _json(ryu_get("/v1.0/topology/hosts"))
    except Exception as exc:
        return f"Error fetching hosts: {exc}"


@mcp.tool()
def get_port_descriptions(switch_id: str = "all") -> str:
    """
    Get OpenFlow port descriptions from ofctl_rest.

    Reasoning hint for the LLM:
    - Flow stats often use numeric output ports. Use this to map numbers to
      interface names such as s1-eth2 or s4-eth1.
    """
    try:
        return _json(ryu_get(f"/stats/portdesc/{switch_id}"))
    except Exception as exc:
        return f"Error fetching port descriptions: {exc}"


# -----------------------------------------------------------------------------
# 2. Traffic and path observability
# -----------------------------------------------------------------------------

@mcp.tool()
def get_flow_stats(switch_id: str = "all") -> str:
    """
    Fetch OpenFlow flow entries.

    Reasoning hint for the LLM:
    - Use this to determine the currently installed forwarding path.
    - For qos_simple_switch_13.py, table 0 is QoS/classification and table 1 is
      forwarding/learning.
    - A set_queue action is a cap/classification decision, not proof of optimization.
    """
    try:
        return _json(ryu_get(f"/stats/flow/{switch_id}"))
    except Exception as exc:
        return f"Error fetching flow stats: {exc}"


@mcp.tool()
def get_port_stats(switch_id: str = "all") -> str:
    """
    Fetch OpenFlow port counters.

    Reasoning hint for the LLM:
    - One snapshot shows cumulative counters.
    - To infer active path, prefer get_port_stats_delta() while traffic is running.
    """
    try:
        return _json(ryu_get(f"/stats/port/{switch_id}"))
    except Exception as exc:
        return f"Error fetching port stats: {exc}"


@mcp.tool()
def get_port_stats_delta(interval_seconds: float = 2.0, switch_id: str = "all") -> str:
    """
    Measure port counter changes over a short interval.

    Reasoning hint for the LLM:
    - Use this during ping/iperf to infer which links are carrying the flow.
    - Rising tx/rx bytes on a port indicate active traffic on that port.
    - This is general and avoids hard-coding topology-specific slow/fast paths.
    """
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


# -----------------------------------------------------------------------------
# 3. Local OVS/queue inspection
# -----------------------------------------------------------------------------

@mcp.tool()
def get_ovs_qos_config(port_name: str | None = None) -> str:
    """
    Inspect OVS QoS and Queue rows, optionally including one port.

    Reasoning hint for the LLM:
    - Use this before assigning traffic to set_queue:N.
    - Queue IDs are arbitrary. Queue 0 is not automatically high priority.
    - If queue 0 max-rate is 2 Mbps, set_queue:0 caps matching traffic near 2 Mbps.
    """
    results = {
        "qos_rows": _run_cmd(["sudo", "ovs-vsctl", "list", "qos"]),
        "queue_rows": _run_cmd(["sudo", "ovs-vsctl", "list", "queue"]),
    }
    if port_name:
        results["port"] = _run_cmd(["sudo", "ovs-vsctl", "list", "port", port_name])
    return _json(results)


@mcp.tool()
def get_ovs_queue_stats(switch_name: str = "s1") -> str:
    """
    Run ovs-ofctl queue-stats for one OVS bridge/switch.

    Reasoning hint for the LLM:
    - If a queue's packets/bytes/errors are increasing, matching traffic is using it.
    - Queue errors/drops can explain UDP loss after QoS is installed.
    """
    return _json(_run_cmd(["sudo", "ovs-ofctl", "-O", "OpenFlow13", "queue-stats", switch_name]))


@mcp.tool()
def get_tc_qdisc(interface_name: str) -> str:
    """
    Inspect Linux TC qdisc/class state for an interface.

    Reasoning hint for the LLM:
    - Ryu topology does not expose Mininet bw/delay.
    - Mininet link shaping is represented in Linux TC. Use this when available to
      discover rate/latency constraints without hard-coded topology metadata.
    """
    return _json({
        "qdisc": _run_cmd(["tc", "qdisc", "show", "dev", interface_name]),
        "qdisc_stats": _run_cmd(["tc", "-s", "qdisc", "show", "dev", interface_name]),
        "class": _run_cmd(["tc", "class", "show", "dev", interface_name]),
        "class_stats": _run_cmd(["tc", "-s", "class", "show", "dev", interface_name]),
    })


# -----------------------------------------------------------------------------
# 4. OVSDB binding
# -----------------------------------------------------------------------------

@mcp.tool()
def bind_ovsdb_bridges() -> str:
    """
    Bind every Ryu-discovered switch DPID to the configured OVSDB address.

    Reasoning hint for the LLM:
    - Must be done before creating OVS QoS queues via rest_qos.py.
    - The model should not provide the OVSDB address; it comes from OVSDB_ADDR.
    - OVS listens with ptcp:6640, but Ryu connects with tcp:127.0.0.1:6640.
    """
    try:
        ovsdb_addr = normalize_ovsdb_addr(OVSDB_ADDR)
        switches = ryu_get("/v1.0/topology/switches")
        results = []

        for switch in switches:
            dpid = switch["dpid"]
            path = f"/v1.0/conf/switches/{dpid}/ovsdb_addr"
            bind_result = ryu_put(path, ovsdb_addr)
            verify_result = ryu_get(path)
            results.append({
                "dpid": dpid,
                "ovsdb_addr_sent": ovsdb_addr,
                "bind_result": bind_result,
                "verified_ovsdb_addr": verify_result,
            })

        return _json({"ovsdb_addr": ovsdb_addr, "bound_count": len(results), "results": results})
    except Exception as exc:
        return f"Error binding OVSDB bridges: {exc}"


# -----------------------------------------------------------------------------
# 5. QoS inspection and actions
# -----------------------------------------------------------------------------

@mcp.tool()
def get_qos_queues(switch_id: str = "all") -> str:
    """
    Get Ryu QoS queue configuration for a switch or all switches.

    Reasoning hint for the LLM:
    - Call this before add_qos_rule().
    - Determine queue ID -> max_rate mapping before using set_queue:N.
    - If throughput is near a queue's max_rate, QoS may be the bottleneck.
    """
    try:
        return _json(ryu_get(f"/qos/queue/{switch_id}"))
    except Exception as exc:
        return f"Error fetching QoS queues: {exc}"


@mcp.tool()
def get_qos_rules(switch_id: str = "all", vlan_id: str = "all") -> str:
    """
    Get Ryu QoS classification rules.

    Reasoning hint for the LLM:
    - Rules with set_queue cap/classify matching traffic.
    - A QoS rule can be the cause of low throughput if it sends traffic to a low-rate queue.
    """
    try:
        path = f"/qos/rules/{switch_id}" if vlan_id == "all" else f"/qos/rules/{switch_id}/{vlan_id}"
        return _json(ryu_get(path))
    except Exception as exc:
        return f"Error fetching QoS rules: {exc}"


@mcp.tool()
def get_qos_status(switch_id: str = "all") -> str:
    """Get Ryu QoS queue status for a switch or all switches."""
    try:
        return _json(ryu_get(f"/qos/queue/status/{switch_id}"))
    except Exception as exc:
        return f"Error fetching QoS status: {exc}"


@mcp.tool()
def set_qos_queues(request: SetQueueRequest) -> str:
    """
    Configure QoS queues on a specific switch/port.

    Reasoning hint for the LLM:
    - Use only when the goal is to intentionally shape, cap, or prioritize traffic.
    - This does not increase bottleneck capacity.
    - Queue IDs are arbitrary and must later be referenced exactly by set_queue:N.
    - Ensure request includes the correct egress port_name for the traffic being shaped.
    """
    try:
        data = request.dict(exclude_none=True)
        switch_id = data.pop("switch_id")
        result = ryu_post(f"/qos/queue/{switch_id}", data)
        return _json(result)
    except Exception as exc:
        return f"Error setting QoS queues: {exc}"


@mcp.tool()
def add_qos_rule(request: AddQoSRuleRequest) -> str:
    """
    Add a QoS classification rule to a switch.

    Reasoning hint for the LLM:
    - Correct endpoint is /qos/rules/{switch_id}.
    - set_queue:N means queue ID N, not priority N.
    - Always inspect get_qos_queues() first and choose a queue whose max_rate matches the intent.
    - Do not use QoS as a generic throughput optimizer. If the problem is a low-capacity path,
      prefer rerouting/flow changes over capping the target flow.
    """
    try:
        data = request.dict(exclude_none=True)
        switch_id = data.pop("switch_id")
        vlan_id = data.pop("vlan_id", None)

        path = f"/qos/rules/{switch_id}" if not vlan_id or vlan_id == "all" else f"/qos/rules/{switch_id}/{vlan_id}"
        result = ryu_post(path, data)
        return _json(result)
    except Exception as exc:
        return f"Error adding QoS rule: {exc}"


@mcp.tool()
def delete_qos_rule(request: DeleteQoSRuleRequest) -> str:
    """
    Delete a QoS classification rule.

    Reasoning hint for the LLM:
    - Use this to remove accidental caps or stale rules before adding new ones.
    - If measured throughput equals a low queue max_rate, consider deleting the matching QoS rule.
    """
    try:
        data = request.dict(exclude_none=True)
        switch_id = data.pop("switch_id")
        vlan_id = data.pop("vlan_id", None)

        path = f"/qos/rules/{switch_id}" if not vlan_id or vlan_id == "all" else f"/qos/rules/{switch_id}/{vlan_id}"
        result = ryu_delete(path, data)
        return _json(result)
    except Exception as exc:
        return f"Error deleting QoS rule: {exc}"


@mcp.tool()
async def safe_set_qos_queues(request: SetQueueRequest, intent: str) -> str:
    """
    Safer wrapper for set_qos_queues() that validates against user intent.

    Reasoning hint for the LLM:
    - Prefer this over raw set_qos_queues() when acting autonomously.
    """
    validation = await validate_qos_action(intent, request.dict(exclude_none=True))
    if not validation.is_safe:
        return _json({
            "status": "REJECTED",
            "reason": validation.reason,
            "suggested_action": validation.suggested_action,
        })
    return set_qos_queues(request)


@mcp.tool()
async def safe_add_qos_rule(request: AddQoSRuleRequest, intent: str) -> str:
    """
    Safer wrapper for add_qos_rule() that validates against user intent.

    Reasoning hint for the LLM:
    - Prefer this over raw add_qos_rule() when acting autonomously.
    - The validator should reject rules that cap a target flow below the user's throughput goal.
    """
    validation = await validate_qos_action(intent, request.dict(exclude_none=True))
    if not validation.is_safe:
        return _json({
            "status": "REJECTED",
            "reason": validation.reason,
            "suggested_action": validation.suggested_action,
        })
    return add_qos_rule(request)


# -----------------------------------------------------------------------------
# 6. OpenFlow action tools
# -----------------------------------------------------------------------------

@mcp.tool()
def add_openflow_entry(flow: dict[str, Any]) -> str:
    """
    Add an OpenFlow entry using Ryu ofctl_rest /stats/flowentry/add.

    Reasoning hint for the LLM:
    - Use for explicit forwarding/path selection.
    - This is the right tool family when the problem is bad path selection.
    - Use QoS for traffic classes; use flow entries for where traffic goes.
    """
    try:
        return _json(ryu_post("/stats/flowentry/add", flow))
    except Exception as exc:
        return f"Error adding OpenFlow entry: {exc}"


@mcp.tool()
def delete_openflow_entry(flow: dict[str, Any]) -> str:
    """
    Delete an OpenFlow entry using Ryu ofctl_rest /stats/flowentry/delete.

    Reasoning hint for the LLM:
    - Use to remove stale or incorrect path rules before installing new ones.
    """
    try:
        return _json(ryu_post("/stats/flowentry/delete", flow))
    except Exception as exc:
        return f"Error deleting OpenFlow entry: {exc}"


# -----------------------------------------------------------------------------
# 7. Recovery and cleanup tools
# -----------------------------------------------------------------------------

@mcp.tool()
def refresh_openflow_connections() -> str:
    """
    Reconnect configured OVS switches to the OpenFlow controller.

    Reasoning hint for the LLM:
    - Use when topology exists but ping/packet-in/learning is stale or broken.
    - This is a recovery/reset action, not a performance optimization.
    """
    results = []
    for sw in OVS_SWITCHES:
        results.append(_run_cmd(["sudo", "ovs-vsctl", "del-controller", sw]))
    for sw in OVS_SWITCHES:
        results.append(_run_cmd(["sudo", "ovs-vsctl", "set-controller", sw, OF_CONTROLLER_ADDR]))
    return _json({"switches": OVS_SWITCHES, "controller": OF_CONTROLLER_ADDR, "results": results})


@mcp.tool()
def clear_ovs_qos_config() -> str:
    """
    Clear all local OVS QoS and Queue rows.

    Reasoning hint for the LLM:
    - Use this to recover from accidental caps or stale QoS state.
    - After clearing, inspect flows too; set_queue OpenFlow rules may still exist.
    """
    results = [
        _run_cmd(["sudo", "ovs-vsctl", "--all", "destroy", "QoS"]),
        _run_cmd(["sudo", "ovs-vsctl", "--all", "destroy", "Queue"]),
    ]
    return _json({"results": results})


@mcp.tool()
def dump_local_flows(switch_name: str = "all", table: str | None = None) -> str:
    """
    Dump OVS flows directly with ovs-ofctl.

    Reasoning hint for the LLM:
    - Useful for debugging what is actually installed on OVS.
    - In this lab, table 0 is QoS/classification and table 1 is forwarding.
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
# 8. Lightweight measurement interpretation
# -----------------------------------------------------------------------------

@mcp.tool()
def interpret_throughput_observation(offered_mbps: float, received_mbps: float, context: str = "") -> str:
    """
    Provide a conservative interpretation of an observed throughput test.

    Reasoning hint for the LLM:
    - This tool does not know the topology answer.
    - It suggests next observations: flow path, port deltas, QoS queues/rules.
    - It explicitly avoids saying QoS is the fix unless the intent is shaping/protection.
    """
    offered = float(offered_mbps)
    received = float(received_mbps)
    ratio = received / offered if offered > 0 else None

    notes = []
    if ratio is not None and ratio < 0.5:
        notes.append("Receiver throughput is much lower than offered load; possible bottleneck, QoS cap, or UDP overload.")
    if received > 0:
        notes.append("Check whether received throughput matches any configured queue max_rate.")
    notes.extend([
        "Use get_qos_rules() and get_qos_queues() to check for active caps.",
        "Use get_flow_stats() and get_port_stats_delta() to infer active path.",
        "If the active path is low-capacity, rerouting/flow installation is more appropriate than QoS.",
        "If multiple classes compete on one egress port, QoS may be appropriate for shaping/protection.",
    ])

    return _json({
        "offered_mbps": offered,
        "received_mbps": received,
        "received_to_offered_ratio": ratio,
        "context": context,
        "interpretation": notes,
    })


# -----------------------------------------------------------------------------
# Entrypoint
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    if os.getenv("POKE_TUNNEL_ENABLED") == "true":
        subprocess.Popen([sys.executable, "run_tunnel.py", "--host", MCP_HOST, "--port", str(MCP_PORT)])
        print("PokeTunnel enabled and starting...")
    else:
        print("PokeTunnel disabled. Set POKE_TUNNEL_ENABLED=true to enable.")

    mcp.run(
        transport="http",
        host=MCP_HOST,
        port=MCP_PORT,
        stateless_http=True,
    )
