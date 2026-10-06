START_TXT = """<b>𝐻𝐸𝑌 {mention} 👋, \n\n𝑆𝐸𝑁𝐷 𝑀𝐸 𝑀𝑂𝑉𝐼𝐸, 𝑆𝐸𝑅𝐼𝐸𝑆, 𝐴𝑁𝐼𝑀𝐸, 𝑆𝐻𝑂𝑊'𝑆 𝑒𝑡𝑐. 𝑁𝐴𝑀𝐸 𝑊𝐼𝑇𝐻 𝐶𝑂𝑅𝑅𝐸𝐶𝑇 𝑆𝑃𝐸𝐿𝐿𝐼𝑁𝐺 😍\n\n<blockquote>🌿 ᴍᴀɪɴᴛᴀɪɴᴇᴅ ʙʏ : <a href="https://t.me/Navex_69">ɴᴀᴠᴇx</a></blockquote></b>"""

HELP_TXT = """<b>How to search</b>
Just type a name in the group, e.g. <code>Inception 2010</code>.
I'll show matching files as buttons (or a text list, depending on admin \
settings) — tap/open one and I'll send it to you here in PM.

<b>Everyone</b>
/myplan — check your premium status
/req or /request — request a file if not found
/trending — most searched titles
/filterwords — words ignored in searches

<b>Admin commands</b>
/index — index an entire channel (auto + manual)
/stats — indexed file count
/settings — force-sub, premium, verification, result display
/add_premium, /remove_premium — manage premium users
/set_shortener, /set_verify_time, /set_tutorial — verification setup
/set_filterword, /remove_filterword — manage ignored search words
"""

NOT_FOUND_TXT = "❌ No results found for <b>{query}</b>."

RESULT_HEADER_TXT = "🔎 Results for <b>{query}</b> — found <b>{total}</b>:"
RESULT_HEADER_CORRECTED_TXT = (
    "🔎 Results for <b>{query}</b> <i>(auto-corrected from \"{original}\")</i> — found <b>{total}</b>:"
)
POSTER_CAPTION_TXT = "🎬 <b>{query}</b>"

# ── progressive search-stage status message (edited in place — one stage
# replaces the previous line each time, never accumulated into a paragraph)
STATUS_STAGE1_TXT = "🔎 Searching {query}..."
STATUS_STAGE2_TXT = "🔁 Checking fuzzy match..."
STATUS_STAGE3_TXT = "🤖 Asking AI..."

# ── Stage 3 suggestion buttons (shown when nothing auto-resolves) ──────────
SUGGESTIONS_HEADER_TXT = "🤔 Couldn't find an exact match for <b>{query}</b>. Did you mean:"
SUGGESTION_NOT_FOUND_TXT = "❌ <b>{title}</b> isn't in the database yet."

SEARCH_EXPIRED_TXT = "⏳ This search has expired. Please search again."

# ── Result-page filters (season / language / episode / year / quality) ──────
FILTER_LABELS = {
    "season": "📅 Season",
    "language": "🌐 Language",
    "episode": "▶️ Episode",
    "year": "📆 Year",
    "quality": "🎞 Quality",
}
FILTER_MENU_TXT = "Choose a {label}:"
FILTER_CLEAR_BTN = "♻️ Any"
HOME_BTN = "🏠 Back to Home"
NO_MATCH_TXT = "😕 No files match the filters you picked."

FILE_SEND_CAPTION = "<code>{file_name}</code>"
FILE_SEND_CAPTION_WITH_LIMIT = "<code>{file_name}</code>\n\n📊 Free file {used}/{limit} for today."

FILE_NOT_FOUND_TXT = "❌ That file is no longer available."

# ── Auto-delete ────────────────────────────────────────────────────────────────
QUERY_AUTODELETE_NOTE = "\n\n⏳ <i>This message will self-destruct in {duration}.</i>"
FILE_AUTODELETE_NOTICE = "🗑 This file will be deleted in <b>{duration}</b> to avoid copyright issues. Forward or save it now."
FILE_AUTODELETE_DONE = "🗑 File deleted."

