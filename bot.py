"""
Timewaster Tracker v3.2  -  two Telegram bots in one process.

PUBLIC BOT  (BOT_TOKEN)        : file reports, /status users (image card), track / withdraw your reports,
                                 answer admin questions, appeal a report against you.
ADMIN BOT   (ADMIN_BOT_TOKEN)  : button panel -> approve / reject / ask reporter / notes /
                                 edit @ / change status / retract / ban / export / diagnostics.
CHANNEL     (CHANNEL_ID)       : approved reports are auto-posted (photos + details) and edited
                                 automatically when the status or the @ changes.
TRACKER                        : follows users by numeric ID, detects @ swaps, updates posts.

Required env : BOT_TOKEN, ADMIN_BOT_TOKEN, CHANNEL_ID, DATABASE_URL, ADMIN_IDS
Optional env : CHANNEL_USERNAME, TRACK_INTERVAL_MIN (30), MAX_PENDING_PER_USER (3),
               REPORT_COOLDOWN_MIN (10), MAX_REPORTS_PER_DAY (5), REMIND_AFTER_HOURS (24), PORT
Requires     : Pillow  (add "Pillow" to requirements.txt)
Optional     : fonts/Montserrat-Bold.ttf + fonts/Montserrat-Regular.ttf next to this file (nicer card text)

v3.2 changes: /check is now /status and answers with a generated profile card image
(avatar, @username, display name, status pill, Reports / Verified / Status boxes, buttons).
/check still works as an alias.
v3.1 changes: automatic column migration for old databases, DB self-check at startup (admins get
a DM if a table is broken), /dbtest admin command, passive_track can no longer crash a handler,
version-independent "forwarded message" filter.
Proof is a Telegram channel link only (no screenshots in the report wizard).
"""
import os, re, io, csv, sys, html, time, asyncio, logging, threading, traceback, textwrap
from datetime import datetime, timedelta, timezone
from functools import wraps
from http.server import BaseHTTPRequestHandler, HTTPServer

from sqlalchemy import (create_engine, inspect, text as sql_text, Column, Integer, BigInteger, String,
                        Text, DateTime, JSON, Boolean, ForeignKey, func, desc)
from sqlalchemy.orm import declarative_base, sessionmaker
from PIL import Image, ImageDraw, ImageFont, ImageOps
from telegram import (Update, InlineKeyboardButton as _IKB, InlineKeyboardMarkup as Kb,
                      InputMediaPhoto, BotCommand, LinkPreviewOptions,
                      MessageOriginUser, MessageOriginHiddenUser)
from telegram.constants import ParseMode, ChatType
from telegram.error import TelegramError, BadRequest, RetryAfter, NetworkError
from telegram.ext import (Application, CommandHandler, MessageHandler, CallbackQueryHandler,
                          ConversationHandler, TypeHandler, filters)

def Btn(text, data):
    """Callback button. (InlineKeyboardButton's 2nd positional arg is `url`, so callback_data must be named.)"""
    return _IKB(text, callback_data=data)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tw")

# ───────────────────────── config ─────────────────────────
def need(name):
    v = os.getenv(name, "").strip()
    if not v: sys.exit(f"Missing environment variable: {name}")
    return v

PUB_TOKEN, ADM_TOKEN = need("BOT_TOKEN"), need("ADMIN_BOT_TOKEN")
if PUB_TOKEN == ADM_TOKEN: sys.exit("BOT_TOKEN and ADMIN_BOT_TOKEN must be two different bots.")
_ch = need("CHANNEL_ID")
CHANNEL = int(_ch) if _ch.lstrip("-").isdigit() else _ch
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "").lstrip("@")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().lstrip("-").isdigit()}
if not ADMIN_IDS: sys.exit("ADMIN_IDS is empty - nobody could use the admin bot. Set it to your numeric Telegram ID.")
DB_URL = need("DATABASE_URL").replace("postgres://", "postgresql://", 1)
TRACK_MIN = int(os.getenv("TRACK_INTERVAL_MIN", "30"))
MAX_PENDING = int(os.getenv("MAX_PENDING_PER_USER", "3"))
COOLDOWN_MIN = int(os.getenv("REPORT_COOLDOWN_MIN", "10"))
MAX_PER_DAY = int(os.getenv("MAX_REPORTS_PER_DAY", "5"))
REMIND_H = int(os.getenv("REMIND_AFTER_HOURS", "24"))
MAX_PHOTOS = 8
POST_WORD = os.getenv("POST_WORD", "timewaster").strip() or "timewaster"     # "...marked as a <word>."
BANNER_URL = os.getenv("BANNER_URL", "").strip()                            # optional https image URL
BANNER_FILE = os.getenv("BANNER_FILE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "banner.png")
_BANNER = {"id": None}                                                      # cached Telegram file_id
LOCK_KEY = 7340117                                  # postgres advisory lock id (single-instance guard)

DEALS = ["Account Trade", "Middleman Deal", "Service", "Other"]
PUBLISHED = ("VERIFIED", "DISPUTED", "RESOLVED")
BADGE = {"PENDING": "⏳ PENDING", "VERIFIED": "✅ VERIFIED", "DISPUTED": "⚠️ DISPUTED",
         "RESOLVED": "🟢 RESOLVED", "REJECTED": "❌ REJECTED"}
NOTE_ICON = {"admin": "📝", "answer": "💬", "appeal": "⚖️"}
HTML = ParseMode.HTML
NOPREV = LinkPreviewOptions(is_disabled=True)
REJECT_REASONS = {"np": "Not enough proof", "dup": "Duplicate report",
                  "fake": "Fake / abusive report", "na": "Not a timewaster case"}

pub: Application = None
adm: Application = None
PUB_USERNAME = ""
STARTED = time.time()
STATE = {"track_at": None, "track_res": None, "remind_at": 0.0, "err_at": 0.0}

class _Forwarded(filters.MessageFilter):
    """Works on every python-telegram-bot version (filters.FORWARDED was removed in newer ones)."""
    def filter(self, message):
        return getattr(message, "forward_origin", None) is not None
FORWARDED = _Forwarded()

# ───────────────────────── database ─────────────────────────
engine = create_engine(DB_URL, pool_pre_ping=True, pool_recycle=300)
Session = sessionmaker(engine, expire_on_commit=False)
Base = declarative_base()

def now(): return datetime.now(timezone.utc)
def aware(d): return d if d is None or d.tzinfo else d.replace(tzinfo=timezone.utc)

class Subject(Base):                      # one real person, many usernames
    __tablename__ = "tw_subjects"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, unique=True, nullable=True)
    username = Column(String(64), nullable=True)
    first_name = Column(String(128), nullable=True)
    trackable = Column(Boolean, nullable=True)
    last_check = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=now)

class Alias(Base):                        # username history
    __tablename__ = "tw_aliases"
    id = Column(Integer, primary_key=True)
    subject_id = Column(Integer, ForeignKey("tw_subjects.id"), index=True)
    username = Column(String(64), index=True)
    seen_at = Column(DateTime(timezone=True), default=now)

class Report(Base):
    __tablename__ = "tw_reports"
    id = Column(Integer, primary_key=True)
    subject_id = Column(Integer, ForeignKey("tw_subjects.id"), index=True)
    reporter_id = Column(BigInteger, index=True)
    reporter_name = Column(String(64))
    deal_type = Column(String(32))
    reason = Column(Text)
    proof_link = Column(String(300), nullable=True)
    photo_ids = Column(JSON, default=list)
    status = Column(String(16), default="PENDING", index=True)
    chan_msgs = Column(JSON, default=list)
    chan_has_photo = Column(Boolean, default=False)
    approved_by = Column(String(64), nullable=True)                      # v3
    published_at = Column(DateTime(timezone=True), nullable=True)        # v3
    created_at = Column(DateTime(timezone=True), default=now)

class StatusLog(Base):                    # the tracking timeline
    __tablename__ = "tw_status_log"
    id = Column(Integer, primary_key=True)
    report_id = Column(Integer, ForeignKey("tw_reports.id"), index=True)
    status = Column(String(16))
    by = Column(String(64))
    note = Column(String(200), nullable=True)
    at = Column(DateTime(timezone=True), default=now)

class Note(Base):                         # v3: admin notes, reporter answers, appeals
    __tablename__ = "tw_notes"
    id = Column(Integer, primary_key=True)
    report_id = Column(Integer, ForeignKey("tw_reports.id"), index=True)
    kind = Column(String(16))             # admin | answer | appeal
    author_id = Column(BigInteger, nullable=True)
    author = Column(String(64))
    text = Column(Text)
    at = Column(DateTime(timezone=True), default=now)

class Banned(Base):
    __tablename__ = "tw_banned"
    user_id = Column(BigInteger, primary_key=True)

MODELS = (Subject, Alias, Report, StatusLog, Note, Banned)

