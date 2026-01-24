import logging
import asyncio
from talos_governance_agent.domain.runtime import TgaRuntime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def main():
    logger.info("Starting Talos Governance Agent...")
    runtime = TgaRuntime()
    # TODO: Initialize MCP server or other transport here
    logger.info("TGA Runtime initialized.")
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
