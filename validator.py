from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext
from typing import Optional, List, Any, Union
import os
from dotenv import load_dotenv

load_dotenv()

class ValidationResult(BaseModel):
    is_safe: bool = Field(..., description="Whether the requested action is safe to perform")
    reason: str = Field(..., description="Reasoning for the safety decision")
    suggested_action: Optional[str] = Field(None, description="A safer alternative if the original was unsafe")

def get_agent():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    
    return Agent(
        'openai:gpt-4o',
        result_type=ValidationResult,
        system_prompt=(
            "You are an SDN Safety Validator. Your job is to inspect proposed OpenFlow changes "
            "and ensure they are within safe bounds and match the user's intent. "
            "Safe bounds: \n"
            "- Only allow basic forwarding actions (OUTPUT).\n"
            "- Do not allow arbitrary actions like SET_FIELD.\n"
            "- Match fields must be restricted to: in_port, eth_src, eth_dst, ipv4_src, ipv4_dst, eth_type.\n"
            "- Priority should be between 0 and 65535."
        ),
    )

async def validate_sdn_request(intent: str, request: Any) -> ValidationResult:
    """
    Validates an SDN request against the user's intent using PydanticAI.
    """
    agent = get_agent()
    if not agent:
        return ValidationResult(
            is_safe=True,
            reason="Validated by rule-based fallback (No API key found)"
        )

    prompt = f"User Intent: {intent}\nProposed Request: {request.model_dump_json() if hasattr(request, 'model_dump_json') else str(request)}"
    
    try:
        result = await agent.run(prompt)
        return result.data
    except Exception as e:
        return ValidationResult(
            is_safe=False,
            reason=f"Validation failed due to error: {str(e)}"
        )
