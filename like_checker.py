"""
Binger Like Checker
===================
A dark-mode desktop app for GroupMe power users.

Features:
  - Like Checker: see who didn't like a message, with member exclusions
  - Leaderboard: rank members by likes given & received over a message range
  - Like History: SQLite-backed tracking of check results over time
  - Analytics: group stats -- most active, most liked, activity by hour
  - Notifications: Windows toast alerts when like rate crosses a threshold

Get your API token at: https://dev.groupme.com
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext, filedialog
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import subprocess
import sys
import threading
import json
import os
import re
import sqlite3
import time
import uuid
import webbrowser
from datetime import datetime, timedelta
from collections import Counter

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

API_BASE = "https://api.groupme.com/v3"
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".binger")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
DB_FILE = os.path.join(CONFIG_DIR, "history.db")

# ── Dark Mode Color Palette ──
C = {
    "bg": "#1e1e2e",
    "bg2": "#252536",
    "bg3": "#2d2d44",
    "surface": "#313147",
    "border": "#3e3e5c",
    "text": "#e0e0ef",
    "dim": "#8888a8",
    "bright": "#ffffff",
    "accent": "#7c6ff7",
    "accent_h": "#6a5ce0",
    "green": "#50e68c",
    "green_d": "#2d8a54",
    "red": "#ff6b7a",
    "red_d": "#b34450",
    "blue": "#64b5f6",
    "orange": "#ffab40",
    "yellow": "#ffe066",
    "link": "#82aaff",
    "gold": "#ffd700",
    "silver": "#c0c0c0",
    "bronze": "#cd7f32",
}

# ─────────────────────────────────────────────────────────────────────
#  API
# ─────────────────────────────────────────────────────────────────────


class GroupMeAPI:
    def __init__(self, token):
        self.token = token
        # Persistent session with connection pooling and automatic retries
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

    def get_me(self):
        return self._get("/users/me")

    def get_groups(self, page=1, per_page=50):
        return self._get("/groups", {"page": page, "per_page": per_page})

    def get_group(self, gid):
        return self._get(f"/groups/{gid}")

    def get_messages(self, gid, before_id=None, limit=100):
        p = {"limit": limit}
        if before_id:
            p["before_id"] = before_id
        return self._get(f"/groups/{gid}/messages", p)

    def get_pinned_messages(self, gid):
        """Fetch pinned messages for a group (undocumented endpoint)."""
        try:
            return self._get(f"/conversations/{gid}/pinned_messages") or []
        except Exception:
            return []

    def send_message(self, gid, text):
        payload = {"message": {"source_guid": str(uuid.uuid4()), "text": text}}
        r = self.session.post(
            f"{API_BASE}/groups/{gid}/messages",
            params={"token": self.token},
            json=payload,
            timeout=15,
        )
        r.raise_for_status()
        return r.json()


# ─────────────────────────────────────────────────────────────────────
#  Config helpers
# ─────────────────────────────────────────────────────────────────────


def _ensure_dir():
    os.makedirs(CONFIG_DIR, exist_ok=True)


def load_config():
    try:
        _ensure_dir()
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_config(data):
    try:
        _ensure_dir()
        with open(CONFIG_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────
#  History DB
# ─────────────────────────────────────────────────────────────────────


class HistoryDB:
    def __init__(self):
        _ensure_dir()
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")  # faster concurrent reads
        self._create_tables()

    def _create_tables(self):
        try:
            # Check if the existing table has the UNIQUE constraint by looking
            # for it in the schema. If the old table exists without it, migrate.
            row = self.conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='checks'"
            ).fetchone()

            if row and "UNIQUE" not in (row[0] or ""):
                # Old table exists without UNIQUE constraint -- migrate it
                self.conn.executescript("""
                    -- Deduplicate: keep only the latest check per group+message
                    DELETE FROM checks WHERE id NOT IN (
                        SELECT MAX(id) FROM checks GROUP BY group_id, message_id
                    );

                    -- Rename old table
                    ALTER TABLE checks RENAME TO checks_old;

                    -- Create new table with UNIQUE constraint
                    CREATE TABLE checks (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts          TEXT NOT NULL,
                        group_id    TEXT NOT NULL,
                        group_name  TEXT NOT NULL,
                        message_id  TEXT NOT NULL,
                        message_text TEXT,
                        sender      TEXT,
                        total_members INTEGER,
                        liked_count INTEGER,
                        not_liked_count INTEGER,
                        like_pct    REAL,
                        liked_names TEXT,
                        not_liked_names TEXT,
                        UNIQUE(group_id, message_id)
                    );

                    -- Copy data over
                    INSERT INTO checks SELECT * FROM checks_old;

                    -- Drop old table
                    DROP TABLE checks_old;
                """)
                self.conn.commit()
            elif not row:
                # No table at all -- create fresh
                self.conn.executescript("""
                    CREATE TABLE checks (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts          TEXT NOT NULL,
                        group_id    TEXT NOT NULL,
                        group_name  TEXT NOT NULL,
                        message_id  TEXT NOT NULL,
                        message_text TEXT,
                        sender      TEXT,
                        total_members INTEGER,
                        liked_count INTEGER,
                        not_liked_count INTEGER,
                        like_pct    REAL,
                        liked_names TEXT,
                        not_liked_names TEXT,
                        UNIQUE(group_id, message_id)
                    );
                """)
                self.conn.commit()

            # Ensure indexes exist
            self.conn.executescript("""
                CREATE INDEX IF NOT EXISTS idx_checks_group ON checks(group_id);
                CREATE INDEX IF NOT EXISTS idx_checks_ts ON checks(ts);
            """)
            self.conn.commit()
        except Exception:
            # Database is corrupt -- try to delete and recreate
            try:
                self.conn.close()
            except Exception:
                pass
            try:
                os.remove(DB_FILE)
            except Exception:
                pass
            self.conn = sqlite3.connect(DB_FILE, check_same_thread=False)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript("""
                CREATE TABLE checks (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts          TEXT NOT NULL,
                    group_id    TEXT NOT NULL,
                    group_name  TEXT NOT NULL,
                    message_id  TEXT NOT NULL,
                    message_text TEXT,
                    sender      TEXT,
                    total_members INTEGER,
                    liked_count INTEGER,
                    not_liked_count INTEGER,
                    like_pct    REAL,
                    liked_names TEXT,
                    not_liked_names TEXT,
                    UNIQUE(group_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_checks_group ON checks(group_id);
                CREATE INDEX IF NOT EXISTS idx_checks_ts ON checks(ts);
            """)
            self.conn.commit()

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def save_check(
        self,
        group_id,
        group_name,
        message_id,
        message_text,
        sender,
        total_members,
        liked_count,
        not_liked_count,
        like_pct,
        liked_names,
        not_liked_names,
    ):
        with self._lock:
            self.conn.execute(
                """INSERT OR REPLACE INTO checks
                   (ts, group_id, group_name, message_id, message_text, sender,
                    total_members, liked_count, not_liked_count, like_pct,
                    liked_names, not_liked_names)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    datetime.now().isoformat(),
                    group_id,
                    group_name,
                    message_id,
                    message_text,
                    sender,
                    total_members,
                    liked_count,
                    not_liked_count,
                    like_pct,
                    json.dumps(liked_names),
                    json.dumps(not_liked_names),
                ),
            )
            self.conn.commit()

    def get_history(self, group_id=None, limit=100):
        with self._lock:
            if group_id:
                rows = self.conn.execute(
                    "SELECT * FROM checks WHERE group_id=? ORDER BY ts DESC LIMIT ?",
                    (group_id, limit),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM checks ORDER BY ts DESC LIMIT ?", (limit,)
                ).fetchall()
        return rows

    def get_repeat_offenders(self, group_id, limit=20):
        """Members who appear most often in not_liked_names."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT not_liked_names FROM checks WHERE group_id=?", (group_id,)
            ).fetchall()
        counter = Counter()
        for (names_json,) in rows:
            try:
                for n in json.loads(names_json):
                    counter[n] += 1
            except Exception:
                pass
        return counter.most_common(limit)


# ─────────────────────────────────────────────────────────────────────
#  Notifications (cross-platform)
# ─────────────────────────────────────────────────────────────────────


# Cache notification library availability
_notif_lib = None
_notif_checked = False


def _sanitize_shell(s):
    """Remove characters that could break shell strings."""
    return re.sub(r'["`$\r\n\\\']', "", str(s))


def send_toast(title, message):
    """Best-effort cross-platform desktop notification."""
    global _notif_lib, _notif_checked

    # Try winotify on Windows (cached check)
    if IS_WINDOWS and not _notif_checked:
        try:
            from winotify import Notification

            _notif_lib = Notification
        except ImportError:
            _notif_lib = None
        _notif_checked = True

    if IS_WINDOWS and _notif_lib is not None:
        try:
            n = _notif_lib(app_id="Binger Like Checker", title=title, msg=message)
            n.show()
            return True
        except Exception:
            pass

    # ── Windows fallback: PowerShell toast ──
    if IS_WINDOWS:
        try:
            safe_title = _sanitize_shell(title)
            safe_msg = _sanitize_shell(message)
            ps = (
                f"[Windows.UI.Notifications.ToastNotificationManager, "
                f"Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null; "
                f"$xml = [Windows.UI.Notifications.ToastNotificationManager]"
                f"::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]"
                f"::ToastText02); "
                f'$texts = $xml.GetElementsByTagName("text"); '
                f'$texts[0].AppendChild($xml.CreateTextNode("{safe_title}")) | Out-Null; '
                f'$texts[1].AppendChild($xml.CreateTextNode("{safe_msg}")) | Out-Null; '
                f"$toast = [Windows.UI.Notifications.ToastNotification]::new($xml); "
                f"[Windows.UI.Notifications.ToastNotificationManager]"
                f'::CreateToastNotifier("Binger Like Checker").Show($toast)'
            )
            subprocess.Popen(["powershell", "-Command", ps], creationflags=0x08000000)
            return True
        except Exception:
            pass

    # ── macOS: osascript notification ──
    if IS_MAC:
        try:
            safe_title = _sanitize_shell(title)
            safe_msg = _sanitize_shell(message)
            script = (
                f'display notification "{safe_msg}" '
                f'with title "Binger Like Checker" subtitle "{safe_title}"'
            )
            subprocess.Popen(["osascript", "-e", script])
            return True
        except Exception:
            pass

    # ── Linux: notify-send ──
    if IS_LINUX:
        try:
            subprocess.Popen(
                ["notify-send", "Binger Like Checker", f"{title}\n{message}"]
            )
            return True
        except Exception:
            pass

    return False


# ─────────────────────────────────────────────────────────────────────
#  Bulk message fetcher (used by leaderboard & analytics)
# ─────────────────────────────────────────────────────────────────────


def fetch_messages_bulk(api, group_id, count, progress_cb=None):
    """Fetch up to `count` messages, calling progress_cb(loaded_so_far)."""
    all_msgs = []
    before_id = None
    remaining = count
    while remaining > 0:
        batch_size = min(remaining, 100)
        result = api.get_messages(group_id, before_id=before_id, limit=batch_size)
        if not result or not result.get("messages"):
            break
        batch = result["messages"]
        all_msgs.extend(batch)
        remaining -= len(batch)
        before_id = batch[-1]["id"]
        if progress_cb:
            progress_cb(len(all_msgs))
        if len(batch) < batch_size:
            break
    return all_msgs


# ═════════════════════════════════════════════════════════════════════
#  MAIN APP
# ═════════════════════════════════════════════════════════════════════


class BingerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Binger Like Checker")
        self.root.minsize(780, 700)
        self.root.configure(bg=C["bg"])

        self.api = None
        self.groups = []
        self.messages = []
        self._display_messages = []
        self.selected_group = None
        self.selected_message = None
        self.user_name = None
        # Feature 11: per-group exclusions -- dict of group_id -> set(user_ids)
        self.excluded_ids = {}
        self._msg_cache = {}  # group_id -> (timestamp, messages) for reuse
        self._cache_ttl = 120  # seconds before cache is stale
        self.db = HistoryDB()
        # Bug 3: initialize _last_not_liked and _last_msg_text
        self._last_not_liked = None
        self._last_msg_text = ""
        # Feature 8: debounce timer id for search filter
        self._filter_after_id = None

        self._apply_theme()
        self._build_ui()
        self._load_saved_config()

        # Feature 13: restore saved window geometry
        cfg = load_config()
        saved_geom = cfg.get("geometry", "")
        if saved_geom:
            try:
                self.root.geometry(saved_geom)
            except Exception:
                self.root.geometry("900x850")
        else:
            self.root.geometry("900x850")

        # Feature 12: keyboard shortcuts
        self.root.bind("<F5>", self._on_f5)
        self.root.bind("<Control-Shift-C>", self._on_ctrl_shift_c)
        self.root.bind("<Control-Shift-c>", self._on_ctrl_shift_c)

    # ──────────────── THEME ────────────────
    def _apply_theme(self):
        s = ttk.Style()
        s.theme_use("clam")

        s.configure("TFrame", background=C["bg"])
        s.configure(
            "TLabel", background=C["bg"], foreground=C["text"], font=("Segoe UI", 10)
        )
        s.configure(
            "Header.TLabel",
            background=C["bg"],
            foreground=C["bright"],
            font=("Segoe UI", 16, "bold"),
        )
        s.configure(
            "Sub.TLabel", background=C["bg"], foreground=C["dim"], font=("Segoe UI", 9)
        )
        s.configure(
            "Accent.TLabel",
            background=C["bg"],
            foreground=C["accent"],
            font=("Segoe UI", 10, "bold"),
        )

        s.configure(
            "TLabelframe",
            background=C["bg"],
            foreground=C["dim"],
            bordercolor=C["border"],
            relief="flat",
        )
        s.configure(
            "TLabelframe.Label",
            background=C["bg"],
            foreground=C["accent"],
            font=("Segoe UI", 10, "bold"),
        )

        s.configure("TNotebook", background=C["bg"], borderwidth=0)
        s.configure(
            "TNotebook.Tab",
            background=C["surface"],
            foreground=C["dim"],
            font=("Segoe UI", 10, "bold"),
            padding=(14, 6),
        )
        s.map(
            "TNotebook.Tab",
            background=[("selected", C["accent"])],
            foreground=[("selected", C["bright"])],
        )

        s.configure(
            "TButton",
            background=C["accent"],
            foreground=C["bright"],
            font=("Segoe UI", 10, "bold"),
            borderwidth=0,
            padding=(12, 6),
        )
        s.map(
            "TButton",
            background=[("active", C["accent_h"]), ("disabled", C["surface"])],
            foreground=[("disabled", C["dim"])],
        )
        s.configure(
            "Action.TButton",
            background=C["green_d"],
            foreground=C["bright"],
            font=("Segoe UI", 11, "bold"),
            padding=(16, 8),
        )
        s.map(
            "Action.TButton",
            background=[("active", C["green"]), ("disabled", C["surface"])],
            foreground=[("disabled", C["dim"])],
        )
        s.configure("Small.TButton", font=("Segoe UI", 9), padding=(8, 4))
        s.configure(
            "Danger.TButton",
            background=C["red_d"],
            foreground=C["bright"],
            font=("Segoe UI", 9, "bold"),
            padding=(8, 4),
        )
        s.map(
            "Danger.TButton",
            background=[("active", C["red"]), ("disabled", C["surface"])],
            foreground=[("disabled", C["dim"])],
        )

        s.configure(
            "TEntry",
            fieldbackground=C["surface"],
            foreground=C["text"],
            insertcolor=C["text"],
            bordercolor=C["border"],
        )
        s.map(
            "TEntry",
            fieldbackground=[("focus", C["bg3"])],
            bordercolor=[("focus", C["accent"])],
        )
        s.configure(
            "TCombobox",
            fieldbackground=C["surface"],
            foreground=C["text"],
            background=C["surface"],
            bordercolor=C["border"],
            arrowcolor=C["accent"],
        )
        s.map(
            "TCombobox",
            fieldbackground=[("focus", C["bg3"])],
            bordercolor=[("focus", C["accent"])],
        )
        s.configure(
            "TCheckbutton",
            background=C["bg"],
            foreground=C["dim"],
            font=("Segoe UI", 9),
        )
        s.configure(
            "TSpinbox",
            fieldbackground=C["surface"],
            foreground=C["text"],
            bordercolor=C["border"],
            arrowcolor=C["accent"],
        )
        s.configure(
            "Accent.Horizontal.TProgressbar",
            background=C["accent"],
            troughcolor=C["surface"],
            bordercolor=C["border"],
            thickness=4,
        )

    # ──────────────── BUILD UI ────────────────
    def _build_ui(self):
        top = ttk.Frame(self.root, padding=(20, 12, 20, 0))
        top.pack(fill=tk.X)

        # Header row
        hdr = ttk.Frame(top)
        hdr.pack(fill=tk.X, pady=(0, 2))
        ttk.Label(hdr, text="Binger Like Checker", style="Header.TLabel").pack(
            side=tk.LEFT
        )
        self.user_label_var = tk.StringVar()
        ttk.Label(hdr, textvariable=self.user_label_var, style="Accent.TLabel").pack(
            side=tk.RIGHT
        )
        self.aot_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            hdr, text="Always on Top", variable=self.aot_var, command=self._toggle_aot
        ).pack(side=tk.RIGHT, padx=(0, 12))
        ttk.Label(top, text="GroupMe like analysis toolkit", style="Sub.TLabel").pack(
            anchor=tk.W, pady=(0, 6)
        )

        # ── Auth row (always visible) ──
        auth = ttk.LabelFrame(top, text="AUTHENTICATION", padding=(12, 6))
        auth.pack(fill=tk.X, pady=(0, 6))
        ar = ttk.Frame(auth)
        ar.pack(fill=tk.X)
        ttk.Label(ar, text="API Token").pack(side=tk.LEFT, padx=(0, 8))
        self.token_var = tk.StringVar()
        self.token_entry = ttk.Entry(
            ar, textvariable=self.token_var, show="*", width=40
        )
        self.token_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        self.show_tok = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            ar,
            text="Show",
            variable=self.show_tok,
            command=lambda: self.token_entry.config(
                show="" if self.show_tok.get() else "*"
            ),
        ).pack(side=tk.LEFT, padx=(0, 4))
        self.save_tok = tk.BooleanVar(value=True)
        ttk.Checkbutton(ar, text="Remember", variable=self.save_tok).pack(
            side=tk.LEFT, padx=(0, 8)
        )
        # Feature 15: connect/disconnect button
        self.connect_btn = ttk.Button(ar, text="Connect", command=self._connect)
        self.connect_btn.pack(side=tk.LEFT)
        ah = ttk.Frame(auth)
        ah.pack(fill=tk.X, pady=(4, 0))
        lnk = ttk.Label(
            ah,
            text="Get your token at dev.groupme.com",
            foreground=C["link"],
            cursor="hand2",
            font=("Segoe UI", 9, "underline"),
            background=C["bg"],
        )
        lnk.pack(side=tk.LEFT)
        lnk.bind("<Button-1>", lambda e: webbrowser.open("https://dev.groupme.com"))

        # ── Group selector (always visible) ──
        gs = ttk.LabelFrame(top, text="SELECT GROUP", padding=(12, 6))
        gs.pack(fill=tk.X, pady=(0, 6))
        gr = ttk.Frame(gs)
        gr.pack(fill=tk.X)
        self.group_combo = ttk.Combobox(gr, state="disabled", width=52)
        self.group_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        self.group_combo.bind("<<ComboboxSelected>>", self._on_group_selected)
        self.member_count_var = tk.StringVar()
        ttk.Label(gr, textvariable=self.member_count_var, style="Sub.TLabel").pack(
            side=tk.LEFT, padx=(0, 8)
        )
        self.excl_btn = ttk.Button(
            gr,
            text="Exclusions",
            style="Small.TButton",
            command=self._open_exclusions,
            state="disabled",
        )
        self.excl_btn.pack(side=tk.LEFT)
        # Feature 14: exclusion count indicator
        self.excl_count_var = tk.StringVar(value="")
        ttk.Label(gr, textvariable=self.excl_count_var, style="Sub.TLabel").pack(
            side=tk.LEFT, padx=(4, 0)
        )

        # ── Progress bar ──
        self.progress = ttk.Progressbar(
            top, mode="indeterminate", style="Accent.Horizontal.TProgressbar"
        )

        # ── Tabs ──
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=20, pady=(6, 0))

        self._build_checker_tab()
        self._build_leaderboard_tab()
        self._build_history_tab()
        self._build_analytics_tab()
        self._build_notifications_tab()

        # ── Status bar ──
        sf = tk.Frame(self.root, bg=C["bg2"], height=26)
        sf.pack(fill=tk.X, side=tk.BOTTOM)
        sf.pack_propagate(False)
        self.status_var = tk.StringVar(value="Enter your API token and click Connect.")
        tk.Label(
            sf,
            textvariable=self.status_var,
            bg=C["bg2"],
            fg=C["dim"],
            font=("Segoe UI", 9),
            anchor=tk.W,
            padx=10,
        ).pack(fill=tk.BOTH, expand=True)

    # ─── Helper: dark scrolled text ───
    def _make_text(self, parent, height=10):
        t = scrolledtext.ScrolledText(
            parent,
            font=("Consolas", 10),
            wrap=tk.WORD,
            height=height,
            state="disabled",
            bg=C["bg2"],
            fg=C["text"],
            insertbackground=C["text"],
            selectbackground=C["accent"],
            selectforeground=C["bright"],
            borderwidth=0,
            highlightthickness=1,
            highlightcolor=C["border"],
            highlightbackground=C["border"],
        )
        for tag, kw in [
            ("header", {"font": ("Segoe UI", 11, "bold"), "foreground": C["bright"]}),
            ("liked", {"foreground": C["green"]}),
            ("not_liked", {"foreground": C["red"], "font": ("Consolas", 10, "bold")}),
            ("info", {"foreground": C["blue"]}),
            ("sep", {"foreground": C["border"]}),
            ("stat", {"foreground": C["orange"], "font": ("Consolas", 10, "bold")}),
            ("pct", {"foreground": C["yellow"], "font": ("Segoe UI", 11, "bold")}),
            ("gold", {"foreground": C["gold"], "font": ("Consolas", 10, "bold")}),
            ("silver", {"foreground": C["silver"], "font": ("Consolas", 10, "bold")}),
            ("bronze", {"foreground": C["bronze"], "font": ("Consolas", 10, "bold")}),
            ("dim", {"foreground": C["dim"]}),
        ]:
            t.tag_configure(tag, **kw)
        return t

    def _tw(self, widget, text, tag=None):
        """Write a single chunk to a text widget. For bulk writes, use _tw_batch."""
        widget.config(state="normal")
        widget.insert(tk.END, text, tag if tag else ())
        widget.config(state="disabled")
        widget.see(tk.END)

    def _tw_batch(self, widget, chunks):
        """Write many (text, tag) chunks at once -- much faster than individual _tw calls.
        chunks: list of (text, tag_or_None) tuples."""
        widget.config(state="normal")
        for text, tag in chunks:
            widget.insert(tk.END, text, tag if tag else ())
        widget.config(state="disabled")
        widget.see(tk.END)

    def _tc(self, widget):
        widget.config(state="normal")
        widget.delete("1.0", tk.END)
        widget.config(state="disabled")

    # ════════════════════════════════════════════════════════
    #  TAB 1: LIKE CHECKER
    # ════════════════════════════════════════════════════════
    def _build_checker_tab(self):
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="  Like Checker  ")

        # Message controls
        mf = ttk.LabelFrame(tab, text="SELECT MESSAGE", padding=(12, 8))
        mf.pack(fill=tk.X, pady=(0, 8))

        mt = ttk.Frame(mf)
        mt.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(mt, text="Load:").pack(side=tk.LEFT, padx=(0, 4))
        self.msg_limit_var = tk.StringVar(value="100")
        ttk.Spinbox(
            mt, from_=20, to=500, increment=20, textvariable=self.msg_limit_var, width=5
        ).pack(side=tk.LEFT, padx=(0, 6))
        self.load_msgs_btn = ttk.Button(
            mt, text="Load Messages", command=self._load_messages, state="disabled"
        )
        self.load_msgs_btn.pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(mt, text="Search:").pack(side=tk.LEFT, padx=(0, 4))
        self.search_var = tk.StringVar()
        # Feature 8: debounced search filter
        self.search_var.trace_add("write", self._on_search_changed)
        ttk.Entry(mt, textvariable=self.search_var, width=24).pack(
            side=tk.LEFT, fill=tk.X, expand=True
        )
        self.msg_count_var = tk.StringVar()
        ttk.Label(mt, textvariable=self.msg_count_var, style="Sub.TLabel").pack(
            side=tk.RIGHT, padx=(8, 0)
        )

        mlf = ttk.Frame(mf)
        mlf.pack(fill=tk.BOTH, expand=True)
        self.msg_listbox = tk.Listbox(
            mlf,
            height=6,
            font=("Consolas", 9),
            selectmode=tk.SINGLE,
            bg=C["surface"],
            fg=C["text"],
            selectbackground=C["accent"],
            selectforeground=C["bright"],
            borderwidth=0,
            highlightthickness=1,
            highlightcolor=C["border"],
            highlightbackground=C["border"],
        )
        sb = ttk.Scrollbar(mlf, orient=tk.VERTICAL, command=self.msg_listbox.yview)
        self.msg_listbox.configure(yscrollcommand=sb.set)
        self.msg_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.msg_listbox.bind("<<ListboxSelect>>", self._on_message_selected)
        # Feature 12: bind Return on listbox to check likes
        self.msg_listbox.bind("<Return>", lambda e: self._check_likes())

        # Action row
        ar = ttk.Frame(tab)
        ar.pack(fill=tk.X, pady=6)
        self.check_btn = ttk.Button(
            ar,
            text="Check Who Didn't Like It",
            style="Action.TButton",
            command=self._check_likes,
            state="disabled",
        )
        self.check_btn.pack(side=tk.LEFT)
        self.pinned_btn = ttk.Button(
            ar,
            text="Pinned Msgs",
            style="Small.TButton",
            command=self._check_pinned,
            state="disabled",
        )
        self.pinned_btn.pack(side=tk.LEFT, padx=(8, 0))
        rr = ttk.Frame(ar)
        rr.pack(side=tk.RIGHT)
        self.copy_btn = ttk.Button(
            rr,
            text="Copy",
            style="Small.TButton",
            command=self._copy_results,
            state="disabled",
        )
        self.copy_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.export_btn = ttk.Button(
            rr,
            text="Export",
            style="Small.TButton",
            command=self._export_results,
            state="disabled",
        )
        self.export_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.shame_btn = ttk.Button(
            rr,
            text="Send Shame List",
            style="Danger.TButton",
            command=self._send_shame_message,
            state="disabled",
        )
        self.shame_btn.pack(side=tk.LEFT)

        # Results
        rf = ttk.LabelFrame(tab, text="RESULTS", padding=(12, 8))
        rf.pack(fill=tk.BOTH, expand=True)
        self.results_text = self._make_text(rf, height=12)
        self.results_text.pack(fill=tk.BOTH, expand=True)

    # ════════════════════════════════════════════════════════
    #  TAB 2: LEADERBOARD
    # ════════════════════════════════════════════════════════
    def _build_leaderboard_tab(self):
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="  Leaderboard  ")

        cf = ttk.Frame(tab)
        cf.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(cf, text="Scan last").pack(side=tk.LEFT, padx=(0, 4))
        self.lb_count_var = tk.StringVar(value="200")
        ttk.Spinbox(
            cf, from_=50, to=2000, increment=50, textvariable=self.lb_count_var, width=6
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Label(cf, text="messages").pack(side=tk.LEFT, padx=(0, 12))
        self.lb_run_btn = ttk.Button(
            cf,
            text="Build Leaderboard",
            command=self._run_leaderboard,
            state="disabled",
        )
        self.lb_run_btn.pack(side=tk.LEFT)
        self.lb_report_btn = ttk.Button(
            cf,
            text="Member Report",
            style="Small.TButton",
            command=self._open_member_report,
            state="disabled",
        )
        self.lb_report_btn.pack(side=tk.LEFT, padx=(8, 0))
        # Feature 10: Copy and Export buttons on Leaderboard tab
        self.lb_copy_btn = ttk.Button(
            cf,
            text="Copy",
            style="Small.TButton",
            command=self._copy_lb,
            state="disabled",
        )
        self.lb_copy_btn.pack(side=tk.LEFT, padx=(8, 4))
        self.lb_export_btn = ttk.Button(
            cf,
            text="Export",
            style="Small.TButton",
            command=self._export_lb,
            state="disabled",
        )
        self.lb_export_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.lb_status_var = tk.StringVar()
        ttk.Label(cf, textvariable=self.lb_status_var, style="Sub.TLabel").pack(
            side=tk.RIGHT
        )

        rf = ttk.LabelFrame(tab, text="LEADERBOARD", padding=(12, 8))
        rf.pack(fill=tk.BOTH, expand=True)
        self.lb_text = self._make_text(rf, height=20)
        self.lb_text.pack(fill=tk.BOTH, expand=True)

    # ════════════════════════════════════════════════════════
    #  TAB 3: HISTORY
    # ════════════════════════════════════════════════════════
    def _build_history_tab(self):
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="  History  ")

        cf = ttk.Frame(tab)
        cf.pack(fill=tk.X, pady=(0, 8))
        self.hist_refresh_btn = ttk.Button(
            cf, text="Refresh History", command=self._refresh_history, state="disabled"
        )
        self.hist_refresh_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.hist_offenders_btn = ttk.Button(
            cf,
            text="Repeat Offenders",
            style="Danger.TButton",
            command=self._show_offenders,
            state="disabled",
        )
        self.hist_offenders_btn.pack(side=tk.LEFT)

        rf = ttk.LabelFrame(tab, text="CHECK HISTORY", padding=(12, 8))
        rf.pack(fill=tk.BOTH, expand=True)
        self.hist_text = self._make_text(rf, height=20)
        self.hist_text.pack(fill=tk.BOTH, expand=True)

    # ════════════════════════════════════════════════════════
    #  TAB 4: ANALYTICS
    # ════════════════════════════════════════════════════════
    def _build_analytics_tab(self):
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="  Analytics  ")

        cf = ttk.Frame(tab)
        cf.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(cf, text="Analyze last").pack(side=tk.LEFT, padx=(0, 4))
        self.an_count_var = tk.StringVar(value="300")
        ttk.Spinbox(
            cf, from_=50, to=2000, increment=50, textvariable=self.an_count_var, width=6
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Label(cf, text="messages").pack(side=tk.LEFT, padx=(0, 12))
        self.an_run_btn = ttk.Button(
            cf, text="Run Analytics", command=self._run_analytics, state="disabled"
        )
        self.an_run_btn.pack(side=tk.LEFT)
        # Feature 10: Copy and Export buttons on Analytics tab
        self.an_copy_btn = ttk.Button(
            cf,
            text="Copy",
            style="Small.TButton",
            command=self._copy_an,
            state="disabled",
        )
        self.an_copy_btn.pack(side=tk.LEFT, padx=(8, 4))
        self.an_export_btn = ttk.Button(
            cf,
            text="Export",
            style="Small.TButton",
            command=self._export_an,
            state="disabled",
        )
        self.an_export_btn.pack(side=tk.LEFT, padx=(0, 4))
        self.an_status_var = tk.StringVar()
        ttk.Label(cf, textvariable=self.an_status_var, style="Sub.TLabel").pack(
            side=tk.RIGHT
        )

        rf = ttk.LabelFrame(tab, text="GROUP ANALYTICS", padding=(12, 8))
        rf.pack(fill=tk.BOTH, expand=True)
        self.an_text = self._make_text(rf, height=20)
        self.an_text.pack(fill=tk.BOTH, expand=True)

    # ════════════════════════════════════════════════════════
    #  TAB 5: NOTIFICATIONS
    # ════════════════════════════════════════════════════════
    def _build_notifications_tab(self):
        tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(tab, text="  Notifications  ")

        info = ttk.LabelFrame(tab, text="LIKE RATE ALERTS", padding=(12, 10))
        info.pack(fill=tk.X, pady=(0, 8))

        r1 = ttk.Frame(info)
        r1.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(r1, text="Alert when like rate falls below:").pack(
            side=tk.LEFT, padx=(0, 6)
        )
        self.notif_threshold_var = tk.StringVar(value="50")
        ttk.Spinbox(
            r1,
            from_=5,
            to=100,
            increment=5,
            textvariable=self.notif_threshold_var,
            width=5,
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Label(r1, text="%").pack(side=tk.LEFT, padx=(0, 12))

        r2 = ttk.Frame(info)
        r2.pack(fill=tk.X, pady=(0, 6))
        self.notif_enabled_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            r2,
            text="Enable notifications after each like check",
            variable=self.notif_enabled_var,
        ).pack(side=tk.LEFT)

        r3 = ttk.Frame(info)
        r3.pack(fill=tk.X)
        ttk.Button(
            r3,
            text="Send Test Notification",
            style="Small.TButton",
            command=self._test_notification,
        ).pack(side=tk.LEFT, padx=(0, 8))
        self.notif_test_var = tk.StringVar()
        ttk.Label(r3, textvariable=self.notif_test_var, style="Sub.TLabel").pack(
            side=tk.LEFT
        )

        log_frame = ttk.LabelFrame(tab, text="NOTIFICATION LOG", padding=(12, 8))
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.notif_text = self._make_text(log_frame, height=12)
        self.notif_text.pack(fill=tk.BOTH, expand=True)

    # ──────────────── ALWAYS ON TOP ────────────────
    def _toggle_aot(self):
        self.root.attributes("-topmost", self.aot_var.get())

    # ──────────────── STATUS / PROGRESS ────────────────
    def _status(self, text):
        self.status_var.set(text)
        self.root.update_idletasks()

    def _show_progress(self):
        self.progress.pack(fill=tk.X, pady=(0, 4))
        self.progress.start(12)

    def _hide_progress(self):
        self.progress.stop()
        self.progress.pack_forget()

    # ──────────────── CONFIG ────────────────
    def _load_saved_config(self):
        cfg = load_config()
        t = cfg.get("token", "")
        if t:
            self.token_var.set(t)
            self.save_tok.set(True)
        # Feature 11: load per-group exclusions
        excl_by_group = cfg.get("excluded_by_group", {})
        if excl_by_group:
            self.excluded_ids = {gid: set(uids) for gid, uids in excl_by_group.items()}
        else:
            # No new key -- start fresh (ignore old "excluded_user_ids")
            self.excluded_ids = {}
        if cfg.get("notif_enabled"):
            self.notif_enabled_var.set(True)
        if cfg.get("notif_threshold"):
            self.notif_threshold_var.set(str(cfg["notif_threshold"]))

    def _save_cfg(self):
        d = {}
        if self.save_tok.get():
            d["token"] = self.token_var.get().strip()
        # Feature 11: save per-group exclusions
        d["excluded_by_group"] = {
            gid: list(uids) for gid, uids in self.excluded_ids.items()
        }
        d["notif_enabled"] = self.notif_enabled_var.get()
        try:
            d["notif_threshold"] = int(self.notif_threshold_var.get())
        except ValueError:
            d["notif_threshold"] = 50
        # Feature 13: save window geometry
        try:
            d["geometry"] = self.root.geometry()
        except Exception:
            pass
        save_config(d)

    # ──────────────── CONNECT / DISCONNECT ────────────────
    def _connect(self):
        # Feature 15: if already connected, disconnect instead
        if self.api is not None:
            self._disconnect()
            return

        token = self.token_var.get().strip()
        if not token:
            messagebox.showwarning(
                "Missing Token", "Please enter your GroupMe API token."
            )
            return
        self.connect_btn.config(state="disabled")
        self._status("Connecting...")
        self._show_progress()

        def work():
            try:
                self.api = GroupMeAPI(token)
                me = self.api.get_me()
                name = me.get("name", "Unknown")
                groups = []
                page = 1
                while True:
                    batch = self.api.get_groups(page=page, per_page=50)
                    if not batch:
                        break
                    groups.extend(batch)
                    if len(batch) < 50:
                        break
                    page += 1
                self.groups = groups
                self.user_name = name
                self.root.after(0, lambda: self._on_connected(name))
            except requests.exceptions.HTTPError as e:
                msg = (
                    "Invalid API token."
                    if (e.response is not None and e.response.status_code == 401)
                    else str(e)
                )
                self.root.after(0, lambda: messagebox.showerror("Error", msg))
                self.root.after(0, lambda: self.connect_btn.config(state="normal"))
                self.root.after(0, lambda: self._status("Connection failed."))
                self.root.after(0, self._hide_progress)
                self.root.after(0, lambda: setattr(self, "api", None))
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Error", str(e)))
                self.root.after(0, lambda: self.connect_btn.config(state="normal"))
                self.root.after(0, lambda: self._status("Connection failed."))
                self.root.after(0, self._hide_progress)
                self.root.after(0, lambda: setattr(self, "api", None))

        threading.Thread(target=work, daemon=True).start()

    def _on_connected(self, name):
        self._hide_progress()
        self._save_cfg()
        self._status(f"Connected as {name}  |  {len(self.groups)} group(s)")
        self.user_label_var.set(f"Connected: {name}")
        # Feature 15: show Disconnect label
        self.connect_btn.config(text="Disconnect", state="normal")
        vals = [f"{g['name']}  ({len(g.get('members', []))})" for g in self.groups]
        self.group_combo["values"] = vals
        self.group_combo.config(state="readonly")
        if vals:
            self.group_combo.current(0)
            self._on_group_selected(None)

    # Feature 15: disconnect method
    def _disconnect(self):
        self.api = None
        self.groups = []
        self.messages = []
        self._display_messages = []
        self.selected_group = None
        self.selected_message = None
        self.user_name = None
        self._msg_cache = {}
        self._last_not_liked = None
        self._last_msg_text = ""

        # Reset UI
        self.connect_btn.config(text="Connect", state="normal")
        self.user_label_var.set("")
        self.group_combo.set("")
        self.group_combo["values"] = []
        self.group_combo.config(state="disabled")
        self.member_count_var.set("")
        self.excl_count_var.set("")

        # Disable buttons
        self.load_msgs_btn.config(state="disabled")
        self.excl_btn.config(state="disabled")
        self.pinned_btn.config(state="disabled")
        self.check_btn.config(state="disabled")
        self.copy_btn.config(state="disabled")
        self.export_btn.config(state="disabled")
        self.shame_btn.config(state="disabled")
        self.lb_run_btn.config(state="disabled")
        self.lb_report_btn.config(state="disabled")
        self.lb_copy_btn.config(state="disabled")
        self.lb_export_btn.config(state="disabled")
        self.an_run_btn.config(state="disabled")
        self.an_copy_btn.config(state="disabled")
        self.an_export_btn.config(state="disabled")
        self.hist_refresh_btn.config(state="disabled")
        self.hist_offenders_btn.config(state="disabled")

        # Clear listbox
        self.msg_listbox.delete(0, tk.END)
        self.msg_count_var.set("")

        self._status("Disconnected.")

    # ──────────────── GROUP SELECTION ────────────────
    def _on_group_selected(self, event):
        idx = self.group_combo.current()
        if idx < 0:
            return
        self.selected_group = self.groups[idx]
        mc = len(self.selected_group.get("members", []))
        self.member_count_var.set(f"{mc} members")
        self._status(f"Group: {self.selected_group['name']}  |  {mc} members")
        self.load_msgs_btn.config(state="normal")
        self.excl_btn.config(state="normal")
        self.pinned_btn.config(state="normal")
        self.lb_run_btn.config(state="normal")
        self.lb_report_btn.config(state="normal")
        self.an_run_btn.config(state="normal")
        self.hist_refresh_btn.config(state="normal")
        self.hist_offenders_btn.config(state="normal")
        self.msg_listbox.delete(0, tk.END)
        self.messages = []
        self._display_messages = []
        self.check_btn.config(state="disabled")
        self.copy_btn.config(state="disabled")
        self.export_btn.config(state="disabled")
        self.shame_btn.config(state="disabled")
        # Feature 14: update exclusion count indicator on group switch
        self._update_excl_count()
        # Bug 2: refresh group data in background when group is selected
        self._refresh_group_data()

    # Feature 14: update exclusion count label
    def _update_excl_count(self):
        if self.selected_group:
            gid = self.selected_group["id"]
            n = len(self.excluded_ids.get(gid, set()))
            if n > 0:
                self.excl_count_var.set(f"({n} excluded)")
            else:
                self.excl_count_var.set("")
        else:
            self.excl_count_var.set("")

    # ──────────────── EXCLUSIONS DIALOG ────────────────
    def _open_exclusions(self):
        if not self.selected_group:
            return
        members = self.selected_group.get("members", [])
        if not members:
            messagebox.showinfo("No Members", "This group has no members.")
            return

        gid = self.selected_group["id"]
        current_excluded = self.excluded_ids.get(gid, set())

        dlg = tk.Toplevel(self.root)
        dlg.title("Member Exclusions")
        dlg.geometry("400x520")
        dlg.configure(bg=C["bg"])
        dlg.transient(self.root)
        dlg.grab_set()

        ttk.Label(dlg, text="Exclude Members", style="Header.TLabel").pack(
            pady=(12, 2), padx=16
        )
        ttk.Label(
            dlg,
            text="Checked members will be EXCLUDED from like checks",
            style="Sub.TLabel",
        ).pack(padx=16, pady=(0, 8))

        # Scrollable checkbox list
        canvas_frame = ttk.Frame(dlg)
        canvas_frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 8))

        canvas = tk.Canvas(canvas_frame, bg=C["bg"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(
            canvas_frame, orient=tk.VERTICAL, command=canvas.yview
        )
        inner = ttk.Frame(canvas)
        inner.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        check_vars = {}
        for m in sorted(members, key=lambda x: x.get("nickname", "").lower()):
            uid = m.get("user_id", "")
            nick = m.get("nickname", "Unknown")
            var = tk.BooleanVar(value=(uid in current_excluded))
            check_vars[uid] = var
            cb = ttk.Checkbutton(inner, text=nick, variable=var)
            cb.pack(anchor=tk.W, pady=1)

        # Buttons
        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(fill=tk.X, padx=16, pady=(0, 12))

        def select_all():
            for v in check_vars.values():
                v.set(True)

        def select_none():
            for v in check_vars.values():
                v.set(False)

        def apply_excl():
            self.excluded_ids[gid] = {uid for uid, v in check_vars.items() if v.get()}
            self._save_cfg()
            n = len(self.excluded_ids[gid])
            self._status(f"Exclusions updated: {n} member(s) excluded")
            # Feature 14: update count indicator
            self._update_excl_count()
            dlg.destroy()

        ttk.Button(
            btn_frame, text="All", style="Small.TButton", command=select_all
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            btn_frame, text="None", style="Small.TButton", command=select_none
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(btn_frame, text="Apply", command=apply_excl).pack(side=tk.RIGHT)

    # ──────────────── MESSAGE CACHE ────────────────
    def _get_cached_or_fetch(self, gid, count, progress_cb=None):
        """Return cached messages if fresh enough and large enough, else fetch."""
        cached = self._msg_cache.get(gid)
        if cached:
            ts, msgs = cached
            if time.time() - ts < self._cache_ttl and len(msgs) >= count:
                return msgs[:count]
        msgs = fetch_messages_bulk(self.api, gid, count, progress_cb)
        self._msg_cache[gid] = (time.time(), msgs)
        return msgs

    # ──────────────── REFRESH GROUP DATA (background) ────────────────
    # Bug 2: separate method to refresh group data in background
    def _refresh_group_data(self):
        """Fetch fresh group data in background and update cached selected_group."""
        if not self.api or not self.selected_group:
            return
        gid = self.selected_group["id"]
        idx = self.group_combo.current()

        def work():
            try:
                fg = self.api.get_group(gid)
                if fg and fg.get("members"):
                    self.root.after(0, lambda: self._apply_refreshed_group(fg, idx))
            except Exception:
                pass

        threading.Thread(target=work, daemon=True).start()

    def _apply_refreshed_group(self, fg, idx):
        """Apply refreshed group data on the main thread."""
        self.selected_group = fg
        if idx >= 0 and idx < len(self.groups):
            self.groups[idx] = fg
        mc = len(fg.get("members", []))
        self.member_count_var.set(f"{mc} members")

    # ──────────────── LOAD MESSAGES ────────────────
    def _load_messages(self):
        if not self.selected_group:
            return
        self.load_msgs_btn.config(state="disabled")
        self._status("Loading messages...")
        self._show_progress()
        gid = self.selected_group["id"]
        try:
            limit = max(20, min(int(self.msg_limit_var.get()), 500))
        except ValueError:
            limit = 100

        def work():
            try:

                def pcb(n):
                    self.root.after(
                        0, lambda: self._status(f"Loading messages... ({n})")
                    )

                msgs = fetch_messages_bulk(self.api, gid, limit, pcb)
                self.messages = msgs
                # Bug 5: populate message cache after fetching
                self._msg_cache[gid] = (time.time(), msgs)
                self.root.after(0, self._populate_messages)
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Error", str(e)))
                self.root.after(0, lambda: self.load_msgs_btn.config(state="normal"))
                self.root.after(0, self._hide_progress)

        threading.Thread(target=work, daemon=True).start()

    def _populate_messages(self):
        self._hide_progress()
        self.msg_listbox.delete(0, tk.END)
        self._display_messages = list(self.messages)
        # Batch insert all rows at once to avoid per-item overhead
        rows = [self._format_msg_row(m) for m in self._display_messages]
        self.msg_listbox.insert(tk.END, *rows)
        self.load_msgs_btn.config(state="normal")
        self.msg_count_var.set(f"{len(self.messages)} msgs")
        self._status(f"Loaded {len(self.messages)} messages")

    @staticmethod
    def _truncate_name(name, width=14):
        """Truncate name with ellipsis if too long, pad to width.
        Optimization 9: show '..'' when truncated."""
        if len(name) <= width:
            return name.ljust(width)
        return name[: width - 2] + ".."

    @staticmethod
    def _attachment_indicators(msg):
        """Build short indicators for message attachments."""
        TYPE_MAP = {
            "image": "[IMG]",
            "video": "[VID]",
            "location": "[LOC]",
            "poll": "[POLL]",
            "event": "[EVT]",
            "file": "[FILE]",
        }
        parts = []
        for att in msg.get("attachments", []):
            atype = att.get("type", "")
            if atype == "emoji":
                continue
            parts.append(TYPE_MAP.get(atype, "[+]"))
        return "".join(parts)

    @staticmethod
    def _format_msg_row(msg):
        """Format a message dict into a listbox display string."""
        ts = datetime.fromtimestamp(msg.get("created_at", 0)).strftime("%m/%d %H:%M")
        name = BingerApp._truncate_name(msg.get("name", "???"), 14)
        indicators = BingerApp._attachment_indicators(msg)
        raw_text = msg.get("text") or ""
        if indicators and raw_text:
            text = f"{indicators} {raw_text}"
        elif indicators:
            text = indicators
        else:
            text = raw_text or "(no text)"
        text = text[:50].replace("\n", " ")
        likes = len(msg.get("favorited_by", []))
        h = "+" if likes > 0 else " "
        return f" [{ts}] {name} {h}{str(likes).rjust(2)}L  {text}"

    # Feature 8: debounced search filter
    def _on_search_changed(self, *args):
        """Called on every keystroke -- schedules a debounced filter."""
        if self._filter_after_id is not None:
            self.root.after_cancel(self._filter_after_id)
        self._filter_after_id = self.root.after(150, self._filter_messages)

    def _filter_messages(self, *args):
        self._filter_after_id = None
        q = self.search_var.get().strip().lower()
        self.msg_listbox.delete(0, tk.END)
        self._display_messages = []
        rows = []
        for m in self.messages:
            t = (m.get("text") or "").lower()
            n = (m.get("name") or "").lower()
            if q in t or q in n:
                self._display_messages.append(m)
                rows.append(self._format_msg_row(m))
        if rows:
            self.msg_listbox.insert(tk.END, *rows)

    def _on_message_selected(self, event):
        sel = self.msg_listbox.curselection()
        if not sel or sel[0] >= len(self._display_messages):
            return
        self.selected_message = self._display_messages[sel[0]]
        self.check_btn.config(state="normal")
        tp = (self.selected_message.get("text") or "(no text)")[:60]
        lk = len(self.selected_message.get("favorited_by", []))
        self._status(f'Message: {lk} like(s) | "{tp}"')

    # ──────────────── LIKE CHECKER ────────────────
    def _get_member_map(self, group):
        """Build user_id->nickname map from cached group data (no network call).
        Bug 2 fix: uses cached self.selected_group['members'] instead of API call."""
        members = group.get("members", [])
        mm = {}
        for m in members:
            uid = m.get("user_id")
            if uid:
                mm[uid] = m.get("nickname", "Unknown")
        return mm

    def _check_likes(self):
        if not self.selected_group or not self.selected_message:
            return
        self._tc(self.results_text)
        self._status("Checking likes...")
        msg = self.selected_message
        group = self.selected_group
        gid = group["id"]

        # Bug 1: run network call + processing in a background thread
        def work():
            try:
                # Refresh group data in background for fresh member list
                fresh_group = None
                if self.api:
                    try:
                        fresh_group = self.api.get_group(gid)
                    except Exception:
                        pass

                # Build member map from fresh or cached data
                src_group = (
                    fresh_group
                    if (fresh_group and fresh_group.get("members"))
                    else group
                )
                members = src_group.get("members", [])
                mm = {}
                for m in members:
                    uid = m.get("user_id")
                    if uid:
                        mm[uid] = m.get("nickname", "Unknown")

                liked_ids = set(msg.get("favorited_by", []))

                # Feature 11: per-group exclusions
                excluded_set = self.excluded_ids.get(gid, set())

                # Apply exclusions
                active_ids = {uid for uid in mm if uid not in excluded_set}
                excluded_names = sorted(
                    [mm[uid] for uid in mm if uid in excluded_set], key=str.lower
                )

                liked, not_liked = [], []
                for uid in sorted(active_ids, key=lambda u: mm[u].lower()):
                    (liked if uid in liked_ids else not_liked).append(mm[uid])

                sender = msg.get("name", "Unknown")
                text = msg.get("text") or "(no text)"
                ts = datetime.fromtimestamp(msg.get("created_at", 0)).strftime(
                    "%Y-%m-%d  %H:%M:%S"
                )
                total = len(active_ids)
                nl = len(not_liked)
                lk = len(liked)
                pct = (lk / total * 100) if total > 0 else 0

                # Render on main thread
                self.root.after(
                    0,
                    lambda: self._render_check_results(
                        src_group,
                        fresh_group,
                        msg,
                        mm,
                        liked,
                        not_liked,
                        excluded_names,
                        sender,
                        text,
                        ts,
                        total,
                        nl,
                        lk,
                        pct,
                    ),
                )
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Error", str(e)))
                self.root.after(0, lambda: self._status("Check failed."))

        threading.Thread(target=work, daemon=True).start()

    def _render_check_results(
        self,
        src_group,
        fresh_group,
        msg,
        mm,
        liked,
        not_liked,
        excluded_names,
        sender,
        text,
        ts,
        total,
        nl,
        lk,
        pct,
    ):
        """Render like check results on the main thread."""
        # Update cached group data if we got fresh data
        if fresh_group and fresh_group.get("members"):
            self.selected_group = fresh_group
            idx = self.group_combo.current()
            if idx >= 0 and idx < len(self.groups):
                self.groups[idx] = fresh_group

        out = []
        W = lambda t, tag=None: out.append((t, tag))

        W("MESSAGE\n", "header")
        W("-" * 52 + "\n", "sep")
        W(f"  From:     {sender}\n", "info")
        W(f"  Date:     {ts}\n", "info")
        W(f"  Text:     {text}\n", "info")
        W("\n")

        W("STATS\n", "header")
        W("-" * 52 + "\n", "sep")
        W(f"  Like Rate:  ", "info")
        W(f"{pct:.0f}%", "pct")
        W(f"  ({lk} of {total} members)\n", "info")
        bw = 30
        filled = round(pct / 100 * bw)
        W(f"  [{'=' * filled}{'-' * (bw - filled)}]\n", "stat")
        if excluded_names:
            W(f"  Excluded:   {len(excluded_names)} member(s)\n", "dim")
        W("\n")

        W(f"DID NOT LIKE  ({nl})\n", "header")
        W("-" * 52 + "\n", "sep")
        if not_liked:
            for i, n in enumerate(not_liked, 1):
                W(f"  {i:>3}. {n}\n", "not_liked")
        else:
            W("  Everyone liked this message!\n", "liked")
        W("\n")

        W(f"LIKED  ({lk})\n", "header")
        W("-" * 52 + "\n", "sep")
        if liked:
            for i, n in enumerate(liked, 1):
                W(f"  {i:>3}. {n}\n", "liked")
        else:
            W("  Nobody liked this message.\n", "not_liked")

        if excluded_names:
            W(f"\nEXCLUDED  ({len(excluded_names)})\n", "header")
            W("-" * 52 + "\n", "sep")
            for i, n in enumerate(excluded_names, 1):
                W(f"  {i:>3}. {n}\n", "dim")

        self._tw_batch(self.results_text, out)
        self._status(f"Result: {nl} didn't like | {lk} liked | {pct:.0f}%")
        self.copy_btn.config(state="normal")
        self.export_btn.config(state="normal")
        self.shame_btn.config(state="normal" if not_liked else "disabled")
        self._last_not_liked = not_liked
        self._last_msg_text = text

        # Save to history
        self.db.save_check(
            self.selected_group["id"],
            self.selected_group["name"],
            msg.get("id", ""),
            text,
            sender,
            total,
            lk,
            nl,
            pct,
            liked,
            not_liked,
        )

        # Notification check
        if self.notif_enabled_var.get():
            try:
                threshold = int(self.notif_threshold_var.get())
            except ValueError:
                threshold = 50
            if pct < threshold:
                title = f"Low Like Rate: {pct:.0f}%"
                body = f'"{text[:60]}" - {nl} member(s) didn\'t like it'
                sent = send_toast(title, body)
                ts_now = datetime.now().strftime("%H:%M:%S")
                self._tw(
                    self.notif_text,
                    f"[{ts_now}] ALERT: {pct:.0f}% < {threshold}% - "
                    f"{nl} non-likers {'(toast sent)' if sent else '(toast failed)'}\n",
                    "not_liked",
                )

    # ──────────────── PINNED MESSAGES QUICK-CHECK ────────────────
    def _check_pinned(self):
        if not self.selected_group or not self.api:
            return
        self._tc(self.results_text)
        self._status("Fetching pinned messages...")
        self._show_progress()
        gid = self.selected_group["id"]

        def work():
            try:
                pinned = self.api.get_pinned_messages(gid)
                # Refresh group for fresh members
                fresh = None
                try:
                    fresh = self.api.get_group(gid)
                except Exception:
                    pass
                self.root.after(0, lambda: self._render_pinned(pinned, fresh))
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Error", str(e)))
                self.root.after(0, self._hide_progress)

        threading.Thread(target=work, daemon=True).start()

    def _render_pinned(self, pinned, fresh_group):
        self._hide_progress()
        self._tc(self.results_text)

        if fresh_group and fresh_group.get("members"):
            self.selected_group = fresh_group
            idx = self.group_combo.current()
            if 0 <= idx < len(self.groups):
                self.groups[idx] = fresh_group

        if not pinned:
            self._tw(
                self.results_text,
                "  No pinned messages found or pinned messages not supported "
                "for this group.\n",
                "dim",
            )
            self._status("No pinned messages found.")
            return

        mm = self._get_member_map(self.selected_group)
        gid = self.selected_group["id"]
        excluded_set = self.excluded_ids.get(gid, set())
        active_ids = {uid for uid in mm if uid not in excluded_set}

        out = []
        W = lambda t, tag=None: out.append((t, tag))
        total_nl = 0

        W(f"PINNED MESSAGES CHECK  ({len(pinned)} messages)\n", "header")
        W("=" * 56 + "\n\n", "sep")

        for pi, msg in enumerate(pinned, 1):
            liked_ids = set(msg.get("favorited_by", []))
            liked = [
                mm[u]
                for u in sorted(active_ids, key=lambda u: mm[u].lower())
                if u in liked_ids
            ]
            not_liked = [
                mm[u]
                for u in sorted(active_ids, key=lambda u: mm[u].lower())
                if u not in liked_ids
            ]
            total_nl += len(not_liked)

            sender = msg.get("name", "Unknown")
            text = (msg.get("text") or "(no text)")[:70]
            ts = datetime.fromtimestamp(msg.get("created_at", 0)).strftime(
                "%m/%d %H:%M"
            )
            total = len(active_ids)
            lk = len(liked)
            nl = len(not_liked)
            pct = (lk / total * 100) if total > 0 else 0

            W(f"  #{pi}  ", "stat")
            W(f'"{text}"\n', "info")
            W(f"       By {sender} on {ts}  |  ", "dim")
            W(f"{lk}/{total} liked ({pct:.0f}%)\n", "pct" if pct >= 80 else "stat")

            if not_liked:
                W(f"       Didn't like: ", "dim")
                W(f"{', '.join(not_liked[:10])}", "not_liked")
                if len(not_liked) > 10:
                    W(f" +{len(not_liked) - 10} more", "dim")
                W("\n")
            else:
                W("       Everyone liked this!\n", "liked")
            W("\n")

        W("-" * 56 + "\n", "sep")
        W(
            f"  SUMMARY: {len(pinned)} pinned messages checked, "
            f"{total_nl} total non-likers across all\n",
            "header",
        )

        self._tw_batch(self.results_text, out)
        self.copy_btn.config(state="normal")
        self.export_btn.config(state="normal")
        self._status(
            f"Checked {len(pinned)} pinned messages | {total_nl} total non-likers"
        )

    # ──────────────── COPY / EXPORT / SHAME ────────────────
    def _copy_results(self):
        t = self.results_text.get("1.0", tk.END).strip()
        if t:
            self.root.clipboard_clear()
            self.root.clipboard_append(t)
            self._status("Copied to clipboard!")

    def _export_results(self):
        t = self.results_text.get("1.0", tk.END).strip()
        if not t:
            return
        gn = (self.selected_group or {}).get("name", "group")
        safe = "".join(c if c.isalnum() or c in " _-" else "_" for c in gn)
        fp = filedialog.asksaveasfilename(
            defaultextension=".txt",
            initialfile=f"binger_{safe}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            filetypes=[("Text", "*.txt"), ("All", "*.*")],
        )
        if fp:
            try:
                with open(fp, "w", encoding="utf-8") as f:
                    f.write(t)
                self._status(f"Exported to {fp}")
            except Exception as e:
                messagebox.showerror("Error", str(e))

    # Feature 10: Leaderboard copy/export
    def _copy_lb(self):
        t = self.lb_text.get("1.0", tk.END).strip()
        if t:
            self.root.clipboard_clear()
            self.root.clipboard_append(t)
            self._status("Leaderboard copied to clipboard!")

    def _export_lb(self):
        t = self.lb_text.get("1.0", tk.END).strip()
        if not t:
            return
        gn = (self.selected_group or {}).get("name", "group")
        safe = "".join(c if c.isalnum() or c in " _-" else "_" for c in gn)
        fp = filedialog.asksaveasfilename(
            defaultextension=".txt",
            initialfile=f"binger_lb_{safe}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            filetypes=[("Text", "*.txt"), ("All", "*.*")],
        )
        if fp:
            try:
                with open(fp, "w", encoding="utf-8") as f:
                    f.write(t)
                self._status(f"Leaderboard exported to {fp}")
            except Exception as e:
                messagebox.showerror("Error", str(e))

    # Feature 10: Analytics copy/export
    def _copy_an(self):
        t = self.an_text.get("1.0", tk.END).strip()
        if t:
            self.root.clipboard_clear()
            self.root.clipboard_append(t)
            self._status("Analytics copied to clipboard!")

    def _export_an(self):
        t = self.an_text.get("1.0", tk.END).strip()
        if not t:
            return
        gn = (self.selected_group or {}).get("name", "group")
        safe = "".join(c if c.isalnum() or c in " _-" else "_" for c in gn)
        fp = filedialog.asksaveasfilename(
            defaultextension=".txt",
            initialfile=f"binger_an_{safe}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            filetypes=[("Text", "*.txt"), ("All", "*.*")],
        )
        if fp:
            try:
                with open(fp, "w", encoding="utf-8") as f:
                    f.write(t)
                self._status(f"Analytics exported to {fp}")
            except Exception as e:
                messagebox.showerror("Error", str(e))

    def _build_shame_text(self, template, not_liked, msg_preview):
        """Build the final shame message from a template string.
        Placeholders:
            {count}   - number of non-likers
            {names}   - numbered list of non-likers
            {message}  - preview of the original message
            {group}   - group name
        """
        numbered = "\n".join(f"{i}. {n}" for i, n in enumerate(not_liked, 1))
        group_name = (self.selected_group or {}).get("name", "the group")
        return (
            template.replace("{count}", str(len(not_liked)))
            .replace("{names}", numbered)
            .replace("{message}", msg_preview)
            .replace("{group}", group_name)
        )

    def _get_default_shame_template(self):
        return (
            "BINGER LIKE CHECKER REPORT\n"
            'The following {count} member(s) did NOT like: "{message}"\n\n'
            "{names}\n\n"
            "Like the message. You've been warned."
        )

    def _send_shame_message(self):
        if not self._last_not_liked or not self.selected_group:
            return
        if not self.api:
            return

        not_liked = self._last_not_liked
        msg_preview = (self._last_msg_text or "a message")[:50]

        # Load saved template or use default
        cfg = load_config()
        saved_template = cfg.get("shame_template", "")
        template = (
            saved_template if saved_template else self._get_default_shame_template()
        )

        # ── Shame Editor Dialog ──
        dlg = tk.Toplevel(self.root)
        dlg.title("Customize Shame Message")
        dlg.geometry("600x520")
        dlg.configure(bg=C["bg"])
        dlg.transient(self.root)
        dlg.grab_set()

        ttk.Label(dlg, text="Shame Message Editor", style="Header.TLabel").pack(
            pady=(12, 2), padx=16
        )
        ttk.Label(
            dlg,
            text="Edit the template below. Use placeholders: {count} {names} {message} {group}",
            style="Sub.TLabel",
        ).pack(padx=16, pady=(0, 8))

        # Template editor
        ttk.Label(dlg, text="TEMPLATE", style="Accent.TLabel").pack(
            anchor=tk.W, padx=16, pady=(4, 2)
        )
        tmpl_text = tk.Text(
            dlg,
            height=8,
            font=("Consolas", 10),
            wrap=tk.WORD,
            bg=C["surface"],
            fg=C["text"],
            insertbackground=C["text"],
            selectbackground=C["accent"],
            selectforeground=C["bright"],
            borderwidth=0,
            highlightthickness=1,
            highlightcolor=C["border"],
            highlightbackground=C["border"],
        )
        tmpl_text.pack(fill=tk.X, padx=16, pady=(0, 8))
        tmpl_text.insert("1.0", template)

        # Live preview
        ttk.Label(dlg, text="PREVIEW", style="Accent.TLabel").pack(
            anchor=tk.W, padx=16, pady=(4, 2)
        )
        preview_text = tk.Text(
            dlg,
            height=8,
            font=("Consolas", 9),
            wrap=tk.WORD,
            state="disabled",
            bg=C["bg2"],
            fg=C["dim"],
            borderwidth=0,
            highlightthickness=1,
            highlightcolor=C["border"],
            highlightbackground=C["border"],
        )
        preview_text.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 8))

        def update_preview(*_args):
            current = tmpl_text.get("1.0", tk.END).strip()
            rendered = self._build_shame_text(current, not_liked, msg_preview)
            preview_text.config(state="normal")
            preview_text.delete("1.0", tk.END)
            preview_text.insert("1.0", rendered)
            preview_text.config(state="disabled")

        tmpl_text.bind("<KeyRelease>", update_preview)
        update_preview()  # initial render

        # Buttons
        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(fill=tk.X, padx=16, pady=(0, 12))

        def reset_template():
            tmpl_text.delete("1.0", tk.END)
            tmpl_text.insert("1.0", self._get_default_shame_template())
            update_preview()

        def send():
            current = tmpl_text.get("1.0", tk.END).strip()
            final = self._build_shame_text(current, not_liked, msg_preview)

            # Save template for future use
            cfg = load_config()
            cfg["shame_template"] = current
            save_config(cfg)

            dlg.destroy()

            def work():
                try:
                    self.api.send_message(self.selected_group["id"], final)
                    self.root.after(0, lambda: self._status("Shame message sent!"))
                    self.root.after(
                        0, lambda: messagebox.showinfo("Sent", "Shame message sent!")
                    )
                except Exception as e:
                    self.root.after(0, lambda: messagebox.showerror("Error", str(e)))

            threading.Thread(target=work, daemon=True).start()

        ttk.Button(
            btn_frame,
            text="Reset to Default",
            style="Small.TButton",
            command=reset_template,
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            btn_frame, text="Cancel", style="Small.TButton", command=dlg.destroy
        ).pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(
            btn_frame, text="Send to Group", style="Danger.TButton", command=send
        ).pack(side=tk.RIGHT)

    # ════════════════════════════════════════════════════════
    #  LEADERBOARD
    # ════════════════════════════════════════════════════════
    def _run_leaderboard(self):
        if not self.selected_group or not self.api:
            return
        self.lb_run_btn.config(state="disabled")
        self._show_progress()
        gid = self.selected_group["id"]
        try:
            count = max(50, min(int(self.lb_count_var.get()), 2000))
        except ValueError:
            count = 200

        def work():
            try:

                def pcb(n):
                    self.root.after(
                        0, lambda: self.lb_status_var.set(f"Fetching... {n} msgs")
                    )

                msgs = self._get_cached_or_fetch(gid, count, pcb)
                self.root.after(0, lambda: self._render_leaderboard(msgs))
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Error", str(e)))
                self.root.after(0, lambda: self.lb_run_btn.config(state="normal"))
                self.root.after(0, self._hide_progress)

        threading.Thread(target=work, daemon=True).start()

    def _render_leaderboard(self, msgs):
        self._hide_progress()
        self.lb_run_btn.config(state="normal")
        self._tc(self.lb_text)

        # Feature 16: empty guard for leaderboard
        if not msgs:
            self._tw(self.lb_text, "  No messages to analyze.\n", "dim")
            self.lb_status_var.set("")
            return

        out = []  # collect (text, tag) for batch write
        W = lambda t, tag=None: out.append((t, tag))

        # Bug 2: use cached group data instead of network call
        mm = self._get_member_map(self.selected_group)

        # Feature 11: per-group exclusions for leaderboard stingiest section
        gid = self.selected_group["id"] if self.selected_group else ""
        excluded_set = self.excluded_ids.get(gid, set())

        # Likes given (who likes other people's messages)
        given = Counter()
        # Likes received (whose messages get liked)
        received = Counter()
        # Messages sent
        sent_count = Counter()

        for m in msgs:
            sender_id = m.get("user_id", "")
            if sender_id in mm:
                sent_count[sender_id] += 1
            for uid in m.get("favorited_by", []):
                if uid in mm:
                    given[uid] += 1
                if sender_id in mm:
                    received[sender_id] += 1

        total_msgs = len(msgs)
        self.lb_status_var.set(f"Analyzed {total_msgs} messages")

        medal = {0: "gold", 1: "silver", 2: "bronze"}

        W(f"LEADERBOARD  ({total_msgs} messages scanned)\n", "header")
        W("=" * 56 + "\n\n", "sep")

        # ── Likes Given ──
        W("MOST LIKES GIVEN (generous likers)\n", "header")
        W("-" * 56 + "\n", "sep")
        for i, (uid, cnt) in enumerate(given.most_common(15)):
            tag = medal.get(i, "liked")
            prefix = ["1st", "2nd", "3rd"][i] if i < 3 else f"{i + 1:>3}"
            avg = cnt / total_msgs * 100 if total_msgs else 0
            W(f"  {prefix.rjust(3)}  {mm[uid]:<20} {cnt:>5} likes  ({avg:.1f}%)\n", tag)
        W("\n")

        # ── Likes Received ──
        W("MOST LIKES RECEIVED (popular posters)\n", "header")
        W("-" * 56 + "\n", "sep")
        for i, (uid, cnt) in enumerate(received.most_common(15)):
            tag = medal.get(i, "liked")
            prefix = ["1st", "2nd", "3rd"][i] if i < 3 else f"{i + 1:>3}"
            per_msg = cnt / sent_count[uid] if sent_count[uid] else 0
            W(
                f"  {prefix.rjust(3)}  {mm[uid]:<20} {cnt:>5} likes  ({per_msg:.1f}/msg)\n",
                tag,
            )
        W("\n")

        # ── Least Likes Given (stingiest) ──
        W("LEAST LIKES GIVEN (stingiest members)\n", "header")
        W("-" * 56 + "\n", "sep")
        all_members_given = [
            (uid, given.get(uid, 0)) for uid in mm if uid not in excluded_set
        ]
        all_members_given.sort(key=lambda x: x[1])
        for i, (uid, cnt) in enumerate(all_members_given[:15]):
            tag = "not_liked" if i < 3 else "dim"
            W(f"  {i + 1:>3}  {mm[uid]:<20} {cnt:>5} likes given\n", tag)
        W("\n")

        # ── Most Active ──
        W("MOST MESSAGES SENT\n", "header")
        W("-" * 56 + "\n", "sep")
        for i, (uid, cnt) in enumerate(sent_count.most_common(15)):
            tag = medal.get(i, "info")
            prefix = ["1st", "2nd", "3rd"][i] if i < 3 else f"{i + 1:>3}"
            W(f"  {prefix.rjust(3)}  {mm[uid]:<20} {cnt:>5} messages\n", tag)

        self._tw_batch(self.lb_text, out)
        # Feature 10: enable copy/export after rendering
        self.lb_copy_btn.config(state="normal")
        self.lb_export_btn.config(state="normal")

    # ════════════════════════════════════════════════════════
    #  MEMBER REPORT CARD
    # ════════════════════════════════════════════════════════
    def _open_member_report(self):
        if not self.selected_group:
            return
        members = self.selected_group.get("members", [])
        if not members:
            messagebox.showinfo("No Members", "This group has no members.")
            return

        dlg = tk.Toplevel(self.root)
        dlg.title("Member Report Card")
        dlg.geometry("350x450")
        dlg.configure(bg=C["bg"])
        dlg.transient(self.root)
        dlg.grab_set()

        ttk.Label(dlg, text="Select a Member", style="Header.TLabel").pack(
            pady=(12, 2), padx=16
        )
        ttk.Label(
            dlg,
            text="Choose a member to generate their report card",
            style="Sub.TLabel",
        ).pack(padx=16, pady=(0, 8))

        listbox = tk.Listbox(
            dlg,
            font=("Consolas", 10),
            bg=C["surface"],
            fg=C["text"],
            selectbackground=C["accent"],
            selectforeground=C["bright"],
            borderwidth=0,
            highlightthickness=1,
            highlightcolor=C["border"],
            highlightbackground=C["border"],
        )
        listbox.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 8))

        sorted_members = sorted(members, key=lambda m: m.get("nickname", "").lower())
        for m in sorted_members:
            listbox.insert(tk.END, f"  {m.get('nickname', 'Unknown')}")

        def on_select():
            sel = listbox.curselection()
            if not sel:
                messagebox.showwarning("No Selection", "Please select a member.")
                return
            member = sorted_members[sel[0]]
            dlg.destroy()
            self._run_member_report(member)

        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(fill=tk.X, padx=16, pady=(0, 12))
        ttk.Button(
            btn_frame, text="Cancel", style="Small.TButton", command=dlg.destroy
        ).pack(side=tk.RIGHT, padx=(4, 0))
        ttk.Button(btn_frame, text="Generate Report", command=on_select).pack(
            side=tk.RIGHT
        )

    def _run_member_report(self, member):
        if not self.selected_group or not self.api:
            return
        self.lb_run_btn.config(state="disabled")
        self._show_progress()
        gid = self.selected_group["id"]
        try:
            count = max(50, min(int(self.lb_count_var.get()), 2000))
        except ValueError:
            count = 200

        def work():
            try:

                def pcb(n):
                    self.root.after(
                        0, lambda: self.lb_status_var.set(f"Fetching... {n} msgs")
                    )

                msgs = self._get_cached_or_fetch(gid, count, pcb)
                self.root.after(0, lambda: self._render_member_report(member, msgs))
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Error", str(e)))
                self.root.after(0, lambda: self.lb_run_btn.config(state="normal"))
                self.root.after(0, self._hide_progress)

        threading.Thread(target=work, daemon=True).start()

    def _render_member_report(self, member, msgs):
        self._hide_progress()
        self.lb_run_btn.config(state="normal")
        self._tc(self.lb_text)

        if not msgs:
            self._tw(self.lb_text, "  No messages to analyze.\n", "dim")
            return

        mm = self._get_member_map(self.selected_group)
        uid = member.get("user_id", "")
        nick = member.get("nickname", "Unknown")
        total_msgs = len(msgs)

        # Compute stats
        sent = 0
        likes_given = 0
        likes_received = 0
        most_liked_msg = None
        most_liked_count = -1
        # Who this member likes most
        likes_to = Counter()
        # Who likes this member most
        likes_from = Counter()

        for m in msgs:
            sender_id = m.get("user_id", "")
            fav_by = m.get("favorited_by", [])

            # Messages sent by this member
            if sender_id == uid:
                sent += 1
                lk = len(fav_by)
                likes_received += lk
                if lk > most_liked_count:
                    most_liked_count = lk
                    most_liked_msg = m
                # Who likes this member's messages
                for liker_id in fav_by:
                    if liker_id in mm and liker_id != uid:
                        likes_from[liker_id] += 1

            # Likes given by this member
            if uid in fav_by:
                likes_given += 1
                if sender_id in mm and sender_id != uid:
                    likes_to[sender_id] += 1

        avg_likes = likes_received / sent if sent > 0 else 0
        like_rate = (likes_given / total_msgs * 100) if total_msgs > 0 else 0

        medal = {0: "gold", 1: "silver", 2: "bronze"}
        out = []
        W = lambda t, tag=None: out.append((t, tag))

        W(f"MEMBER REPORT CARD: {nick}\n", "header")
        W("=" * 56 + "\n\n", "sep")

        W("OVERVIEW\n", "header")
        W("-" * 56 + "\n", "sep")
        W(f"  Messages Sent:      {sent}\n", "info")
        W(
            f"  Likes Given:        {likes_given}  (liked {like_rate:.0f}% of all msgs)\n",
            "stat",
        )
        W(f"  Likes Received:     {likes_received}\n", "info")
        W(f"  Avg Likes/Message:  {avg_likes:.1f}\n", "stat")
        W("\n")

        # Like rate bar
        bw = 30
        filled = round(like_rate / 100 * bw)
        W(f"  Like Rate:  ", "info")
        W(f"{like_rate:.0f}%", "pct")
        W(f"  [{('=' * filled) + ('-' * (bw - filled))}]\n", "stat")
        W("\n")

        # Most liked message
        if most_liked_msg and most_liked_count > 0:
            W("MOST LIKED MESSAGE\n", "header")
            W("-" * 56 + "\n", "sep")
            ml_text = (most_liked_msg.get("text") or "(no text)")[:70]
            ml_ts = datetime.fromtimestamp(
                most_liked_msg.get("created_at", 0)
            ).strftime("%m/%d %H:%M")
            W(f'  "{ml_text}"\n', "gold")
            W(f"  On {ml_ts}  |  {most_liked_count} likes\n", "info")
            W("\n")

        # Top 5 people this member likes most
        W("TOP 5: WHO THEY LIKE MOST\n", "header")
        W("-" * 56 + "\n", "sep")
        if likes_to:
            for i, (target_id, cnt) in enumerate(likes_to.most_common(5)):
                tag = medal.get(i, "liked")
                W(
                    f"  {i + 1:>3}  {mm.get(target_id, '?'):<20} {cnt:>4} likes given\n",
                    tag,
                )
        else:
            W("  No likes given to anyone.\n", "dim")
        W("\n")

        # Top 5 people who like this member most
        W("TOP 5: WHO LIKES THEM MOST\n", "header")
        W("-" * 56 + "\n", "sep")
        if likes_from:
            for i, (fan_id, cnt) in enumerate(likes_from.most_common(5)):
                tag = medal.get(i, "liked")
                W(
                    f"  {i + 1:>3}  {mm.get(fan_id, '?'):<20} {cnt:>4} likes given\n",
                    tag,
                )
        else:
            W("  Nobody has liked their messages.\n", "dim")

        self._tw_batch(self.lb_text, out)
        self.lb_copy_btn.config(state="normal")
        self.lb_export_btn.config(state="normal")
        self.lb_status_var.set(f"Report for {nick} ({total_msgs} msgs analyzed)")

    # ════════════════════════════════════════════════════════
    #  HISTORY
    # ════════════════════════════════════════════════════════
    def _refresh_history(self):
        if not self.selected_group:
            return
        self._tc(self.hist_text)
        out = []
        W = lambda t, tag=None: out.append((t, tag))

        rows = self.db.get_history(self.selected_group["id"], limit=50)
        W(f"CHECK HISTORY  ({self.selected_group['name']})\n", "header")
        W("=" * 60 + "\n\n", "sep")

        if not rows:
            W("  No checks recorded yet. Run a like check first.\n", "dim")
            self._tw_batch(self.hist_text, out)
            return

        for row in rows:
            # row: id, ts, gid, gname, mid, mtext, sender, total, liked, notliked, pct, liked_names, notliked_names
            (
                rid,
                ts,
                gid,
                gname,
                mid,
                mtext,
                sender,
                total,
                lk,
                nl,
                pct,
                _,
                nl_names_json,
            ) = row
            ts_short = ts[:19].replace("T", "  ")
            mtext_short = (mtext or "")[:45]
            W(f"  [{ts_short}]\n", "info")
            W(f'    "{mtext_short}"\n', "dim")
            W(f"    {lk}/{total} liked ({pct:.0f}%)  |  ", "stat")
            W(f"{nl} didn't like\n", "not_liked" if nl > 0 else "liked")
            try:
                names = json.loads(nl_names_json)
                if names:
                    W(f"    Non-likers: {', '.join(names[:8])}", "not_liked")
                    if len(names) > 8:
                        W(f" +{len(names) - 8} more", "dim")
                    W("\n")
            except Exception:
                pass
            W("\n")

        self._tw_batch(self.hist_text, out)

    def _show_offenders(self):
        if not self.selected_group:
            return
        self._tc(self.hist_text)
        out = []
        W = lambda t, tag=None: out.append((t, tag))

        offenders = self.db.get_repeat_offenders(self.selected_group["id"], limit=30)
        W(f"REPEAT OFFENDERS  ({self.selected_group['name']})\n", "header")
        W("=" * 56 + "\n", "sep")
        W("  Members who most frequently don't like messages:\n\n", "dim")

        if not offenders:
            W("  No data yet. Run some like checks first.\n", "dim")
            self._tw_batch(self.hist_text, out)
            return

        medal = {0: "gold", 1: "silver", 2: "bronze"}
        for i, (name, count) in enumerate(offenders):
            tag = medal.get(i, "not_liked" if i < 5 else "dim")
            prefix = ["1st", "2nd", "3rd"][i] if i < 3 else f"{i + 1:>3}"
            W(f"  {prefix.rjust(3)}  {name:<24} {count:>4} time(s)\n", tag)

        self._tw_batch(self.hist_text, out)

    # ════════════════════════════════════════════════════════
    #  ANALYTICS
    # ════════════════════════════════════════════════════════
    def _run_analytics(self):
        if not self.selected_group or not self.api:
            return
        self.an_run_btn.config(state="disabled")
        self._show_progress()
        gid = self.selected_group["id"]
        try:
            count = max(50, min(int(self.an_count_var.get()), 2000))
        except ValueError:
            count = 300

        def work():
            try:

                def pcb(n):
                    self.root.after(
                        0, lambda: self.an_status_var.set(f"Fetching... {n} msgs")
                    )

                msgs = self._get_cached_or_fetch(gid, count, pcb)
                self.root.after(0, lambda: self._render_analytics(msgs))
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Error", str(e)))
                self.root.after(0, lambda: self.an_run_btn.config(state="normal"))
                self.root.after(0, self._hide_progress)

        threading.Thread(target=work, daemon=True).start()

    def _render_analytics(self, msgs):
        self._hide_progress()
        self.an_run_btn.config(state="normal")
        self._tc(self.an_text)
        out = []  # collect (text, tag) for batch write
        W = lambda t, tag=None: out.append((t, tag))

        # Bug 2: use cached group data instead of network call
        mm = self._get_member_map(self.selected_group)
        total = len(msgs)
        if total == 0:
            self._tw(self.an_text, "  No messages to analyze.\n", "dim")
            return

        self.an_status_var.set(f"Analyzed {total} messages")

        # Compute stats
        msgs_by_user = Counter()
        likes_on_msg = []
        hour_count = Counter()
        day_count = Counter()
        total_likes = 0
        most_liked_msg = None
        most_liked_count = -1
        zero_like_msgs = 0
        words_by_user = Counter()

        for m in msgs:
            uid = m.get("user_id", "")
            if uid in mm:
                msgs_by_user[uid] += 1
                text = m.get("text") or ""
                words_by_user[uid] += len(text.split())

            lk = len(m.get("favorited_by", []))
            likes_on_msg.append(lk)
            total_likes += lk
            if lk == 0:
                zero_like_msgs += 1
            if lk > most_liked_count:
                most_liked_count = lk
                most_liked_msg = m

            ts = m.get("created_at", 0)
            dt = datetime.fromtimestamp(ts)
            hour_count[dt.hour] += 1
            day_count[dt.strftime("%A")] += 1

        avg_likes = total_likes / total if total else 0

        # Time range
        if msgs:
            oldest = datetime.fromtimestamp(msgs[-1].get("created_at", 0))
            newest = datetime.fromtimestamp(msgs[0].get("created_at", 0))
            span = newest - oldest
        else:
            oldest = newest = datetime.now()
            span = timedelta()

        W(f"GROUP ANALYTICS  ({self.selected_group['name']})\n", "header")
        W("=" * 60 + "\n\n", "sep")

        W("OVERVIEW\n", "header")
        W("-" * 60 + "\n", "sep")
        W(f"  Messages Analyzed:  {total}\n", "info")
        W(
            f"  Time Span:          {oldest.strftime('%m/%d/%Y')} - {newest.strftime('%m/%d/%Y')}"
            f"  ({span.days} days)\n",
            "info",
        )
        W(f"  Active Members:     {len(msgs_by_user)}\n", "info")
        W(f"  Total Likes:        {total_likes}\n", "info")
        W(f"  Avg Likes/Message:  {avg_likes:.1f}\n", "stat")
        W(
            f"  Zero-Like Messages: {zero_like_msgs} ({zero_like_msgs / total * 100:.0f}%)\n",
            "not_liked" if zero_like_msgs > total * 0.3 else "dim",
        )
        W("\n")

        # Most liked message
        if most_liked_msg:
            W("MOST LIKED MESSAGE\n", "header")
            W("-" * 60 + "\n", "sep")
            ml_text = (most_liked_msg.get("text") or "(no text)")[:80]
            ml_sender = most_liked_msg.get("name", "???")
            ml_ts = datetime.fromtimestamp(
                most_liked_msg.get("created_at", 0)
            ).strftime("%m/%d %H:%M")
            W(f'  "{ml_text}"\n', "gold")
            W(f"  By {ml_sender} on {ml_ts}  |  ", "info")
            W(f"{most_liked_count} likes\n", "stat")
            W("\n")

        # Activity by hour
        W("ACTIVITY BY HOUR\n", "header")
        W("-" * 60 + "\n", "sep")
        max_h = max(hour_count.values()) if hour_count else 1
        for h in range(24):
            cnt = hour_count.get(h, 0)
            bar_len = round(cnt / max_h * 25) if max_h else 0
            label = f"{h:02d}:00"
            bar = "#" * bar_len
            tag = "stat" if cnt == max_h and cnt > 0 else "dim"
            W(f"  {label}  {bar:<25}  {cnt}\n", tag)
        W("\n")

        # Activity by day
        W("ACTIVITY BY DAY\n", "header")
        W("-" * 60 + "\n", "sep")
        day_order = [
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ]
        max_d = max(day_count.values()) if day_count else 1
        for d in day_order:
            cnt = day_count.get(d, 0)
            bar_len = round(cnt / max_d * 25) if max_d else 0
            bar = "#" * bar_len
            tag = "stat" if cnt == max_d and cnt > 0 else "dim"
            W(f"  {d:<10}  {bar:<25}  {cnt}\n", tag)
        W("\n")

        # Chattiest members (by word count)
        W("MOST WORDS WRITTEN\n", "header")
        W("-" * 60 + "\n", "sep")
        medal = {0: "gold", 1: "silver", 2: "bronze"}
        for i, (uid, wc) in enumerate(words_by_user.most_common(10)):
            tag = medal.get(i, "info")
            mc = msgs_by_user[uid]
            avg_w = wc / mc if mc else 0
            W(
                f"  {i + 1:>3}  {mm.get(uid, '?'):<20} {wc:>6} words  "
                f"({mc} msgs, {avg_w:.0f} avg)\n",
                tag,
            )

        self._tw_batch(self.an_text, out)
        # Feature 10: enable copy/export after rendering
        self.an_copy_btn.config(state="normal")
        self.an_export_btn.config(state="normal")

    # ════════════════════════════════════════════════════════
    #  NOTIFICATIONS
    # ════════════════════════════════════════════════════════
    def _test_notification(self):
        ok = send_toast("Binger Like Checker", "This is a test notification!")
        if ok:
            self.notif_test_var.set("Test notification sent!")
        else:
            hint = (
                "install winotify"
                if IS_WINDOWS
                else "check notification permissions"
                if IS_MAC
                else "install notify-send"
            )
            self.notif_test_var.set(f"Could not send notification ({hint})")
        self._save_cfg()

    # ════════════════════════════════════════════════════════
    #  KEYBOARD SHORTCUTS (Feature 12)
    # ════════════════════════════════════════════════════════
    def _on_f5(self, event=None):
        """F5: reload messages if a group is selected."""
        if self.selected_group and self.api:
            self._load_messages()

    def _on_ctrl_shift_c(self, event=None):
        """Ctrl+Shift+C: copy current tab's results."""
        current_tab = self.notebook.index(self.notebook.select())
        if current_tab == 0:
            self._copy_results()
        elif current_tab == 1:
            self._copy_lb()
        elif current_tab == 3:
            self._copy_an()


# ═════════════════════════════════════════════════════════════════════
def main():
    # Enable DPI awareness for sharp rendering on high-DPI displays (Windows only)
    if IS_WINDOWS:
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                import ctypes

                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

    root = tk.Tk()

    # macOS: use native scaling
    if IS_MAC:
        try:
            root.tk.call("tk", "scaling", 2.0)
        except Exception:
            pass

    try:
        root.iconbitmap(default="")
    except Exception:
        pass
    app = BingerApp(root)

    # Graceful shutdown: close DB and save geometry on exit
    def on_close():
        # Feature 13: save geometry before closing
        app._save_cfg()
        app.db.close()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
