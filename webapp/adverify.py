"""
Rewarded-ad verification with AdsGram (replaces the shortener step when /settings -> Verification
-> Mode is "Watch Ad").

Flow
  1. plugins/deliver.py sends a "Verify Now (Watch Ad)" Mini App button -> GET /verify?t=<token>
  2. The page shows the AdsGram ad. When the user watches it to the end it calls POST /api/adverify.
  3. AdsGram's own server ALSO calls GET /adsgram/reward?userid=..&key=.. (the "Reward URL" of the
     ad unit). /api/adverify only verifies the user once that confirmation is there, so calling the
     API by hand (without watching) does nothing.
  4. The user is marked verified (same 3-tier logic as the shortener) and the file is sent.

If AdsGram has no ad for the user (or its script is blocked), the page offers the old shortener
link for that tier instead (POST /api/adfallback).
"""
import asyncio
import hmac
import json
import logging

from aiohttp import web

from config import ADSGRAM_BLOCK_ID, ADSGRAM_SECRET, ADSGRAM_REQUIRE_REWARD, ADSGRAM_WAIT_SECONDS
from database.settings_db import get_settings
from database.verify_db import peek_token, consume_token, mark_verified, record_ad_reward, wait_ad_reward
from log_utils import log_user_verified
from shortlink import make_short_link
from utils import temp
from webapp.auth import validate_init_data

logger = logging.getLogger(__name__)

_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Verify</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script src="https://sad.adsgram.ai/js/sad.min.js"></script>
<style>
  :root{--bg:var(--tg-theme-bg-color,#0f1115);--fg:var(--tg-theme-text-color,#f2f4f8);
        --hint:var(--tg-theme-hint-color,#8b93a1);--btn:var(--tg-theme-button-color,#2f80ed);
        --btnfg:var(--tg-theme-button-text-color,#fff);--card:var(--tg-theme-secondary-bg-color,#181b22)}
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:var(--bg);color:var(--fg);font-family:-apple-system,Segoe UI,Roboto,sans-serif;padding:20px}
  .card{width:100%;max-width:380px;background:var(--card);border-radius:20px;padding:28px 22px;text-align:center}
  .icon{font-size:44px;margin-bottom:6px}
  h1{font-size:20px;margin:6px 0 8px}
  p{margin:0 0 20px;color:var(--hint);font-size:15px;line-height:1.45}
  button{width:100%;border:0;border-radius:12px;padding:14px;font-size:16px;font-weight:600;
         background:var(--btn);color:var(--btnfg);margin-top:10px}
  button.ghost{background:transparent;color:var(--btn);border:1px solid var(--btn)}
  button:disabled{opacity:.55}
</style></head>
<body><div class="card">
  <div class="icon" id="ic">🎬</div>
  <h1 id="t">Unlock your file</h1>
  <p id="m">Watch one short ad to continue. It takes about 15–30 seconds, then your file is sent automatically.</p>
  <button id="go">▶ Watch Ad</button>
  <button id="alt" class="ghost" style="display:none"></button>
</div>
<script>
const tg = (window.Telegram && Telegram.WebApp) || {initData:"",ready(){},expand(){},close(){},openLink(u){location.href=u}};
tg.ready(); tg.expand && tg.expand();
const BLOCK = __BLOCK__;
const TOKEN = new URLSearchParams(location.search).get("t") || "";
const $ = id => document.getElementById(id);
let ctrl = null, mode = "watch", busy = false;
try { ctrl = window.Adsgram ? window.Adsgram.init({blockId: BLOCK}) : null; } catch (e) { ctrl = null; }

function say(icon, title, msg){ $("ic").textContent = icon; $("t").textContent = title; $("m").textContent = msg; }
function setGo(label, m, show = true){ $("go").textContent = label; $("go").style.display = show ? "" : "none"; mode = m; }
async function post(path){
  try {
    const r = await fetch(path, {method:"POST", headers:{"X-Init-Data": tg.initData || "", "Content-Type":"application/json"}, body: JSON.stringify({t: TOKEN})});
    return await r.json();
  } catch (e) { return {ok:false, error:"server"}; }
}

async function confirmAd(){
  say("⏳", "Checking…", "Confirming your ad, one moment.");
  $("go").disabled = true;
  const j = await post("/api/adverify");
  $("go").disabled = false;
  if (j.ok) {
    say("✅", "Verified", "Your file is on its way. Go back to the chat.");
    setGo("", "done", false);
    setTimeout(() => tg.close && tg.close(), 1500);
  } else if (j.error === "expired") {
    say("⚠️", "Link expired", "Go back to the bot and request the file again.");
    setGo("", "done", false);
  } else if (j.error === "not_confirmed") {
    say("⏳", "Almost there", "We couldn't confirm the ad yet. Tap below to check again.");
    setGo("Check again", "check");
  } else {
    say("⚠️", "Something went wrong", "Please try again.");
    setGo("Try again", "check");
  }
}

async function fallback(){
  const j = await post("/api/adfallback");
  if (j.ok) {
    say("ℹ️", "No ad available", "Use the backup verification link instead.");
    setGo("", "done", false);
    $("alt").textContent = "🔗 Verify with backup link";
    $("alt").style.display = "";
    $("alt").onclick = () => tg.openLink(j.url);
  } else if (j.error === "expired") {
    say("⚠️", "Link expired", "Go back to the bot and request the file again.");
    setGo("", "done", false);
  } else {
    say("😕", "No ad right now", "Please try again in a little while.");
    setGo("Try again", "watch");
  }
}

async function watch(){
  if (!TOKEN) { say("⚠️", "Invalid link", "Go back to the bot and request the file again."); setGo("", "done", false); return; }
  if (!ctrl) { return fallback(); }
  try {
    await ctrl.show();
  } catch (res) {
    // AdsGram rejects when the ad was skipped (error:false) or could not be shown (error:true).
    if (res && res.error === false) {
      say("⏩", "Ad not finished", "Please watch the whole ad to unlock your file.");
      setGo("▶ Watch Ad again", "watch");
      return;
    }
    return fallback();
  }
  await confirmAd();
}

$("go").onclick = async () => {
  if (busy) return;
  busy = true;
  try { mode === "check" ? await confirmAd() : await watch(); } finally { busy = false; }
};
</script></body></html>
"""


async def page(request: web.Request):
    html = _PAGE.replace("__BLOCK__", json.dumps(ADSGRAM_BLOCK_ID).replace("</", "<\\/"))
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})


def _json(data: dict, status: int = 200):
    return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})


async def _read(request: web.Request):
    """-> (telegram user id, token) or (None, None) when the request isn't from the Mini App."""
    user = validate_init_data(request.headers.get("X-Init-Data", ""))
    if not user:
        return None, None
    try:
        body = await request.json()
    except Exception:
        body = {}
    return int(user["id"]), str(body.get("t", "")).strip()


async def api_verify(request: web.Request):
    uid, token = await _read(request)
    if uid is None:
        return _json({"ok": False, "error": "auth"}, 401)
    if not token or not await peek_token(token, uid):
        return _json({"ok": False, "error": "expired"})

    if ADSGRAM_REQUIRE_REWARD and not await wait_ad_reward(uid, ADSGRAM_WAIT_SECONDS):
        return _json({"ok": False, "error": "not_confirmed"})

    doc = await consume_token(token, uid)
    if not doc:  # used by a double tap / expired while the ad played
        return _json({"ok": False, "error": "expired"})

    await mark_verified(uid, doc["tier"])
    asyncio.create_task(_finish(uid, doc))
    return _json({"ok": True})


async def _finish(uid: int, doc: dict):
    """Log it and send the file, exactly like tapping "Get my file" after a shortener."""
    bot = temp.BOT
    if bot is None:
        return
    try:
        log_user_verified(await bot.get_users(uid), doc["tier"])
    except Exception:
        logger.warning("Couldn't log ad verification for %s", uid, exc_info=True)
    try:
        from plugins.deliver import deliver_file
        await deliver_file(bot, uid, doc["file_id"])
    except Exception:
        logger.exception("Couldn't send the file after ad verification (user %s)", uid)
        try:
            from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            from strings import VERIFY_DONE_TXT, VERIFY_GET_FILE_BTN
            await bot.send_message(uid, VERIFY_DONE_TXT, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                VERIFY_GET_FILE_BTN, url=f"https://t.me/{temp.U_NAME}?start=file_{doc['file_id']}")]]))
        except Exception:
            logger.warning("Couldn't send the Get my file button to %s", uid, exc_info=True)


