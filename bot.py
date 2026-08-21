#!/usr/bin/env python3
"""KiteBot entry point.

    python bot.py            # run the Telegram bot (needs TELEGRAM_BOT_TOKEN)
    python bot.py --dry-run  # print today's digest to stdout, no Telegram needed
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
from datetime import time as dtime
from zoneinfo import ZoneInfo

from kitebot import config

log = logging.getLogger("kitebot")


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(config.ROOT / ".env")


def _daily_time(settings) -> dtime:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", settings.daily_post_time.strip())
    if not match:
        raise SystemExit(f"daily_post_time must look like 07:00, got {settings.daily_post_time!r}")
    hour, minute = int(match[1]), int(match[2])
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise SystemExit(f"daily_post_time out of range: {settings.daily_post_time!r}")
    try:
        tz = ZoneInfo(settings.timezone)
    except Exception as exc:
        raise SystemExit(f"unknown timezone {settings.timezone!r}: {exc}") from exc
    return dtime(hour, minute, tzinfo=tz)


async def _dry_run() -> None:
    from kitebot import messages
    from kitebot.checker import gather_results

    settings = config.load_settings()
    spots = config.load_spots(settings)
    if not spots:
        print("No spots configured.")
        return
    results = await gather_results(spots, settings)
    print(messages.to_plain(messages.build_digest(results, settings)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Kitesurf forecast Telegram bot")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the current digest to stdout and exit (no Telegram)")
    args = parser.parse_args()

    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    _load_dotenv()

    if args.dry_run:
        asyncio.run(_dry_run())
        return

    from telegram.ext import Application

    from kitebot import handlers

    settings = config.load_settings()
    post_time = _daily_time(settings)
    token = config.get_token()

    app = Application.builder().token(token).post_init(handlers.post_init).build()
    if app.job_queue is None:
        raise SystemExit('JobQueue missing — install "python-telegram-bot[job-queue]"')
    handlers.register(app)
    app.job_queue.run_daily(handlers.daily_job, time=post_time, name="daily-digest")
    log.info(
        "starting: daily digest at %s (%s), %d spot(s) configured",
        settings.daily_post_time, settings.timezone, len(config.load_spots()),
    )
    app.run_polling()


if __name__ == "__main__":
    main()
