"""
Pydantic models shared by the MCP server and the SDN safety validator.

These mirror a *small* subset of OpenFlow 1.3 match fields and Ryu REST payloads so that:
  - install_forwarding_flow can parse LLM-provided match dicts safely (types, optional fields).
  - validate_sdn_request receives a structured snapshot of what would be sent to Ryu.
"""

from __future__ import annotations
from typing import Any, Dict, List, Optional
from pydantic import BaseModel

class FlowMatch(BaseModel):
    """Fields we allow in a flow match (L2/L3/L4); None means 'wildcard' for that field in Ryu."""
    in_port: Optional[int] = None
    eth_type: Optional[int] = None
    ipv4_src: Optional[str] = None
    ipv4_dst: Optional[str] = None
    ip_proto: Optional[int] = None
    tcp_src: Optional[int] = None
    tcp_dst: Optional[int] = None
    udp_src: Optional[int] = None
    udp_dst: Optional[int] = None

class ForwardingFlowRequest(BaseModel):
    """Complete description of a proposed simple forwarding rule before it hits the controller."""
    switch_id: str
    match: FlowMatch
    out_port: int
    priority: int = 100

class DeleteFlowRequest(BaseModel):
    """Shape for delete operations (kept for API symmetry / future tools); my_server builds Ryu JSON inline."""
    switch_id: str
    flow_id: Optional[int] = None
    match: Optional[FlowMatch] = None

class ValidationResult(BaseModel):
    """Outcome from the validator agent (or rule fallback): whether to allow the SDN change."""
    is_safe: bool
    reason: str
    suggested_action: Optional[str] = None
