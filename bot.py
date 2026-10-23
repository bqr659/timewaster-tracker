"""
Timewaster Tracker v2  -  no website, two Telegram bots in one process.

PUBLIC BOT  (BOT_TOKEN)        : file reports, /check users, track your own reports
ADMIN BOT   (ADMIN_BOT_TOKEN)  : button panel -> approve / reject / edit @ / change status
CHANNEL     (CHANNEL_ID)       : approved reports are auto-posted (photos + details),
                                 and edited automatically when the status or @ changes.
TRACKER                        : follows users by numeric ID, detects @ swaps, updates posts.
"""
import os, re, html, asyncio, logging, threading
from datetime import datetime, timezone
from functools import wraps
from http.server import BaseHTTPRequestHandler, HTTPServer

from sqlalchemy import (create_engine, Column, Integer, BigInteger, String, Text,
                        DateTime, JSON, Boolean, ForeignKey, func, desc)
from sqlalchemy.orm import declarative_base, sessionmaker
from telegram import (Update, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb,
                      InputMediaPhoto, BotCommand, LinkPreviewOptions,
                      MessageOriginUser, MessageOriginHiddenUser)
from telegram.constants import ParseMode, ChatType
from telegram.error import TelegramError, BadRequest
from telegram.ext import (Application, CommandHandler, MessageHandler, CallbackQueryHandler,
                          ConversationHandler, ContextTypes, TypeHandler, filters)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tw")

# ───────────────────────── config ─────────────────────────
PUB_TOKEN = os.environ["BOT_TOKEN"]
ADM_TOKEN = os.environ["ADMIN_BOT_TOKEN"]
_ch = os.environ["CHANNEL_ID"].strip()
CHANNEL = int(_ch) if _ch.lstrip("-").isdigit() else _ch
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "").lstrip("@")      # optional, for post links
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
DB_URL = os.environ["DATABASE_URL"].replace("postgres://", "postgresql://", 1)
TRACK_MIN = int(os.getenv("TRACK_INTERVAL_MIN", "30"))
MAX_PENDING = int(os.getenv("MAX_PENDING_PER_USER", "3"))
MAX_PHOTOS = 8

DEALS = ["Account Trade", "Middleman Deal", "Service", "Other"]
PUBLISHED = ("VERIFIED", "DISPUTED", "RESOLVED")
BADGE = {"PENDING": "⏳ PENDING", "VERIFIED": "✅ VERIFIED", "DISPUTED": "⚠️ DISPUTED",
         "RESOLVED": "🟢 RESOLVED", "REJECTED": "❌ REJECTED"}
HTML = ParseMode.HTML
NOPREV = LinkPreviewOptions(is_disabled=True)
REJECT_REASONS = {"np": "Not enough proof", "dup": "Duplicate report",
                  "fake": "Fake / abusive report", "na": "Not a timewaster case"}

pub: Application = None
adm: Application = None
PUB_USERNAME = ""

# ───────────────────────── database ─────────────────────────
engine = create_engine(DB_URL, pool_pre_ping=True, pool_recycle=300)
Session = sessionmaker(engine, expire_on_commit=False)
Base = declarative_base()

def now(): return datetime.now(timezone.utc)

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
    created_at = Column(DateTime(timezone=True), default=now)

class StatusLog(Base):                    # the tracking timeline
    __tablename__ = "tw_status_log"
    id = Column(Integer, primary_key=True)
    report_id = Column(Integer, ForeignKey("tw_reports.id"), index=True)
    status = Column(String(16))
    by = Column(String(64))
    note = Column(String(200), nullable=True)
    at = Column(DateTime(timezone=True), default=now)

class Banned(Base):
    __tablename__ = "tw_banned"
    user_id = Column(BigInteger, primary_key=True)

Base.metadata.create_all(engine)

# ───────────────────────── helpers ─────────────────────────
def esc(x): return html.escape(str(x if x is not None else ""))
def label(sub): return f"@{sub.username}" if sub.username else f"ID {sub.user_id}"
USERNAME_RE = re.compile(r"[A-Za-z0-9_]{4,32}")

def log_status(s, r, status, by, note=None):
    r.status = status
    s.add(StatusLog(report_id=r.id, status=status, by=by, note=note))

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
    return f"https://t.me/{CHANNEL_USERNAME}/{r.chan_msgs[0]}" if CHANNEL_USERNAME and r.chan_msgs else None

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

