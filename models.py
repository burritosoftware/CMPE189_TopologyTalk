from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any, Union

class FlowMatch(BaseModel):
    in_port: Optional[int] = None
    eth_src: Optional[str] = None
    eth_dst: Optional[str] = None
    ipv4_src: Optional[str] = None
    ipv4_dst: Optional[str] = None
    eth_type: Optional[int] = None

class ForwardingFlowRequest(BaseModel):
    switch_id: Union[str, int]
    match: FlowMatch
    out_port: int
    priority: int = 100

class GroupBucket(BaseModel):
    weight: Optional[int] = None
    watch_port: Optional[int] = None
    watch_group: Optional[int] = None
    actions: List[Dict[str, Any]]  # Should be constrained in validator

class SelectGroupRequest(BaseModel):
    switch_id: Union[str, int]
    group_id: int
    buckets: List[GroupBucket]

class FastFailoverGroupRequest(BaseModel):
    switch_id: Union[str, int]
    group_id: int
    buckets: List[GroupBucket]

class FlowToGroupRequest(BaseModel):
    switch_id: Union[str, int]
    match: FlowMatch
    group_id: int
    priority: int = 100

class DeleteFlowRequest(BaseModel):
    switch_id: Union[str, int]
    flow_id: Optional[int] = None  # Could be cookie
    match: Optional[FlowMatch] = None
    priority: Optional[int] = None

class DeleteGroupRequest(BaseModel):
    switch_id: Union[str, int]
    group_id: int