# ── Force-subscribe ──────────────────────────────────────────────────────────
FSUB_REQUIRED_TXT = (
    "👋 <b>Hey {mention}!</b>\n\n"
    "🛑 You must join the channel(s) below before I can send you this file.\n"
    "👉 Join all of them, then tap <b>Try Again</b>."
)
TRY_AGAIN_BTN = "🔄 Try Again"

# ── Verification ──────────────────────────────────────────────────────────────
VERIFY_PROMPT_TXT = (
    "👋 <b>Hey {mention}!</b>\n\n"
    "📌 You need to complete verification (step {tier}/3) before I can send this file.\n"
    "Tap <b>Verify</b>, follow the page, then come back — I'll send the file automatically."
)
VERIFY_BTN = "🔐 Verify Now"
VERIFY_TUTORIAL_BTN = "📖 How to Verify"
VERIFY_DONE_TXT = "✅ Verification complete! Tap below to get your file."
VERIFY_GET_FILE_BTN = "📥 Get my file"
VERIFY_EXPIRED_TXT = "⚠️ This verification link has expired or was already used. Please request the file again."

# ── Premium ────────────────────────────────────────────────────────────────────
MYPLAN_ACTIVE_TXT = "💎 <b>You have an active premium plan.</b>\n\n⌛ Expires: <code>{expiry}</code>"
MYPLAN_NONE_TXT = "You don't have an active premium plan."
PREMIUM_ADDED_TXT = "✅ Premium granted to <code>{user_id}</code> until <code>{expiry}</code>."
PREMIUM_REMOVED_TXT = "✅ Premium removed from <code>{user_id}</code>."
PREMIUM_NOT_FOUND_TXT = "That user doesn't have an active premium plan."
PREMIUM_USAGE_TXT = "Usage: <code>/add_premium user_id 1month</code>\n\nDuration examples: <code>1day</code>, <code>2hours</code>, <code>1month</code>, <code>1year</code>."

# ── /settings panel ────────────────────────────────────────────────────────────
SETTINGS_MAIN_TXT = "⚙️ <b>Bot Settings</b>\n\nTap a name to see its details, or the status button to toggle it."

FSUB_MENU_HEADER = "📢 <b>Force-Subscribe Channels</b>\n\nTap a channel to remove it."
FSUB_MENU_EMPTY = "📢 <b>Force-Subscribe Channels</b>\n\nNo channels added yet."
FSUB_ADD_PROMPT = "Forward a message from the channel, or send its @username / -100 ID."
FSUB_ADD_NOT_CHANNEL = "That's not a channel."
FSUB_ADD_NOT_ADMIN = "I need to be an admin there (with permission to invite users) before I can enforce this."
FSUB_ADD_OK = "✅ Added <b>{title}</b> to force-subscribe."
FSUB_ADD_FAILED = "Couldn't resolve that channel: <code>{error}</code>"
FSUB_REMOVED_TXT = "✅ Removed from force-subscribe."

PREMIUM_MENU_EMPTY = "💎 <b>Premium Users</b>\n\nNo active premium users."
PREMIUM_MENU_ROW = "👤 <code>{user_id}</code> — expires <code>{expiry}</code>\n"

VERIFY_MENU_HEADER = "🔗 <b>Verification Shorteners</b>\n\n{body}\n\nUse /set_shortener, /set_verify_time and /set_tutorial to change these."
VERIFY_TIER_ROW = (
    "<b>Tier {tier}</b>\n"
    "Domain: <code>{domain}</code>\n"
    "API key: <code>{api}</code>\n"
    "Tutorial: {tutorial}\n"
)
SET_SHORTENER_USAGE = "Usage: <code>/set_shortener tier domain api_key</code>\n\nExample: <code>/set_shortener 1 shortner.in abc123</code>"
SET_SHORTENER_OK = "✅ Tier {tier} shortener saved.\nDemo link: {demo}"
SET_SHORTENER_WARN = "⚠️ Saved, but I couldn't generate a test link — double check the domain and API key."
SET_VERIFY_TIME_USAGE = "Usage: <code>/set_verify_time gap seconds</code>\n\n<code>gap</code> is <code>1</code> (tier1→tier2) or <code>2</code> (tier2→tier3)."
SET_VERIFY_TIME_OK = "✅ Gap {gap} set to <code>{seconds}</code> seconds."
SET_TUTORIAL_USAGE = "Usage: <code>/set_tutorial tier url</code>"
SET_TUTORIAL_OK = "✅ Tier {tier} tutorial link saved."