async def tell(uid, text, kb=None):
    try:
        await pub.bot.send_message(uid, text, parse_mode=HTML, reply_markup=kb, link_preview_options=NOPREV)
    except TelegramError: pass

async def alert_admins(text, kb=None):
    for a in ADMIN_IDS:
        try:
            await adm.bot.send_message(a, text, parse_mode=HTML, reply_markup=kb, link_preview_options=NOPREV)
        except TelegramError: pass

# ───────────────────────── channel posting ─────────────────────────
def render_post(s, r, limit):
    sub = s.get(Subject, r.subject_id)
    al = aliases_of(s, sub)
    base = s.query(Report).filter(Report.subject_id == sub.id, Report.status.in_(PUBLISHED))
    total, ver = base.count(), base.filter(Report.status == "VERIFIED").count()

    def build(reason):
        L = [f"🚨 <b>TIMEWASTER REPORT #{r.id}</b>", f"Status: <b>{BADGE[r.status]}</b>", "",
             f"👤 <b>{esc(label(sub))}</b>"]
        if sub.user_id: L.append(f"🆔 <code>{sub.user_id}</code>")
        if al: L.append("🔄 Also seen as: " + ", ".join("@" + esc(a) for a in al[-5:]))
        L += [f"🏷 {esc(r.deal_type)}", f"📅 {r.created_at:%d %b %Y}", "", f"📝 {esc(reason)}"]
        if r.proof_link: L.append(f'\n🔗 <a href="{esc(r.proof_link)}">Proof channel</a>')
        L.append(f"\n📊 Reports on this user: {total} ({ver} verified)")
        if PUB_USERNAME: L.append(f"🤖 Check anyone: @{PUB_USERNAME}")
        return "\n".join(L)

    reason = r.reason or ""
    text = build(reason)
    while len(text) > limit and len(reason) > 21:
        reason = reason[:max(20, len(reason) - (len(text) - limit) - 5)].rstrip() + "…"
        text = build(reason)
    return text

async def publish(rid):
    with Session() as s:
        r = s.get(Report, rid)
        photos = list(r.photo_ids or [])
        text = render_post(s, r, 1024 if photos else 4000)
    if not photos:
        ms = [await pub.bot.send_message(CHANNEL, text, parse_mode=HTML, link_preview_options=NOPREV)]
    elif len(photos) == 1:
        ms = [await pub.bot.send_photo(CHANNEL, photos[0], caption=text, parse_mode=HTML)]
    else:
        media = [InputMediaPhoto(photos[0], caption=text, parse_mode=HTML)] + \
                [InputMediaPhoto(p) for p in photos[1:10]]
        ms = await pub.bot.send_media_group(CHANNEL, media=media)
    with Session() as s:
        r = s.get(Report, rid)
        r.chan_msgs = [m.message_id for m in ms]
        r.chan_has_photo = bool(photos)
        s.commit()

async def refresh_post(rid):
    with Session() as s:
        r = s.get(Report, rid)
        if not r or not r.chan_msgs: return
        photo, mid = r.chan_has_photo, r.chan_msgs[0]
        text = render_post(s, r, 1024 if photo else 4000)
    try:
        if photo:
            await pub.bot.edit_message_caption(CHANNEL, mid, caption=text, parse_mode=HTML)
        else:
            await pub.bot.edit_message_text(text, chat_id=CHANNEL, message_id=mid,
                                            parse_mode=HTML, link_preview_options=NOPREV)
    except BadRequest as e:
        if "not modified" not in str(e).lower(): log.warning("refresh #%s: %s", rid, e)
    except TelegramError as e:
        log.warning("refresh #%s: %s", rid, e)

async def refresh_subject(sub_id):
    with Session() as s:
        ids = [r.id for r in s.query(Report).filter(Report.subject_id == sub_id, Report.status.in_(PUBLISHED))]
    for i in ids: await refresh_post(i)

async def retract(rid):
    with Session() as s:
        r = s.get(Report, rid); ids = list(r.chan_msgs or [])
    for mid in ids:
        try: await pub.bot.delete_message(CHANNEL, mid)
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
            chat = await pub.bot.get_chat(uid)
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
    return checked, changed

