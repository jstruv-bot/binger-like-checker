"""
Binger Like Checker Bot
=======================
A GroupMe bot that checks who didn't like messages from Sirs.

Commands:
  !help                  - Show all commands
  !check                 - Check the last Sir message for non-likers
  !check N               - Check the last N Sir messages
  !sirs                  - List current Sirs
  !addsir @Name          - Add a Sir by name
  !removesir @Name       - Remove a Sir
  !leaderboard           - Show who misses Sir messages most (last 200 msgs)
  !leaderboard N         - Scan last N messages
  !report @Name          - Show a member's Sir report card
  !shame                 - Send shame list for last check
  !exclude @Name         - Exclude a member from checks
  !unexclude @Name       - Remove exclusion
  !ping                  - Check if bot is alive

Setup:
  1. Set environment variables: GROUPME_TOKEN, BOT_ID, GROUP_ID
  2. pip install flask requests
  3. python bot.py
  4. Set your callback URL at dev.groupme.com/bots to http://yourserver:5000/callback
"""

import os
import json
import sqlite3
import time
import threading
from collections import Counter
from datetime import datetime
from flask import Flask, request

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ─────────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────────

TOKEN = os.environ.get("GROUPME_TOKEN", "")
BOT_ID = os.environ.get("BOT_ID", "")
GROUP_ID = os.environ.get("GROUP_ID", "")
PORT = int(os.environ.get("PORT", 5000))

API_BASE = "https://api.groupme.com/v3"
BOT_POST_URL = "https://api.groupme.com/v3/bots/post"

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".binger_bot")
DB_FILE = os.path.join(CONFIG_DIR, "bot.db")

app = Flask(__name__)

# ─────────────────────────────────────────────────────────────────────
#  API CLIENT (with member map caching)
# ─────────────────────────────────────────────────────────────────────


class GroupMeAPI:
    def __init__(self, token):
        self.token = token
        self.session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
        self.session.mount("https://", adapter)
        # Member map cache: (timestamp, member_map)
        self._mm_cache = {}
        self._mm_ttl = 60  # seconds

    def _get(self, endpoint, params=None):
        if params is None:
            params = {}
        params["token"] = self.token
        r = self.session.get(f"{API_BASE}{endpoint}", params=params, timeout=15)
        r.raise_for_status()
        return r.json().get("response")

    def get_group(self, gid):
        return self._get(f"/groups/{gid}")

    def get_messages(self, gid, before_id=None, limit=100):
        p = {"limit": limit}
        if before_id:
            p["before_id"] = before_id
        return self._get(f"/groups/{gid}/messages", p)

    def get_member_map(self, gid):
        """Get user_id -> nickname map, cached for 60s."""
        cached = self._mm_cache.get(gid)
        if cached:
            ts, mm = cached
            if time.time() - ts < self._mm_ttl:
                return mm
        group = self.get_group(gid)
        mm = {}
        for m in group.get("members", []):
            uid = m.get("user_id")
            if uid:
                mm[uid] = m.get("nickname", "Unknown")
        self._mm_cache[gid] = (time.time(), mm)
        return mm

    def fetch_messages(self, gid, count):
        """Fetch up to `count` messages."""
        all_msgs = []
        before_id = None
        remaining = count
        while remaining > 0:
            batch_size = min(remaining, 100)
            result = self.get_messages(gid, before_id=before_id, limit=batch_size)
            if not result or not result.get("messages"):
                break
            batch = result["messages"]
            all_msgs.extend(batch)
            remaining -= len(batch)
            before_id = batch[-1]["id"]
            if len(batch) < batch_size:
                break
        return all_msgs

    def get_message_by_id(self, gid, message_id):
        """Fetch a single message by ID. Returns the message dict or None."""
        # GroupMe doesn't have a single-message endpoint, so we fetch
        # a small batch around the target message ID
        try:
            result = self.get_messages(gid, before_id=str(int(message_id) + 1), limit=1)
            if result and result.get("messages"):
                for m in result["messages"]:
                    if m.get("id") == message_id:
                        return m
        except Exception:
            pass
        # Fallback: fetch a batch after the message ID and look for it
        try:
            result = self.get_messages(gid, before_id=message_id, limit=1)
            # The message we want is the one just before this batch
            # Try fetching with the message as the "after" boundary
            p = {"after_id": str(int(message_id) - 1), "limit": 5}
            p["token"] = self.token
            r = self.session.get(
                f"{API_BASE}/groups/{gid}/messages", params=p, timeout=15
            )
            r.raise_for_status()
            resp = r.json().get("response", {})
            for m in resp.get("messages", []):
                if m.get("id") == message_id:
                    return m
        except Exception:
            pass
        return None

    def fetch_sir_messages(self, gid, sir_ids, count):
        """Fetch messages until `count` Sir messages are found.
        Smarter than fetching a fixed number -- stops early once enough are found."""
        sir_msgs = []
        before_id = None
        fetched = 0
        max_fetch = count * 20 + 100  # safety cap
        while len(sir_msgs) < count and fetched < max_fetch:
            batch_size = min(100, max_fetch - fetched)
            result = self.get_messages(gid, before_id=before_id, limit=batch_size)
            if not result or not result.get("messages"):
                break
            batch = result["messages"]
            for m in batch:
                if m.get("user_id") in sir_ids:
                    sir_msgs.append(m)
                    if len(sir_msgs) >= count:
                        break
            fetched += len(batch)
            before_id = batch[-1]["id"]
            if len(batch) < batch_size:
                break
        return sir_msgs

    def post_bot_message(self, text):
        """Send a bot message using the pooled session with retry."""
        self.session.post(
            BOT_POST_URL, json={"bot_id": BOT_ID, "text": text}, timeout=10
        )