INDEX_MENU_HEADER = "📚 <b>Indexed Channels</b>\n\nTap a channel to remove it from auto-indexing."
INDEX_MENU_EMPTY = "📚 <b>Indexed Channels</b>\n\nNo channels added yet."
INDEX_MENU_ROW = "🗑 {title} — {count} files"
INDEX_ADD_PROMPT = "Forward a message from the channel, or send its @username / -100 ID."
INDEX_ADD_NOT_CHANNEL = "That's not a channel."
INDEX_ADD_NOT_ADMIN = "I need to be an admin there so I can reliably receive its posts."
INDEX_ADD_OK = "✅ Added <b>{title}</b> for auto-indexing."
INDEX_ADD_FAILED = "Couldn't resolve that channel: <code>{error}</code>"
INDEX_REMOVED_TXT = "✅ Removed from auto-indexing."

ASK_NUMBER_TIMEOUT = "⌛ Timed out — no changes made."
ASK_NUMBER_INVALID = "That's not a valid number — no changes made."
ASK_NUMBER_RETRY = "❌ <b>That's not a valid value.</b> Try again, or tap Back.\n\n"
ASK_BACK_BTN = "⬅️ Back"

ASK_QUERY_DELAY_PROMPT = (
    "Send how long a search-result message should stay before I delete it.\n\n"
    "Seconds (e.g. <code>300</code>) or with a unit (e.g. <code>5min</code>, <code>2hours</code>, <code>1day</code>)."
)
QUERY_DELAY_SET_TXT = "✅ Search results will now self-delete after <b>{duration}</b>."

ASK_FILE_DELAY_PROMPT = (
    "Send how long a delivered file should stay before I delete it.\n\n"
    "Seconds (e.g. <code>600</code>) or with a unit (e.g. <code>10min</code>, <code>2hours</code>, <code>1day</code>)."
)
FILE_DELAY_SET_TXT = "✅ Delivered files will now self-delete after <b>{duration}</b>."

ASK_FILE_LIMIT_PROMPT = "Send how many free files a non-premium user can get per day (e.g. <code>2</code>)."
FILE_LIMIT_SET_TXT = "✅ Free daily file limit set to <code>{count}</code>."
FILE_LIMIT_NEEDS_VERIFY_NOTE = "\n\n⚠️ File limit only takes effect while <b>Verification</b> is also on — it's currently off, so this has no effect yet."

# ── Movie Update Notification (settings panel) ────────────────────────────────
MOVIE_UPDATE_MENU_HEADER = (
    "🎬 <b>Movie Update Notifications</b>\n\n"
    "Status: {status}\n\n"
    "Automatically posts a formatted update to the update channel whenever a "
    "new file lands in one of the fetch channels."
)
MOVIE_UPDATE_FETCH_HEADER = "📥 <b>Fetch Channels</b>\n\nFiles posted in these channels trigger movie updates.\n\n"
MOVIE_UPDATE_FETCH_ROW = "{icon} {title}\n"
MOVIE_UPDATE_FETCH_EMPTY = "📥 <b>Fetch Channels</b>\n\nNo fetch channels configured."
MOVIE_UPDATE_FETCH_ADDED = "✅ Added <b>{title}</b> to fetch channels — I'm an admin there and will pick up new files."
MOVIE_UPDATE_FETCH_REMOVED = "✅ Removed from fetch channels."
MOVIE_UPDATE_FETCH_PROMPT = "Forward a message from the channel to watch for new files, or send its @username / -100 ID."
MOVIE_UPDATE_BOT_NOT_IN = "❌ I'm not a member of <b>{title}</b>. Add me to the channel as an <b>admin</b>, then try again."
MOVIE_UPDATE_BOT_NOT_ADMIN = "❌ I'm in <b>{title}</b> but not an admin. Make me an <b>admin</b> there (Telegram only sends channel posts to admin bots), then try again."
MOVIE_UPDATE_BOT_CHECK_FAILED = "❌ Couldn't verify my permissions in <b>{title}</b>: <code>{error}</code>"
MOVIE_UPDATE_FETCH_LEGEND = "\n⚠️ = I'm not an admin there, so I can't see new files. Fix it, then reopen this menu."
MOVIE_UPDATE_WARN_NO_POST = "\n\n⚠️ <b>I can't post in the update channel.</b> Make me an admin there with <i>Post Messages</i> permission."