async def tracker_loop():
    await asyncio.sleep(30)
    while True:
        try: await track_all()
        except Exception: log.exception("tracker")
        await asyncio.sleep(TRACK_MIN * 60)

async def passive_track(u: Update, c):
    """If a tracked user ever talks to the public bot, we learn their current @ for free."""
    usr = u.effective_user
    if not usr or usr.is_bot: return
    with Session() as s:
        sub = s.query(Subject).filter_by(user_id=usr.id).first()
        if not sub: return
        old = set_username(s, sub, usr.username) if usr.username else None
        sub.trackable, sub.last_check = True, now(); s.commit(); sid = sub.id
    if old: await on_username_change(sid, old, usr.username)

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
    await say(u, "⏳ <b>Timewaster Tracker</b>\nReport flakers, check who you're dealing with, "
                 "and follow your reports.", pub_home_kb())

async def p_help(u, c):
    if u.callback_query: await u.callback_query.answer()
    await say(u, "<b>How it works</b>\n1. File a report with proof (screenshots + optional proof channel).\n"
                 "2. An admin reviews it. If approved it's posted on the channel with your proof.\n"
                 "3. You get a message whenever the status changes.\n\n"
                 "/report · /check · /my · /track &lt;id&gt; · /cancel", pub_home_kb())

async def rep_start(u, c):
    if u.callback_query: await u.callback_query.answer()
    uid = u.effective_user.id
    with Session() as s:
        if s.get(Banned, uid):
            await say(u, "🚫 You can't file reports."); return ConversationHandler.END
        pend = s.query(Report).filter_by(reporter_id=uid, status="PENDING").count()
    if pend >= MAX_PENDING:
        await say(u, f"You already have {pend} reports waiting for review. Please wait for those first.")
        return ConversationHandler.END
    c.user_data.pop("await", None)
    c.user_data["rep"] = {"photos": []}
    await say(u, "<b>Step 1/4 · Who are you reporting?</b>\nSend their @username or numeric ID.\n"
                 "💡 <i>Best:</i> forward me any message from them — I'll capture their ID, so reports "
                 "follow them even if they change @.", Kb([[Btn("✖️ Cancel", "cancel")]]))
    return TARGET

async def got_target(u, c):
    m = u.message
    uid, uname, name, err = parse_identity(m)
    if err:
        await say(u, "⚠️ " + err); return TARGET
    if uid == u.effective_user.id:
        await say(u, "You can't report yourself 🙂"); return TARGET
    if uname and not uid:               # best-effort: resolve @ -> ID
        try:
            ch = await c.bot.get_chat("@" + uname)
            if ch.type == ChatType.PRIVATE: uid, name = ch.id, ch.full_name
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
    await say(u, f"<b>Step 4/4 · Proof</b>\nSend your screenshots here (up to {MAX_PHOTOS}) and/or a link to a "
                 "Telegram channel with your proof. Press <b>Done</b> when finished.",
              Kb([[Btn("✅ Done", "pf:done")]]))
    return PROOF

async def got_photo(u, c):
    rep = c.user_data["rep"]
    if len(rep["photos"]) >= MAX_PHOTOS:
        await say(u, f"Max {MAX_PHOTOS} photos reached."); return PROOF
    rep["photos"].append(u.message.photo[-1].file_id)
    await say(u, f"📸 {len(rep['photos'])}/{MAX_PHOTOS} saved.", Kb([[Btn("✅ Done", "pf:done")]]))
    return PROOF

async def got_link(u, c):
    t = (u.message.text or "").strip()
    if t.lower().startswith(("t.me/", "telegram.me/")): t = "https://" + t
    if not t.lower().startswith("https://") or " " in t or len(t) > 300:
        await say(u, "Send a valid link like https://t.me/yourchannel, or photos."); return PROOF
    c.user_data["rep"]["link"] = t
    await say(u, "🔗 Link saved.", Kb([[Btn("✅ Done", "pf:done")]]))
    return PROOF

