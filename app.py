import os, uuid, asyncio, threading
from datetime import datetime, timezone

from flask import Flask, jsonify, request, send_from_directory
from sqlalchemy import create_engine, Column, String, Text, DateTime, JSON, or_
from sqlalchemy.orm import declarative_base, sessionmaker
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          ConversationHandler, ContextTypes, filters)

BOT_TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = os.getenv("BASE_URL", "http://localhost:5000").rstrip("/")
ADMIN_IDS = {x.strip() for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
DB_URL = os.environ["DATABASE_URL"].replace("postgres://", "postgresql://", 1)
STATUSES = {"VERIFIED", "PENDING", "DISPUTED", "RESOLVED"}
DEALS = ["Account Trade", "Middleman Deal", "Service", "Other"]

# ---------- Database ----------
engine = create_engine(DB_URL, pool_pre_ping=True, pool_recycle=300)
Session = sessionmaker(engine, expire_on_commit=False)
Base = declarative_base()

class Report(Base):
    __tablename__ = "reports"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    target_username = Column(String(64), index=True, nullable=False)
    target_id = Column(String(32), nullable=True)
    reported_by = Column(String(64), nullable=False)
    reason = Column(Text, nullable=False)
    deal_type = Column(String(32), nullable=False)
    proof_urls = Column(JSON, default=list)
    status = Column(String(16), default="PENDING", index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {"id": self.id, "target_username": self.target_username,
                "target_id": self.target_id, "reason": self.reason,
                "deal_type": self.deal_type, "proof_urls": self.proof_urls or [],
                "status": self.status, "created_at": self.created_at.isoformat()}

Base.metadata.create_all(engine)

# ---------- Web ----------
app = Flask(__name__, static_folder="static")

@app.get("/")
def index():
    return send_from_directory("static", "index.html")

@app.get("/health")
def health():
    return "ok"

@app.get("/api/reports")
def api_reports():
    q = request.args.get("q", "").strip().lstrip("@")
    status = request.args.get("status", "").upper()
    with Session() as s:
        query = s.query(Report).filter(Report.status != "PENDING")  # pending = hidden until reviewed
        if status in STATUSES - {"PENDING"}:
            query = query.filter(Report.status == status)
        if q:
            query = query.filter(or_(Report.target_username.ilike(f"%{q}%"), Report.target_id == q))
        rows = query.order_by(Report.created_at.desc()).limit(200).all()
        return jsonify([r.to_dict() for r in rows])

# ---------- Telegram bot ----------
TARGET, DEAL, REASON, PROOF = range(4)

async def report_start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    c.user_data.clear()
    await u.message.reply_text("Send the timewaster's @username or numeric Telegram ID.\n/cancel to stop.")
    return TARGET

async def got_target(u, c):
    t = u.message.text.strip().lstrip("@")
    if not t or " " in t:
        await u.message.reply_text("Send just a username or numeric ID, e.g. @john or 12345678.")
        return TARGET
    c.user_data["target"] = t
    kb = ReplyKeyboardMarkup([[d] for d in DEALS], one_time_keyboard=True, resize_keyboard=True)
    await u.message.reply_text("What type of deal was it?", reply_markup=kb)
    return DEAL

async def got_deal(u, c):
    d = u.message.text.strip()
    c.user_data["deal"] = d if d in DEALS else "Other"
    await u.message.reply_text("Briefly describe what happened.", reply_markup=ReplyKeyboardRemove())
    return REASON

async def got_reason(u, c):
    c.user_data["reason"] = u.message.text.strip()[:1500]
    c.user_data["proofs"] = []
    await u.message.reply_text("Now create a Telegram channel, put ALL your proof in it (screenshots, chats, receipts), then send me the channel link here (for example https://t.me/yourchannel).\n\nNo proof? Send /skip.")
    return PROOF

async def got_proof(u, c):
    m = u.message
    link = (m.text or "").strip()
    if link.lower().startswith(("t.me/", "telegram.me/")):
        link = "https://" + link
    if not link.lower().startswith(("http://", "https://")) or " " in link:
        await m.reply_text("Please send the channel link, like https://t.me/yourchannel, or /skip.")
        return PROOF
    c.user_data["proofs"] = [link]
    return await finish(u, c)

def detail(r):
    return (f"ID: {r.id}\nTarget: @{r.target_username}\nReported by: {r.reported_by}\n"
            f"Type: {r.deal_type}\nStatus: {r.status}\nReason: {r.reason}\n"
            f"Proof: {', '.join(r.proof_urls or []) or 'none'}")

async def finish(u, c):
    d, user = c.user_data, u.effective_user
    t = d["target"]
    rep = Report(id=str(uuid.uuid4()), target_username=t, target_id=t if t.isdigit() else None,
                 reported_by=f"@{user.username}" if user.username else str(user.id),
                 reason=d["reason"], deal_type=d["deal"],
                 proof_urls=d.get("proofs", []), status="PENDING")
    with Session() as s:
        s.add(rep)
        s.commit()
    await u.message.reply_text(
        f"✅ Report submitted.\n\nID: {rep.id}\nStatus: PENDING (visible publicly once an admin reviews it)\n"
        f"Track it: {BASE_URL}/#{rep.id}", reply_markup=ReplyKeyboardRemove())
    for aid in ADMIN_IDS:
        try:
            await c.bot.send_message(aid, "🆕 New report\n\n" + detail(rep) +
                f"\n\n/setstatus {rep.id[:8]} VERIFIED\n/reject {rep.id[:8]}")
        except Exception:
            pass
    c.user_data.clear()
    return ConversationHandler.END

async def cancel(u, c):
    c.user_data.clear()
    await u.message.reply_text("Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def setstatus(u, c):
    if str(u.effective_user.id) not in ADMIN_IDS:
        return
    if len(c.args) != 2 or c.args[1].upper() not in STATUSES:
        await u.message.reply_text("Usage: /setstatus <id-prefix> VERIFIED|PENDING|DISPUTED|RESOLVED")
        return
    with Session() as s:
        rows = s.query(Report).filter(Report.id.like(f"{c.args[0]}%")).all()
        if len(rows) != 1:
            await u.message.reply_text(f"Matched {len(rows)} reports; use a more specific prefix.")
            return
        rows[0].status = c.args[1].upper()
        s.commit()
    await u.message.reply_text("Updated.")

def find_one(s, prefix):
    rows = s.query(Report).filter(Report.id.like(f"{prefix}%")).all()
    return rows[0] if len(rows) == 1 else None

async def pending(u, c):
    if str(u.effective_user.id) not in ADMIN_IDS:
        return
    with Session() as s:
        rows = s.query(Report).filter(Report.status == "PENDING").order_by(Report.created_at).limit(20).all()
    if not rows:
        await u.message.reply_text("No pending reports.")
        return
    await u.message.reply_text("Pending reports:\n\n" + "\n".join(
        f"{r.id[:8]}  @{r.target_username}  ({r.deal_type})" for r in rows) +
        "\n\nOpen one with /view <id>")

async def view(u, c):
    if str(u.effective_user.id) not in ADMIN_IDS:
        return
    with Session() as s:
        r = find_one(s, c.args[0]) if c.args else None
    if not r:
        await u.message.reply_text("Usage: /view <id-prefix> (no unique match found)")
        return
    await u.message.reply_text(detail(r) + f"\n\n/setstatus {r.id[:8]} VERIFIED\n/reject {r.id[:8]}")

async def reject(u, c):
    if str(u.effective_user.id) not in ADMIN_IDS:
        return
    with Session() as s:
        r = find_one(s, c.args[0]) if c.args else None
        if not r:
            await u.message.reply_text("Usage: /reject <id-prefix> (no unique match found)")
            return
        s.delete(r)
        s.commit()
    await u.message.reply_text("Report rejected and deleted.")

async def check(u, c):
    if not c.args:
        await u.message.reply_text("Usage: /check @username")
        return
    t = c.args[0].lstrip("@")
    with Session() as s:
        rows = s.query(Report).filter(Report.status != "PENDING",
                or_(Report.target_username.ilike(t), Report.target_id == t)).all()
    if not rows:
        await u.message.reply_text(f"No public reports for @{t}.")
        return
    v = sum(r.status == "VERIFIED" for r in rows)
    await u.message.reply_text(f"@{t}: {len(rows)} report(s), {v} verified.\n{BASE_URL}/?q={t}")

async def start(u, c):
    await u.message.reply_text("Use /report to file a report, /check @user to look someone up.")

def run_bot():
    asyncio.set_event_loop(asyncio.new_event_loop())
    bot = Application.builder().token(BOT_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("report", report_start)],
        states={
            TARGET: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_target)],
            DEAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_deal)],
            REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, got_reason)],
            PROOF: [CommandHandler("skip", finish),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, got_proof)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    bot.add_handler(conv)
    bot.add_handler(CommandHandler("start", start))
    bot.add_handler(CommandHandler("check", check))
    bot.add_handler(CommandHandler("setstatus", setstatus))
    bot.add_handler(CommandHandler("pending", pending))
    bot.add_handler(CommandHandler("view", view))
    bot.add_handler(CommandHandler("reject", reject))
    bot.run_polling(stop_signals=None, close_loop=False)

if os.getenv("RUN_BOT", "1") == "1":
    threading.Thread(target=run_bot, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))