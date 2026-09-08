import re
import sqlite3
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from fuzzywuzzy import process

# ================= تنظیمات احراز هویت =================
# تنها با توکن بات‌فادر بدون نیاز به API_ID
BOT_TOKEN = '8936060141:AAHD7N56eK7FtIq_FBy8E1txGNKkV2lWQjI'

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
    c.execute('''
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER,
            event_id TEXT,
            scenario TEXT,
            side TEXT,
            is_win INTEGER,
            UNIQUE(player_id, event_id),
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

# ================= منطق تشخیص نقش و سناریو =================
def detect_side(scenario, role):
    sc = scenario.lower().strip()
    ro = role.lower().strip()

    # ۱. نقش‌های مستقل
    independents = ['jack', 'جک', 'nostra', 'نوسترا', 'sherlock', 'شرلوک', 'churchill', 'چرچیل']
    if any(ind in ro for ind in independents):
        return "Independent"

    # ۲. مافیاهای پایه و اضافه شونده در بازی‌های ۱۲ الی ۱۵ نفره
    mafia_roles = ['don', 'دن', 'nato', 'ناتو', 'mafia', 'مافیا']

    # ۳. مافیاهای اختصاصی هر سناریو
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

# ================= استخراج اطلاعات بازی =================
def process_text_data(text, unique_msg_id):
    try:
        scenario_match = re.search(r'𝐒𝐂𝐄𝐍𝐀𝐑𝐈𝐎\s*•\s*(.+)', text)
        win_match = re.search(r'𝐖𝐈𝐍\s*•\s*(.+)', text)
        event_match = re.search(r'𝐄𝐕𝐄𝐍𝐓\s*•\s*([0-9]+)', text)

        if not scenario_match or not win_match:
            return False

        scenario = scenario_match.group(1).strip()
        win_text = win_match.group(1).strip().lower()
        event_id = event_match.group(1).strip() if event_match else str(unique_msg_id)

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
                INSERT OR IGNORE INTO matches (player_id, event_id, scenario, side, is_win)
                VALUES (?, ?, ?, ?, ?)
            ''', (player_id, event_id, scenario, side, is_win))

        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Error processing: {e}")
        return False

# ================= هندلرهای ربات =================
async def channel_post_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post or update.message
    if msg and msg.text:
        if "𝐏𝐋𝐀𝐘𝐄𝐑𝐒" in msg.text and "𝐖𝐈𝐍" in msg.text:
            success = process_text_data(msg.text, msg.message_id)
            if success:
                print(f"Event ثبت شد: شناسه {msg.message_id}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سلام! ربات تحلیل آمار مافیا آماده است.\n"
        "برای دیدن گزارش دستور /report را ارسال کنید."
    )

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect('mafia_stats.db')
    c = conn.cursor()

    # فیلتر بازیکنان بالای ۳۰ بازی
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
    print("ربات با موفقیت فعال شد و منتظر دریافت پیام‌هاست...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("report", report_command))
    # دریافت و پردازش خودکار پیام‌های ارسال شده در کانال یا پی‌وی
    app.add_handler(MessageHandler(filters.ALL, channel_post_handler))

    app.run_polling()