async def proof_done(u, c):
    if u.callback_query: await u.callback_query.answer()
    r = c.user_data["rep"]
    who = f"@{r['uname']}" if r.get("uname") else f"ID {r['uid']}"
    await say(u, f"<b>Please confirm</b>\n\n👤 {esc(who)}" + (f" (ID <code>{r['uid']}</code>)" if r.get("uid") and r.get("uname") else "") +
                 f"\n🏷 {esc(r['deal'])}\n📝 {esc(r['reason'])}\n📸 {len(r['photos'])} photo(s)"
                 f"\n🔗 {esc(r.get('link') or 'no link')}\n\nSubmit this report?",
              Kb([[Btn("✅ Submit", "c:yes"), Btn("✖️ Cancel", "c:no")]]))
    return CONFIRM

async def submit(u, c):
    q = u.callback_query; await q.answer()
    d, usr = c.user_data.get("rep"), u.effective_user
    if not d: return ConversationHandler.END
    with Session() as s:
        sub = upsert_subject(s, d.get("uname"), d.get("uid"), d.get("name"))
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

# ---- check ----
def lookup_text(t):
    t = t.strip().lstrip("@")
    with Session() as s:
        out = []
        for sub in find_subjects(s, t):
            reps = (s.query(Report).filter(Report.subject_id == sub.id, Report.status.in_(PUBLISHED))
                    .order_by(desc(Report.created_at)).all())
            if not reps: continue
            al = aliases_of(s, sub)
            ver = sum(r.status == "VERIFIED" for r in reps)
            head = f"🔎 <b>{esc(label(sub))}</b>" + (f"  🆔 <code>{sub.user_id}</code>" if sub.user_id else "")
            if al: head += "\n🔄 Also seen as: " + ", ".join("@" + esc(a) for a in al[-6:])
            lines = [head, f"⚠️ <b>{len(reps)}</b> public report(s) · {ver} verified", ""]
            for r in reps[:8]:
                url = post_url(r)
                lines.append(f"• {BADGE[r.status]} · {esc(r.deal_type)} · {r.created_at:%d %b %Y}"
                             + (f' — <a href="{url}">post</a>' if url else ""))
            out.append("\n".join(lines))
    return "\n\n".join(out) or f"✅ No public reports for <b>{esc(t)}</b>."

async def p_check(u, c):
    if c.args:
        await say(u, lookup_text(c.args[0])); return
    c.user_data["await"] = "check"
    await say(u, "Send the @username or ID to look up.")

async def p_text(u, c):
    if c.user_data.get("await") == "check" and u.message and u.message.text:
        c.user_data.pop("await")
        await say(u, lookup_text(u.message.text), pub_home_kb())

# ---- my reports / tracking ----
def timeline(rid, uid):
    with Session() as s:
        r = s.get(Report, rid)
        if not r or r.reporter_id != uid: return "Report not found."
        sub = s.get(Subject, r.subject_id)
        logs = s.query(StatusLog).filter_by(report_id=rid).order_by(StatusLog.at).all()
        L = [f"📄 <b>Report #{r.id}</b> on {esc(label(sub))}", f"Status: <b>{BADGE[r.status]}</b>", "", "<b>Timeline</b>"]
        for l in logs:
            L.append(f"• {l.at:%d %b %H:%M} — {BADGE.get(l.status, l.status)}" + (f" ({esc(l.note)})" if l.note else ""))
        if post_url(r): L.append(f'\n<a href="{post_url(r)}">View channel post</a>')
        return "\n".join(L)

async def p_mine(u, c):
    if u.callback_query: await u.callback_query.answer()
    with Session() as s:
        rows = s.query(Report).filter_by(reporter_id=u.effective_user.id).order_by(desc(Report.id)).limit(15).all()
        btns = [[Btn(f"#{r.id} {label(s.get(Subject, r.subject_id))} · {BADGE[r.status]}", f"mr:{r.id}")] for r in rows]
    if not btns: await say(u, "You haven't filed any reports yet.", pub_home_kb()); return
    await say(u, "📋 <b>Your reports</b> (tap to track)", Kb(btns))

async def p_mr(u, c):
    q = u.callback_query; await q.answer()
    await q.message.reply_text(timeline(int(q.data.split(":")[1]), u.effective_user.id),
                               parse_mode=HTML, link_preview_options=NOPREV)

async def p_track(u, c):
    if not c.args or not c.args[0].lstrip("#").isdigit():
        await say(u, "Usage: /track &lt;report id&gt;"); return
    await say(u, timeline(int(c.args[0].lstrip("#")), u.effective_user.id))