def init_db():
    """Create missing tables, then add ANY column the models expect but an older database lacks."""
    Base.metadata.create_all(engine)
    insp = inspect(engine)
    with engine.begin() as cx:
        for table in Base.metadata.sorted_tables:
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have: continue
                ddl = col.type.compile(dialect=engine.dialect)
                cx.execute(sql_text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {ddl}'))
                log.warning("migrated: added %s.%s (%s)", table.name, col.name, ddl)

def db_selfcheck():
    """Return a list of human-readable problems (empty list = database is fine)."""
    bad = []
    for model in MODELS:
        try:
            with Session() as s:
                s.query(model).count()
        except Exception as e:
            bad.append(f"{model.__tablename__}: {str(e).strip().splitlines()[0][:220]}")
    return bad

def acquire_lock():
    """Only one copy may poll Telegram. A second copy waits here (fixes 'Conflict' errors on redeploy)."""
    if engine.dialect.name != "postgresql": return None
    cx = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    while not cx.execute(sql_text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar():
        log.warning("Another instance is running - waiting for it to stop...")
        time.sleep(5)
    return cx

# ───────────────────────── helpers ─────────────────────────
def esc(x): return html.escape(str(x if x is not None else ""))
def label(sub): return f"@{sub.username}" if sub.username else f"ID {sub.user_id}"
USERNAME_RE = re.compile(r"[A-Za-z0-9_]{4,32}")
_hits = {}

def throttle(uid, key, limit, per):
    """True if allowed. Simple in-memory sliding window."""
    t, k = time.monotonic(), (uid, key)
    q = [x for x in _hits.get(k, []) if t - x < per]
    ok = len(q) < limit
    if ok: q.append(t)
    _hits[k] = q
    return ok

def log_status(s, r, status, by, note=None):
    r.status = status
    s.add(StatusLog(report_id=r.id, status=status, by=by, note=note))

def claim(rid, frm, to, by, note=None):
    """Atomically move a report between statuses. False if someone else already did (double-click safe)."""
    frm = (frm,) if isinstance(frm, str) else tuple(frm)
    with Session() as s:
        n = (s.query(Report).filter(Report.id == rid, Report.status.in_(frm))
             .update({"status": to}, synchronize_session=False))
        if n:
            s.add(StatusLog(report_id=rid, status=to, by=by, note=note)); s.commit()
        else:
            s.rollback()
        return bool(n)

def aliases_of(s, sub):
    out, seen = [], {(sub.username or "").lower()}
    for a in s.query(Alias).filter_by(subject_id=sub.id).order_by(Alias.seen_at):
        if a.username.lower() not in seen:
            seen.add(a.username.lower()); out.append(a.username)
    return out

def set_username(s, sub, new):
    """Set current @. Returns the previous @ if it actually changed, else None."""
    old = sub.username
    sub.username = new
    if not s.query(Alias).filter(Alias.subject_id == sub.id,
                                 func.lower(Alias.username) == new.lower()).first():
        s.add(Alias(subject_id=sub.id, username=new))
    return old if old and old.lower() != new.lower() else None

def upsert_subject(s, username=None, user_id=None, name=None):
    username = (username or "").lstrip("@").strip() or None
    sub = s.query(Subject).filter_by(user_id=user_id).first() if user_id else None
    if not sub and username:
        sub = (s.query(Subject).join(Alias, Alias.subject_id == Subject.id)
               .filter(func.lower(Alias.username) == username.lower()).first())
        if sub and user_id and sub.user_id and sub.user_id != user_id:
            sub = None                      # same @ now used by a different account
        elif sub and user_id:
            sub.user_id = user_id           # learned the ID of a known @
    if not sub:
        sub = Subject(user_id=user_id); s.add(sub); s.flush()
    if name: sub.first_name = name
    if username: set_username(s, sub, username)
    return sub

def merge_subjects(s, keep, drop):
    s.query(Report).filter_by(subject_id=drop.id).update({"subject_id": keep.id})
    s.query(Alias).filter_by(subject_id=drop.id).update({"subject_id": keep.id})
    s.flush(); s.delete(drop); s.flush()

def find_subjects(s, t):
    t = t.strip().lstrip("@")
    if t.isdigit():
        return s.query(Subject).filter_by(user_id=int(t)).all()
    return (s.query(Subject).join(Alias, Alias.subject_id == Subject.id)
            .filter(func.lower(Alias.username) == t.lower()).distinct().all())

def post_url(r):
    if not r.chan_msgs: return None
    if CHANNEL_USERNAME: return f"https://t.me/{CHANNEL_USERNAME}/{r.chan_msgs[0]}"
    if isinstance(CHANNEL, int) and str(CHANNEL).startswith("-100"):     # private channel link
        return f"https://t.me/c/{str(CHANNEL)[4:]}/{r.chan_msgs[0]}"
    return None

def parse_identity(m):
    """text / forwarded message -> (user_id, username, name, error)"""
    o = m.forward_origin
    if isinstance(o, MessageOriginUser):
        u = o.sender_user
        return u.id, u.username, u.full_name, None
    if isinstance(o, MessageOriginHiddenUser):
        return None, None, None, "That user hides their forwards. Send their @username or numeric ID instead."
    t = (m.text or "").strip().lstrip("@")
    if t.lower().startswith(("https://t.me/", "t.me/")):
        t = t.rstrip("/").rsplit("/", 1)[-1]
    if t.isdigit(): return int(t), None, None, None
    if USERNAME_RE.fullmatch(t): return None, t, None, None
    return None, None, None, "Send a @username, a numeric ID, or forward a message from the user."

async def tg(fn, *a, **k):
    """Call a Telegram API method, waiting out flood limits instead of failing."""
    for _ in range(3):
        try: return await fn(*a, **k)
        except RetryAfter as e:
            ra = e.retry_after
            await asyncio.sleep((ra.total_seconds() if hasattr(ra, "total_seconds") else ra) + 1)
    return await fn(*a, **k)

async def tell(uid, text, kb=None):
    try:
        await tg(pub.bot.send_message, uid, text, parse_mode=HTML, reply_markup=kb, link_preview_options=NOPREV)
    except TelegramError: pass

async def alert_admins(text, kb=None):
    for a in ADMIN_IDS:
        try:
            await tg(adm.bot.send_message, a, text, parse_mode=HTML, reply_markup=kb, link_preview_options=NOPREV)
        except TelegramError: pass

async def notify_appellants(rid, text):
    with Session() as s:
        ids = {n.author_id for n in s.query(Note).filter_by(report_id=rid, kind="appeal") if n.author_id}
    for i in ids: await tell(i, text)

async def channel_check():
    """(ok, message) - can the public bot post / edit / delete in the channel?"""
    try:
        me = await pub.bot.get_chat_member(CHANNEL, pub.bot.id)
    except TelegramError as e:
        return False, f"Can't access the channel ({e}). Check CHANNEL_ID and add the PUBLIC bot as a channel admin."
    if me.status == "creator": return True, "Channel OK."
    if me.status != "administrator": return False, "The public bot is not an admin of the channel."
    miss = [n for n, a in (("post", "can_post_messages"), ("edit", "can_edit_messages"),
                           ("delete", "can_delete_messages")) if not getattr(me, a, False)]
    if miss: return False, "The public bot lacks channel permission(s): " + ", ".join(miss)
    return True, "Channel OK (public bot can post, edit and delete)."

# ───────────────────────── channel posting ─────────────────────────
def render_post(s, r):
    """Compact channel caption:  ❌ User @name (123456) has been marked as a timewaster."""
    sub = s.get(Subject, r.subject_id)
    who = "User"
    if sub.username: who += f' <a href="https://t.me/{esc(sub.username)}">@{esc(sub.username)}</a>'
    if sub.user_id: who += f" (<code>{sub.user_id}</code>)"
    if r.status == "DISPUTED":
        return f"⚠️ {who} has been reported as a {esc(POST_WORD)} — <b>status: disputed</b>."
    if r.status == "RESOLVED":
        return f"🟢 {who} — report <b>resolved</b>."
    return f"❌ {who} has been marked as a {esc(POST_WORD)}."

def post_kb(s, r, profile=True):
    """[View Profile] [View Full Report] buttons under the channel post."""
    sub = s.get(Subject, r.subject_id)
    row = []
    if profile:
        url = (f"https://t.me/{sub.username}" if sub.username
               else f"tg://user?id={sub.user_id}" if sub.user_id else None)
        if url: row.append(_IKB("View Profile", url=url))
    if PUB_USERNAME:
        row.append(_IKB("View Full Report", url=f"https://t.me/{PUB_USERNAME}?start=r{r.id}"))
    return Kb([row]) if row else None

def banner_source():
    if _BANNER["id"]: return _BANNER["id"]
    if BANNER_URL: return BANNER_URL
    try:
        with open(BANNER_FILE, "rb") as f: return f.read()
    except OSError:
        return None

async def publish(rid):
    with Session() as s:
        r = s.get(Report, rid)
        body, kbs = render_post(s, r), (post_kb(s, r), post_kb(s, r, profile=False))
    img = banner_source()

    async def send(kb):
        if img is None:
            return await tg(pub.bot.send_message, CHANNEL, body, parse_mode=HTML,
                            link_preview_options=NOPREV, reply_markup=kb)
        m = await tg(pub.bot.send_photo, CHANNEL, img, caption=body, parse_mode=HTML, reply_markup=kb)
        if m.photo: _BANNER["id"] = m.photo[-1].file_id
        return m

    try:
        m = await send(kbs[0])
    except BadRequest as e:                 # e.g. profile button refused by the user's privacy settings
        if "button" not in str(e).lower(): raise
        m = await send(kbs[1])
    with Session() as s:
        r = s.get(Report, rid)
        r.chan_msgs = [m.message_id]
        r.chan_has_photo = img is not None
        r.published_at = now()
        s.commit()

async def refresh_post(rid):
    with Session() as s:
        r = s.get(Report, rid)
        if not r or not r.chan_msgs: return
        photo, mid, album = r.chan_has_photo, r.chan_msgs[0], len(r.chan_msgs) > 1
        body, kbs = render_post(s, r), (post_kb(s, r), post_kb(s, r, profile=False))
    for i, kb in enumerate(kbs):
        if album: kb = None                 # old multi-photo posts cannot carry buttons
        try:
            if photo:
                await tg(pub.bot.edit_message_caption, CHANNEL, mid, caption=body, parse_mode=HTML, reply_markup=kb)
            else:
                await tg(pub.bot.edit_message_text, body, chat_id=CHANNEL, message_id=mid,
                         parse_mode=HTML, link_preview_options=NOPREV, reply_markup=kb)
            return
        except BadRequest as e:
            m = str(e).lower()
            if "not modified" in m: return
            if "button" in m and i == 0 and not album: continue
            log.warning("refresh #%s: %s", rid, e); return
        except TelegramError as e:
            log.warning("refresh #%s: %s", rid, e); return

async def refresh_subject(sub_id):
    with Session() as s:
        ids = [r.id for r in s.query(Report).filter(Report.subject_id == sub_id, Report.status.in_(PUBLISHED))]
    for i in ids:
        await refresh_post(i); await asyncio.sleep(0.35)

async def retract(rid):
    with Session() as s:
        r = s.get(Report, rid); ids = list(r.chan_msgs or [])
    for mid in ids:
        try: await tg(pub.bot.delete_message, CHANNEL, mid)
        except TelegramError: pass
    with Session() as s:
        r = s.get(Report, rid); r.chan_msgs = []; s.commit()

# ───────────────────────── username tracking ─────────────────────────
async def on_username_change(sub_id, old, new):
    await refresh_subject(sub_id)
    with Session() as s:
        sub = s.get(Subject, sub_id)
        last = s.query(Report).filter_by(subject_id=sub_id).order_by(desc(Report.id)).first()
        uid = sub.user_id
    kb = Kb([[Btn(f"📂 Open report #{last.id}", f"r:{last.id}")]]) if last else None
    await alert_admins(f"🔄 <b>Username change detected</b>\n@{esc(old)} → <b>@{esc(new)}</b>\n"
                       f"ID: <code>{uid}</code>\nChannel posts were updated automatically.", kb)

async def track_all():
    checked = changed = 0
    with Session() as s:
        ids = [x.id for x in s.query(Subject).filter(Subject.user_id.isnot(None))]
    for sid in ids:
        with Session() as s:
            sub = s.get(Subject, sid)
            if not sub: continue
            uid = sub.user_id
        old = new = None
        try:
            chat = await tg(pub.bot.get_chat, uid)
            ok = True
            with Session() as s:
                sub = s.get(Subject, sid)
                if chat.username: old = set_username(s, sub, chat.username); new = chat.username
                if chat.full_name: sub.first_name = chat.full_name
                sub.trackable, sub.last_check = True, now(); s.commit()
        except TelegramError:
            ok = False
            with Session() as s:
                sub = s.get(Subject, sid); sub.trackable, sub.last_check = False, now(); s.commit()
        checked += ok
        if old:
            changed += 1; await on_username_change(sid, old, new)
        await asyncio.sleep(0.4)
    STATE["track_at"], STATE["track_res"] = now(), (checked, changed, len(ids))
    return checked, changed

async def tracker_loop():
    await asyncio.sleep(30)
    while True:
        try: await track_all()
        except Exception: log.exception("tracker")
        await asyncio.sleep(TRACK_MIN * 60)

async def reminder_loop():
    """Nudge admins about reports that have been waiting too long."""
    await asyncio.sleep(120)
    while True:
        try:
            cutoff = now() - timedelta(hours=REMIND_H)
            with Session() as s:
                old = s.query(Report).filter(Report.status == "PENDING", Report.created_at < cutoff).order_by(Report.id).all()
                ids = [r.id for r in old]
            if ids and time.time() - STATE["remind_at"] > 12 * 3600:
                STATE["remind_at"] = time.time()
                await alert_admins(f"⏰ <b>{len(ids)} report(s)</b> have been waiting over {REMIND_H}h for review.",
                                   Kb([[Btn(f"📂 Open oldest (#{ids[0]})", f"r:{ids[0]}")]]))
        except Exception: log.exception("reminder")
        await asyncio.sleep(1800)

async def passive_track(u: Update, c):
    """If a tracked user ever talks to the public bot, we learn their current @ for free.
    Wrapped so that it can never break the real handlers."""
    try:
        usr = u.effective_user
        if not usr or usr.is_bot: return
        with Session() as s:
            sub = s.query(Subject).filter_by(user_id=usr.id).first()
            if not sub: return
            old = set_username(s, sub, usr.username) if usr.username else None
            sub.trackable, sub.last_check = True, now(); s.commit(); sid = sub.id
        if old: await on_username_change(sid, old, usr.username)
    except Exception:
        log.exception("passive_track")

# ───────────────────────── /status profile card (image) ─────────────────────────
FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
CARD_STYLE = {   # status -> (badge text, accent colour, one-line note)
    "FLAGGED":  ("FLAGGED",    (239, 83, 80),   "Verified timewaster reports exist. Deal with caution."),
    "DISPUTED": ("DISPUTED",   (255, 167, 38),  "Reports exist, but the case is currently disputed."),
    "RESOLVED": ("RESOLVED",   (102, 187, 106), "Previously reported, but the case has been resolved."),
    "CLEAN":    ("NO REPORTS", (100, 181, 246), "No public reports were found for this account."),
}
BG, PANEL, WHITE, MUTED, LINE = (15, 17, 23), (24, 27, 36), (245, 247, 250), (140, 148, 163), (44, 48, 60)

def _font(bold, size):
    for p in (os.path.join(FONT_DIR, "Montserrat-Bold.ttf" if bold else "Montserrat-Regular.ttf"),
              "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"):
        try: return ImageFont.truetype(p, size)
        except OSError: continue
    return ImageFont.load_default(size)

def _mix(a, b, t):
    return tuple(int(a[i] * t + b[i] * (1 - t)) for i in range(3))

def _fit(dr, text, font, maxw):
    while len(text) > 1 and dr.textlength(text, font=font) > maxw:
        text = text[:-2].rstrip() + "…"
    return text

def render_card(d, avatar=None):
    """Clean status card. d = dict(username, name, uid, status, reports, verified, last, bot). Returns PNG bytes."""
    W, H = 1000, 460
    S = 2                                                     # draw at 2x, shrink for smooth edges
    img = Image.new("RGB", (W * S, H * S), BG)
    dr = ImageDraw.Draw(img)
    lab, col, note = CARD_STYLE[d["status"]]
    def F(bold, size): return _font(bold, size * S)
    def R(box, r, **k): dr.rounded_rectangle([v * S for v in box], r * S, **k)

    R((0, 0, W - 1, H - 1), 30, fill=PANEL)                   # card
    dr.rectangle((0, 0, W * S, 10 * S), fill=col)             # accent bar, clipped by the corners below
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, W * S - 1, H * S - 1), 30 * S, fill=255)
    bg = Image.new("RGB", img.size, BG); bg.paste(img, (0, 0), mask); img = bg; dr = ImageDraw.Draw(img)

    # avatar
    D, ax, ay = 190, 60, 70
    pic = None
    if avatar:
        try: pic = ImageOps.fit(Image.open(io.BytesIO(avatar)).convert("RGB"), (D * S, D * S))
        except Exception: pic = None
    if pic is None:
        pic = Image.new("RGB", (D * S, D * S), _mix(col, PANEL, 0.35))
        ImageDraw.Draw(pic).text((D * S // 2, D * S // 2), (d["name"] or d["username"] or "?")[0].upper(),
                                 font=F(True, 90), fill=col, anchor="mm")
    m = Image.new("L", (D * S, D * S), 0); ImageDraw.Draw(m).ellipse((0, 0, D * S - 1, D * S - 1), fill=255)
    dr.ellipse(((ax - 7) * S, (ay - 7) * S, (ax + D + 7) * S, (ay + D + 7) * S), fill=col)
    dr.ellipse(((ax - 3) * S, (ay - 3) * S, (ax + D + 3) * S, (ay + D + 3) * S), fill=PANEL)
    img.paste(pic, (ax * S, ay * S), m)

    # name / @username / id
    x = 300
    dr.text((x * S, 68 * S), _fit(dr, d["name"] or "Unknown", F(True, 46), 640 * S), font=F(True, 46), fill=WHITE)
    sub = f"@{d['username']}" if d["username"] else "no username"
    if d.get("uid"): sub += f"   ·   ID {d['uid']}"
    dr.text((x * S, 128 * S), _fit(dr, sub, F(False, 24), 640 * S), font=F(False, 24), fill=MUTED)

    # status badge + note
    bf = F(True, 22)
    tw = dr.textlength(lab, font=bf) / S
    R((x, 180, x + tw + 62, 222), 21, fill=_mix(col, PANEL, 0.18), outline=col, width=2 * S)
    dr.ellipse(((x + 18) * S, 195 * S, (x + 30) * S, 207 * S), fill=col)
    dr.text(((x + 42) * S, 201 * S), lab, font=bf, fill=col, anchor="lm")
    dr.text((x * S, 244 * S), _fit(dr, note, F(False, 22), 640 * S), font=F(False, 22), fill=MUTED)

    # stats row
    dr.line((60 * S, 320 * S, (W - 60) * S, 320 * S), fill=LINE, width=2 * S)
    cols = [(str(d["reports"]), "REPORTS"), (str(d["verified"]), "VERIFIED"), (d.get("last") or "—", "LAST REPORT")]
    cw = (W - 120) / 3
    for i, (v, k) in enumerate(cols):
        cx = int(60 + cw * i + cw / 2)
        dr.text((cx * S, 360 * S), v, font=F(True, 44 if len(v) < 8 else 34), fill=WHITE, anchor="mm")
        dr.text((cx * S, 398 * S), k, font=F(True, 17), fill=MUTED, anchor="mm")
        if i: dr.line((int(60 + cw * i) * S, 338 * S, int(60 + cw * i) * S, 410 * S), fill=LINE, width=2 * S)

    dr.text(((W // 2) * S, 436 * S), f"Powered by @{d['bot']}", font=F(False, 16), fill=(95, 102, 118), anchor="mm")
    img = img.resize((W, H), Image.LANCZOS)
    out = io.BytesIO(); img.save(out, "PNG"); return out.getvalue()

async def avatar_bytes(uid):
    if not uid: return None
    try:
        ph = await pub.bot.get_user_profile_photos(uid, limit=1)
        if not ph.photos: return None
        f = await pub.bot.get_file(ph.photos[0][-1].file_id)
        return bytes(await f.download_as_bytearray())
    except TelegramError:
        return None

def status_data(t):
    """-> list of dicts (one per matching person). Empty list if the account is unknown."""
    t = t.strip().lstrip("@"); out = []
    with Session() as s:
        for sub in find_subjects(s, t)[:3]:
            reps = (s.query(Report).filter(Report.subject_id == sub.id, Report.status.in_(PUBLISHED))
                    .order_by(desc(Report.id)).all())
            sts = {r.status for r in reps}
            st = ("FLAGGED" if "VERIFIED" in sts else "DISPUTED" if "DISPUTED" in sts
                  else "RESOLVED" if "RESOLVED" in sts else "CLEAN")
            last = reps[0] if reps else None
            out.append(dict(uid=sub.user_id, username=sub.username, name=sub.first_name, status=st,
                            reports=len(reps), verified=sum(r.status == "VERIFIED" for r in reps),
                            bot=PUB_USERNAME, rid=last.id if last else None,
                            last=f"{last.created_at:%d %b %Y}" if last else None,
                            proof=(post_url(last) or last.proof_link) if last else None))
    return out

async def send_status(u, t):
    rows = await asyncio.to_thread(status_data, t)
    if not rows:                                     # unknown account -> clean card
        t = t.strip().lstrip("@")
        rows = [dict(uid=int(t) if t.isdigit() else None, username=None if t.isdigit() else t, name=None,
                     status="CLEAN", reports=0, verified=0, bot=PUB_USERNAME, rid=None, last=None, proof=None)]
    private = u.effective_chat.type == ChatType.PRIVATE      # buttons only in the bot's own chat
    for d in rows:
        png = await asyncio.to_thread(render_card, d, await avatar_bytes(d["uid"]))
        if not private:                                       # groups / supergroups: image only, no buttons
            await u.effective_message.reply_photo(io.BytesIO(png)); continue
        prof = (f"https://t.me/{d['username']}" if d["username"]
                else f"tg://user?id={d['uid']}" if d["uid"] else None)
        kb = []
        top = ([_IKB("View Profile", url=prof)] if prof else []) + \
              ([_IKB("View Proof", url=d["proof"])] if d["proof"] else [])
        if top: kb.append(top)
        if d["rid"]: kb.append([_IKB("View Full Report", url=f"https://t.me/{PUB_USERNAME}?start=r{d['rid']}")])
        kb.append([Btn("Report User", "pm:report")])
        try:
            await u.effective_message.reply_photo(io.BytesIO(png), reply_markup=Kb(kb))
        except BadRequest:                           # e.g. tg://user link refused by privacy settings
            await u.effective_message.reply_photo(io.BytesIO(png), reply_markup=Kb(kb[1:]))

# ═════════════════════════ PUBLIC BOT ═════════════════════════
TARGET, DEAL, REASON, PROOF, CONFIRM = range(5)

def pub_home_kb():
    return Kb([[Btn("📝 File a report", "pm:report"), Btn("🔍 Check a user", "pm:check")],
               [Btn("📋 My reports", "pm:mine"), Btn("ℹ️ Help", "pm:help")]])

async def say(u, text, kb=None):
    return await u.effective_message.reply_text(text, parse_mode=HTML, reply_markup=kb,
                                                link_preview_options=NOPREV)

async def p_start(u, c):
    c.user_data.pop("await", None)
    arg = c.args[0] if c.args else ""
    if len(arg) > 1 and arg[0] == "r" and arg[1:].isdigit():        # "View Full Report" from a channel post
        with Session() as s:
            r = s.get(Report, int(arg[1:]))
            if r and r.status in PUBLISHED:
                card, photos = report_card(s, r), list(r.photo_ids or [])
                more = subject_card(s, s.get(Subject, r.subject_id))
                multi = s.query(Report).filter(Report.subject_id == r.subject_id, Report.status.in_(PUBLISHED)).count() > 1
            else:
                card = None
        if card:
            await say(u, card, pub_home_kb())
            try:
                if len(photos) == 1: await c.bot.send_photo(u.effective_chat.id, photos[0], caption="📸 Proof")
                elif photos: await c.bot.send_media_group(u.effective_chat.id,
                                                          [InputMediaPhoto(p) for p in photos[:10]])
            except TelegramError as e:
                log.warning("proof send failed for #%s: %s", arg[1:], e)
            if multi and more: await say(u, more)
            return
    await say(u, "⏳ <b>Timewaster Tracker</b>\nReport flakers, check who you're dealing with, "
                 "and follow your reports.", pub_home_kb())

async def p_help(u, c):
    if u.callback_query: await u.callback_query.answer()
    await say(u, "<b>How it works</b>\n1. File a report with proof (a link to a Telegram channel containing your proof).\n"
                 "2. An admin reviews it. If approved it's posted on the channel with your proof.\n"
                 "3. You get a message whenever the status changes, and admins may ask you for more info.\n\n"
                 "<b>Commands</b>\n/report · /status · /my · /track &lt;id&gt; · /appeal &lt;id&gt; "
                 "(if a report is about <i>you</i>) · /cancel", pub_home_kb())

async def rep_start(u, c):
    if u.callback_query: await u.callback_query.answer()
    uid = u.effective_user.id
    with Session() as s:
        if s.get(Banned, uid):
            await say(u, "🚫 You can't file reports."); return ConversationHandler.END
        pend = s.query(Report).filter_by(reporter_id=uid, status="PENDING").count()
        last = aware(s.query(func.max(Report.created_at)).filter(Report.reporter_id == uid).scalar())
        today = s.query(Report).filter(Report.reporter_id == uid, Report.created_at > now() - timedelta(days=1)).count()
    if pend >= MAX_PENDING:
        await say(u, f"You already have {pend} reports waiting for review. Please wait for those first.")
        return ConversationHandler.END
    if today >= MAX_PER_DAY:
        await say(u, f"Daily limit reached ({MAX_PER_DAY} reports / 24h). Try again tomorrow.")
        return ConversationHandler.END
    if last and now() - last < timedelta(minutes=COOLDOWN_MIN):
        wait = COOLDOWN_MIN - int((now() - last).total_seconds() // 60)
        await say(u, f"Please wait about {wait} min before filing another report.")
        return ConversationHandler.END
    c.user_data.pop("await", None)
    c.user_data["rep"] = {"photos": []}
    await say(u, "<b>Step 1/4 · Who are you reporting?</b>\nSend their @username or numeric ID.\n"
                 "💡 <i>Best:</i> forward me any message from them — I'll capture their ID, so reports "
                 "follow them even if they change @.", Kb([[Btn("✖️ Cancel", "cancel")]]))
    return TARGET

async def got_target(u, c):
    uid, uname, name, err = parse_identity(u.message)
    if err:
        await say(u, "⚠️ " + err); return TARGET
    if uid == u.effective_user.id:
        await say(u, "You can't report yourself 🙂"); return TARGET
    if uid in ADMIN_IDS:
        await say(u, "That account is protected and can't be reported."); return TARGET
    if uname and not uid:               # best-effort: resolve @ -> ID
        try:
            ch = await c.bot.get_chat("@" + uname)
            if ch.type == ChatType.PRIVATE:
                uid, name = ch.id, ch.full_name
                if uid in ADMIN_IDS:
                    await say(u, "That account is protected and can't be reported."); return TARGET
        except TelegramError: pass
    c.user_data["rep"].update(uid=uid, uname=uname, name=name)
    kb = Kb([[Btn(d, f"d:{i}")] for i, d in enumerate(DEALS)])
    await say(u, "<b>Step 2/4 · What type of deal was it?</b>", kb)
    return DEAL

async def got_deal(u, c):
    q = u.callback_query; await q.answer()
    c.user_data["rep"]["deal"] = DEALS[int(q.data.split(":")[1])]
    await q.message.reply_text("Step 3/4 · Describe what happened (min 15 characters).")
    return REASON

async def got_reason(u, c):
    t = (u.message.text or "").strip()
    if len(t) < 15:
        await say(u, "Please add a bit more detail (at least 15 characters)."); return REASON
    c.user_data["rep"]["reason"] = t[:1200]
    await say(u, "<b>Step 4/4 · Proof</b>\nPlease create a Telegram channel and send all your proof to that channel.\n"
                 "Once that's done, simply send me the channel URL.",
              Kb([[Btn("✖️ Cancel", "cancel")]]))
    return PROOF

async def got_photo(u, c):
    await say(u, "Please don't send photos here. Put your proof in a Telegram channel and send me the channel URL "
                 "(for example https://t.me/yourchannel).")
    return PROOF

async def got_link(u, c):
    t = (u.message.text or "").strip()
    if t.lower().startswith(("t.me/", "telegram.me/")): t = "https://" + t
    if not t.lower().startswith(("https://t.me/", "https://telegram.me/")) or " " in t or len(t) > 300:
        await say(u, "Send a valid channel link like https://t.me/yourchannel"); return PROOF
    c.user_data["rep"]["link"] = t
    return await proof_done(u, c)

async def proof_done(u, c):
    if u.callback_query: await u.callback_query.answer()
    r = c.user_data.get("rep")
    if not r: return ConversationHandler.END
    if not r.get("link"):
        await say(u, "⚠️ Please send your proof channel link first."); return PROOF
    who = f"@{r['uname']}" if r.get("uname") else f"ID {r['uid']}"
    await say(u, f"<b>Please confirm</b>\n\n👤 {esc(who)}" +
                 (f" (ID <code>{r['uid']}</code>)" if r.get("uid") and r.get("uname") else "") +
                 f"\n🏷 {esc(r['deal'])}\n📝 {esc(r['reason'])}"
                 f"\n🔗 {esc(r['link'])}\n\nSubmit this report?",
              Kb([[Btn("✅ Submit", "c:yes"), Btn("✖️ Cancel", "c:no")]]))
    return CONFIRM

async def submit(u, c):
    q = u.callback_query; await q.answer()
    d, usr = c.user_data.get("rep"), u.effective_user
    if not d: return ConversationHandler.END
    with Session() as s:
        sub = upsert_subject(s, d.get("uname"), d.get("uid"), d.get("name"))
        if sub.user_id in ADMIN_IDS:
            s.rollback(); c.user_data.pop("rep", None)
            await q.message.reply_text("That account is protected and can't be reported."); return ConversationHandler.END
        dup = s.query(Report).filter(Report.subject_id == sub.id, Report.reporter_id == usr.id,
                                     Report.status.in_(("PENDING",) + PUBLISHED)).first()
        if dup:
            dup_id = dup.id; s.rollback(); c.user_data.pop("rep", None)
            await q.message.reply_text(f"You already reported this user (report #{dup_id}). "
                                       f"Use /track {dup_id} to follow it.", reply_markup=pub_home_kb())
            return ConversationHandler.END
        rep = Report(subject_id=sub.id, reporter_id=usr.id,
                     reporter_name=f"@{usr.username}" if usr.username else str(usr.id),
                     deal_type=d["deal"], reason=d["reason"], proof_link=d.get("link"),
                     photo_ids=d["photos"], status="PENDING")
        s.add(rep); s.flush()
        s.add(StatusLog(report_id=rep.id, status="PENDING", by="reporter", note="Submitted"))
        s.commit(); rid, lab = rep.id, label(sub)
    c.user_data.pop("rep", None)
    await q.message.reply_text(f"✅ Report #{rid} submitted.\nYou'll be notified when it's reviewed. "
                               f"Track it with /track {rid} or 📋 My reports.", reply_markup=pub_home_kb())
    await alert_admins(f"🆕 <b>New report #{rid}</b> on {esc(lab)}", Kb([[Btn("📂 Open", f"r:{rid}")]]))
    return ConversationHandler.END

async def cancel(u, c):
    if u.callback_query: await u.callback_query.answer()
    c.user_data.pop("rep", None)
    await say(u, "Cancelled.", pub_home_kb())
    return ConversationHandler.END

async def p_cancel_any(u, c):                         # /cancel outside the report wizard
    c.user_data.pop("await", None)
    await say(u, "Cancelled.", pub_home_kb())

def report_card(s, r):
    """What 'View Full Report' shows: every detail of one published report."""
    sub = s.get(Subject, r.subject_id)
    al = aliases_of(s, sub)
    n = s.query(Report).filter(Report.subject_id == sub.id, Report.status.in_(PUBLISHED)).count()
    L = [f"📄 <b>Full report #{r.id}</b>", f"Status: <b>{BADGE[r.status]}</b>", "",
         f"👤 <b>{esc(label(sub))}</b>"]
    if sub.user_id: L.append(f"🆔 <code>{sub.user_id}</code>")
    if al: L.append("🔄 Also seen as: " + ", ".join("@" + esc(a) for a in al[-5:]))
    L += [f"🏷 {esc(r.deal_type)}", f"📅 {r.created_at:%d %b %Y}", "", f"📝 {esc(r.reason)}"]
    if r.proof_link: L.append(f'\n🔗 <a href="{esc(r.proof_link)}">Proof channel</a>')
    L.append(f"\n📊 Public reports on this user: {n}")
    L.append(f"⚖️ Disagree? /appeal {r.id}")
    return "\n".join(L)

# ---- check / status ----
def subject_card(s, sub):
    reps = (s.query(Report).filter(Report.subject_id == sub.id, Report.status.in_(PUBLISHED))
            .order_by(desc(Report.created_at)).all())
    if not reps: return None
    al = aliases_of(s, sub)
    ver = sum(r.status == "VERIFIED" for r in reps)
    head = f"🔎 <b>{esc(label(sub))}</b>" + (f"  🆔 <code>{sub.user_id}</code>" if sub.user_id else "")
    if al: head += "\n🔄 Also seen as: " + ", ".join("@" + esc(a) for a in al[-6:])
    lines = [head, f"⚠️ <b>{len(reps)}</b> public report(s) · {ver} verified", ""]
    for r in reps[:8]:
        url = post_url(r)
        lines.append(f"• {BADGE[r.status]} · {esc(r.deal_type)} · {r.created_at:%d %b %Y}"
                     + (f' — <a href="{url}">post</a>' if url else ""))
    return "\n".join(lines)

def lookup_text(t):
    t = t.strip().lstrip("@")
    with Session() as s:
        out = [x for x in (subject_card(s, sub) for sub in find_subjects(s, t)) if x]
    return "\n\n".join(out) or f"✅ No public reports for <b>{esc(t)}</b>."

async def p_check(u, c):                              # /status (and the old /check alias)
    if not throttle(u.effective_user.id, "check", 20, 3600):
        await say(u, "Too many lookups - try again in a bit."); return
    if c.args:
        await send_status(u, c.args[0]); return
    if u.effective_chat.type != ChatType.PRIVATE:             # in groups the target must be in the command
        await say(u, "Usage: /status @username  (or a numeric ID)"); return
    c.user_data["await"] = ("check", None)
    await say(u, "Send the @username or ID to look up.")

# ---- my reports / tracking / withdraw ----
def timeline(rid, uid):
    with Session() as s:
        r = s.get(Report, rid)
        if not r or r.reporter_id != uid: return "Report not found.", None
        sub = s.get(Subject, r.subject_id)
        logs = s.query(StatusLog).filter_by(report_id=rid).order_by(StatusLog.at).all()
        L = [f"📄 <b>Report #{r.id}</b> on {esc(label(sub))}", f"Status: <b>{BADGE[r.status]}</b>", "", "<b>Timeline</b>"]
        for l in logs:
            L.append(f"• {l.at:%d %b %H:%M} — {BADGE.get(l.status, l.status)}" + (f" ({esc(l.note)})" if l.note else ""))
        if post_url(r): L.append(f'\n<a href="{post_url(r)}">View channel post</a>')
        kb = Kb([[Btn("🗑 Withdraw this report", f"wd:{rid}")]]) if r.status == "PENDING" else None
        return "\n".join(L), kb

async def p_mine(u, c):
    if u.callback_query: await u.callback_query.answer()
    with Session() as s:
        rows = s.query(Report).filter_by(reporter_id=u.effective_user.id).order_by(desc(Report.id)).limit(15).all()
        btns = [[Btn(f"#{r.id} {label(s.get(Subject, r.subject_id))} · {BADGE[r.status]}", f"mr:{r.id}")] for r in rows]
    if not btns: await say(u, "You haven't filed any reports yet.", pub_home_kb()); return
    await say(u, "📋 <b>Your reports</b> (tap to track)", Kb(btns))

async def p_mr(u, c):
    q = u.callback_query; await q.answer()
    body, kb = timeline(int(q.data.split(":")[1]), u.effective_user.id)
    await q.message.reply_text(body, parse_mode=HTML, reply_markup=kb, link_preview_options=NOPREV)

async def p_withdraw(u, c):
    q = u.callback_query; rid = int(q.data.split(":")[1])
    with Session() as s:
        r = s.get(Report, rid); mine = bool(r and r.reporter_id == u.effective_user.id)
    if mine and claim(rid, "PENDING", "REJECTED", f"reporter:{u.effective_user.id}", "Withdrawn by reporter"):
        await q.answer("Withdrawn"); await q.message.reply_text(f"🗑 Report #{rid} withdrawn.")
    else:
        await q.answer("Can't withdraw - it was already reviewed.", show_alert=True)

async def p_track(u, c):
    if not c.args or not c.args[0].lstrip("#").isdigit():
        await say(u, "Usage: /track &lt;report id&gt;"); return
    body, kb = timeline(int(c.args[0].lstrip("#")), u.effective_user.id)
    await say(u, body, kb)

async def p_menu(u, c):
    q = u.callback_query; await q.answer()
    act = q.data.split(":")[1]
    if act == "check":
        c.user_data["await"] = ("check", None); await q.message.reply_text("Send the @username or ID to look up.")
    elif act == "mine": await p_mine(u, c)
    elif act == "help": await p_help(u, c)

# ---- reporter answers an admin question / reported user appeals ----
async def p_answer_btn(u, c):
    q = u.callback_query; rid = int(q.data.split(":")[1])
    with Session() as s:
        r = s.get(Report, rid); ok = bool(r and r.reporter_id == u.effective_user.id)
    if not ok: await q.answer("Not your report.", show_alert=True); return
    await q.answer()
    c.user_data["await"] = ("answer", rid)
    await q.message.reply_text(f"💬 Send your reply for report #{rid} (text and/or screenshots). "
                               "Press Done when finished.", reply_markup=Kb([[Btn("✅ Done", "ad:done")]]))

async def p_answer_done(u, c):
    q = u.callback_query; await q.answer()
    c.user_data.pop("await", None)
    await q.message.reply_text("👍 Thanks - the admins have your reply.", reply_markup=pub_home_kb())

def is_subject(s, sub, usr):
    if sub.user_id and sub.user_id == usr.id: return "ID match ✅"
    names = {a.lower() for a in aliases_of(s, sub)} | {(sub.username or "").lower()}
    if usr.username and usr.username.lower() in names: return "@ match only ⚠️ (not verified by ID)"
    return None

async def p_appeal(u, c):
    if not c.args or not c.args[0].lstrip("#").isdigit():
        await say(u, "Usage: /appeal &lt;report id&gt;  (the number is in the full report)"); return
    rid = int(c.args[0].lstrip("#"))
    with Session() as s:
        r = s.get(Report, rid)
        sub = s.get(Subject, r.subject_id) if r and r.status in PUBLISHED else None
        ok = bool(sub and is_subject(s, sub, u.effective_user))
    if not ok:
        await say(u, "I can only take appeals on a published report from the reported account itself."); return
    c.user_data["await"] = ("appeal", rid)
    await say(u, f"⚖️ <b>Appeal report #{rid}</b>\nWrite your side of the story (text / links). "
                 "An admin will review it. /cancel to abort.")

async def p_incoming(u, c):
    """Text/photos sent to the public bot outside the report wizard."""
    st, m, usr = c.user_data.get("await"), u.message, u.effective_user
    if not st or not m: return
    kind, rid = st
    body = (m.text or m.caption or "").strip()
    if kind == "check":
        if not m.text: return
        c.user_data.pop("await")
        if not throttle(usr.id, "check", 20, 3600):
            await say(u, "Too many lookups - try again in a bit."); return
        await send_status(u, m.text); return
    if kind not in ("answer", "appeal"): return
    photo = m.photo[-1].file_id if m.photo else None
    if photo and kind == "appeal":
        await say(u, "Appeals accept text and links only - paste a link to your proof."); return
    if not body and not photo: return
    if not throttle(usr.id, "note", 12, 3600):
        await say(u, "Too many messages - please wait a bit."); return
    verdict = ""
    with Session() as s:
        r = s.get(Report, rid)
        if not r or (kind == "answer" and r.reporter_id != usr.id):
            c.user_data.pop("await", None); await say(u, "Report not found."); return
        if photo and len(r.photo_ids or []) < MAX_PHOTOS:
            r.photo_ids = list(r.photo_ids or []) + [photo]
        if kind == "appeal":
            verdict = is_subject(s, s.get(Subject, r.subject_id), usr) or ""
        s.add(Note(report_id=rid, kind=kind, author_id=usr.id,
                   author=f"@{usr.username}" if usr.username else str(usr.id), text=(body or "[photo]")[:1500]))
        s.commit()
    who = f"@{usr.username}" if usr.username else f"ID {usr.id}"
    title = f"💬 <b>Reporter reply · report #{rid}</b>" if kind == "answer" else f"⚖️ <b>APPEAL · report #{rid}</b> ({esc(verdict)})"
    await alert_admins(f"{title}\nFrom {esc(who)}\n\n{esc(body or '[photo added - see 🖼 Proofs]')}",
                       Kb([[Btn(f"📂 Open report #{rid}", f"r:{rid}")]]))
    if kind == "appeal":
        c.user_data.pop("await", None)
        await say(u, "⚖️ Appeal received. If the report's status changes you'll be told.", pub_home_kb())
    else:
        await say(u, "✅ Sent.", Kb([[Btn("✅ Done", "ad:done")]]))

def register_public(app):
    app.add_handler(TypeHandler(Update, passive_track), group=-1)
    conv = ConversationHandler(
        entry_points=[CommandHandler("report", rep_start),
                      CallbackQueryHandler(rep_start, pattern=r"^pm:report$")],
        states={
            TARGET: [MessageHandler((filters.TEXT | FORWARDED) & ~filters.COMMAND, got_target)],
            DEAL: [CallbackQueryHandler(got_deal, pattern=r"^d:\d$")],
            REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_reason)],
            PROOF: [MessageHandler(filters.PHOTO, got_photo),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, got_link)],
            CONFIRM: [CallbackQueryHandler(submit, pattern=r"^c:yes$")],
        },
        fallbacks=[CommandHandler("cancel", cancel),
                   CallbackQueryHandler(cancel, pattern=r"^(c:no|cancel)$")],
        allow_reentry=True)
    app.add_handler(conv)
    app.add_handler(CommandHandler("start", p_start))
    app.add_handler(CommandHandler("help", p_help))
    app.add_handler(CommandHandler("cancel", p_cancel_any))
    app.add_handler(CommandHandler(["status", "check"], p_check))     # /check kept as an alias
    app.add_handler(CommandHandler("my", p_mine))
    app.add_handler(CommandHandler("track", p_track))
    app.add_handler(CommandHandler("appeal", p_appeal))
    app.add_handler(CallbackQueryHandler(p_menu, pattern=r"^pm:(check|mine|help)$"))
    app.add_handler(CallbackQueryHandler(p_mr, pattern=r"^mr:\d+$"))
    app.add_handler(CallbackQueryHandler(p_withdraw, pattern=r"^wd:\d+$"))
    app.add_handler(CallbackQueryHandler(p_answer_btn, pattern=r"^ra:\d+$"))
    app.add_handler(CallbackQueryHandler(p_answer_done, pattern=r"^ad:done$"))
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND & filters.ChatType.PRIVATE,
                                   p_incoming), group=1)