EXPORT_CAPTIONS_PREPARING = "📄 Preparing export…"
EXPORT_CAPTIONS_CAPTION_TXT = "📄 Exported <b>{count}</b> file captions from the database."
EXPORT_CAPTIONS_EMPTY_TXT = "⚠️ No files are indexed yet — nothing to export."

BACKFILL_RUNNING = "🔁 Backfilling search index — this may take a moment…"
BACKFILL_DONE_TXT = (
    "✅ Backfill complete — updated <b>{count}</b> file(s).\n\n"
    "Files indexed before this update needed this one-time step so search "
    "can find them; anything indexed from now on gets it automatically."
)

# ── Request feature ─────────────────────────────────────────────────────────────
REQUEST_SENT_TXT = "✅ <b>Your request has been sent to admin!</b>\n\n📮 Requested: <code>{query}</code>"
REQUEST_RECEIVED_TXT = (
    "#FILE_REQUEST\n\n"
    "👤 User: {user_mention}\n"
    "🆔 ID: <code>{user_id}</code>\n"
    "🔍 Query: <code>{query}</code>\n\n"
    "Please check if this file is available and add it to the database."
)
REQUEST_NOT_CONFIGURED_TXT = "⚠️ Request feature is not configured by admin."
REQUEST_BTN_TXT = "📮 Request to Admin"
REQUEST_AUTO_TIMEOUT_TXT = "⏳ No interaction — auto-sending request to admin..."

# ── Admin response messages ─────────────────────────────────────────────────────
ALREADY_AVAILABLE_TXT = "📌 Requested – <code>{requested_name}</code>\n\nYour request is already available 😋, just re-send movie name in group."
NOT_RELEASED_TXT = "📌 Requested – <code>{requested_name}</code>\n\nSorry your request is not released yet 😢. Admin keep monitor your requests, wait for release and then send requested file name in group."
NOT_AVAILABLE_TXT = "❌ Your requested movie is not available on the internet.\n\n📌 Requested – <code>{requested_name}</code>"
UPLOADED_TXT = "📌 Requested – <code>{requested_name}</code>\n\nYour request is uploaded ☺️, just re-send movie name in group."
CHECK_SPELLING_TXT = "📌 Requested – <code>{requested_name}</code>\n\nAdmin can't find any movie and series of this name. Make sure, your spelling is correct ⚠️. Check spelling on google and then request again ❗"
YEAR_LANGUAGE_TXT = "📌 Requested – <code>{requested_name}</code>\n\nBro please tell me years, language, bollywood or hollywood etc., then I will upload 😬. Just re-send request with more info."
WRONG_SPELLING_TXT = "📌 Requested – <code>{requested_name}</code>\n\n✏️ Admin provided correct spelling: <code>{correct_spelling}</code>\n\nPlease request again with correct spelling."
CUSTOM_REPLY_TXT = "📌 Requested – <code>{requested_name}</code>\n\n💬 Admin replied:\n{custom_message}"

