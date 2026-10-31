import asyncio
import sys

if sys.platform == "win32":  # psycopg's async mode cannot use Windows' default Proactor loop
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