async def p_menu(u, c):
    q = u.callback_query; await q.answer()
    act = q.data.split(":")[1]
    if act == "check":
        c.user_data["await"] = "check"; await q.message.reply_text("Send the @username or ID to look up.")
    elif act == "mine": await p_mine(u, c)
    elif act == "help": await p_help(u, c)

def register_public(app):
    app.add_handler(TypeHandler(Update, passive_track), group=-1)
    conv = ConversationHandler(
        entry_points=[CommandHandler("report", rep_start),
                      CallbackQueryHandler(rep_start, pattern=r"^pm:report$")],
        states={
            TARGET: [MessageHandler((filters.TEXT | filters.FORWARDED) & ~filters.COMMAND, got_target)],
            DEAL: [CallbackQueryHandler(got_deal, pattern=r"^d:\d$")],
            REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_reason)],
            PROOF: [MessageHandler(filters.PHOTO, got_photo),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, got_link),
                    CallbackQueryHandler(proof_done, pattern=r"^pf:done$"),
                    CommandHandler("done", proof_done)],
            CONFIRM: [CallbackQueryHandler(submit, pattern=r"^c:yes$")],
        },
        fallbacks=[CommandHandler("cancel", cancel),
                   CallbackQueryHandler(cancel, pattern=r"^(c:no|cancel)$")],
        allow_reentry=True)
    app.add_handler(conv)
    app.add_handler(CommandHandler("start", p_start))
    app.add_handler(CommandHandler("help", p_help))
    app.add_handler(CommandHandler("check", p_check))
    app.add_handler(CommandHandler("my", p_mine))
    app.add_handler(CommandHandler("track", p_track))
    app.add_handler(CallbackQueryHandler(p_menu, pattern=r"^pm:(check|mine|help)$"))
    app.add_handler(CallbackQueryHandler(p_mr, pattern=r"^mr:\d+$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, p_text), group=1)

# ═════════════════════════ ADMIN BOT ═════════════════════════
def admin_only(fn):
    @wraps(fn)
    async def w(u, c):
        if u.effective_user.id not in ADMIN_IDS:
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
             [Btn("📡 Run tracker now", "m:track"), Btn("🚫 Banned reporters", "m:bans")]])
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
    track = ("🟢 live (auto-detects @ changes)" if sub.trackable else
             "🔴 can't auto-track (user must have started the public bot)" if sub.trackable is False else
             "🟡 not checked yet") if sub.user_id else "⚪ no ID yet → tap ✏️ and forward a message from them"
    L = [f"📄 <b>Report #{r.id}</b> — {BADGE[r.status]}", "",
         f"👤 <b>{esc(label(sub))}</b>",
         f"🆔 {'<code>%s</code>' % sub.user_id if sub.user_id else 'unknown'}"]
    if al: L.append("🔄 Past @: " + ", ".join("@" + esc(a) for a in al))
    L += [f"📡 {track}", f"🏷 {esc(r.deal_type)}", f"👮 Reporter: {esc(r.reporter_name)} (<code>{r.reporter_id}</code>)",
          f"📅 {r.created_at:%d %b %Y %H:%M}", f"📊 Other reports on this user: {others}", "",
          f"📝 {esc(r.reason)}"]
    if r.proof_link: L.append(f'\n🔗 <a href="{esc(r.proof_link)}">Proof link</a>')
    L.append(f"🖼 {len(r.photo_ids or [])} photo(s)")
    if post_url(r): L.append(f'📢 <a href="{post_url(r)}">Channel post</a>')
    elif r.chan_msgs: L.append("📢 Posted on channel")

    rows = []
    if r.status == "PENDING":
        rows.append([Btn("✅ Approve & post", f"a:ok:{r.id}"), Btn("❌ Reject", f"a:rj:{r.id}")])
    elif r.status in PUBLISHED:
        rows.append([Btn(("✓ " if r.status == k else "") + k.title(), f"a:st:{r.id}:{k}") for k in PUBLISHED])
    else:
        rows.append([Btn("↩️ Restore to pending", f"a:rest:{r.id}")])
    row2 = [Btn("✏️ Edit @ / ID", f"a:ed:{r.id}")]
    if r.photo_ids: row2.append(Btn(f"🖼 Proofs ({len(r.photo_ids)})", f"a:px:{r.id}"))
    rows.append(row2)
    row3 = [Btn("🚫 Ban reporter", f"a:ban:{r.id}")]
    if r.status in PUBLISHED:
        row3 = [Btn("🔄 Refresh post", f"a:rf:{r.id}"), Btn("🗑 Retract", f"a:ret:{r.id}")] + row3
    rows.append(row3)
    rows.append([Btn("⏭ Next pending", f"n:{r.id}")] + HOME_BTN)
    return "\n".join(L), Kb(rows)