# ── Movie Update Notification ───────────────────────────────────────────────
MOVIE_UPDATE_NOTIFY_TXT = """<b>{tag} ➤ {filename}</b>

<blockquote>🎭 ɢᴇɴʀᴇs     : <b>{genres}</b>
📺 ᴏᴛᴛ          : <b>{ott}</b>
🎞️ ǫᴜᴀʟɪᴛʏ  : <b>{quality}</b>
📐 ʀᴇsᴏʟᴜᴛɪᴏɴ : <b>{resolution}</b>
🎧 ᴀᴜᴅɪᴏ       : <b>{language}</b>
🔥 ʀᴀᴛɪɴɢ      : <b>{rating} ⭐</b>
{episodes}</blockquote>"""

MANUAL_UPDATE_NOTIFY_TXT = """<b>{tag} ➤ {filename}</b>

<blockquote>🎭 ɢᴇɴʀᴇs     : <b>{genres}</b>
📺 ᴏᴛᴛ          : <b>{ott}</b>
🎞️ ǫᴜᴀʟɪᴛʏ  : <b>{quality}</b>
📐 ʀᴇsᴏʟᴜᴛɪᴏɴ : <b>{resolution}</b>
🎧 ᴀᴜᴅɪᴏ       : <b>{language}</b>
🔥 ʀᴀᴛɪɴɢ      : <b>{rating} ⭐</b>
{episodes}</blockquote>"""

# ── restart notice (admin DM) and log-channel messages ──────────────────────
RESTART_TXT = """🔄 <b>Bot Restarted</b>

🤖 @{username} is back online.
🕐 <b>Time:</b> {time}"""

NEW_USER_LOG_TXT = """#NewUser
👤 <b>New user started the bot</b>

<b>Name:</b> {mention}
<b>ID:</b> <code>{user_id}</code>
<b>Username:</b> {username}
<b>Total users:</b> {total}
<b>Time:</b> {time}
<b>Bot:</b> @{bot}"""

USER_VERIFIED_LOG_TXT = """#UserVerified
✅ <b>User verified</b> — {ordinal} verification ({tier}/3)

<b>Name:</b> {mention}
<b>ID:</b> <code>{user_id}</code>
<b>Username:</b> {username}
<b>Time:</b> {time}"""

# ── Fast Download ─────────────────────────────────────────────────────────────
FAST_DOWNLOAD_BTN = "⚡ Fast Download"
FAST_DOWNLOAD_LINK_BTN = "⬇️ Download"
FAST_GENERATING_TXT = (
    "⏳ Generating your link...\n\n"
    "✅ Valid for {hours} hours, then it expires.\n"
)
FAST_LIMIT_REACHED_TXT = "⚠️ You've used all {limit} fast-download links for today. Try again tomorrow."
FAST_UNAVAILABLE_TXT = "⚠️ Fast download is busy right now. Please use the file above."
FAST_ERROR_TXT = "❌ Couldn't create the link. Please try again in a moment."
FAST_WATCH_BTN = "▶️ Watch"

# Posted in BIN_CHANNEL (as a reply to the file) every time a link is generated.
BIN_USER_INFO_TXT = """📥 <b>Fast download link generated</b>

📁 <b>File:</b> <code>{file_name}</code>
🆔 <b>User ID:</b> <code>{user_id}</code>
👤 <b>User:</b> {user_link}"""


# ── Link guard ───────────────────────────────────────────────────────────────
LINK_NOT_ALLOWED_TXT = "🚫 {mention}, sending link is not allowed."

# ── Autofilter on/off ────────────────────────────────────────────────────────
MAINTENANCE_TXT = "🛠 <b>Bot is under maintenance.</b>\n\nPlease try again later."
AUTOFILTER_INFO_TXT = "Turn the autofilter ON or OFF. While it is OFF the bot doesn't search and replies that it is under maintenance."
EMPTY_QUERY_TXT = "❌ Please send a movie / series name to search."