# ═════════════════════════ ADMIN BOT ═════════════════════════
def admin_only(fn):
    @wraps(fn)
    async def w(u, c):
        usr = u.effective_user
        if not usr or usr.id not in ADMIN_IDS:
            if u.callback_query: await u.callback_query.answer("Not authorized", show_alert=True)
            elif u.effective_message: await u.effective_message.reply_text("⛔ Not authorized.")
            return
        return await fn(u, c)
    return w

async def show(u, text, kb=None):
    try:
        if u.callback_query:
            await u.callback_query.edit_message_text(text, parse_mode=HTML, reply_markup=kb,
                                                     link_preview_options=NOPREV)
        else:
            await u.effective_message.reply_text(text, parse_mode=HTML, reply_markup=kb,
                                                 link_preview_options=NOPREV)
    except BadRequest as e:
        if "not modified" not in str(e).lower(): raise

HOME_BTN = [Btn("🏠 Menu", "m:home")]

async def home(u, c):
    with Session() as s:
        pend = s.query(Report).filter_by(status="PENDING").count()
        pubd = s.query(Report).filter(Report.status.in_(PUBLISHED)).count()
    kb = Kb([[Btn(f"📥 Pending ({pend})", "l:p:0"), Btn(f"📢 Published ({pubd})", "l:pub:0")],
             [Btn("🔎 Search", "m:search"), Btn("📊 Stats", "m:stats")],
             [Btn("📡 Run tracker now", "m:track"), Btn("🚫 Banned reporters", "m:bans")],
             [Btn("📤 Export CSV", "m:export"), Btn("🩺 Diagnostics", "m:diag")]])
    await show(u, "🛡 <b>Admin panel</b>\nApprove, reject and manage reports with the buttons below.", kb)

