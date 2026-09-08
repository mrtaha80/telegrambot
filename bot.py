import re
import hashlib
import sqlite3
import asyncio
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from fuzzywuzzy import process

BOT_TOKEN = '8936060141:AAHD7N56eK7FtIq_FBy8E1txGNKkV2lWQjI'

# مدیریت بافر برای ارسال دسته‌ای پیام‌ها
BATCH_STORAGE = {}
BATCH_TASKS = {}
TOTAL_PROCESSED_COUNT = 0

# ================= دیتابیس =================
def init_db():
    conn = sqlite3.connect('mafia_stats.db')
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE COLLATE NOCASE
        )
    ''')
    # جدول بازی‌ها با کلید یکتای ترکیبی (Hash) برای جلوگیری از تداخل
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
    cursor.execute("SELECT id, name FROM players")
    existing_players = cursor.fetchall()
    
    if existing_players:
        names = [p[1] for p in existing_players]
        best_match, score = process.extractOne(clean_name, names)
        if score >= 85:
            for p in existing_players:
                if p[1] == best_match:
                    return p[0], p[1]

    cursor.execute("INSERT OR IGNORE INTO players (name) VALUES (?)", (clean_name,))
    cursor.execute("SELECT id FROM players WHERE name = ?", (clean_name,))
    row = cursor.fetchone()
    return row[0], clean_name

# ================= تشخیص نقش و سناریو =================
def detect_side(scenario, role):
    sc = scenario.lower().strip()
    ro = role.lower().strip()

    independents = ['jack', 'جک', 'nostra', 'نوسترا', 'sherlock', 'شرلوک', 'churchill', 'چرچیل']
    if any(ind in ro for ind in independents):
        return "Independent"

    mafia_roles = ['don', 'دن', 'nato', 'ناتو', 'mafia', 'مافیا']

    if any(s in sc for s in ['takavar', 'تکاور']):
        mafia_roles.extend(['grogangir', 'گروگانگیر', 'گروگان گیر'])
    elif any(s in sc for s in ['bazpors', 'بازپرس']):
        mafia_roles.extend(['shayad', 'شیاد'])
    elif any(s in sc for s in ['mozakere', 'مذاکره']):
        mafia_roles.extend(['mozakere', 'مذاکره کننده', 'خریدار', 'خریداری کننده'])
    elif any(s in sc for s in ['kapo', 'کاپو']):
        mafia_roles.extend(['jadogar', 'جادوگر', 'jalad', 'جلاد'])
    elif any(s in sc for s in ['hanibal', 'هانیبال']):
        mafia_roles.extend(['hanibal', 'هانیبال', 'saye', 'سایه'])
    elif any(s in sc for s in ['namayande', 'نماینده']):
        # در سناریو نماینده وکیل شهروند است و یاغی/هکر مافیا هستند
        mafia_roles.extend(['yaghi', 'یاغی', 'hacker', 'هکر'])
    elif any(s in sc for s in ['pishrafte', 'پیشرفته']):
        mafia_roles.extend(['vakil', 'وکیل', 'terrorist', 'تروریست', 'natasha', 'ناتاشا'])
    elif any(s in sc for s in ['elclassico', 'الکلاسیکو', 'ال کلاسیکو']):
        mafia_roles.extend(['khoan', 'خوان', 'blanco', 'بلانکو', 'pablo', 'scobar', 'پابلو', 'اسکوبار'])
    elif any(s in sc for s in ['nostra', 'نوسترا', 'jack', 'جک', 'sherlock', 'شرلوک', 'pedarkhande', 'پدرخوانده', 'پدر خوانده']):
        mafia_roles.extend(['pedarkhande', 'پدرخوانده', 'پدر خوانده', 'matador', 'ماتادور', 'saul', 'سال گودمن', 'سال'])

    for m in mafia_roles:
        if m in ro:
            return "Mafia"

    return "Citizen"

# ================= استخراج دقیق اطلاعات =================
def process_text_data(text, fallback_id):
    try:
        scenario_match = re.search(r'𝐒𝐂𝐄𝐍𝐀𝐑𝐈𝐎\s*•\s*(.+)', text)
        win_match = re.search(r'𝐖𝐈𝐍\s*•\s*(.+)', text)
        event_match = re.search(r'𝐄𝐕𝐄𝐍𝐓\s*•\s*([0-9]+)', text)
        date_match = re.search(r'𝐃𝐀𝐓𝐄\s*•\s*(.+)', text)
        time_match = re.search(r'𝐓𝐈𝐌𝐄\s*•\s*(.+)', text)
        god_match = re.search(r'𝐆𝐎𝐃\s*•\s*(.+)', text)

        if not scenario_match or not win_match:
            return False

        scenario = scenario_match.group(1).strip()
        win_text = win_match.group(1).strip().lower()
        event_id = event_match.group(1).strip() if event_match else str(fallback_id)
        date_str = date_match.group(1).strip() if date_match else ""
        time_str = time_match.group(1).strip() if time_match else ""
        god_str = god_match.group(1).strip().lower() if god_match else ""

        # ساخت امضای یکتای چندمتغیره برای جلوگیری از تداخل ایونت‌های هم‌شماره
        signature_raw = f"{event_id}_{scenario}_{date_str}_{time_str}_{god_str}"
        game_signature = hashlib.md5(signature_raw.encode('utf-8')).hexdigest()

        winning_side = None
        if 'مافیا' in win_text or 'mafia' in win_text:
            winning_side = "Mafia"
        elif any(w in win_text for w in ['شهر', 'citizen', 'کی اس', 'ks']):
            winning_side = "Citizen"

        if not winning_side or "𝐏𝐋𝐀𝐘𝐄𝐑𝐒" not in text:
            return False

        players_part = text.split("𝐏𝐋𝐀𝐘𝐄𝐑𝐒")[1]
        if "𝐖𝐈𝐍" in players_part:
            players_part = players_part.split("𝐖𝐈𝐍")[0]

        conn = sqlite3.connect('mafia_stats.db')
        c = conn.cursor()

        inserted_any = False
        for line in players_part.strip().splitlines():
            line = line.strip()
            slot_match = re.search(r'[➊-➓0-9۰-۹]+\s*[:\.\-]\s*(.+)', line)
            if not slot_match:
                continue

            content = slot_match.group(1).strip()
            eng_match = re.search(r'^([a-zA-Z0-9_\-\s]+)', content)
            if eng_match and len(eng_match.group(1).strip()) > 0:
                name = eng_match.group(1).strip()
                role = content[len(eng_match.group(0)):].strip()
            else:
                parts = content.split()
                name = parts[0] if parts else ""
                role = " ".join(parts[1:]) if len(parts) > 1 else "ساده"

            if not role:
                tokens = content.split()
                if len(tokens) > 1:
                    name = " ".join(tokens[:-1])
                    role = tokens[-1]

            if not name or name.lower() == 'god':
                continue

            side = detect_side(scenario, role)
            if side == "Independent":
                continue

            is_win = 1 if side == winning_side else 0
            player_id, _ = get_or_create_player(c, name)

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
        print(f"Error parsing: {e}")
        return False

# ================= هندلرهای ارسال دسته‌ای =================
async def flush_batch(chat_id, context: ContextTypes.DEFAULT_TYPE):
    global TOTAL_PROCESSED_COUNT
    await asyncio.sleep(2.5)  # ۲.۵ ثانیه وقفه بعد از دریافت آخرین پیام در صف
    
    messages = BATCH_STORAGE.pop(chat_id, [])
    BATCH_TASKS.pop(chat_id, None)

    if not messages:
        return

    added_in_this_batch = 0
    for text, msg_id in messages:
        if process_text_data(text, msg_id):
            added_in_this_batch += 1

    TOTAL_PROCESSED_COUNT += added_in_this_batch

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"📥 **گزارش پردازش دسته‌ای:**\n"
            f"🔹 پیام‌های دریافتی در این پارت: {len(messages)}\n"
            f"✅ بازی‌های جدید و مجزا ثبت‌شده: {added_in_this_batch}\n"
            f"📊 مجموع کل بازی‌های ثبت‌شده تا الان: {TOTAL_PROCESSED_COUNT}"
        ),
        parse_mode="Markdown"
    )

async def handle_incoming_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post or update.message
    if not msg or not msg.text:
        return

    if "𝐏𝐋𝐀𝐘𝐄𝐑𝐒" in msg.text and "𝐖𝐈𝐍" in msg.text:
        chat_id = msg.chat_id
        if chat_id not in BATCH_STORAGE:
            BATCH_STORAGE[chat_id] = []

        BATCH_STORAGE[chat_id].append((msg.text, msg.message_id))

        # ریست کردن تایمر برای تجمیع فورواردهای ۱۰۰ تایی
        if chat_id in BATCH_TASKS:
            BATCH_TASKS[chat_id].cancel()

        BATCH_TASKS[chat_id] = asyncio.create_task(flush_batch(chat_id, context))

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! سیستم دریافت اطلاعات هوشمند مافیا آماده است.\n\n"
        "می‌توانید پیام‌ها را به صورت تک‌تک یا ۱۰۰ تا ۱۰۰ تا فوروارد کنید.\n"
        "سیستم به صورت خودکار پیام‌های هم‌زمان را دسته‌بندی و ذخیره می‌کند.\n"
        "برای خروجی گرفتن دستور /report را ارسال نمایید."
    )

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect('mafia_stats.db')
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
        HAVING total_games > 30
        ORDER BY overall_win_rate DESC
    ''')
    results = c.fetchall()
    conn.close()

    if not results:
        await update.message.reply_text("هنوز بازیکنی با بیش از ۳۰ بازی ثبت نشده است.")
        return

    report = "📊 **گزارش عملکرد بازیکنان برتر (بالای ۳۰ بازی)**\n\n"
    mafia_leaders = []
    citizen_leaders = []

    for idx, row in enumerate(results, 1):
        name, total_g, win_rate, m_games, m_wins, c_games, c_wins = row
        m_rate = (m_wins * 100 // m_games) if m_games > 0 else 0
        c_rate = (c_wins * 100 // c_games) if c_games > 0 else 0

        mafia_leaders.append((name, m_rate, m_games, m_wins))
        citizen_leaders.append((name, c_rate, c_games, c_wins))

        report += f"🎖 **رتبه {idx}: {name.title()}**\n"
        report += f"🎮 مجموع بازی‌ها: {total_g} | 🏆 درصد برد کل: {win_rate:.1f}%\n"
        report += f"🔪 ساید مافیا: {m_rate}% برد ({m_wins} از {m_games})\n"
        report += f"🛡 ساید شهروند: {c_rate}% برد ({c_wins} از {c_games})\n"
        report += "─────────────────────\n"

    mafia_leaders.sort(key=lambda x: (x[1], x[2]), reverse=True)
    report += "\n🔥 **رتبه‌بندی برترین‌ها با کارت مافیا:**\n"
    for r, (n, rate, games, wins) in enumerate(mafia_leaders[:5], 1):
        report += f"{r}. {n.title()} ⟵ {rate}% برد ({wins} برد از {games} بازی)\n"

    citizen_leaders.sort(key=lambda x: (x[1], x[2]), reverse=True)
    report += "\n🛡 **رتبه‌بندی برترین‌ها با کارت شهروندی:**\n"
    for r, (n, rate, games, wins) in enumerate(citizen_leaders[:5], 1):
        report += f"{r}. {n.title()} ⟵ {rate}% برد ({wins} برد از {games} بازی)\n"

    await update.message.reply_text(report, parse_mode="Markdown")

if __name__ == '__main__':
    init_db()
    print("ربات فعال شد و آماده دریافت دسته‌ای پیام‌هاست...")
    
    # تنظیم پروکسی داخلی فیلترشکن (پورت پیش‌فرض v2rayN و هیدینفای معمولاً 10808 یا 10809 است)
    # اگر پورت نرم‌افزار شما فرق می‌کند، فقط عدد را تغییر دهید
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .proxy("socks5://127.0.0.1:10808")
        .get_updates_proxy("socks5://127.0.0.1:10808")
        .build()
    )
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_incoming_messages))

    app.run_polling()