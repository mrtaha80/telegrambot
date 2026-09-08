import re
import hashlib
import sqlite3
import asyncio
import unicodedata
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from fuzzywuzzy import process

BOT_TOKEN = '8936060141:AAHD7N56eK7FtIq_FBy8E1txGNKkV2lWQjI'

BATCH_STORAGE = {}
BATCH_TASKS = {}
TOTAL_PROCESSED_COUNT = 0
DB_LOCK = asyncio.Lock()

def normalize_text(text):
    if not text:
        return ""
    text = unicodedata.normalize('NFKD', text)
    persian_nums = '۰۱۲۳۴۵۶۷۸۹'
    for i, p in enumerate(persian_nums):
        text = text.replace(p, str(i))
    return text

# ================= دیتابیس =================
def init_db():
    conn = sqlite3.connect('mafia_stats.db', timeout=30.0)
    c = conn.cursor()
    c.execute('PRAGMA journal_mode=WAL;')
    c.execute('''
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE COLLATE NOCASE
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER,
            game_signature TEXT,
            event_id TEXT,
            scenario TEXT,
            side TEXT,
            is_win INTEGER,
            UNIQUE(player_id, game_signature),
            FOREIGN KEY(player_id) REFERENCES players(id)
        )
    ''')
    conn.commit()
    conn.close()

def get_or_create_player(cursor, raw_name):
    clean_name = raw_name.strip().lower()
    clean_name = re.sub(r'[\.\-_:]', ' ', clean_name)
    clean_name = " ".join(clean_name.split())

    if not clean_name or len(clean_name) < 2:
        return None, None

    cursor.execute("SELECT id, name FROM players")
    existing_players = cursor.fetchall()
    
    if existing_players:
        names = [p[1] for p in existing_players]
        best_match, score = process.extractOne(clean_name, names)
        if score >= 88 and abs(len(clean_name) - len(best_match)) <= 2:
            for p in existing_players:
                if p[1] == best_match:
                    return p[0], p[1]

    cursor.execute("INSERT OR IGNORE INTO players (name) VALUES (?)", (clean_name,))
    cursor.execute("SELECT id FROM players WHERE name = ?", (clean_name,))
    row = cursor.fetchone()
    return row[0], clean_name

# ================= تشخیص نقش‌ها =================
def detect_side(scenario, role):
    sc = scenario.lower().strip()
    ro = role.lower().strip()

    independents = ['jack', 'جک', 'nostra', 'نوسترا', 'sherlock', 'شرلوک', 'churchill', 'چرچیل']
    if any(ind in ro for ind in independents):
        return "Independent"

    mafia_roles = [
        'don', 'دن', 'nato', 'ناتو', 'رئیس مافیا', 'رئیس', 'مافیای ساده', 
        'mafia sade', 'mafia', 'مافیا'
    ]

    if any(s in sc for s in ['takavar', 'تکاور']):
        mafia_roles.extend(['grogangir', 'گروگانگیر', 'گروگان گیر'])
    elif any(s in sc for s in ['bazpors', 'بازپرس']):
        mafia_roles.extend(['shayad', 'شیاد'])
    elif any(s in sc for s in ['mozakere', 'مذاکره']):
        mafia_roles.extend(['mozakere', 'مذاکره کننده', 'خریدار'])
    elif any(s in sc for s in ['kapo', 'capo', 'کاپو']):
        mafia_roles.extend(['jadogar', 'جادوگر', 'jalad', 'جلاد'])
    elif any(s in sc for s in ['hanibal', 'hannibal', 'هانیبال']):
        mafia_roles.extend(['hanibal', 'hannibal', 'هانیبال', 'saye', 'سایه'])
    elif any(s in sc for s in ['namayande', 'namayandeh', 'نماینده']):
        mafia_roles.extend(['yaghi', 'یاغی', 'hacker', 'هکر'])
    elif any(s in sc for s in ['pishrafte', 'پیشرفته']):
        mafia_roles.extend(['vakil', 'وکیل', 'terrorist', 'تروریست', 'natasha', 'ناتاشا'])
    elif any(s in sc for s in ['elclassico', 'الکلاسیکو']):
        mafia_roles.extend(['khoan', 'خوان', 'blanco', 'بلانکو', 'pablo', 'scobar', 'پابلو'])
    elif any(s in sc for s in ['god father', 'pedarkhande', 'پدرخوانده', 'نوسترا', 'nostra', 'jack', 'جک', 'شرلوک']):
        mafia_roles.extend(['pedarkhande', 'پدرخوانده', 'پدر خوانده', 'matador', 'ماتادور', 'saul', 'گودمن', 'سال گودمن'])

    for m in mafia_roles:
        if m in ro:
            return "Mafia"

    return "Citizen"