# Lazy-initialized: created on first use, not at import time
_api = None


def get_api():
    global _api
    if _api is None:
        _api = GroupMeAPI(TOKEN)
    return _api


# ─────────────────────────────────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────────────────────────────────


class BotDB:
    def __init__(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS sirs (
                group_id TEXT NOT NULL,
                user_id  TEXT NOT NULL,
                nickname TEXT,
                PRIMARY KEY (group_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS exclusions (
                group_id TEXT NOT NULL,
                user_id  TEXT NOT NULL,
                nickname TEXT,
                PRIMARY KEY (group_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS last_check (
                group_id    TEXT PRIMARY KEY,
                not_liked   TEXT,
                msg_preview TEXT
            );
        """)
        self.conn.commit()

    def get_sirs(self, gid):
        with self._lock:
            rows = self.conn.execute(
                "SELECT user_id, nickname FROM sirs WHERE group_id=?", (gid,)
            ).fetchall()
        return {uid: nick for uid, nick in rows}

    def add_sir(self, gid, uid, nickname):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO sirs VALUES (?,?,?)", (gid, uid, nickname)
            )
            self.conn.commit()

    def remove_sir(self, gid, uid):
        with self._lock:
            self.conn.execute(
                "DELETE FROM sirs WHERE group_id=? AND user_id=?", (gid, uid)
            )
            self.conn.commit()

    def get_exclusions(self, gid):
        with self._lock:
            rows = self.conn.execute(
                "SELECT user_id, nickname FROM exclusions WHERE group_id=?", (gid,)
            ).fetchall()
        return {uid: nick for uid, nick in rows}

    def add_exclusion(self, gid, uid, nickname):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO exclusions VALUES (?,?,?)", (gid, uid, nickname)
            )
            self.conn.commit()

    def remove_exclusion(self, gid, uid):
        with self._lock:
            self.conn.execute(
                "DELETE FROM exclusions WHERE group_id=? AND user_id=?", (gid, uid)
            )
            self.conn.commit()

    def save_last_check(self, gid, not_liked_names, msg_preview):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO last_check VALUES (?,?,?)",
                (gid, json.dumps(not_liked_names), msg_preview),
            )
            self.conn.commit()

    def get_last_check(self, gid):
        with self._lock:
            row = self.conn.execute(
                "SELECT not_liked, msg_preview FROM last_check WHERE group_id=?", (gid,)
            ).fetchone()
        if row:
            return json.loads(row[0]), row[1]
        return [], ""


# Lazy-initialized
_db = None


def get_db():
    global _db
    if _db is None:
        _db = BotDB()
    return _db


# ─────────────────────────────────────────────────────────────────────
#  BOT MESSAGING
# ─────────────────────────────────────────────────────────────────────


def send_bot_message(text):
    """Send a message as the bot. Splits long messages with delay between chunks."""
    api = get_api()
    MAX_LEN = 990
    chunks = []
    while len(text) > MAX_LEN:
        split_at = text.rfind("\n", 0, MAX_LEN)
        if split_at == -1:
            split_at = MAX_LEN
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    chunks.append(text)

    for i, chunk in enumerate(chunks):
        if chunk.strip():
            api.post_bot_message(chunk.strip())
            if i < len(chunks) - 1:
                time.sleep(0.3)  # avoid rate limits between chunks


def find_member_by_name(mm, name):
    """Find a member by partial name match. Returns (user_id, nickname) or None."""
    name_lower = name.lower().strip().lstrip("@")
    if not name_lower:
        return None
    # Exact match first
    for uid, nick in mm.items():
        if nick.lower() == name_lower:
            return uid, nick
    # Partial match
    for uid, nick in mm.items():
        if name_lower in nick.lower():
            return uid, nick
    return None


def get_context():
    """Get common context (api, db, sirs, exclusions, member_map) in one call."""
    api = get_api()
    db = get_db()
    sirs = db.get_sirs(GROUP_ID)
    excl = db.get_exclusions(GROUP_ID)
    mm = api.get_member_map(GROUP_ID)
    return api, db, sirs, excl, mm


# ─────────────────────────────────────────────────────────────────────
#  RATE LIMITING
# ─────────────────────────────────────────────────────────────────────

_last_cmd_time = {}
_cmd_cooldown = 3  # seconds between same command


def check_rate_limit(cmd):
    """Returns True if the command is rate-limited (should be blocked)."""
    now = time.time()
    last = _last_cmd_time.get(cmd, 0)
    if now - last < _cmd_cooldown:
        return True
    _last_cmd_time[cmd] = now
    return False


# ─────────────────────────────────────────────────────────────────────
#  COMMAND HANDLERS (all wrapped in try/except)
# ─────────────────────────────────────────────────────────────────────


def safe_run(func, args):
    """Run a command handler with error reporting."""
    try:
        func(args)
    except Exception as e:
        try:
            send_bot_message(f"Error: {str(e)[:200]}")
        except Exception:
            pass


def cmd_help(_args):
    send_bot_message(
        "BINGER BOT COMMANDS\n"
        "====================\n"
        "!check        - Check last Sir message\n"
        "!check N      - Check last N Sir messages\n"
        "!sirs         - List Sirs\n"
        "!addsir Name  - Add a Sir\n"
        "!removesir Name - Remove a Sir\n"
        "!leaderboard  - Sir non-liker rankings\n"
        "!leaderboard N - Scan last N messages\n"
        "!report Name  - Member's Sir report\n"
        "!shame        - Send shame list\n"
        "!exclude Name - Exclude from checks\n"
        "!unexclude Name - Remove exclusion\n"
        "!ping         - Am I alive?"
    )


def cmd_ping(_args):
    send_bot_message("Binger Bot is alive.")


def cmd_sirs(_args):
    db = get_db()
    sirs = db.get_sirs(GROUP_ID)
    if not sirs:
        send_bot_message("No Sirs set. Use !addsir Name to add one.")
        return
    lines = ["CURRENT SIRS", "=" * 20]
    for i, nick in enumerate(sirs.values(), 1):
        lines.append(f"  {i}. {nick}")
    send_bot_message("\n".join(lines))


def cmd_addsir(name):
    if not name:
        send_bot_message("Usage: !addsir Name")
        return
    api = get_api()
    mm = api.get_member_map(GROUP_ID)
    result = find_member_by_name(mm, name)
    if not result:
        send_bot_message(f'Could not find member "{name}"')
        return
    uid, nick = result
    get_db().add_sir(GROUP_ID, uid, nick)
    send_bot_message(f"{nick} is now a Sir.")


def cmd_removesir(name):
    if not name:
        send_bot_message("Usage: !removesir Name")
        return
    sirs = get_db().get_sirs(GROUP_ID)
    result = find_member_by_name(sirs, name)
    if not result:
        send_bot_message(f'"{name}" is not a Sir.')
        return
    uid, nick = result
    get_db().remove_sir(GROUP_ID, uid)
    send_bot_message(f"{nick} is no longer a Sir.")


def cmd_exclude(name):
    if not name:
        send_bot_message("Usage: !exclude Name")
        return
    mm = get_api().get_member_map(GROUP_ID)
    result = find_member_by_name(mm, name)
    if not result:
        send_bot_message(f'Could not find member "{name}"')
        return
    uid, nick = result
    get_db().add_exclusion(GROUP_ID, uid, nick)
    send_bot_message(f"{nick} is now excluded from checks.")


def cmd_unexclude(name):
    if not name:
        send_bot_message("Usage: !unexclude Name")
        return
    excl = get_db().get_exclusions(GROUP_ID)
    result = find_member_by_name(excl, name)
    if not result:
        send_bot_message(f'"{name}" is not excluded.')
        return
    uid, nick = result
    get_db().remove_exclusion(GROUP_ID, uid)
    send_bot_message(f"{nick} is no longer excluded.")


def cmd_check(count_str):
    try:
        count = max(1, min(int(count_str or "1"), 20))
    except ValueError:
        count = 1

    api, db, sirs, excl, mm = get_context()
    if not sirs:
        send_bot_message("No Sirs set. Use !addsir Name first.")
        return

    sir_ids = set(sirs.keys())
    excl_ids = set(excl.keys())
    active_ids = {uid for uid in mm if uid not in excl_ids}

    # Smart fetch: find exactly N Sir messages
    sir_msgs = api.fetch_sir_messages(GROUP_ID, sir_ids, count)

    if not sir_msgs:
        send_bot_message("No Sir messages found in recent history.")
        return

    all_non_likers = set()
    lines = [
        f"BINGER CHECK ({len(sir_msgs)} Sir msg{'s' if len(sir_msgs) != 1 else ''})",
        "=" * 40,
    ]

    for i, msg in enumerate(sir_msgs, 1):
        liked_ids = set(msg.get("favorited_by", []))
        sender_id = msg.get("user_id", "")
        not_liked = [
            mm[u]
            for u in sorted(active_ids, key=lambda u: mm[u].lower())
            if u not in liked_ids and u != sender_id
        ]
        all_non_likers.update(not_liked)

        sender = msg.get("name", "?")
        text = (msg.get("text") or "(media)")[:40]
        ts = datetime.fromtimestamp(msg.get("created_at", 0)).strftime("%m/%d %H:%M")
        total = len(active_ids) - (1 if sender_id in active_ids else 0)
        lk = total - len(not_liked)
        pct = (lk / total * 100) if total > 0 else 0

        lines.append(f'\n#{i} "{text}"')
        lines.append(f"   By {sender} on {ts} | {lk}/{total} liked ({pct:.0f}%)")
        if not_liked:
            nl_str = ", ".join(not_liked[:10])
            if len(not_liked) > 10:
                nl_str += f" +{len(not_liked) - 10} more"
            lines.append(f"   Didn't like: {nl_str}")
        else:
            lines.append("   Everyone liked this!")

    lines.append(
        f"\n{len(all_non_likers)} unique non-liker{'s' if len(all_non_likers) != 1 else ''} total"
    )

    sorted_nl = sorted(all_non_likers, key=str.lower)
    preview = (sir_msgs[0].get("text") or "(media)")[:50]
    db.save_last_check(GROUP_ID, sorted_nl, preview)
    send_bot_message("\n".join(lines))


def cmd_check_reply(reply_message_id):
    """Check a specific message by ID (triggered by replying with !check)."""
    api_inst, db_inst, sirs, excl, mm = get_context()
    excl_ids = set(excl.keys())
    active_ids = {uid for uid in mm if uid not in excl_ids}

    msg = api_inst.get_message_by_id(GROUP_ID, reply_message_id)
    if not msg:
        send_bot_message("Could not find the replied-to message.")
        return

    liked_ids = set(msg.get("favorited_by", []))
    sender_id = msg.get("user_id", "")
    not_liked = [
        mm[u]
        for u in sorted(active_ids, key=lambda u: mm[u].lower())
        if u not in liked_ids and u != sender_id
    ]

    sender = msg.get("name", "?")
    text = (msg.get("text") or "(media)")[:50]
    ts = datetime.fromtimestamp(msg.get("created_at", 0)).strftime("%m/%d %H:%M")
    total = len(active_ids) - (1 if sender_id in active_ids else 0)
    lk = total - len(not_liked)
    pct = (lk / total * 100) if total > 0 else 0

    lines = [
        f'BINGER CHECK: "{text}"',
        f"By {sender} on {ts} | {lk}/{total} liked ({pct:.0f}%)",
        "=" * 35,
    ]
    if not_liked:
        lines.append(f"Didn't like ({len(not_liked)}):")
        for i, n in enumerate(not_liked, 1):
            lines.append(f"  {i}. {n}")
    else:
        lines.append("Everyone liked this!")

    # Save for !shame
    sorted_nl = sorted(not_liked, key=str.lower) if not_liked else []
    db_inst.save_last_check(GROUP_ID, sorted_nl, text)
    send_bot_message("\n".join(lines))


def cmd_leaderboard(count_str):
    try:
        count = max(50, min(int(count_str or "200"), 2000))
    except ValueError:
        count = 200

    api, db, sirs, excl, mm = get_context()
    if not sirs:
        send_bot_message("No Sirs set. Use !addsir Name first.")
        return

    sir_ids = set(sirs.keys())
    excl_ids = set(excl.keys())
    active_ids = {uid for uid in mm if uid not in excl_ids}

    msgs = api.fetch_messages(GROUP_ID, count)

    sir_msg_count = 0
    non_likes = Counter()

    for m in msgs:
        sender_id = m.get("user_id", "")
        if sender_id not in sir_ids:
            continue
        sir_msg_count += 1
        fav_set = set(m.get("favorited_by", []))
        for uid in active_ids:
            if uid != sender_id and uid not in fav_set:
                non_likes[uid] += 1

    if sir_msg_count == 0:
        send_bot_message("No Sir messages found in the scanned range.")
        return

    sir_names = [sirs[uid] for uid in sir_ids if uid in sirs]
    lines = [
        "SIR NON-LIKER LEADERBOARD",
        f"Sirs: {', '.join(sir_names)}",
        f"{sir_msg_count} Sir messages in {len(msgs)} scanned",
        "=" * 35,
    ]

    ranked = non_likes.most_common(20)
    for i, (uid, cnt) in enumerate(ranked, 1):
        pct = cnt / sir_msg_count * 100
        lines.append(f"  {i:>2}. {mm.get(uid, '?'):<18} {cnt:>3} missed ({pct:.0f}%)")

    if not ranked:
        lines.append("  Everyone liked all Sir messages!")

    send_bot_message("\n".join(lines))


def cmd_report(name):
    if not name:
        send_bot_message("Usage: !report Name")
        return

    api, db, sirs, excl, mm = get_context()
    result = find_member_by_name(mm, name)
    if not result:
        send_bot_message(f'Could not find member "{name}"')
        return

    uid, nick = result
    sir_ids = set(sirs.keys())

    msgs = api.fetch_messages(GROUP_ID, 200)

    sent = 0
    likes_given = 0
    likes_received = 0
    sir_total = 0
    sir_missed = 0

    for m in msgs:
        sender_id = m.get("user_id", "")
        fav_set = set(m.get("favorited_by", []))

        if sender_id == uid:
            sent += 1
            likes_received += len(fav_set)

        if uid in fav_set:
            likes_given += 1

        if sir_ids and sender_id in sir_ids and sender_id != uid:
            sir_total += 1
            if uid not in fav_set:
                sir_missed += 1

    total_msgs = len(msgs)
    like_rate = (likes_given / total_msgs * 100) if total_msgs else 0
    avg_likes = likes_received / sent if sent else 0

    lines = [f"REPORT CARD: {nick}", "=" * 30]

    if sir_ids:
        sir_liked = sir_total - sir_missed
        sir_pct = (sir_missed / sir_total * 100) if sir_total else 0
        lines.append(f"\nSIR MESSAGES")
        lines.append(f"  Sir Messages:    {sir_total}")
        lines.append(f"  Liked:           {sir_liked}")
        lines.append(f"  Didn't Like:     {sir_missed} ({sir_pct:.0f}%)")

    lines.append(f"\nOVERALL ({total_msgs} msgs scanned)")
    lines.append(f"  Messages Sent:   {sent}")
    lines.append(f"  Likes Given:     {likes_given} ({like_rate:.0f}% of all)")
    lines.append(f"  Likes Received:  {likes_received}")
    lines.append(f"  Avg Likes/Msg:   {avg_likes:.1f}")

    send_bot_message("\n".join(lines))


def cmd_shame(_args):
    db = get_db()
    not_liked, preview = db.get_last_check(GROUP_ID)
    if not not_liked:
        send_bot_message("No check results to shame. Run !check first.")
        return

    lines = [
        "BINGER LIKE CHECKER REPORT",
        f'The following {len(not_liked)} member(s) did NOT like: "{preview}"',
        "",
    ]
    for i, n in enumerate(not_liked, 1):
        lines.append(f"{i}. {n}")
    lines.append("\nLike the message. You've been warned.")

    send_bot_message("\n".join(lines))


# ─────────────────────────────────────────────────────────────────────
#  CALLBACK HANDLER
# ─────────────────────────────────────────────────────────────────────

COMMANDS = {
    "!help": cmd_help,
    "!ping": cmd_ping,
    "!sirs": cmd_sirs,
    "!addsir": cmd_addsir,
    "!removesir": cmd_removesir,
    "!exclude": cmd_exclude,
    "!unexclude": cmd_unexclude,
    "!check": cmd_check,
    "!leaderboard": cmd_leaderboard,
    "!report": cmd_report,
    "!shame": cmd_shame,
}


@app.route("/callback", methods=["POST"])
def callback():
    data = request.get_json(silent=True)
    if not data:
        return "OK", 200

    # Ignore bot's own messages
    if data.get("sender_type") == "bot":
        return "OK", 200

    text = (data.get("text") or "").strip()
    if not text.startswith("!"):
        return "OK", 200

    # Check if this is a reply to a specific message
    reply_id = None
    for att in data.get("attachments", []):
        if att.get("type") == "reply":
            reply_id = att.get("reply_id")
            break

    parts = text.split(None, 1)
    cmd = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""

    # If replying to a message with !check, check that specific message
    if cmd == "!check" and reply_id:
        if not check_rate_limit("!check_reply"):
            threading.Thread(
                target=safe_run, args=(cmd_check_reply, reply_id), daemon=True
            ).start()
        return "OK", 200

    handler = COMMANDS.get(cmd)
    if handler:
        if check_rate_limit(cmd):
            return "OK", 200  # silently ignore spam
        threading.Thread(target=safe_run, args=(handler, args), daemon=True).start()

    return "OK", 200


@app.route("/", methods=["GET"])
def health():
    return "Binger Bot is running.", 200


# ─────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not TOKEN:
        print("ERROR: Set GROUPME_TOKEN environment variable")
        exit(1)
    if not BOT_ID:
        print("ERROR: Set BOT_ID environment variable")
        exit(1)
    if not GROUP_ID:
        print("ERROR: Set GROUP_ID environment variable")
        exit(1)

    print(f"Binger Bot starting on port {PORT}...")
    print(f"  Callback URL: http://0.0.0.0:{PORT}/callback")
    print(f"  Group ID: {GROUP_ID}")
    print(f"  Bot ID: {BOT_ID[:8]}...")
    app.run(host="0.0.0.0", port=PORT)
