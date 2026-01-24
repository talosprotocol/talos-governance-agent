import time
import secrets

def uuid7() -> str:
    """Generate a UUIDv7 string (Draft 04)."""
    ns = time.time_ns()
    ms = ns // 1_000_000

    # Random bits
    rand_a = secrets.randbits(12)
    rand_b = secrets.randbits(62)

    # Construct the 128-bit integer
    uuid_int = (ms & 0xFFFFFFFFFFFF) << 80
    uuid_int |= (0x7 << 76)
    uuid_int |= (rand_a << 64)
    uuid_int |= (0x2 << 62)
    uuid_int |= rand_b

    # Format as hex string with dashes
    h = f"{uuid_int:032x}"
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
