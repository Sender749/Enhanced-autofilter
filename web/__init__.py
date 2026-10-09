from aiohttp import web

from config import FASTDL_SERVER_ENABLED


async def _health(_):
    return web.Response(text="ok")


web_app = web.Application()
web_app.add_routes([web.get("/", _health)])

# Fast-download routes only exist when BIN_CHANNEL + STREAM_SECRET are set.
if FASTDL_SERVER_ENABLED:
    from fastdl.server import register_routes
    register_routes(web_app)

# Web app (Mini App pages + JSON API) — see webapp/.
import webapp
webapp.register_routes(web_app)
