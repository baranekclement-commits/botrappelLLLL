import os
import re
import json
import asyncio
import logging
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
import httpx
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
import redis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
CHAT_ID = int(os.environ.get("CHAT_ID", "0"))
HEURE_MATIN = int(os.environ.get("HEURE_MATIN", "7"))
REDIS_URL = os.environ.get("REDIS_URL", "")

r = redis.from_url(REDIS_URL, decode_responses=True)

ONBOARDING_QUESTIONS = [
    "Salut ! Je suis ton assistant perso 🤙 Je vais apprendre à te connaître pour être vraiment utile. C'est quoi ton prénom ?",
    "Quel âge t'as et t'es en quelle classe ?",
    "T'as des objectifs sportifs ? (perdre du poids, prendre du muscle, juste bouger...) ou t'es plutôt pas sportif pour l'instant ?",
    "T'as des douleurs ou blessures à éviter ? (dos, genoux, bras, rien de tout ça...)",
    "Tu te lèves à quelle heure en semaine ? Et le weekend ?",
    "T'as des matières où tu galères à l'école ? Des trucs que tu veux qu'on révise ensemble ?",
    "C'est quoi tes loisirs ? (jeux vidéo, musique, sport, sorties...)",
    "T'as une routine le matin ou tu te lèves et c'est le chaos 😄 ?",
    "Dernière question : comment tu préfères qu'on se parle ? (détendu, motivant, direct, tu t'en fous...)"
]

PROFILE_KEYS = ["prénom", "âge et classe", "objectifs sportifs", "douleurs/blessures",
                "heure de réveil", "matières difficiles", "loisirs", "routine matin", "style de communication"]

# ── Mémoire Redis ─────────────────────────────────────────────────────────────

def load_memory():
    try:
        data = r.get("memory")
        if data:
            return json.loads(data)
    except Exception as e:
        logger.error(f"Redis load error: {e}")
    return {"onboarding_done": False, "onboarding_step": 0, "profile": {}, "notes": [], "reminders": []}

def save_memory(memory):
    try:
        r.set("memory", json.dumps(memory, ensure_ascii=False))
    except Exception as e:
        logger.error(f"Redis save error: {e}")

def load_conversation():
    try:
        data = r.get("conversation")
        if data:
            return json.loads(data)[-20:]
    except Exception as e:
        logger.error(f"Redis conv load error: {e}")
    return []

def save_conversation(messages):
    try:
        r.set("conversation", json.dumps(messages[-20:], ensure_ascii=False))
    except Exception as e:
        logger.error(f"Redis conv save error: {e}")

# ── Keep-alive ────────────────────────────────────────────────────────────────

class KeepAlive(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *args):
        pass

def start_keepalive():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), KeepAlive)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info(f"Keep-alive démarré sur port {port}")

# ── Prompt système ────────────────────────────────────────────────────────────

def build_system_prompt(memory):
    profile = memory.get("profile", {})
    notes = memory.get("notes", [])
    reminders = memory.get("reminders", [])

    profile_text = "\n".join(f"- {k}: {v}" for k, v in profile.items()) or "Pas encore de profil complet"
    notes_text = "\n".join(f"- {n}" for n in notes[-10:]) or "Aucune note"
    reminders_text = "\n".join(f"- {r['text']} à {r['time']}" for r in reminders if not r.get("done")) or "Aucun rappel"
    now = datetime.now(pytz.timezone("Europe/Paris"))

    return f"""Tu es l'assistant personnel de {profile.get("prénom", "ton utilisateur")}, un vrai ami intelligent et bienveillant sur Telegram.

DATE ET HEURE ACTUELLE: {now.strftime("%A %d %B %Y à %H:%M")}

PROFIL DE L'UTILISATEUR:
{profile_text}

NOTES ET INFORMATIONS MÉMORISÉES:
{notes_text}

RAPPELS EN COURS:
{reminders_text}

TES RÈGLES:
1. Tu parles naturellement comme un ami — tu comprends les fautes d'ortho, le verlan, le franglais
2. Tu RETIENS tout ce qu'il te dit — si il mentionne une info importante, mémorise-la
3. Quand tu détectes un rappel, réponds normalement ET ajoute à la fin:
   [RAPPEL:HH:MM:texte du rappel]
   Exemple: [RAPPEL:18:00:Appeler maman]
4. Quand tu détectes une info à mémoriser, ajoute à la fin:
   [MEMOIRE:clé:valeur]
   Exemple: [MEMOIRE:blessure bras:a mal au bras droit depuis lundi]
5. Quand il dit "bonjour" le matin, génère sa routine : sport 10 min adapté + motivation courte
6. Exercices SANS matériel, adaptés à son profil et ses blessures
7. Réponds TOUJOURS en français, sois naturel et direct"""

# ── Gemini API ────────────────────────────────────────────────────────────────

def _gemini_sync(messages, system_prompt):
    # Convertit le format OpenAI -> Gemini
    gemini_messages = []
    for m in messages:
        role = "user" if m["role"] == "user" else "model"
        gemini_messages.append({
            "role": role,
            "parts": [{"text": m["content"]}]
        })

    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": gemini_messages,
        "generationConfig": {
            "maxOutputTokens": 500,
            "temperature": 0.8
        }
    }

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"

    with httpx.Client(timeout=30) as client:
        resp = client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]

async def ask_ai(messages, system_prompt):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _gemini_sync, messages, system_prompt)