def list_view(kind, page):
    per = 8
    with Session() as s:
        q = s.query(Report)
        q = q.filter_by(status="PENDING").order_by(Report.id) if kind == "p" else \
            q.filter(Report.status.in_(PUBLISHED)).order_by(desc(Report.id))
        total = q.count()
        rows = q.offset(page * per).limit(per).all()
        btns = [[Btn(f"#{r.id} {label(s.get(Subject, r.subject_id))} · {r.deal_type}"
                     + ("" if kind == "p" else f" · {r.status[:4]}"), f"r:{r.id}")] for r in rows]
    nav = []
    if page > 0: nav.append(Btn("◀️", f"l:{kind}:{page - 1}"))
    if (page + 1) * per < total: nav.append(Btn("▶️", f"l:{kind}:{page + 1}"))
    title = "📥 <b>Pending reports</b>" if kind == "p" else "📢 <b>Published reports</b>"
    if not rows: title += "\n\nNothing here."
    return f"{title} ({total})", Kb(btns + ([nav] if nav else []) + [HOME_BTN])

def report_view(s, r):
    sub = s.get(Subject, r.subject_id)
    al = aliases_of(s, sub)
    others = s.query(Report).filter(Report.subject_id == sub.id, Report.id != r.id,
                                    Report.status != "REJECTED").count()
    rs = dict(s.query(Report.status, func.count()).filter(Report.reporter_id == r.reporter_id)
              .group_by(Report.status).all())
    good, bad = sum(rs.get(k, 0) for k in PUBLISHED), rs.get("REJECTED", 0)
    banned = " · 🚫 BANNED" if s.get(Banned, r.reporter_id) else ""
    track = ("🟢 live (auto-detects @ changes)" if sub.trackable else
             "🔴 can't auto-track (user must have started the public bot)" if sub.trackable is False else
             "🟡 not checked yet") if sub.user_id else "⚪ no ID yet → tap ✏️ and forward a message from them"
    L = [f"📄 <b>Report #{r.id}</b> — {BADGE[r.status]}", "",
         f"👤 <b>{esc(label(sub))}</b>",
         f"🆔 {'<code>%s</code>' % sub.user_id if sub.user_id else 'unknown'}"]
    if al: L.append("🔄 Past @: " + ", ".join("@" + esc(a) for a in al))
    L += [f"📡 {track}", f"🏷 {esc(r.deal_type)}",
          f"👮 Reporter: {esc(r.reporter_name)} (<code>{r.reporter_id}</code>)",
          f"📈 Reporter record: {sum(rs.values())} filed · {good} approved · {bad} rejected{banned}",
          f"📅 {r.created_at:%d %b %Y %H:%M}", f"📊 Other reports on this user: {others}", "",
          f"📝 {esc(r.reason)}"]
    if r.proof_link: L.append(f'\n🔗 <a href="{esc(r.proof_link)}">Proof link</a>')
    L.append(f"🖼 {len(r.photo_ids or [])} photo(s)")
    if r.approved_by: L.append(f"🧑‍⚖️ Approved by {esc(r.approved_by)}")
    if post_url(r): L.append(f'📢 <a href="{post_url(r)}">Channel post</a>')
    elif r.chan_msgs: L.append("📢 Posted on channel")
    notes = s.query(Note).filter_by(report_id=r.id).order_by(desc(Note.at)).limit(3).all()
    if notes:
        L.append("\n<b>Latest notes</b>")
        L += [f"{NOTE_ICON.get(n.kind, '•')} {esc(n.author)}: {esc(n.text[:200])}" for n in reversed(notes)]

    rows = []
    if r.status == "PENDING":
        rows.append([Btn("✅ Approve & post", f"a:ok:{r.id}"), Btn("❌ Reject", f"a:rj:{r.id}")])
    elif r.status in PUBLISHED:
        rows.append([Btn(("✓ " if r.status == k else "") + k.title(), f"a:st:{r.id}:{k}") for k in PUBLISHED])
    else:
        rows.append([Btn("↩️ Restore to pending", f"a:rest:{r.id}")])
    rows.append([Btn("❓ Ask reporter", f"a:ask:{r.id}"), Btn("📝 Add note", f"a:nt:{r.id}")])
    row2 = [Btn("✏️ Edit @ / ID", f"a:ed:{r.id}")]
    if r.photo_ids: row2.append(Btn(f"🖼 Proofs ({len(r.photo_ids)})", f"a:px:{r.id}"))
    rows.append(row2)
    row3 = [Btn("📜 History", f"a:hx:{r.id}"), Btn("🚫 Ban reporter", f"a:ban:{r.id}")]
    if r.status in PUBLISHED:
        row3 = [Btn("🔄 Refresh post", f"a:rf:{r.id}"), Btn("🗑 Retract", f"a:ret:{r.id}")] + row3[:1]
        rows.append(row3); rows.append([Btn("🚫 Ban reporter", f"a:ban:{r.id}")])
    else:
        rows.append(row3)
    rows.append([Btn("⏭ Next pending", f"n:{r.id}")] + HOME_BTN)
    return "\n".join(L), Kb(rows)

