"""``python -m app.web`` — the entry point the compose service runs."""
import asyncio

from app.web.server import main

if __name__ == "__main__":
    asyncio.run(main())
