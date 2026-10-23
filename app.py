import os, uuid, asyncio, threading
from datetime import datetime, timezone

import httpx
from flask import Flask, jsonify, request, send_from_directory, Response, abort
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

@app.get("/media/<file_id>")
def media(file_id):
    """Proxy Telegram photos so the bot token never reaches the browser."""
    base = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile"
    info = httpx.get(base, params={"file_id": file_id}, timeout=15).json()
    if not info.get("ok"):
        abort(404)
    r = httpx.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{info['result']['file_path']}", timeout=30)
    return Response(r.content, mimetype=r.headers.get("content-type", "image/jpeg"),
                    headers={"Cache-Control": "public, max-age=86400"})

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
    await u.message.reply_text("Send proof: links and/or screenshots (one or many).\nSend /done when finished, or /skip for none.")
    return PROOF

async def got_proof(u, c):
    m, proofs = u.message, c.user_data.setdefault("proofs", [])
    if m.photo:
        proofs.append(f"{BASE_URL}/media/{m.photo[-1].file_id}")
    elif m.text and m.text.startswith(("http://", "https://")):
        proofs.append(m.text.strip())
    else:
        await m.reply_text("Send a link (http/https) or a photo, then /done.")
        return PROOF
    await m.reply_text(f"Added ({len(proofs)}). Send more or /done.")
    return PROOF

async def finish(u, c):
    d, user = c.user_data, u.effective_user
    rid = str(uuid.uuid4())
    t = d["target"]
    with Session() as s:
        s.add(Report(id=rid, target_username=t, target_id=t if t.isdigit() else None,
                     reported_by=f"@{user.username}" if user.username else str(user.id),
                     reason=d["reason"], deal_type=d["deal"],
                     proof_urls=d.get("proofs", []), status="PENDING"))
        s.commit()
    await u.message.reply_text(
        f"✅ Report submitted.\n\nID: {rid}\nStatus: PENDING (visible publicly once an admin reviews it)\n"
        f"Track it: {BASE_URL}/#{rid}", reply_markup=ReplyKeyboardRemove())
    for aid in ADMIN_IDS:
        try:
            await c.bot.send_message(aid, f"New report {rid[:8]} on @{t} ({d['deal']}).\n/setstatus {rid[:8]} VERIFIED")
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
            PROOF: [CommandHandler(["done", "skip"], finish),
                    MessageHandler((filters.PHOTO | filters.TEXT) & ~filters.COMMAND, got_proof)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    bot.add_handler(conv)
    bot.add_handler(CommandHandler("start", start))
    bot.add_handler(CommandHandler("check", check))
    bot.add_handler(CommandHandler("setstatus", setstatus))
    bot.run_polling(stop_signals=None, close_loop=False)

if os.getenv("RUN_BOT", "1") == "1":
    threading.Thread(target=run_bot, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
