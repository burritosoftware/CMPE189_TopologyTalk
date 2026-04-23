from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any

class QueueConfig(BaseModel):
    max_rate: Optional[int] = Field(None, description="Maximum rate for the queue in bps")
    min_rate: Optional[int] = Field(None, description="Minimum rate for the queue in bps")

class SetQueueRequest(BaseModel):
    switch_id: str = Field(..., description="The DPID of the switch or 'all'")
    port_name: Optional[str] = Field(None, description="Name of the port (e.g., 's1-eth1'). If omitted, all ports are targets.")
    type: str = Field("linux-htb", description="Queue type: 'linux-htb' or other supported types")
    max_rate: Optional[int] = Field(None, description="Maximum rate for the port in bps")
    queues: List[QueueConfig] = Field(..., description="List of queue configurations")

class QoSRuleMatch(BaseModel):
    in_port: Optional[int] = None
    dl_src: Optional[str] = None
    dl_dst: Optional[str] = None
    dl_type: Optional[str] = None
    nw_src: Optional[str] = None
    nw_dst: Optional[str] = None
    ipv6_src: Optional[str] = None
    ipv6_dst: Optional[str] = None
    nw_proto: Optional[str] = None
    tp_src: Optional[int] = None
    tp_dst: Optional[int] = None
    ip_dscp: Optional[int] = None

class QoSRuleActions(BaseModel):
    mark: Optional[int] = Field(None, description="DSCP value to mark")
    meter: Optional[int] = Field(None, description="Meter ID to apply")
    queue: Optional[int] = Field(None, description="Queue ID to use")

class AddQoSRuleRequest(BaseModel):
    switch_id: str = Field(..., description="The DPID of the switch or 'all'")
    vlan_id: Optional[str] = Field(None, description="VLAN ID or 'all'")
    priority: int = Field(1, ge=0, le=65533, description="Priority of the rule (0-65533)")
    match: QoSRuleMatch = Field(..., description="Match criteria for the rule")
    actions: QoSRuleActions = Field(..., description="Actions to apply")

class DeleteQoSRuleRequest(BaseModel):
    switch_id: str = Field(..., description="The DPID of the switch or 'all'")
    vlan_id: Optional[str] = Field(None, description="VLAN ID or 'all'")
    qos_id: str = Field(..., description="QoS ID to delete or 'all'")
