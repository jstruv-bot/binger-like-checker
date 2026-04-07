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
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS checks (
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
        # Migrate: deduplicate any existing rows from before the unique constraint
        self._deduplicate()

    def _deduplicate(self):
        """One-time migration: remove duplicate rows for the same group+message,
        keeping only the most recent check per message."""
        try:
            self.conn.execute("""
                DELETE FROM checks WHERE id NOT IN (
                    SELECT MAX(id) FROM checks GROUP BY group_id, message_id
                )
            """)
            self.conn.commit()
        except Exception:
            pass

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
#  Notifications (Windows toast)
# ─────────────────────────────────────────────────────────────────────


# Cache winotify availability so we don't re-check every call
_winotify_cls = None
_winotify_checked = False


def _sanitize_ps(s):
    """Remove characters that could break or inject into a PowerShell string."""
    return re.sub(r'["`$\r\n]', "", str(s))


def send_toast(title, message):
    """Best-effort Windows toast notification."""
    global _winotify_cls, _winotify_checked

    # Try winotify (cached check)
    if not _winotify_checked:
        try:
            from winotify import Notification

            _winotify_cls = Notification
        except ImportError:
            _winotify_cls = None
        _winotify_checked = True

    if _winotify_cls is not None:
        try:
            n = _winotify_cls(app_id="Binger Like Checker", title=title, msg=message)
            n.show()
            return True
        except Exception:
            pass

    # Fallback: PowerShell native toast (sanitized inputs)
    try:
        import subprocess

        safe_title = _sanitize_ps(title)
        safe_msg = _sanitize_ps(message)
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
        subprocess.Popen(
            ["powershell", "-Command", ps], creationflags=0x08000000
        )  # CREATE_NO_WINDOW
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
        self.root.geometry("900x850")
        self.root.minsize(780, 700)
        self.root.configure(bg=C["bg"])

        self.api = None
        self.groups = []
        self.messages = []
        self._display_messages = []
        self.selected_group = None
        self.selected_message = None
        self.user_name = None
        self.excluded_user_ids = set()
        self._msg_cache = {}  # group_id -> (timestamp, messages) for reuse
        self._cache_ttl = 120  # seconds before cache is stale
        self.db = HistoryDB()

        self._apply_theme()
        self._build_ui()
        self._load_saved_config()

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
        self.search_var.trace_add("write", self._filter_messages)
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
        self.excluded_user_ids = set(cfg.get("excluded_user_ids", []))
        if cfg.get("notif_enabled"):
            self.notif_enabled_var.set(True)
        if cfg.get("notif_threshold"):
            self.notif_threshold_var.set(str(cfg["notif_threshold"]))

    def _save_cfg(self):
        d = {}
        if self.save_tok.get():
            d["token"] = self.token_var.get().strip()
        d["excluded_user_ids"] = list(self.excluded_user_ids)
        d["notif_enabled"] = self.notif_enabled_var.get()
        try:
            d["notif_threshold"] = int(self.notif_threshold_var.get())
        except ValueError:
            d["notif_threshold"] = 50
        save_config(d)

    # ──────────────── CONNECT ────────────────
    def _connect(self):
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
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Error", str(e)))
                self.root.after(0, lambda: self.connect_btn.config(state="normal"))
                self.root.after(0, lambda: self._status("Connection failed."))
                self.root.after(0, self._hide_progress)

        threading.Thread(target=work, daemon=True).start()

    def _on_connected(self, name):
        self._hide_progress()
        self._save_cfg()
        self._status(f"Connected as {name}  |  {len(self.groups)} group(s)")
        self.user_label_var.set(f"Connected: {name}")
        self.connect_btn.config(state="normal")
        vals = [f"{g['name']}  ({len(g.get('members', []))})" for g in self.groups]
        self.group_combo["values"] = vals
        self.group_combo.config(state="readonly")
        if vals:
            self.group_combo.current(0)
            self._on_group_selected(None)

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
        self.lb_run_btn.config(state="normal")
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

    # ──────────────── EXCLUSIONS DIALOG ────────────────
    def _open_exclusions(self):
        if not self.selected_group:
            return
        members = self.selected_group.get("members", [])
        if not members:
            messagebox.showinfo("No Members", "This group has no members.")
            return

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
            var = tk.BooleanVar(value=(uid in self.excluded_user_ids))
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
            self.excluded_user_ids = {uid for uid, v in check_vars.items() if v.get()}
            self._save_cfg()
            n = len(self.excluded_user_ids)
            self._status(f"Exclusions updated: {n} member(s) excluded")
            dlg.destroy()

        ttk.Button(
            btn_frame, text="All", style="Small.TButton", command=select_all
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            btn_frame, text="None", style="Small.TButton", command=select_none
        ).pack(side=tk.LEFT, padx=(0, 4))
        excl_count = len([1 for v in check_vars.values() if v.get()])
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

                self.messages = fetch_messages_bulk(self.api, gid, limit, pcb)
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
    def _format_msg_row(msg):
        """Format a message dict into a listbox display string."""
        ts = datetime.fromtimestamp(msg.get("created_at", 0)).strftime("%m/%d %H:%M")
        name = msg.get("name", "???")[:14].ljust(14)
        text = (msg.get("text") or "(attachment)")[:50].replace("\n", " ")
        likes = len(msg.get("favorited_by", []))
        h = "+" if likes > 0 else " "
        return f" [{ts}] {name} {h}{str(likes).rjust(2)}L  {text}"

    def _filter_messages(self, *args):
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
        """Build user_id->nickname map, refreshing group data."""
        members = group.get("members", [])
        if self.api:
            try:
                fg = self.api.get_group(group["id"])
                if fg and fg.get("members"):
                    members = fg["members"]
                    self.selected_group = fg
                    idx = self.group_combo.current()
                    if idx >= 0:
                        self.groups[idx] = fg
            except Exception:
                pass
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
        msg = self.selected_message
        mm = self._get_member_map(self.selected_group)
        liked_ids = set(msg.get("favorited_by", []))

        # Apply exclusions
        active_ids = {uid for uid in mm if uid not in self.excluded_user_ids}
        excluded_names = sorted(
            [mm[uid] for uid in mm if uid in self.excluded_user_ids], key=str.lower
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

    def _send_shame_message(self):
        if not getattr(self, "_last_not_liked", None) or not self.selected_group:
            return
        names = ", ".join(self._last_not_liked)
        preview = getattr(self, "_last_msg_text", "a message")[:50]
        if not messagebox.askyesno(
            "Send Shame List",
            f"Send a message calling out {len(self._last_not_liked)} member(s) "
            f'who didn\'t like: "{preview}"?\n\nMembers: {names}',
        ):
            return
        txt = (
            f"BINGER LIKE CHECKER REPORT\n"
            f"The following {len(self._last_not_liked)} member(s) did NOT like: "
            f'"{preview}"\n\n'
        )
        for i, n in enumerate(self._last_not_liked, 1):
            txt += f"{i}. {n}\n"
        txt += "\nLike the message. You've been warned."

        def work():
            try:
                self.api.send_message(self.selected_group["id"], txt)
                self.root.after(0, lambda: self._status("Shame message sent!"))
                self.root.after(
                    0, lambda: messagebox.showinfo("Sent", "Shame message sent!")
                )
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Error", str(e)))

        threading.Thread(target=work, daemon=True).start()

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
        out = []  # collect (text, tag) for batch write
        W = lambda t, tag=None: out.append((t, tag))

        mm = self._get_member_map(self.selected_group)

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
            (uid, given.get(uid, 0)) for uid in mm if uid not in self.excluded_user_ids
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

    # ════════════════════════════════════════════════════════
    #  NOTIFICATIONS
    # ════════════════════════════════════════════════════════
    def _test_notification(self):
        ok = send_toast("Binger Like Checker", "This is a test notification!")
        if ok:
            self.notif_test_var.set("Test notification sent!")
        else:
            self.notif_test_var.set(
                "Could not send notification (install winotify for best results)"
            )
        self._save_cfg()


# ═════════════════════════════════════════════════════════════════════
def main():
    # Enable DPI awareness for sharp rendering on high-DPI displays
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
    except Exception:
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    root = tk.Tk()
    try:
        root.iconbitmap(default="")
    except Exception:
        pass
    app = BingerApp(root)

    # Graceful shutdown: close DB on exit
    def on_close():
        app.db.close()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