# ── Parsing réponse ───────────────────────────────────────────────────────────

def parse_response(text, memory):
    clean = text

    for heure, rappel_text in re.findall(r'\[RAPPEL:(\d{2}:\d{2}):(.+?)\]', text):
        memory["reminders"].append({
            "time": heure,
            "text": rappel_text.strip(),
            "done": False,
            "created": datetime.now().isoformat()
        })
        clean = clean.replace(f"[RAPPEL:{heure}:{rappel_text}]", "")

    for key, value in re.findall(r'\[MEMOIRE:(.+?):(.+?)\]', text):
        memory["notes"].append(f"{key}: {value}")
        clean = clean.replace(f"[MEMOIRE:{key}:{value}]", "")

    save_memory(memory)
    return clean.strip()

# ── Jobs planifiés ────────────────────────────────────────────────────────────

async def send_morning_routine(context):
    memory = load_memory()
    if not memory.get("onboarding_done"):
        return
    now = datetime.now(pytz.timezone("Europe/Paris"))
    system_prompt = build_system_prompt(memory)
    messages = load_conversation() + [{"role": "user", "content": f"Bonjour ! C'est le matin, {now.strftime('%A %d %B')}. Génère ma routine du jour."}]
    try:
        response = await ask_ai(messages, system_prompt)
        clean = parse_response(response, memory)
        await context.bot.send_message(chat_id=CHAT_ID, text=f"🌅 Bonne journée !\n\n{clean}")
    except Exception as e:
        logger.error(f"Erreur routine matin: {e}")

async def check_reminders(context):
    memory = load_memory()
    now = datetime.now(pytz.timezone("Europe/Paris"))
    current_time = now.strftime("%H:%M")
    changed = False
    for item in memory.get("reminders", []):
        if not item.get("done") and item.get("time") == current_time:
            try:
                await context.bot.send_message(chat_id=CHAT_ID, text=f"⏰ Rappel !\n\n{item['text']}")
                item["done"] = True
                changed = True
            except Exception as e:
                logger.error(f"Erreur rappel: {e}")
    if changed:
        save_memory(memory)

# ── Handlers Telegram ─────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    memory = load_memory()
    memory.update({"onboarding_done": False, "onboarding_step": 0, "profile": {}})
    save_memory(memory)
    await update.message.reply_text(ONBOARDING_QUESTIONS[0])

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    memory = load_memory()
    user_text = update.message.text.strip()

    if not memory.get("onboarding_done"):
        step = memory.get("onboarding_step", 0)
        if step < len(PROFILE_KEYS):
            memory["profile"][PROFILE_KEYS[step]] = user_text
        step += 1
        memory["onboarding_step"] = step
        if step < len(ONBOARDING_QUESTIONS):
            save_memory(memory)
            await update.message.reply_text(ONBOARDING_QUESTIONS[step])
        else:
            memory["onboarding_done"] = True
            save_memory(memory)
            prenom = memory["profile"].get("prénom", "toi")
            await update.message.reply_text(
                f"Parfait {prenom} ! 🔥 Je te connais maintenant.\n\n"
                f"Parle-moi normalement — je retiens tout.\n"
                f"Dis \"bonjour\" chaque matin pour ta routine.\n"
                f"Pour un rappel, dis-le moi juste naturellement.\n\n"
                f"C'est parti 💪"
            )
        return

    conversation = load_conversation()
    system_prompt = build_system_prompt(memory)
    conversation.append({"role": "user", "content": user_text})

    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
        response = await ask_ai(conversation, system_prompt)
        clean = parse_response(response, memory)
        conversation.append({"role": "assistant", "content": clean})
        save_conversation(conversation)
        await update.message.reply_text(clean)
    except Exception as e:
        logger.error(f"Erreur message: {e}")
        await update.message.reply_text("Petit bug technique, réessaie dans 2 secondes 🙏")

async def profil_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    memory = load_memory()
    profile = memory.get("profile", {})
    notes = memory.get("notes", [])
    reminders = [r for r in memory.get("reminders", []) if not r.get("done")]
    text = "👤 Ce que je sais sur toi :\n\n"
    for k, v in profile.items():
        text += f"• {k} : {v}\n"
    if notes:
        text += "\n📝 Notes mémorisées :\n"
        for n in notes[-5:]:
            text += f"• {n}\n"
    if reminders:
        text += "\n⏰ Rappels actifs :\n"
        for r in reminders:
            text += f"• {r['time']} — {r['text']}\n"
    await update.message.reply_text(text)

async def rappels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    memory = load_memory()
    reminders = [r for r in memory.get("reminders", []) if not r.get("done")]
    if not reminders:
        await update.message.reply_text("Aucun rappel actif 👍")
        return
    text = "⏰ Tes rappels actifs :\n\n"
    for i, r in enumerate(reminders, 1):
        text += f"{i}. {r['time']} — {r['text']}\n"
    await update.message.reply_text(text)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    start_keepalive()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    scheduler = AsyncIOScheduler(timezone="Europe/Paris")
    scheduler.add_job(check_reminders, "interval", minutes=1, args=[app])
    scheduler.add_job(send_morning_routine, CronTrigger(hour=HEURE_MATIN, minute=0), args=[app])
    scheduler.start()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profil", profil_command))
    app.add_handler(CommandHandler("rappels", rappels_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info(f"✅ Assistant Gemini démarré — routine à {HEURE_MATIN}h00")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
