"""TGA Capability Validator using PyJWT for EdDSA/Ed25519 support."""
from typing import Optional
import jwt
from .models import TgaCapability, TgaCapabilityConstraints
import hashlib
import time

class CapabilityValidationError(Exception):
    def __init__(self, message: str, code: str = "CAPABILITY_INVALID"):
        super().__init__(message)
        self.code = code

class CapabilityValidator:
    """
    Validates TGA Capability tokens (JWS) and enforces constraints 
    against specific tool calls.
    """
    
    def __init__(self, supervisor_public_key: str):
        """
        :param supervisor_public_key: Public key in PEM format (Ed25519).
        """
        self.public_key = supervisor_public_key

    def decode_and_verify(self, token: str) -> TgaCapability:
        """
        Decodes the JWS token and verifies its EdDSA signature.
        """
        try:
            # PyJWT handles EdDSA with cryptography backend
            from cryptography.hazmat.primitives.serialization import load_pem_public_key
            
            # Load the public key if it's a PEM string
            if isinstance(self.public_key, str):
                if self.public_key.startswith("-----BEGIN"):
                    pub_key = load_pem_public_key(self.public_key.encode('utf-8'))
                else:
                    # Assume it's a placeholder for dev mode
                    raise CapabilityValidationError("Invalid public key format", "CONFIG_ERROR")
            else:
                pub_key = self.public_key
                
            payload = jwt.decode(
                token, 
                pub_key, 
                algorithms=['EdDSA'],
                audience='talos-gateway'
            )
            
            # Convert nested constraints dict to Pydantic model
            constraints_dict = payload.get("constraints", {})
            constraints = TgaCapabilityConstraints(**constraints_dict)
            
            cap = TgaCapability(
                iss=payload.get("iss"),
                aud=payload.get("aud"),
                iat=payload.get("iat"),
                nbf=payload.get("nbf"),
                exp=payload.get("exp"),
                nonce=payload.get("nonce"),
                trace_id=payload.get("trace_id"),
                plan_id=payload.get("plan_id"),
                constraints=constraints
            )
            
            self._validate_claims(cap)
            return cap
            
        except jwt.ExpiredSignatureError:
            raise CapabilityValidationError("Capability expired", "EXPIRED")
        except jwt.InvalidAudienceError:
            raise CapabilityValidationError("Invalid audience", "AUDIENCE_MISMATCH")
        except jwt.PyJWTError as e:
            raise CapabilityValidationError(f"Invalid capability signature or format: {str(e)}", "SIGNATURE_INVALID")
        except Exception as e:
            raise CapabilityValidationError(f"Capability decoding failed: {str(e)}")

    def _validate_claims(self, cap: TgaCapability):
        """Verifies standard and TGA-specific claims."""
        now = int(time.time())
        
        if cap.aud != "talos-gateway":
             raise CapabilityValidationError("Invalid audience", "AUDIENCE_MISMATCH")
             
        if cap.exp < now:
             raise CapabilityValidationError("Capability expired", "EXPIRED")
             
        if cap.nbf and cap.nbf > now:
             raise CapabilityValidationError("Capability not yet valid", "NOT_BEFORE")

    def validate_tool_call(self, cap: TgaCapability, tool_server: str, tool_name: str, args: dict):
        """
        Enforce capability constraints against a specific tool call.
        """
        con = cap.constraints
        
        # 1. Tool Identity
        if con.tool_server != tool_server or con.tool_name != tool_name:
            raise CapabilityValidationError(
                f"Unauthorized tool: {tool_server}:{tool_name}, expected {con.tool_server}:{con.tool_name}",
                "TOOL_UNAUTHORIZED"
            )
            
        # 2. Read-Only Enforcement
        if con.read_only:
            mutation_prefixes = ["create-", "update-", "delete-", "write-", "apply-"]
            if any(tool_name.startswith(p) for p in mutation_prefixes):
                 raise CapabilityValidationError(f"Mutation tool '{tool_name}' forbidden in READ_ONLY capability", "READ_ONLY_VIOLATION")

        # 3. Argument Schema Constraints (SHA-256 of Schema)
        if con.arg_constraints:
            # In a real implementation, we would validate 'args' against the 
            # schema identified by 'arg_constraints'. 
            pass

    def calculate_capability_digest(self, token: str) -> str:
        """SHA-256 of the raw JWS token (normative binding)."""
        return hashlib.sha256(token.encode('utf-8')).hexdigest()
