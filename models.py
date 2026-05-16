from __future__ import annotations
from typing import Any, Dict, List, Optional
from pydantic import BaseModel

class FlowMatch(BaseModel):
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
    switch_id: str
    match: FlowMatch
    out_port: int
    priority: int = 100

class DeleteFlowRequest(BaseModel):
    switch_id: str
    flow_id: Optional[int] = None
    match: Optional[FlowMatch] = None

class ValidationResult(BaseModel):
    is_safe: bool
    reason: str
    suggested_action: Optional[str] = None
