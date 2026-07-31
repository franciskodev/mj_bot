import asyncio
import logging

from bot import InterpolBot
from config import DISCORD_TOKEN


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


async def run_bot() -> None:
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing. Create .env and add your bot token.")

    bot = InterpolBot()
    await bot.database.connect()

    try:
        await bot.start(DISCORD_TOKEN)
    finally:
        await bot.close()
        await bot.database.close()


def main() -> None:
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
