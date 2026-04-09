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
import re
import sqlite3
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
#  API CLIENT
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
        self.session.mount("http://", adapter)

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
        """Get user_id -> nickname map for the group."""
        group = self.get_group(gid)
        mm = {}
        for m in group.get("members", []):
            uid = m.get("user_id")
            if uid:
                mm[uid] = m.get("nickname", "Unknown")
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


api = GroupMeAPI(TOKEN)

# ─────────────────────────────────────────────────────────────────────
#  DATABASE (Sirs, Exclusions, Last Check)
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

    # ── Sirs ──
    def get_sirs(self, gid):
        with self._lock:
            rows = self.conn.execute(
                "SELECT user_id, nickname FROM sirs WHERE group_id=?", (gid,)
            ).fetchall()
        return {uid: nick for uid, nick in rows}

    def add_sir(self, gid, uid, nickname):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO sirs (group_id, user_id, nickname) VALUES (?,?,?)",
                (gid, uid, nickname),
            )
            self.conn.commit()

    def remove_sir(self, gid, uid):
        with self._lock:
            self.conn.execute(
                "DELETE FROM sirs WHERE group_id=? AND user_id=?", (gid, uid)
            )
            self.conn.commit()

    # ── Exclusions ──
    def get_exclusions(self, gid):
        with self._lock:
            rows = self.conn.execute(
                "SELECT user_id, nickname FROM exclusions WHERE group_id=?", (gid,)
            ).fetchall()
        return {uid: nick for uid, nick in rows}

    def add_exclusion(self, gid, uid, nickname):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO exclusions (group_id, user_id, nickname) VALUES (?,?,?)",
                (gid, uid, nickname),
            )
            self.conn.commit()

    def remove_exclusion(self, gid, uid):
        with self._lock:
            self.conn.execute(
                "DELETE FROM exclusions WHERE group_id=? AND user_id=?", (gid, uid)
            )
            self.conn.commit()

    # ── Last Check (for !shame) ──
    def save_last_check(self, gid, not_liked_names, msg_preview):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO last_check (group_id, not_liked, msg_preview) VALUES (?,?,?)",
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


db = BotDB()

# ─────────────────────────────────────────────────────────────────────
#  BOT MESSAGING
# ─────────────────────────────────────────────────────────────────────


def send_bot_message(text):
    """Send a message as the bot. Splits long messages."""
    MAX_LEN = 990  # GroupMe limit is 1000, leave margin
    chunks = []
    while len(text) > MAX_LEN:
        split_at = text.rfind("\n", 0, MAX_LEN)
        if split_at == -1:
            split_at = MAX_LEN
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    chunks.append(text)

    for chunk in chunks:
        if chunk.strip():
            requests.post(
                BOT_POST_URL, json={"bot_id": BOT_ID, "text": chunk.strip()}, timeout=10
            )


def find_member_by_name(mm, name):
    """Find a member by partial name match. Returns (user_id, nickname) or None."""
    name_lower = name.lower().strip().lstrip("@")
    # Exact match first
    for uid, nick in mm.items():
        if nick.lower() == name_lower:
            return uid, nick
    # Partial match
    for uid, nick in mm.items():
        if name_lower in nick.lower():
            return uid, nick
    return None


# ─────────────────────────────────────────────────────────────────────
#  COMMAND HANDLERS
# ─────────────────────────────────────────────────────────────────────


def cmd_help():
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


def cmd_ping():
    send_bot_message("Binger Bot is alive.")


def cmd_sirs():
    sirs = db.get_sirs(GROUP_ID)
    if not sirs:
        send_bot_message("No Sirs set. Use !addsir Name to add one.")
        return
    lines = ["CURRENT SIRS", "=" * 20]
    for i, (uid, nick) in enumerate(sirs.items(), 1):
        lines.append(f"  {i}. {nick}")
    send_bot_message("\n".join(lines))


