"""TGA Capability Validator using PyJWT for EdDSA/Ed25519 support."""
from typing import Dict, Any
import jwt
import hashlib
import time
import base64
import json
from .models import TgaCapability, TgaCapabilityConstraints

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
                pub_key, # type: ignore
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

    def validate_tool_call(self, cap: TgaCapability, tool_server: str, tool_name: str, args: Dict[str, Any]):
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
            if any(tool_name.lower().startswith(p) for p in mutation_prefixes):
                 raise CapabilityValidationError(f"Mutation tool '{tool_name}' forbidden in READ_ONLY capability", "READ_ONLY_VIOLATION")

        # 3. Target Allowlist Enforcement
        if con.target_allowlist and "*" not in con.target_allowlist:
            import fnmatch
            # Identify target candidates from args (common fields)
            target_candidates = ["repo", "repository", "path", "url", "target", "container", "resource"]
            found_target = False
            for cand in target_candidates:
                if cand in args:
                    val = str(args[cand])
                    if any(fnmatch.fnmatch(val, pattern) for pattern in con.target_allowlist):
                        found_target = True
                        break
            
            if not found_target:
                raise CapabilityValidationError(
                    f"Target unauthorized by allowlist: {args}. Required matching one of {con.target_allowlist}",
                    "TARGET_UNAUTHORIZED"
                )

        # 4. Argument Schema Constraints (Deterministic)
        if con.arg_constraints_digest:
            actual_args_digest = self._compute_args_digest(args)
            if actual_args_digest != con.arg_constraints_digest:
                # Note: In some systems this is a schema digest, but for deterministic 
                # authz we treat it as a strict argument pin here if it doesn't match a known schema.
                raise CapabilityValidationError(
                    f"Arguments do not match constraints. Expected digest {con.arg_constraints_digest}, got {actual_args_digest}",
                    "ARGS_UNAUTHORIZED"
                )

    def _compute_args_digest(self, args: Dict[str, Any]) -> str:
        """Compute base64url SHA-256 of canonical JSON args."""
        canonical = json.dumps(args, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")

    def calculate_capability_digest(self, token: str) -> str:
        """
        SHA-256 of the raw JWS token (normative binding).
        Returns base64url encoded digest without padding.
        """
        digest = hashlib.sha256(token.encode('utf-8')).digest()
        return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")