async def show_report(u, rid, note=None):
    with Session() as s:
        r = s.get(Report, rid)
        if not r:
            await show(u, "Report not found.", Kb([HOME_BTN])); return
        text, kb = report_view(s, r)
    await show(u, (note + "\n\n" if note else "") + text, kb)

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
        week = s.query(Report).filter(Report.created_at > now().replace(hour=0, minute=0) - __import__("datetime").timedelta(days=7)).count()
    L = ["📊 <b>Stats</b>", ""] + [f"{BADGE[k]}: {by.get(k, 0)}" for k in BADGE]
    L += ["", f"👥 People tracked: {subs} ({tracked} live)", f"🆕 Reports last 7 days: {week}"]
    if top:
        L += ["", "<b>Most reported</b>"] + [f"• {esc(label(sub))} — {n}" for sub, n in top]
    return "\n".join(L)

@admin_only
async def a_start(u, c):
    c.user_data.pop("await", None); await home(u, c)

@admin_only
async def a_cancel(u, c):
    c.user_data.pop("await", None); await u.message.reply_text("Cancelled. /menu")

@admin_only
async def a_cb(u, c):
    q = u.callback_query; await q.answer()
    p = q.data.split(":"); k = p[0]

    if k == "m":
        act = p[1]
        if act == "home": c.user_data.pop("await", None); await home(u, c)
        elif act == "search":
            c.user_data["await"] = ("search", None)
            await show(u, "🔎 Send @username, numeric ID or part of a username.", Kb([HOME_BTN]))
        elif act == "stats": await show(u, stats_text(), Kb([HOME_BTN]))
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
        text, kb = list_view(p[1], int(p[2])); await show(u, text, kb)

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
            if status in PUBLISHED: await show_report(u, rid, "Already posted."); return
            with Session() as s:
                r = s.get(Report, rid); log_status(s, r, "VERIFIED", f"admin:{u.effective_user.id}", "Approved"); s.commit()
            try:
                await publish(rid)
            except TelegramError as e:
                with Session() as s:
                    r = s.get(Report, rid); log_status(s, r, "PENDING", "system", "Post failed"); s.commit()
                await show(u, f"❌ Couldn't post to the channel: <code>{esc(e)}</code>\n\nMake the <b>public bot</b> an "
                              "admin of the channel with permission to post, and check CHANNEL_ID.",
                           Kb([[Btn("↩️ Back", f"r:{rid}")], HOME_BTN])); return
            await refresh_subject(sub_id)
            await tell(reporter, f"✅ Your report #{rid} was <b>approved</b> and posted on the channel.")
            await show_report(u, rid, "✅ Approved & posted.")

        elif act == "rj":
            kb = Kb([[Btn(v, f"a:rjr:{rid}:{kk}")] for kk, v in REJECT_REASONS.items()]
                    + [[Btn("No reason", f"a:rjr:{rid}:none")], [Btn("↩️ Back", f"r:{rid}")]])
            await show(u, f"❌ Reject report #{rid} — pick a reason (sent to the reporter):", kb)

        elif act == "rjr":
            reason = REJECT_REASONS.get(p[3])
            with Session() as s:
                r = s.get(Report, rid); log_status(s, r, "REJECTED", f"admin:{u.effective_user.id}", reason); s.commit()
            await tell(reporter, f"❌ Your report #{rid} was rejected." + (f"\nReason: {esc(reason)}" if reason else ""))
            await show_report(u, rid, "❌ Rejected.")

        elif act == "st":
            new = p[3]
            if new not in PUBLISHED or status not in PUBLISHED: return
            with Session() as s:
                r = s.get(Report, rid); log_status(s, r, new, f"admin:{u.effective_user.id}"); s.commit()
            await refresh_subject(sub_id)
            await tell(reporter, f"ℹ️ Report #{rid} status changed to <b>{BADGE[new]}</b>.")
            await show_report(u, rid, f"Status → {BADGE[new]} (channel post updated)")

        elif act == "rf":
            await refresh_subject(sub_id); await show_report(u, rid, "🔄 Post(s) refreshed.")

        elif act == "ret":
            await retract(rid)
            with Session() as s:
                r = s.get(Report, rid); log_status(s, r, "REJECTED", f"admin:{u.effective_user.id}", "Retracted"); s.commit()
            await refresh_subject(sub_id)
            await tell(reporter, f"ℹ️ Report #{rid} was removed from the channel.")
            await show_report(u, rid, "🗑 Retracted & deleted from channel.")

        elif act == "rest":
            with Session() as s:
                r = s.get(Report, rid); log_status(s, r, "PENDING", f"admin:{u.effective_user.id}", "Restored"); s.commit()
            await show_report(u, rid, "↩️ Restored to pending.")

        elif act == "ed":
            c.user_data["await"] = ("edit", rid)
            await show(u, f"✏️ <b>Report #{rid}</b> — send the new <b>@username</b>, a <b>numeric ID</b>, or "
                          "<b>forward a message from the user</b> (gets both ID + current @).\n/cancel to abort.",
                       Kb([[Btn("↩️ Back", f"r:{rid}")]]))

        elif act == "px":
            try:
                imgs = await fetch_photos(photos)
                media = [InputMediaPhoto(b) for b in imgs[:10]]
                if len(media) == 1: await c.bot.send_photo(u.effective_chat.id, media[0].media)
                else: await c.bot.send_media_group(u.effective_chat.id, media)
            except TelegramError as e:
                await c.bot.send_message(u.effective_chat.id, f"Couldn't load proofs: {e}")

        elif act == "ban":
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
    if kind == "search":
        c.user_data.pop("await")
        t = (m.text or "").strip().lstrip("@")
        with Session() as s:
            q = s.query(Report).join(Subject, Report.subject_id == Subject.id)
            if t.isdigit():
                q = q.filter((Subject.user_id == int(t)) | (Report.id == int(t)))
            else:
                ids = [x.subject_id for x in s.query(Alias).filter(Alias.username.ilike(f"%{t}%"))]
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

