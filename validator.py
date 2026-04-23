from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext
from typing import Optional, List
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
    
    # This agent will act as the cross-check layer
    return Agent(
        'openai:gpt-4o',
        result_type=ValidationResult,
        system_prompt=(
            "You are an SDN QoS Safety Validator. Your job is to inspect proposed QoS changes "
            "and ensure they are within safe bounds and match the user's intent. "
            "Safe bounds: \n"
            "- max_rate should not exceed 1Gbps (1000000000 bps)\n"
            "- priority should be between 0 and 65533\n"
            "- Do not allow actions that would likely blackhole traffic unless explicitly confirmed."
        ),
    )

async def validate_qos_action(intent: str, proposed_action: dict) -> ValidationResult:
    """
    Validates a QoS action against the user's intent.
    """
    # Fallback to simple rule-based validation if no API key
    max_rate = proposed_action.get("max_rate")
    if max_rate and isinstance(max_rate, int) and max_rate > 1000000000:
        return ValidationResult(
            is_safe=False,
            reason="Max rate exceeds 1Gbps safety limit (Rule-based fallback)",
            suggested_action="Set max_rate to 1000000000 or less"
        )
    
    agent = get_agent()
    if not agent:
        return ValidationResult(
            is_safe=True,
            reason="Validated by rule-based fallback (No API key found)"
        )

    prompt = f"User Intent: {intent}\nProposed Action: {proposed_action}"
    
    try:
        result = await agent.run(prompt)
        return result.data
    except Exception as e:
        return ValidationResult(
            is_safe=False,
            reason=f"Validation failed due to error: {str(e)}"
        )