# ── Filter words ─────────────────────────────────────────────────────────────
FILTERWORDS_LIST_TXT = "🚫 <b>Filter words</b> ({count})\n\nThese words are ignored in every search:\n\n{words}"
FILTERWORDS_EMPTY_TXT = "ℹ️ No filter words are set."
SET_FILTERWORD_USAGE = (
    "Usage: <code>/set_filterword word1, word2, some phrase</code>\n\n"
    "Separate multiple words / phrases with a comma. They are removed from every search query."
)
SET_FILTERWORD_OK = (
    "✅ <b>Filter words updated</b>\n\n"
    "➕ Added: {added}\n"
    "↔️ Already set: {existing}\n\n"
    "Total: <b>{total}</b>"
)
REMOVE_FILTERWORD_USAGE = (
    "Usage: <code>/remove_filterword word1, some phrase</code>\n\n"
    "Separate multiple words / phrases with a comma. See all with /filterwords."
)
REMOVE_FILTERWORD_OK = (
    "✅ <b>Filter words updated</b>\n\n"
    "🗑 Removed: {removed}\n"
    "❓ Not found: {missing}\n\n"
    "Total: <b>{total}</b>"
)

# ── /trending ────────────────────────────────────────────────────────────────
TRENDING_HEADER_TXT = "🔥 <b>Trending Searches</b>\n\nMost searched titles — tap one to get its files:"
TRENDING_EMPTY_TXT = "🔥 No trending searches yet. Search for something first!"
TRENDING_NOT_IN_DB_TXT = "❌ {title} isn't in the database anymore."

# ── PM filter (admin switch in /settings) ──────────────────────────────────────
PM_FILTER_INFO_TXT = "PM Filter ON: the bot searches movie names typed in its DM. OFF: it asks users to search in the movie group instead. Group search always stays ON."
PM_SEARCH_OFF_TXT = (
    "<b>👋 ʜᴇʏ {mention}!\n\n"
    "🚫 ᴍᴏᴠɪᴇ sᴇᴀʀᴄʜ ɪɴ ʙᴏᴛ ᴘᴍ ɪs ᴛᴜʀɴᴇᴅ ᴏꜰꜰ.\n"
    "🎬 ᴘʟᴇᴀsᴇ sᴇᴀʀᴄʜ ʏᴏᴜʀ ᴍᴏᴠɪᴇ ɪɴ ᴏᴜʀ ᴍᴏᴠɪᴇ ɢʀᴏᴜᴘ — ᴛᴀᴘ ᴛʜᴇ ʙᴜᴛᴛᴏɴ ʙᴇʟᴏᴡ 👇</b>"
)
PM_SEARCH_OFF_BTN = "🎬 ᴍᴏᴠɪᴇ ɢʀᴏᴜᴘ"

# ── Welcome message switch ─────────────────────────────────────────────────────
WELCOME_INFO_TXT = "Welcome ON: when someone joins a group where I'm admin, I send the welcome video with their mention. OFF: no welcome message."

# ── Premium page ───────────────────────────────────────────────────────────────
PREMIUM_OWNER_BTN = "👤 ᴏᴡɴᴇʀ"
PREMIUM_MYPLAN_BTN = "💎 ᴍʏ ᴘʟᴀɴ"
PREMIUM_BACK_BTN = "⬅️ ʙᴀᴄᴋ"
MYPLAN_ALERT_ACTIVE = "💎 Premium active\nExpires: {expiry}"
MYPLAN_ALERT_NONE = "You don't have an active premium plan.\nTap Owner to buy one."

# ── /telegraph ─────────────────────────────────────────────────────────────────
TELEGRAPH_PROMPT_TXT = (
    "📎 Send me a <b>photo or video</b> (reply to one with /telegraph, or send it now).\n"
    "I'll give you a link + the Telegram <code>file_id</code>.\n\n"
    "<i>Waiting 60 seconds…</i>"
)
TELEGRAPH_TIMEOUT_TXT = "⏳ Timed out. Send /telegraph again."
TELEGRAPH_NOT_MEDIA_TXT = "❌ That's not a photo or video. Send /telegraph again."
TELEGRAPH_TOO_BIG_TXT = "❌ File too big: <b>{size}</b> (max <b>{limit}</b> for the current provider)."
TELEGRAPH_DOWNLOADING_TXT = "⬇️ Downloading…"
TELEGRAPH_UPLOADING_TXT = "⬆️ Uploading…"
TELEGRAPH_FAILED_TXT = "❌ <b>Upload failed on every provider.</b>\n\n<code>{error}</code>"
TELEGRAPH_CANCEL_BTN = "❌ Cancel"
TELEGRAPH_TEMP_NOTE = "\n\n⏳ <i>This host keeps files only <b>{keep}</b> — for config.py use the file_id below, it never expires.</i>"
TELEGRAPH_DONE_TXT = (
    "✅ <b>Link ready</b>\n\n"
    "🔗 <code>{url}</code>\n"
    "🌐 Host: <code>{host}</code>\n\n"
    "🆔 <b>file_id</b> (use for WELCOME_VIDEO / PREMIUM_PHOTO):\n<code>{file_id}</code>\n\n"
    "Paste the link or file_id into <code>config.py</code>.{note}"
)

