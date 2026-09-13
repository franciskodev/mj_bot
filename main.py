import asyncio
import logging
import signal

from bot import InterpolovBot
from config import DISCORD_TOKEN


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)


async def run_bot() -> None:
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing. Create .env and add your bot token.")

    bot = InterpolovBot()
    try:
        await bot.database.connect()
    except Exception:
        try:
            await bot.close()
        finally:
            try:
                await bot.database.close(bot.voice_shutdown_at)
            except Exception:
                logging.exception("Could not close the database after startup failure")
        raise
    loop = asyncio.get_running_loop()
    shutdown_task: asyncio.Task[None] | None = None
    handled_signals: list[signal.Signals] = []

    def request_shutdown() -> None:
        nonlocal shutdown_task
        if shutdown_task is None:
            shutdown_task = loop.create_task(bot.close())

    for shutdown_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(shutdown_signal, request_shutdown)
        except NotImplementedError:
            break
        handled_signals.append(shutdown_signal)

    try:
        await bot.start(DISCORD_TOKEN)
        if bot.fatal_error is not None:
            raise bot.fatal_error
    finally:
        for shutdown_signal in handled_signals:
            loop.remove_signal_handler(shutdown_signal)
        try:
            if shutdown_task is not None:
                await shutdown_task
            elif not bot.is_closed():
                await bot.close()
        finally:
            await bot.database.close(bot.voice_shutdown_at)


def main() -> None:
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