# ================= استخراج اطلاعات =================
def process_text_data(raw_text, fallback_id):
    try:
        text = normalize_text(raw_text)

        scenario_match = re.search(r'(?:scenario|سناریو)\s*[:•\-_]\s*([^\n\r]+)', text, re.IGNORECASE)
        win_match = re.search(r'(?:winner|win|برنده|برد)\s*[:•\-_]\s*([^\n\r]+)', text, re.IGNORECASE)
        event_match = re.search(r'(?:event|ایونت)\s*[:#•\-_ ]*([0-9]+)', text, re.IGNORECASE)
        date_match = re.search(r'(?:date|تاریخ|📅)\s*[:•\-_ ]*([0-9/\-]+)', text, re.IGNORECASE)
        time_match = re.search(r'(?:time|ساعت|🕒|⏳)\s*[:•\-_ ]*([0-9:]+)', text, re.IGNORECASE)
        god_match = re.search(r'(?:god|گاد)\s*[:•\-_]\s*([^\n\r]+)', text, re.IGNORECASE)

        if not scenario_match or not win_match:
            return False

        scenario = scenario_match.group(1).strip()
        win_text = win_match.group(1).strip().lower()
        event_id = event_match.group(1).strip() if event_match else str(fallback_id)
        date_str = date_match.group(1).strip() if date_match else ""
        time_str = time_match.group(1).strip() if time_match else ""
        god_str = god_match.group(1).strip().lower() if god_match else ""

        sig_raw = f"{event_id}_{scenario}_{date_str}_{time_str}_{god_str}"
        game_signature = hashlib.md5(sig_raw.encode('utf-8')).hexdigest()

        winning_side = None
        if any(w in win_text for w in ['مافیا', 'mafia']):
            winning_side = "Mafia"
        elif any(w in win_text for w in ['شهر', 'citizen', 'کی اس', 'ks']):
            winning_side = "Citizen"

        if not winning_side:
            return False

        players_block = ""
        players_match = re.search(r'(?:players|بازیکنان|پلیرها)([\s\S]*?)(?:winner|win|🏆|$)', text, re.IGNORECASE)
        if players_match:
            players_block = players_match.group(1)
        else:
            return False

        conn = sqlite3.connect('mafia_stats.db', timeout=30.0)
        c = conn.cursor()

        inserted_any = False
        for line in players_block.strip().splitlines():
            line = line.strip()
            # فیلتر خطوط خالی، خط‌چین‌ها و ایموجی‌های جداکننده مثل 🥀
            if not line or any(c in line for c in ['━', '┄', '─', '🥀']):
                continue

            clean_line = re.sub(r'^[^a-zA-Z\u0600-\u06FF]*[0-9➊-➓]+[^a-zA-Z\u0600-\u06FF]*', '', line).strip()
            if not clean_line:
                continue

            clean_line = re.sub(r'[👈👉].*$', '', clean_line).strip()
            clean_line = re.sub(r'\(.*?\)', '', clean_line).strip()

            tokens = clean_line.split()
            if not tokens:
                continue

            if len(tokens) >= 3 and any(k in " ".join(tokens[-2:]) for k in ['مافیا', 'ساده', 'مافیای ساده', 'رئیس مافیا', 'گودمن']):
                name = " ".join(tokens[:-2])
                role = " ".join(tokens[-2:])
            elif len(tokens) >= 2:
                name = " ".join(tokens[:-1])
                role = tokens[-1]
            else:
                name = tokens[0]
                role = "ساده"

            if not name or name.lower() == 'god':
                continue

            side = detect_side(scenario, role)
            if side == "Independent":
                continue

            player_id, _ = get_or_create_player(c, name)
            if not player_id:
                continue

            is_win = 1 if side == winning_side else 0
            c.execute('''
                INSERT OR IGNORE INTO matches (player_id, game_signature, event_id, scenario, side, is_win)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (player_id, game_signature, event_id, scenario, side, is_win))
            
            if c.rowcount > 0:
                inserted_any = True

        conn.commit()
        conn.close()
        return inserted_any

    except Exception as e:
        print(f"Error parsing event: {e}")
        return False

# تابع تقسیم پیام‌های طولانی به بسته‌های زیر ۴۰۰۰ کاراکتر
async def send_large_text(update_or_chat_id, text, context):
    max_len = 3800
    lines = text.split('\n')
    current_chunk = ""
    
    target_chat = update_or_chat_id if isinstance(update_or_chat_id, (int, str)) else update_or_chat_id.effective_chat.id

    for line in lines:
        if len(current_chunk) + len(line) + 1 > max_len:
            await context.bot.send_message(chat_id=target_chat, text=current_chunk, parse_mode="Markdown")
            current_chunk = line + "\n"
            await asyncio.sleep(0.3)
        else:
            current_chunk += line + "\n"
            
    if current_chunk.strip():
        await context.bot.send_message(chat_id=target_chat, text=current_chunk, parse_mode="Markdown")

# ================= هندلرها =================
async def flush_batch(chat_id, context: ContextTypes.DEFAULT_TYPE):
    global TOTAL_PROCESSED_COUNT
    await asyncio.sleep(2.5)
    
    messages = BATCH_STORAGE.pop(chat_id, [])
    BATCH_TASKS.pop(chat_id, None)

    if not messages:
        return

    added = 0
    async with DB_LOCK:
        for text, msg_id in messages:
            if process_text_data(text, msg_id):
                added += 1

    TOTAL_PROCESSED_COUNT += added

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"📥 **گزارش پردازش دسته‌ای:**\n"
                f"🔹 کل پیام‌های دریافت شده: {len(messages)}\n"
                f"✅ بازی‌های جدید ثبت‌شده: {added}\n"
                f"📊 مجموع کل بازی‌های ثبت‌شده: {TOTAL_PROCESSED_COUNT}"
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Error sending batch summary: {e}")

async def handle_incoming_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post or update.message
    if not msg:
        return

    raw_content = msg.text or msg.caption
    if not raw_content:
        return

    norm_content = normalize_text(raw_content).lower()
    
    if any(k in norm_content for k in ['player', 'بازیکن', 'سیت', 'ساده', 'مافیا']) and any(w in norm_content for w in ['win', 'برد', 'شهروند', 'مافیا']):
        chat_id = msg.chat_id
        if chat_id not in BATCH_STORAGE:
            BATCH_STORAGE[chat_id] = []

        BATCH_STORAGE[chat_id].append((raw_content, msg.message_id))

        if chat_id in BATCH_TASKS:
            BATCH_TASKS[chat_id].cancel()

        BATCH_TASKS[chat_id] = asyncio.create_task(flush_batch(chat_id, context))

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ربات آماده است! پیام‌ها را ارسال کنید و با /report گزارش بگیرید.")

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with DB_LOCK:
        conn = sqlite3.connect('mafia_stats.db', timeout=30.0)
        c = conn.cursor()

        c.execute('''
            SELECT 
                p.name,
                COUNT(m.id) as total_games,
                (SUM(CASE WHEN m.is_win = 1 THEN 1 ELSE 0 END) * 100.0 / COUNT(m.id)) as overall_win_rate,
                SUM(CASE WHEN m.side = 'Mafia' THEN 1 ELSE 0 END) as mafia_games,
                SUM(CASE WHEN m.side = 'Mafia' AND m.is_win = 1 THEN 1 ELSE 0 END) as mafia_wins,
                SUM(CASE WHEN m.side = 'Citizen' THEN 1 ELSE 0 END) as citizen_games,
                SUM(CASE WHEN m.side = 'Citizen' AND m.is_win = 1 THEN 1 ELSE 0 END) as citizen_wins
            FROM players p
            JOIN matches m ON p.id = m.player_id
            GROUP BY p.id
            HAVING total_games >= 1
            ORDER BY overall_win_rate DESC, total_games DESC
        ''')
        results = c.fetchall()
        conn.close()

    if not results:
        await update.message.reply_text("هنوز هیچ بازی‌ای در سیستم ذخیره نشده است.")
        return

    report = "📊 **رتبه‌بندی عملکرد بازیکنان**\n\n"
    mafia_leaders = []
    citizen_leaders = []

    for idx, row in enumerate(results, 1):
        name, total_g, win_rate, m_games, m_wins, c_games, c_wins = row
        m_rate = (m_wins * 100 // m_games) if m_games > 0 else 0
        c_rate = (c_wins * 100 // c_games) if c_games > 0 else 0

        if m_games > 0:
            mafia_leaders.append((name, m_rate, m_games, m_wins))
        if c_games > 0:
            citizen_leaders.append((name, c_rate, c_games, c_wins))

        report += f"🎖 **{idx}. {name.title()}**\n"
        report += f"🎮 بازی‌ها: {total_g} | 🏆 برد کل: {win_rate:.1f}%\n"
        report += f"🔪 مافیا: {m_rate}% ({m_wins}/{m_games}) | 🛡 شهر: {c_rate}% ({c_wins}/{c_games})\n"
        report += "─────────────────\n"

    mafia_leaders.sort(key=lambda x: (x[1], x[2]), reverse=True)
    report += "\n🔥 **برترین‌های ساید مافیا:**\n"
    for r, (n, rate, games, wins) in enumerate(mafia_leaders[:5], 1):
        report += f"{r}. {n.title()} ⟵ {rate}% برد ({wins}/{games})\n"

    citizen_leaders.sort(key=lambda x: (x[1], x[2]), reverse=True)
    report += "\n🛡 **برترین‌های ساید شهروند:**\n"
    for r, (n, rate, games, wins) in enumerate(citizen_leaders[:5], 1):
        report += f"{r}. {n.title()} ⟵ {rate}% برد ({wins}/{games})\n"

    # ارسال امن پیام‌ها بدون محدودیت طول کاراکتر
    await send_large_text(update, report, context)

# ================= راه‌اندازی =================
if __name__ == '__main__':
    init_db()
    print("ربات با سیستم مدیریت پیام‌های طولانی فعال شد...")
    
    # افزایش تایم‌اوت شبکه برای مقابله با کندی پروکسی
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .read_timeout(60)
        .write_timeout(60)
        .connect_timeout(60)
        .build()
    )
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_incoming_messages))

    app.run_polling()