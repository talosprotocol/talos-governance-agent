from pydantic import BaseModel, Field
from typing import List, Optional
from uuid import UUID

class TgaCapabilityConstraints(BaseModel):
    """Deep constraints enforced at the Gateway for TGA tool calls."""
    tool_server: str = Field(..., description="Target tool server (e.g. mcp-github)")
    tool_name: str = Field(..., description="Target tool name (e.g. create-pr)")
    target_allowlist: List[str] = Field(..., description="List of allowed repos, paths, or service IDs")
    arg_constraints: Optional[str] = Field(None, description="SHA-256 digest of the JSON Schema fragment for args")
    read_only: bool = Field(False, description="If True, deny any mutation tools")

class TgaCapability(BaseModel):
    """
    Representation of a JWS/EdDSA signed capability minted by the Supervisor.
    Standard claims (iss, aud, exp, nbf, iat) are joined by TGA-specific constraints.
    """
    iss: str = Field(..., description="Issuer (Supervisor identity/DID)")
    aud: str = Field("talos-gateway", description="Audience (must be talos-gateway)")
    iat: int = Field(..., description="Issued at (Unix epoch)")
    nbf: Optional[int] = Field(None, description="Not before (Unix epoch)")
    exp: int = Field(..., description="Expiration (Unix epoch)")
    nonce: str = Field(..., description="Unique nonce for replay protection")
    
    trace_id: UUID = Field(..., description="The shared trace identifier")
    plan_id: UUID = Field(..., description="The planned goal identifier")
    
    constraints: TgaCapabilityConstraints = Field(..., description="Deep execution constraints")