async def show_report(u, rid, note=None):
    with Session() as s:
        r = s.get(Report, rid)
        if not r:
            await show(u, "Report not found.", Kb([HOME_BTN])); return
        body, kb = report_view(s, r)
    await show(u, (note + "\n\n" if note else "") + body, kb)

async def fetch_photos(ids):
    out = []
    for fid in ids:
        f = await pub.bot.get_file(fid)
        out.append(bytes(await f.download_as_bytearray()))
    return out

def stats_text():
    with Session() as s:
        by = dict(s.query(Report.status, func.count()).group_by(Report.status).all())
        subs = s.query(Subject).count()
        tracked = s.query(Subject).filter(Subject.trackable.is_(True)).count()
        top = (s.query(Subject, func.count(Report.id).label("n")).join(Report, Report.subject_id == Subject.id)
               .filter(Report.status.in_(PUBLISHED)).group_by(Subject.id).order_by(desc("n")).limit(5).all())
        week = s.query(Report).filter(Report.created_at > now() - timedelta(days=7)).count()
        oldest = aware(s.query(func.min(Report.created_at)).filter(Report.status == "PENDING").scalar())
    L = ["📊 <b>Stats</b>", ""] + [f"{BADGE[k]}: {by.get(k, 0)}" for k in BADGE]
    L += ["", f"👥 People tracked: {subs} ({tracked} live)", f"🆕 Reports last 7 days: {week}"]
    if oldest: L.append(f"⏱ Oldest pending: {int((now() - oldest).total_seconds() // 3600)}h")
    if top:
        L += ["", "<b>Most reported</b>"] + [f"• {esc(label(sub))} — {n}" for sub, n in top]
    return "\n".join(L)

