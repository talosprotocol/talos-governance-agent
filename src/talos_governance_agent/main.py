import logging
import os
import asyncio
from talos_governance_agent.adapters.mcp_server import mcp, init_runtime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Default dev public key (ed25519) - matching example tests if needed
# In production, this MUST be set via TGA_SUPERVISOR_PUBLIC_KEY env var
DEV_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAGb9ECWmYfD98O4vQedFq+W97E8B8+n0o5eL74w7j74Q=
-----END PUBLIC KEY-----"""

async def run_server():
    logger.info("Starting Talos Governance Agent MCP Server...")
    
    # Configuration
    db_path = os.getenv("TGA_DB_PATH", "governance_agent.db")
    supervisor_pub_key = os.getenv("TGA_SUPERVISOR_PUBLIC_KEY", DEV_PUBLIC_KEY)
    
    # Initialize infrastructure
    logger.info(f"Initializing SQLite state store at {db_path}")
    store = init_runtime(db_path, supervisor_pub_key)
    await store.initialize()
    
    logger.info("TGA Runtime and MCP Server initialized.")
    
    # Run FastMCP (Stdio mode by default)
    # Note: FastMCP.run() is synchronous but we are in an async context.
    # We can use the mcp.run() directly if we are at the top level, 
    # but here we might want to use the internal server if we need more control.
    # For simplicity, we just use the default run()
    mcp.run()

if __name__ == "__main__":
    # Since mcp.run() handles its own loop if not provided, 
    # but we need to initialize the store first.
    # Alternatively, we can use the async startup of FastMCP if available.
    
    # For FastMCP, the simplest is to call run() which handles the event loop.
    # However, we need to await store.initialize().
    
    loop = asyncio.get_event_loop()
    db_path = os.getenv("TGA_DB_PATH", "governance_agent.db")
    supervisor_pub_key = os.getenv("TGA_SUPERVISOR_PUBLIC_KEY", DEV_PUBLIC_KEY)
    
    store = loop.run_until_complete(init_runtime(db_path, supervisor_pub_key))
    loop.run_until_complete(store.initialize())
    
    logger.info("Starting FastMCP server...")
    mcp.run()
