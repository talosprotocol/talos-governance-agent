"""TGA Domain Module for Talos Governance Agent."""
from .models import TgaCapability, TgaCapabilityConstraints
from .validator import CapabilityValidator, CapabilityValidationError

__all__ = [
    "TgaCapability",
    "TgaCapabilityConstraints", 
    "CapabilityValidator",
    "CapabilityValidationError",
]