def safe_cell(x):
    x = "" if x is None else str(x)
    return "'" + x if x and x[0] in "=+-@\t\r" else x      # block CSV formula injection

def export_csv():
    out = io.StringIO(); w = csv.writer(out)
    w.writerow(["id", "status", "target_username", "target_id", "deal", "reporter_id", "reporter",
                "created_at", "approved_by", "published_at", "proof_link", "photos", "reason"])
    with Session() as s:
        for r, sub in (s.query(Report, Subject).join(Subject, Report.subject_id == Subject.id)
                       .order_by(Report.id)):
            w.writerow([safe_cell(x) for x in (r.id, r.status, sub.username, sub.user_id, r.deal_type,
                        r.reporter_id, r.reporter_name, r.created_at, r.approved_by, r.published_at,
                        r.proof_link, len(r.photo_ids or []), r.reason)])
    return out.getvalue().encode("utf-8-sig")

async def diagnostics():
    ok, msg = await channel_check()
    with Session() as s:
        pend = s.query(Report).filter_by(status="PENDING").count()
    tr = STATE["track_res"]
    up = int(time.time() - STARTED)
    return ("🩺 <b>Diagnostics</b>\n\n" + ("🟢 " if ok else "🔴 ") + esc(msg) +
            f"\n🤖 Public bot: @{esc(PUB_USERNAME)}\n👮 Admins: {len(ADMIN_IDS)}\n📥 Pending: {pend}\n"
            f"📡 Tracker: " + (f"last run {STATE['track_at']:%H:%M} UTC - {tr[0]}/{tr[2]} reachable"
                               if tr else "hasn't run yet") +
            f"\n⏱ Uptime: {up // 3600}h {up % 3600 // 60}m · DB: {engine.dialect.name}")