# ── Premium page (shown by the "premium" button) ──────────────────────────────
PREMIUM_TEXT = """<b><i><blockquote>ᴀᴠᴀɪʟᴀʙʟᴇ ᴘʟᴀɴs  ♻️</blockquote>

• 𝟷 ᴡᴇᴇᴋ  -  ₹15
• 𝟷 ᴍᴏɴᴛʜ  -  ₹50
• 2 ᴍᴏɴᴛʜs  -  ₹80
• 3 ᴍᴏɴᴛʜs  -  ₹100

•─────•─────────•─────•
<blockquote>ᴘʀᴇᴍɪᴜᴍ ꜰᴇᴀᴛᴜʀᴇs  🎁</blockquote>

○ ɴᴏ ɴᴇᴇᴅ ᴛᴏ ᴠᴇʀɪꜰʏ
○ ᴅɪʀᴇᴄᴛ ꜰɪʟᴇs   
○ ᴀᴅ-ꜰʀᴇᴇ ᴇxᴘᴇʀɪᴇɴᴄᴇ 
○ ʜɪɢʜ-sᴘᴇᴇᴅ ᴅᴏᴡɴʟᴏᴀᴅ ʟɪɴᴋ                         
○ ᴍᴜʟᴛɪ-ᴘʟᴀʏᴇʀ sᴛʀᴇᴀᴍɪɴɢ ʟɪɴᴋs                           
○ ᴜɴʟɪᴍɪᴛᴇᴅ ᴍᴏᴠɪᴇꜱ, ꜱᴇʀɪᴇꜱ & ᴀɴɪᴍᴇ                                                                         
○ ꜰᴜʟʟ ᴀᴅᴍɪɴ sᴜᴘᴘᴏʀᴛ                              
○ ʀᴇǫᴜᴇsᴛ ᴡɪʟʟ ʙᴇ ᴄᴏᴍᴘʟᴇᴛᴇᴅ ɪɴ 𝟷ʜ
•─────•─────────•─────•


✨ ᴜᴘɪ ɪᴅ - <code>navex69@axl</code>

ᴄʜᴇᴄᴋ ʏᴏᴜʀ ᴀᴄᴛɪᴠᴇ ᴘʟᴀɴ  /myplan

💢 ᴍᴜsᴛ sᴇɴᴅ sᴄʀᴇᴇɴsʜᴏᴛ ᴀꜰᴛᴇʀ ᴘᴀʏᴍᴇɴᴛ

‼️ ᴀꜰᴛᴇʀ sᴇɴᴅɪɴɢ ᴀ sᴄʀᴇᴇɴsʜᴏᴛ ᴘʟᴇᴀsᴇ ɢɪᴠᴇ ᴍᴇ sᴏᴍᴇ ᴛɪᴍᴇ ᴛᴏ ᴀᴅᴅ ʏᴏᴜ ɪɴ ᴛʜᴇ ᴘʀᴇᴍɪᴜᴍ ᴠᴇʀsɪᴏɴ.</i></b>"""

# ── /req format help (auto-deleted with the other bot messages) ───────────────
REQUEST_HELP_TXT = (
    "📮 <b>How to use:</b>\n\n"
    "<code>/request Movie Name</code>\n"
    "<code>/req Movie Name</code>\n\n"
    "Example: <code>/req Inception 2010</code>"
)