def cmd_addsir(name):
    if not name:
        send_bot_message("Usage: !addsir Name")
        return
    mm = api.get_member_map(GROUP_ID)
    result = find_member_by_name(mm, name)
    if not result:
        send_bot_message(f'Could not find member "{name}"')
        return
    uid, nick = result
    db.add_sir(GROUP_ID, uid, nick)
    send_bot_message(f"{nick} is now a Sir.")


def cmd_removesir(name):
    if not name:
        send_bot_message("Usage: !removesir Name")
        return
    sirs = db.get_sirs(GROUP_ID)
    mm = {uid: nick for uid, nick in sirs.items()}
    result = find_member_by_name(mm, name)
    if not result:
        send_bot_message(f'"{name}" is not a Sir.')
        return
    uid, nick = result
    db.remove_sir(GROUP_ID, uid)
    send_bot_message(f"{nick} is no longer a Sir.")


def cmd_exclude(name):
    if not name:
        send_bot_message("Usage: !exclude Name")
        return
    mm = api.get_member_map(GROUP_ID)
    result = find_member_by_name(mm, name)
    if not result:
        send_bot_message(f'Could not find member "{name}"')
        return
    uid, nick = result
    db.add_exclusion(GROUP_ID, uid, nick)
    send_bot_message(f"{nick} is now excluded from checks.")


def cmd_unexclude(name):
    if not name:
        send_bot_message("Usage: !unexclude Name")
        return
    excl = db.get_exclusions(GROUP_ID)
    result = find_member_by_name(excl, name)
    if not result:
        send_bot_message(f'"{name}" is not excluded.')
        return
    uid, nick = result
    db.remove_exclusion(GROUP_ID, uid)
    send_bot_message(f"{nick} is no longer excluded.")


def cmd_check(count_str="1"):
    try:
        count = max(1, min(int(count_str), 20))
    except ValueError:
        count = 1

    sirs = db.get_sirs(GROUP_ID)
    if not sirs:
        send_bot_message("No Sirs set. Use !addsir Name first.")
        return

    sir_ids = set(sirs.keys())
    excl = set(db.get_exclusions(GROUP_ID).keys())
    mm = api.get_member_map(GROUP_ID)
    active_ids = {uid for uid in mm if uid not in excl}

    # Fetch enough messages to find N Sir messages
    msgs = api.fetch_messages(GROUP_ID, count * 10 + 50)
    sir_msgs = [m for m in msgs if m.get("user_id") in sir_ids][:count]

    if not sir_msgs:
        send_bot_message("No Sir messages found in recent history.")
        return

    all_non_likers = set()
    lines = [
        f"BINGER CHECK ({len(sir_msgs)} Sir message{'s' if len(sir_msgs) != 1 else ''})",
        "=" * 40,
    ]

    for i, msg in enumerate(sir_msgs, 1):
        liked_ids = set(msg.get("favorited_by", []))
        not_liked = [
            mm[u]
            for u in sorted(active_ids, key=lambda u: mm[u].lower())
            if u not in liked_ids and u != msg.get("user_id")
        ]
        all_non_likers.update(not_liked)

        sender = msg.get("name", "?")
        text = (msg.get("text") or "(media)")[:40]
        ts = datetime.fromtimestamp(msg.get("created_at", 0)).strftime("%m/%d %H:%M")
        total = len(active_ids) - 1  # exclude sender
        lk = total - len(not_liked)
        pct = (lk / total * 100) if total > 0 else 0

        lines.append(f'\n#{i} "{text}"')
        lines.append(f"   By {sender} on {ts} | {lk}/{total} liked ({pct:.0f}%)")
        if not_liked:
            lines.append(f"   Didn't like: {', '.join(not_liked[:10])}")
            if len(not_liked) > 10:
                lines[-1] += f" +{len(not_liked) - 10} more"
        else:
            lines.append("   Everyone liked this!")

    lines.append(
        f"\n{len(all_non_likers)} unique non-liker{'s' if len(all_non_likers) != 1 else ''} total"
    )

    # Save for !shame
    sorted_nl = sorted(all_non_likers, key=str.lower)
    preview = (sir_msgs[0].get("text") or "(media)")[:50]
    db.save_last_check(GROUP_ID, sorted_nl, preview)

    send_bot_message("\n".join(lines))


