"""Netflix-style Telegram Mini App on top of the bot's file database."""
import asyncio
import logging

from config import WEBAPP_ENABLED, WEBAPP_URL, WEBAPP_SYNC_SECONDS, STREAM_ONLY

logger = logging.getLogger(__name__)
_task = None


def register_routes(app):
    if WEBAPP_ENABLED and not STREAM_ONLY:  # the stream-only host has no bot to deliver files
        from webapp.api import register_routes as _reg
        _reg(app)


async def start(bot):
    """Called once from Bot.start(): indexes, background catalog loop, menu button."""
    global _task
    if not WEBAPP_ENABLED:
        logger.info("Web app is OFF (WEBAPP_ENABLED=false).")
        return
    from webapp.catalog import ensure_indexes, background_loop
    await ensure_indexes()
    _task = asyncio.create_task(background_loop(WEBAPP_SYNC_SECONDS))
    if not WEBAPP_URL:
        logger.warning("Web app is ON but WEBAPP_URL / STREAM_BASE_URL isn't set — the Open button is disabled.")
        return
    try:
        from pyrogram.types import MenuButtonWebApp, WebAppInfo
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Movies", web_app=WebAppInfo(url=f"{WEBAPP_URL}/app")))
    except Exception:
        logger.info("Couldn't set the web app menu button (the /app command still works).", exc_info=True)
    logger.info("Web app ready at %s/app", WEBAPP_URL)