def register_admin(app):
    app.add_handler(CommandHandler(["start", "menu"], a_start))
    app.add_handler(CommandHandler("cancel", a_cancel))
    app.add_handler(CallbackQueryHandler(a_cb))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, a_text))

# ═════════════════════════ run ═════════════════════════
async def on_error(u, c):
    log.error("handler error", exc_info=c.error)

def health_server():
    port = os.getenv("PORT")
    if not port: return
    class H(BaseHTTPRequestHandler):
        def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        def log_message(self, *a): pass
    threading.Thread(target=HTTPServer(("0.0.0.0", int(port)), H).serve_forever, daemon=True).start()

async def main():
    global pub, adm, PUB_USERNAME
    health_server()
    pub = Application.builder().token(PUB_TOKEN).build()
    adm = Application.builder().token(ADM_TOKEN).build()
    register_public(pub); register_admin(adm)
    pub.add_error_handler(on_error); adm.add_error_handler(on_error)
    for a in (pub, adm):
        await a.initialize(); await a.start()
    PUB_USERNAME = pub.bot.username
    await pub.bot.set_my_commands([BotCommand("start", "Menu"), BotCommand("report", "File a report"),
                                   BotCommand("check", "Look up a user"), BotCommand("my", "My reports"),
                                   BotCommand("track", "Track a report by ID"), BotCommand("cancel", "Cancel")])
    await adm.bot.set_my_commands([BotCommand("menu", "Admin panel"), BotCommand("cancel", "Cancel action")])
    for a in (pub, adm):
        await a.updater.start_polling(drop_pending_updates=True)
    log.info("Running. Public bot @%s | admins: %s", PUB_USERNAME, sorted(ADMIN_IDS))
    task = asyncio.create_task(tracker_loop())
    try:
        await asyncio.Event().wait()
    finally:
        task.cancel()
        for a in (pub, adm):
            await a.updater.stop(); await a.stop(); await a.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