def cmd_leaderboard(count_str="200"):
    try:
        count = max(50, min(int(count_str), 2000))
    except ValueError:
        count = 200

    sirs = db.get_sirs(GROUP_ID)
    if not sirs:
        send_bot_message("No Sirs set. Use !addsir Name first.")
        return

    sir_ids = set(sirs.keys())
    excl = set(db.get_exclusions(GROUP_ID).keys())
    mm = api.get_member_map(GROUP_ID)

    msgs = api.fetch_messages(GROUP_ID, count)

    sir_msg_count = 0
    non_likes = Counter()

    for m in msgs:
        sender_id = m.get("user_id", "")
        if sender_id in sir_ids:
            sir_msg_count += 1
            fav_set = set(m.get("favorited_by", []))
            for uid in mm:
                if uid not in excl and uid != sender_id and uid not in fav_set:
                    non_likes[uid] += 1

    if sir_msg_count == 0:
        send_bot_message("No Sir messages found in the scanned range.")
        return

    sir_names = [sirs.get(uid, "?") for uid in sir_ids]
    lines = [
        f"SIR NON-LIKER LEADERBOARD",
        f"Sirs: {', '.join(sir_names)}",
        f"{sir_msg_count} Sir messages scanned ({count} total)",
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

    mm = api.get_member_map(GROUP_ID)
    result = find_member_by_name(mm, name)
    if not result:
        send_bot_message(f'Could not find member "{name}"')
        return

    uid, nick = result
    sirs = db.get_sirs(GROUP_ID)
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
    sir_pct = (sir_missed / sir_total * 100) if sir_total else 0

    lines = [f"REPORT CARD: {nick}", "=" * 30]

    if sir_ids:
        sir_liked = sir_total - sir_missed
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


def cmd_shame():
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
    "!help": lambda args: cmd_help(),
    "!ping": lambda args: cmd_ping(),
    "!sirs": lambda args: cmd_sirs(),
    "!addsir": lambda args: cmd_addsir(args),
    "!removesir": lambda args: cmd_removesir(args),
    "!exclude": lambda args: cmd_exclude(args),
    "!unexclude": lambda args: cmd_unexclude(args),
    "!check": lambda args: cmd_check(args or "1"),
    "!leaderboard": lambda args: cmd_leaderboard(args or "200"),
    "!report": lambda args: cmd_report(args),
    "!shame": lambda args: cmd_shame(),
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

    # Parse command and args
    parts = text.split(None, 1)
    cmd = parts[0].lower()
    args = parts[1].strip() if len(parts) > 1 else ""

    handler = COMMANDS.get(cmd)
    if handler:
        # Run in a thread so we don't block the callback
        threading.Thread(target=handler, args=(args,), daemon=True).start()

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
        print("  Get your token at https://dev.groupme.com")
        exit(1)
    if not BOT_ID:
        print("ERROR: Set BOT_ID environment variable")
        print("  Create a bot at https://dev.groupme.com/bots")
        exit(1)
    if not GROUP_ID:
        print("ERROR: Set GROUP_ID environment variable")
        print("  Find your group ID in the URL at web.groupme.com")
        exit(1)

    print(f"Binger Bot starting on port {PORT}...")
    print(f"  Callback URL: http://0.0.0.0:{PORT}/callback")
    print(f"  Group ID: {GROUP_ID}")
    print(f"  Bot ID: {BOT_ID[:8]}...")
    app.run(host="0.0.0.0", port=PORT)