@admin_only
async def a_start(u, c):
    c.user_data.pop("await", None); await home(u, c)

@admin_only
async def a_cancel(u, c):
    c.user_data.pop("await", None); await u.message.reply_text("Cancelled. /menu")

@admin_only
async def a_export(u, c):
    await u.effective_message.reply_document(io.BytesIO(export_csv()), filename="reports.csv")

@admin_only
async def a_dbtest(u, c):
    """Tests every table separately and prints the exact database error, if any."""
    lines = []
    for model in MODELS:
        try:
            with Session() as s:
                lines.append(f"OK  {model.__tablename__}: {s.query(model).count()} rows")
        except Exception:
            lines.append(f"ERR {model.__tablename__}:\n{traceback.format_exc()[-500:]}")
    await u.message.reply_text("\n".join(lines)[:3900])

def confirm_kb(yes, rid, label_yes):
    return Kb([[Btn(label_yes, yes), Btn("↩️ Cancel", f"r:{rid}")]])

@admin_only
async def a_cb(u, c):
    q = u.callback_query; await q.answer()
    p = q.data.split(":"); k = p[0]
    me = f"admin:{u.effective_user.id}"

    if k == "m":
        act = p[1]
        if act == "home": c.user_data.pop("await", None); await home(u, c)
        elif act == "search":
            c.user_data["await"] = ("search", None)
            await show(u, "🔎 Send @username, numeric ID or part of a username.", Kb([HOME_BTN]))
        elif act == "stats": await show(u, stats_text(), Kb([HOME_BTN]))
        elif act == "diag": await show(u, await diagnostics(), Kb([HOME_BTN]))
        elif act == "export":
            await c.bot.send_document(u.effective_chat.id, io.BytesIO(export_csv()), filename="reports.csv")
        elif act == "track":
            await show(u, "📡 Checking all tracked users…")
            n, ch = await track_all()
            await show(u, f"📡 Done. Reachable: {n}, username changes: {ch}.", Kb([HOME_BTN]))
        elif act == "bans":
            with Session() as s:
                ids = [b.user_id for b in s.query(Banned).all()]
            kb = Kb([[Btn(f"Unban {i}", f"u:{i}")] for i in ids] + [HOME_BTN])
            await show(u, "🚫 <b>Banned reporters</b>" + ("" if ids else "\n\nNone."), kb)

    elif k == "l":
        body, kb = list_view(p[1], int(p[2])); await show(u, body, kb)

    elif k == "r": await show_report(u, int(p[1]))

    elif k == "n":
        with Session() as s:
            nxt = (s.query(Report).filter(Report.status == "PENDING", Report.id > int(p[1])).order_by(Report.id).first()
                   or s.query(Report).filter(Report.status == "PENDING", Report.id != int(p[1])).order_by(Report.id).first())
            nid = nxt.id if nxt else None
        if nid: await show_report(u, nid)
        else: await show(u, "🎉 No other pending reports.", Kb([HOME_BTN]))

    elif k == "u":
        with Session() as s:
            b = s.get(Banned, int(p[1]))
            if b: s.delete(b); s.commit()
        await show(u, "✅ Unbanned.", Kb([HOME_BTN]))

    elif k == "a":
        act, rid = p[1], int(p[2])
        with Session() as s:
            r = s.get(Report, rid)
            if not r: await show(u, "Report not found.", Kb([HOME_BTN])); return
            status, sub_id, reporter = r.status, r.subject_id, r.reporter_id
            photos = list(r.photo_ids or [])

        if act == "ok":
            if not claim(rid, "PENDING", "VERIFIED", me, "Approved"):
                await show_report(u, rid, "ℹ️ Already handled by someone else."); return
            with Session() as s:
                s.get(Report, rid).approved_by = me; s.commit()
            try:
                await publish(rid)
            except TelegramError as e:
                claim(rid, "VERIFIED", "PENDING", "system", "Post failed")
                await show(u, f"❌ Couldn't post to the channel: <code>{esc(e)}</code>\n\nMake the <b>public bot</b> an "
                              "admin of the channel with permission to post, and check CHANNEL_ID (see 🩺 Diagnostics).",
                           Kb([[Btn("↩️ Back", f"r:{rid}")], HOME_BTN])); return
            await refresh_subject(sub_id)
            with Session() as s: link = post_url(s.get(Report, rid))
            await tell(reporter, f"✅ Your report #{rid} was <b>approved</b> and posted on the channel."
                                 + (f'\n<a href="{link}">View post</a>' if link else ""))
            await show_report(u, rid, "✅ Approved & posted.")

        elif act == "rj":
            kb = Kb([[Btn(v, f"a:rjr:{rid}:{kk}")] for kk, v in REJECT_REASONS.items()]
                    + [[Btn("✍️ Custom reason", f"a:rjc:{rid}"), Btn("No reason", f"a:rjr:{rid}:none")],
                       [Btn("↩️ Back", f"r:{rid}")]])
            await show(u, f"❌ Reject report #{rid} — pick a reason (sent to the reporter):", kb)

        elif act == "rjc":
            c.user_data["await"] = ("rejc", rid)
            await show(u, f"✍️ Send the rejection reason for report #{rid}.\n/cancel to abort.",
                       Kb([[Btn("↩️ Back", f"r:{rid}")]]))

        elif act == "rjr":
            reason = REJECT_REASONS.get(p[3])
            if not claim(rid, "PENDING", "REJECTED", me, reason):
                await show_report(u, rid, "ℹ️ Already handled."); return
            await tell(reporter, f"❌ Your report #{rid} was rejected." + (f"\nReason: {esc(reason)}" if reason else ""))
            await show_report(u, rid, "❌ Rejected.")

        elif act == "st":
            new = p[3]
            if new not in PUBLISHED or status not in PUBLISHED or new == status: return
            if not claim(rid, PUBLISHED, new, me): return
            await refresh_subject(sub_id)
            await tell(reporter, f"ℹ️ Report #{rid} status changed to <b>{BADGE[new]}</b>.")
            await notify_appellants(rid, f"ℹ️ The report #{rid} you appealed is now <b>{BADGE[new]}</b>.")
            await show_report(u, rid, f"Status → {BADGE[new]} (channel post updated)")

        elif act == "rf":
            await refresh_subject(sub_id); await show_report(u, rid, "🔄 Post(s) refreshed.")

        elif act == "ret":
            await show(u, f"🗑 Delete report #{rid} from the channel and mark it rejected?",
                       confirm_kb(f"a:rety:{rid}", rid, "Yes, retract"))

        elif act == "rety":
            await retract(rid)
            claim(rid, PUBLISHED, "REJECTED", me, "Retracted")
            await refresh_subject(sub_id)
            await tell(reporter, f"ℹ️ Report #{rid} was removed from the channel.")
            await notify_appellants(rid, f"✅ The report #{rid} you appealed was removed from the channel.")
            await show_report(u, rid, "🗑 Retracted & deleted from channel.")

        elif act == "rest":
            claim(rid, "REJECTED", "PENDING", me, "Restored")
            await show_report(u, rid, "↩️ Restored to pending.")

        elif act == "ed":
            c.user_data["await"] = ("edit", rid)
            await show(u, f"✏️ <b>Report #{rid}</b> — send the new <b>@username</b>, a <b>numeric ID</b>, or "
                          "<b>forward a message from the user</b> (gets both ID + current @).\n/cancel to abort.",
                       Kb([[Btn("↩️ Back", f"r:{rid}")]]))

        elif act == "ask":
            c.user_data["await"] = ("ask", rid)
            await show(u, f"❓ Type your question for the reporter of #{rid}. They can reply with text and screenshots.\n"
                          "/cancel to abort.", Kb([[Btn("↩️ Back", f"r:{rid}")]]))

        elif act == "nt":
            c.user_data["await"] = ("note", rid)
            await show(u, f"📝 Type an internal note for report #{rid} (only admins see it).\n/cancel to abort.",
                       Kb([[Btn("↩️ Back", f"r:{rid}")]]))

        elif act == "hx":
            with Session() as s:
                logs = s.query(StatusLog).filter_by(report_id=rid).order_by(desc(StatusLog.at)).limit(25).all()
            body = f"📜 <b>History · report #{rid}</b>\n\n" + "\n".join(
                f"• {l.at:%d %b %H:%M} · {BADGE.get(l.status, l.status)} · {esc(l.by)}"
                + (f" — {esc(l.note)}" if l.note else "") for l in reversed(logs))
            await show(u, body, Kb([[Btn("↩️ Back", f"r:{rid}")]]))

        elif act == "px":
            try:
                imgs = await fetch_photos(photos)
                if len(imgs) == 1: await c.bot.send_photo(u.effective_chat.id, imgs[0])
                else: await c.bot.send_media_group(u.effective_chat.id, [InputMediaPhoto(b) for b in imgs[:10]])
            except TelegramError as e:
                await c.bot.send_message(u.effective_chat.id, f"Couldn't load proofs: {e}")

        elif act == "ban":
            await show(u, f"🚫 Ban reporter <code>{reporter}</code> from filing reports?",
                       confirm_kb(f"a:bany:{rid}", rid, "Yes, ban"))

        elif act == "bany":
            with Session() as s:
                if not s.get(Banned, reporter): s.add(Banned(user_id=reporter)); s.commit()
            await show_report(u, rid, f"🚫 Reporter {reporter} banned from filing reports.")