async def api_fallback(request: web.Request):
    """No ad for this user -> hand out the old shortener link for the tier they need."""
    uid, token = await _read(request)
    if uid is None:
        return _json({"ok": False, "error": "auth"}, 401)
    doc = await peek_token(token, uid) if token else None
    if not doc:
        return _json({"ok": False, "error": "expired"})

    shortener = (await get_settings())["shorteners"].get(str(doc["tier"]), {})
    domain, api = shortener.get("domain", ""), shortener.get("api", "")
    if not (domain and api):  # never hand out the bare t.me link: that would skip verification
        return _json({"ok": False, "error": "no_fallback"})
    long_url = f"https://t.me/{temp.U_NAME}?start=notcopy_{token}"
    short_url = await make_short_link(long_url, domain, api)
    if short_url == long_url:  # shortener API failed
        return _json({"ok": False, "error": "no_fallback"})
    return _json({"ok": True, "url": short_url})


async def reward_callback(request: web.Request):
    """AdsGram's server-to-server "ad watched" call (the ad unit's Reward URL)."""
    if not hmac.compare_digest(request.query.get("key", ""), ADSGRAM_SECRET):
        return web.Response(status=403, text="forbidden")
    try:
        uid = int(request.query.get("userid", ""))
    except ValueError:
        return web.Response(status=400, text="bad userid")
    await record_ad_reward(uid)
    logger.info("AdsGram confirmed a watched ad for user %s", uid)
    return web.Response(text="ok")


def register_routes(app: web.Application):
    app.add_routes([
        web.get("/verify", page),
        web.post("/api/adverify", api_verify),
        web.post("/api/adfallback", api_fallback),
        web.get("/adsgram/reward", reward_callback),
    ])
