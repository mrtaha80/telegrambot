import os
import re
import hashlib
import sqlite3
import asyncio
import logging
import unicodedata
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.request import HTTPXRequest
from fuzzywuzzy import process

from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

logging.getLogger('fuzzywuzzy').setLevel(logging.ERROR)

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
    conn = sqlite3.connect('mafia_stats.db', timeout=60.0)
    c = conn.cursor()
    c.execute('PRAGMA journal_mode=WAL;')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE COLLATE NOCASE
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS processed_games (
            game_hash TEXT PRIMARY KEY
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            player_id INTEGER,
            game_hash TEXT,
            event_id TEXT,
            scenario TEXT,
            side TEXT,
            is_win INTEGER,
            UNIQUE(player_id, game_hash),
            FOREIGN KEY(player_id) REFERENCES players(id)
        )
    ''')
    conn.commit()
    conn.close()

def get_or_create_player(cursor, raw_name):
    clean_name = raw_name.strip().lower()
    clean_name = re.sub(r'[\.\-_:]', ' ', clean_name)
    clean_name = " ".join(clean_name.split())

    if not clean_name or len(clean_name) < 1:
        return None, None

    cursor.execute("SELECT id, name FROM players")
    existing_players = cursor.fetchall()
    
    if existing_players:
        names = [p[1] for p in existing_players]
        best_match, score = process.extractOne(clean_name, names)
        if clean_name == best_match or (score >= 90 and abs(len(clean_name) - len(best_match)) <= 2):
            for p in existing_players:
                if p[1] == best_match:
                    return p[0], p[1]

    cursor.execute("INSERT OR IGNORE INTO players (name) VALUES (?)", (clean_name,))
    cursor.execute("SELECT id FROM players WHERE name = ?", (clean_name,))
    row = cursor.fetchone()
    return row[0], clean_name

# ================= تشخیص ساید و نقش‌ها =================
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

# ================= پردازش اطلاعات =================
def process_text_data(raw_text, fallback_id):
    try:
        cleaned_raw = "".join(raw_text.split())
        exact_game_hash = hashlib.md5(cleaned_raw.encode('utf-8')).hexdigest()

        conn = sqlite3.connect('mafia_stats.db', timeout=60.0)
        c = conn.cursor()

        c.execute("SELECT 1 FROM processed_games WHERE game_hash = ?", (exact_game_hash,))
        if c.fetchone():
            conn.close()
            return False

        text = normalize_text(raw_text)

        scenario_match = re.search(r'(?:scenario|سناریو)\s*[:•\-_]\s*([^\n\r]+)', text, re.IGNORECASE)
        win_match = re.search(r'(?:winner|win|برنده|برد)\s*[:•\-_]\s*([^\n\r]+)', text, re.IGNORECASE)
        event_match = re.search(r'(?:event|ایونت)\s*[:#•\-_ ]*([0-9]+)', text, re.IGNORECASE)

        if not scenario_match or not win_match:
            conn.close()
            return False

        scenario = scenario_match.group(1).strip()
        win_text = win_match.group(1).strip().lower()
        event_id = event_match.group(1).strip() if event_match else str(fallback_id)

        winning_side = None
        if any(w in win_text for w in ['مافیا', 'mafia']):
            winning_side = "Mafia"
        elif any(w in win_text for w in ['شهر', 'citizen', 'کی اس', 'ks']):
            winning_side = "Citizen"

        if not winning_side:
            conn.close()
            return False

        players_match = re.search(r'(?:players|بازیکنان|پلیرها)([\s\S]*?)(?:winner|win|🏆|$)', text, re.IGNORECASE)
        if not players_match:
            conn.close()
            return False

        players_block = players_match.group(1)
        inserted_any = False

        for line in players_block.strip().splitlines():
            line = line.strip()
            if not line or any(sym in line for sym in ['━', '┄', '─', '🥀', '🎭', '🕯']):
                continue

            clean_line = re.sub(r'^[^a-zA-Z\u0600-\u06FF]*[0-9➊-➓]+[\s\:\.\-\/\•]*', '', line).strip()
            if not clean_line:
                continue

            clean_line = re.sub(r'[👈👉].*$', '', clean_line).strip()
            clean_line = re.sub(r'\(.*?\)', '', clean_line).strip()

            lang_split = re.search(r'^([a-zA-Z0-9\.\s_-]+)([\u0600-\u06FF\s].*)$', clean_line)
            if lang_split:
                name = lang_split.group(1).strip()
                role = lang_split.group(2).strip()
            else:
                tokens = clean_line.split()
                if len(tokens) >= 2:
                    name = tokens[0]
                    role = " ".join(tokens[1:])
                else:
                    name = clean_line
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
                INSERT OR IGNORE INTO matches (player_id, game_hash, event_id, scenario, side, is_win)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (player_id, exact_game_hash, event_id, scenario, side, is_win))
            
            inserted_any = True

        if inserted_any:
            c.execute("INSERT OR IGNORE INTO processed_games (game_hash) VALUES (?)", (exact_game_hash,))

        conn.commit()
        conn.close()
        return inserted_any

    except Exception as e:
        print(f"Error parsing event: {e}")
        return False

# ================= ساخت PDF =================
def generate_pdf_report(results, mafia_leaders, citizen_leaders, filename="Mafia_Leaderboard.pdf"):
    doc = SimpleDocTemplate(
        filename,
        pagesize=letter,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36
    )
    elements = []
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        'MainTitle',
        parent=styles['Heading1'],
        fontSize=20,
        leading=24,
        textColor=colors.HexColor('#0F172A'),
        alignment=1,
        spaceAfter=10
    )
    subtitle_style = ParagraphStyle(
        'SubTitle',
        parent=styles['Normal'],
        fontSize=11,
        textColor=colors.HexColor('#64748B'),
        alignment=1,
        spaceAfter=20
    )
    section_style = ParagraphStyle(
        'SectionHeading',
        parent=styles['Heading2'],
        fontSize=13,
        leading=16,
        textColor=colors.HexColor('#0F172A'),
        spaceBefore=14,
        spaceAfter=8
    )

    elements.append(Paragraph("<b>CAFE MAFIA STATISTICAL REPORT</b>", title_style))
    elements.append(Paragraph("Official Leaderboard (Minimum 10 Games)", subtitle_style))

    table_data = [["Rank", "Player", "Matches", "Win Rate", "Mafia Record", "Citizen Record"]]
    for idx, row in enumerate(results, 1):
        name, total_g, win_rate, m_games, m_wins, c_games, c_wins = row
        m_rate = (m_wins * 100 // m_games) if m_games > 0 else 0
        c_rate = (c_wins * 100 // c_games) if c_games > 0 else 0
        table_data.append([
            str(idx),
            name.title(),
            str(total_g),
            f"{win_rate:.1f}%",
            f"{m_rate}% ({m_wins}/{m_games})",
            f"{c_rate}% ({c_wins}/{c_games})"
        ])

    main_table = Table(table_data, colWidths=[40, 140, 60, 75, 110, 110])
    main_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0F172A')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('ALIGN', (1, 1), (1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor('#F8FAFC'), colors.HexColor('#FFFFFF')]),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
        ('FONTSIZE', (0, 1), (-1, -1), 9),
        ('TOPPADDING', (0, 1), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 5),
    ]))
    elements.append(main_table)
    elements.append(Spacer(1, 15))

    elements.append(Paragraph("<b>Top Performers by Side</b>", section_style))
    top_side_data = [["Top Mafia Players", "Top Citizen Players"]]
    max_len = max(len(mafia_leaders[:5]), len(citizen_leaders[:5]))
    
    for i in range(max_len):
        m_txt = f"{i+1}. {mafia_leaders[i][0].title()} — {mafia_leaders[i][1]}% ({mafia_leaders[i][3]}/{mafia_leaders[i][2]})" if i < len(mafia_leaders[:5]) else ""
        c_txt = f"{i+1}. {citizen_leaders[i][0].title()} — {citizen_leaders[i][1]}% ({citizen_leaders[i][3]}/{citizen_leaders[i][2]})" if i < len(citizen_leaders[:5]) else ""
        top_side_data.append([m_txt, c_txt])

    side_table = Table(top_side_data, colWidths=[270, 270])
    side_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, 0), colors.HexColor('#DC2626')),
        ('BACKGROUND', (1, 0), (1, 0), colors.HexColor('#2563EB')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
    ]))
    elements.append(side_table)

    doc.build(elements)
    return filename

# ================= ارسال پیام طولانی =================
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

# ================= هندلرهای تلگرام =================
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

    conn = sqlite3.connect('mafia_stats.db', timeout=60.0)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM processed_games")
    all_stored_games = c.fetchone()[0]
    conn.close()

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"📥 **گزارش پردازش دسته‌ای:**\n"
                f"🔹 پیام‌های بررسی‌شده: {len(messages)}\n"
                f"✅ بازی‌های جدید اضافه شده: {added}\n"
                f"🔁 بازی‌های تکراری رد شده (مو به مو یکسان): {len(messages) - added}\n"
                f"📊 مجموع کل بازی‌های ثبت‌شده: {all_stored_games}"
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
    await update.message.reply_text("ربات آماده است! پیام‌ها را فوروارد کنید و با /report آمار بگیرید.")

async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with DB_LOCK:
        conn = sqlite3.connect('mafia_stats.db', timeout=60.0)
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
            HAVING total_games >= 10
            ORDER BY overall_win_rate DESC, total_games DESC
        ''')
        results = c.fetchall()
        conn.close()

    if not results:
        await update.message.reply_text("هنوز بازیکنی به حد نصاب حداقل ۱۰ بازی نرسیده است.")
        return

    report = "📊 **رتبه‌بندی بازیکنان (حداقل ۱۰ بازی)**\n\n"
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
    report += "\n🔥 **۵ بازیکن برتر در ساید مافیا:**\n"
    for r, (n, rate, games, wins) in enumerate(mafia_leaders[:5], 1):
        report += f"{r}. {n.title()} ⟵ {rate}% برد ({wins}/{games})\n"

    citizen_leaders.sort(key=lambda x: (x[1], x[2]), reverse=True)
    report += "\n🛡 **۵ بازیکن برتر در ساید شهروند:**\n"
    for r, (n, rate, games, wins) in enumerate(citizen_leaders[:5], 1):
        report += f"{r}. {n.title()} ⟵ {rate}% برد ({wins}/{games})\n"

    await send_large_text(update, report, context)

    pdf_path = generate_pdf_report(results, mafia_leaders, citizen_leaders)
    try:
        with open(pdf_path, 'rb') as pdf_file:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=pdf_file,
                filename="CafeMafia_Leaderboard.pdf",
                caption="📄 نسخه PDF گزارش عملکرد بازیکنان (حداقل ۱۰ بازی)"
            )
    except Exception as e:
        print(f"Error sending PDF: {e}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.warning(f"شبکه با اختلال موقت مواجه شد: {context.error}")

# ================= اجرای برنامه =================
if __name__ == '__main__':
    init_db()
    print("ربات با ظرفیت اتصال بالا (Pool Size 100) فعال شد...")
    
    # تنظیم ابزار مدیریت شبکه با پشتیبانی از حجم بالای اتصال موازی
    custom_request = HTTPXRequest(
        connection_pool_size=100,
        pool_timeout=60.0,
        read_timeout=60.0,
        write_timeout=60.0,
        connect_timeout=60.0
    )
    
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .request(custom_request)
        .get_updates_request(custom_request)
        .build()
    )
    
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_incoming_messages))

    app.run_polling()