@admin_only
async def a_text(u, c):
    st = c.user_data.get("await")
    if not st:
        await home(u, c); return
    kind, rid = st
    m = u.message
    me = f"admin:{u.effective_user.id}"
    body = (m.text or "").strip()

    if kind == "search":
        c.user_data.pop("await")
        t = body.lstrip("@")
        with Session() as s:
            q = s.query(Report).join(Subject, Report.subject_id == Subject.id)
            if t.isdigit():
                q = q.filter((Subject.user_id == int(t)) | (Report.id == int(t)))
            else:
                pat = t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                ids = [x.subject_id for x in s.query(Alias).filter(Alias.username.ilike(f"%{pat}%", escape="\\"))]
                q = q.filter(Subject.id.in_(ids or [0]))
            rows = q.order_by(desc(Report.id)).limit(15).all()
            btns = [[Btn(f"#{r.id} {label(s.get(Subject, r.subject_id))} · {BADGE[r.status]}", f"r:{r.id}")] for r in rows]
        await show(u, f"🔎 {len(rows)} result(s) for <b>{esc(t)}</b>", Kb(btns + [HOME_BTN]))
        return

    if kind == "edit":
        uid, uname, name, err = parse_identity(m)
        if err: await show(u, "⚠️ " + esc(err)); return
        c.user_data.pop("await")
        with Session() as s:
            r = s.get(Report, rid); sub = s.get(Subject, r.subject_id); old = None
            if uid and sub.user_id != uid:
                other = s.query(Subject).filter_by(user_id=uid).first()
                if other and other.id != sub.id:
                    merge_subjects(s, keep=other, drop=sub); sub = other
                else:
                    sub.user_id = uid
            if uname: old = set_username(s, sub, uname)
            if name: sub.first_name = name
            sub.trackable = None
            s.commit(); sid = sub.id
        await refresh_subject(sid)
        await show_report(u, rid, "✏️ Identity updated" + (f" (@{esc(old)} → @{esc(uname)})" if old else "") +
                                  " — channel posts refreshed.")
        return

    if not body:
        await show(u, "Please send text."); return
    c.user_data.pop("await")

    if kind == "ask":
        with Session() as s:
            r = s.get(Report, rid); reporter = r.reporter_id
            s.add(Note(report_id=rid, kind="admin", author=me, text="Asked reporter: " + body[:1000])); s.commit()
        await tell(reporter, f"❓ <b>An admin has a question about your report #{rid}</b>\n\n{esc(body)}",
                   Kb([[Btn("💬 Reply", f"ra:{rid}")]]))
        await show_report(u, rid, "❓ Question sent to the reporter.")

    elif kind == "note":
        with Session() as s:
            s.add(Note(report_id=rid, kind="admin", author=me, text=body[:1500])); s.commit()
        await show_report(u, rid, "📝 Note saved.")

    elif kind == "rejc":
        reason = body[:180]
        if not claim(rid, "PENDING", "REJECTED", me, reason):
            await show_report(u, rid, "ℹ️ Already handled."); return
        with Session() as s: reporter = s.get(Report, rid).reporter_id
        await tell(reporter, f"❌ Your report #{rid} was rejected.\nReason: {esc(reason)}")
        await show_report(u, rid, "❌ Rejected.")

def register_admin(app):
    app.add_handler(CommandHandler(["start", "menu"], a_start))
    app.add_handler(CommandHandler("cancel", a_cancel))
    app.add_handler(CommandHandler("export", a_export))
    app.add_handler(CommandHandler("dbtest", a_dbtest))
    app.add_handler(CallbackQueryHandler(a_cb))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, a_text))

# ═════════════════════════ run ═════════════════════════
async def on_error(u, c):
    log.error("handler error", exc_info=c.error)
    if isinstance(c.error, NetworkError): return                 # transient network hiccup
    if time.time() - STATE["err_at"] > 600:                      # at most one alert / 10 min
        STATE["err_at"] = time.time()
        await alert_admins(f"⚠️ <b>Bot error</b>\n<code>{esc(repr(c.error)[:300])}</code>")

def health_server():
    """Tiny web server so Render sees an open port and an uptime pinger can keep the service awake."""
    port = os.getenv("PORT")
    if not port: return
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        def do_HEAD(self):                                       # UptimeRobot free plan uses HEAD
            self.send_response(200); self.end_headers()
        def log_message(self, *a): pass
    threading.Thread(target=HTTPServer(("0.0.0.0", int(port)), H).serve_forever, daemon=True).start()

async def main():
    global pub, adm, PUB_USERNAME
    health_server()                                              # answer health checks while we wait for the lock
    init_db()
    lock = await asyncio.to_thread(acquire_lock)
    pub = Application.builder().token(PUB_TOKEN).build()
    adm = Application.builder().token(ADM_TOKEN).build()
    register_public(pub); register_admin(adm)
    pub.add_error_handler(on_error); adm.add_error_handler(on_error)
    for a in (pub, adm):
        await a.initialize(); await a.start()
    PUB_USERNAME = pub.bot.username
    await pub.bot.set_my_commands([BotCommand("start", "Menu"), BotCommand("report", "File a report"),
                                   BotCommand("status", "Look up a user"), BotCommand("my", "My reports"),
                                   BotCommand("track", "Track a report by ID"),
                                   BotCommand("appeal", "Appeal a report about you"), BotCommand("cancel", "Cancel")])
    await adm.bot.set_my_commands([BotCommand("menu", "Admin panel"), BotCommand("export", "Export reports CSV"),
                                   BotCommand("dbtest", "Test the database"), BotCommand("cancel", "Cancel action")])
    for a in (pub, adm):
        await a.updater.start_polling(drop_pending_updates=True)
    ok, msg = await channel_check()
    (log.info if ok else log.error)("%s", msg)
    if not ok: await alert_admins("⚠️ <b>Channel problem</b>\n" + esc(msg))
    bad = await asyncio.to_thread(db_selfcheck)
    if bad:
        log.error("DATABASE PROBLEMS: %s", bad)
        await alert_admins("🔴 <b>Database problem at startup</b>\n<code>" + esc("\n".join(bad))[:3000] + "</code>")
    else:
        log.info("Database self-check OK.")
    log.info("Running. Public bot @%s | admins: %s", PUB_USERNAME, sorted(ADMIN_IDS))
    tasks = [asyncio.create_task(tracker_loop()), asyncio.create_task(reminder_loop())]
    try:
        await asyncio.Event().wait()
    finally:
        for t in tasks: t.cancel()
        for a in (pub, adm):
            await a.updater.stop(); await a.stop(); await a.shutdown()
        if lock is not None: lock.close()

if __name__ == "__main__":
    asyncio.run(main())