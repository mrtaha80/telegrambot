import os
import re
import hashlib
import sqlite3
import asyncio
import logging
import unicodedata
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes
)
from telegram.request import HTTPXRequest
from fuzzywuzzy import fuzz

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

SEARCH_STATE = 1
SEAT_SYMBOLS = "➊➋➌➍➎➏➐➑➒➓❶❷❸❹❺❻❼❽❾❿⓫⓬⓭⓮⓯"

def normalize_text(text):
    if not text:
        return ""
    invisible_chars = ['\u200b', '\u200c', '\u200d', '\u200e', '\u200f', '\ufeff', '\u202a', '\u202b', '\u202c', '\u202d', '\u202e']
    for ch in invisible_chars:
        text = text.replace(ch, ' ')
    text = unicodedata.normalize('NFKD', text)
    persian_nums = '۰۱۲۳۴۵۶۷۸۹'
    for i, p in enumerate(persian_nums):
        text = text.replace(p, str(i))
    return text

def deep_clean_line(text):
    if not text:
        return ""
    cleaned = re.sub(r'^[^\w\u0600-\u06FF]*[\d\u2776-\u277F\u2780-\u2793\u2460-\u2473]+[^\w\u0600-\u06FF]*', '', text).strip()
    return cleaned

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
            game_signature TEXT PRIMARY KEY
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
    clean_name = re.sub(rf'[{SEAT_SYMBOLS}]', '', clean_name)
    clean_name = re.sub(r'[\.\-_:]', ' ', clean_name)
    clean_name = " ".join(clean_name.split())

    if not clean_name or len(clean_name) < 2 or clean_name.isdigit() or clean_name == 'god':
        return None, None

    if not re.search(r'[a-zA-Z\u0600-\u06FF]', clean_name):
        return None, None

    cursor.execute("SELECT id, name FROM players")
    existing_players = cursor.fetchall()
    
    if existing_players:
        for pid, existing_name in existing_players:
            if clean_name == existing_name:
                return pid, existing_name
            
            ratio = fuzz.ratio(clean_name, existing_name)
            len_diff = abs(len(clean_name) - len(existing_name))
            if ratio >= 82 and len_diff <= 2:
                return pid, existing_name

    cursor.execute("INSERT OR IGNORE INTO players (name) VALUES (?)", (clean_name,))
    cursor.execute("SELECT id FROM players WHERE name = ?", (clean_name,))
    row = cursor.fetchone()
    return row[0], clean_name

# ================= تشخیص نقش و ساید =================
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

# ================= استخراج دقیق اطلاعات =================
def process_text_data(raw_text, fallback_id):
    try:
        norm = normalize_text(raw_text)

        event_match = re.search(r'(?:event|ایونت)\s*[:#•\-_ ]*([0-9]+)', norm, re.IGNORECASE)
        scenario_match = re.search(r'(?:scenario|سناریو)\s*[:•\-_]\s*([^\n\r]+)', norm, re.IGNORECASE)
        win_match = re.search(r'(?:winner|win|برنده|برد)\s*[:•\-_]\s*([^\n\r]+)', norm, re.IGNORECASE)
        god_match = re.search(r'(?:god|گاد)\s*[:•\-_]\s*([^\n\r]+)', norm, re.IGNORECASE)
        date_match = re.search(r'(?:date|تاریخ|📅)\s*[:•\-_ ]*([0-9/\-]+)', norm, re.IGNORECASE)
        time_match = re.search(r'(?:time|ساعت|🕒|⏳)\s*[:•\-_ ]*([0-9:]+)', norm, re.IGNORECASE)

        if not scenario_match or not win_match:
            return False

        event_id = event_match.group(1).strip() if event_match else str(fallback_id)
        scenario = scenario_match.group(1).strip()
        win_text = win_match.group(1).strip().lower()
        god = god_match.group(1).strip().lower() if god_match else ""
        date = date_match.group(1).strip() if date_match else ""
        time_val = time_match.group(1).strip() if time_match else ""

        winning_side = None
        if any(w in win_text for w in ['مافیا', 'mafia']):
            winning_side = "Mafia"
        elif any(w in win_text for w in ['شهر', 'citizen', 'کی اس', 'ks']):
            winning_side = "Citizen"

        if not winning_side:
            return False

        players_match = re.search(r'(?:players|بازیکنان|پلیرها)([\s\S]*?)(?:winner|win|🏆|$)', norm, re.IGNORECASE)
        if not players_match:
            return False

        players_block = players_match.group(1)
        parsed_players = []

        for line in players_block.strip().splitlines():
            line = line.strip()
            if not line or any(sym in line for sym in ['━', '┄', '─', '🥀', '🎭', '🕯']):
                continue

            clean_line = deep_clean_line(line)
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

            if not name or len(name) < 2 or not re.search(r'[a-zA-Z\u0600-\u06FF]', name):
                continue

            side = detect_side(scenario, role)
            if side != "Independent":
                parsed_players.append((name.lower(), role.lower(), side))

        if len(parsed_players) < 5:
            return False

        players_fingerprint = "-".join(sorted([f"{p[0]}:{p[1]}" for p in parsed_players]))
        full_game_identity = f"{event_id}_{scenario.lower()}_{god}_{date}_{time_val}_{winning_side}_{players_fingerprint}"
        game_signature = hashlib.sha256(full_game_identity.encode('utf-8')).hexdigest()

        conn = sqlite3.connect('mafia_stats.db', timeout=60.0)
        c = conn.cursor()

        c.execute("SELECT 1 FROM processed_games WHERE game_signature = ?", (game_signature,))
        if c.fetchone():
            conn.close()
            return False

        for name, role, side in parsed_players:
            player_id, _ = get_or_create_player(c, name)
            if not player_id:
                continue

            is_win = 1 if side == winning_side else 0
            c.execute('''
                INSERT OR IGNORE INTO matches (player_id, game_signature, event_id, scenario, side, is_win)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (player_id, game_signature, event_id, scenario, side, is_win))

        c.execute("INSERT OR IGNORE INTO processed_games (game_signature) VALUES (?)", (game_signature,))
        conn.commit()
        conn.close()
        return True

    except Exception as e:
        print(f"Error parsing event: {e}")
        return False

# ================= ساخت فایل PDF (با امتیاز بیزی) =================
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
        fontSize=10,
        textColor=colors.HexColor('#64748B'),
        alignment=1,
        spaceAfter=18
    )
    section_style = ParagraphStyle(
        'SectionHeading',
        parent=styles['Heading2'],
        fontSize=12,
        leading=16,
        textColor=colors.HexColor('#0F172A'),
        spaceBefore=14,
        spaceAfter=8
    )

    elements.append(Paragraph("<b>CAFE MAFIA STATISTICAL REPORT</b>", title_style))
    elements.append(Paragraph("Ranked by Bayesian Regularized Score (Min 18 Games | Per Side: Min 9 Games)", subtitle_style))

    table_data = [["Rank", "Player", "Matches", "Bayesian Pts", "Raw Win%", "Mafia", "Citizen"]]
    for idx, row in enumerate(results, 1):
        name, total_g, raw_win, bayes_score, m_games, m_wins, c_games, c_wins = row
        m_rate = (m_wins * 100 // m_games) if m_games > 0 else 0
        c_rate = (c_wins * 100 // c_games) if c_games > 0 else 0
        table_data.append([
            str(idx),
            name.title(),
            str(total_g),
            f"{bayes_score:.1f}",
            f"{raw_win:.1f}%",
            f"{m_rate}% ({m_wins}/{m_games})",
            f"{c_rate}% ({c_wins}/{c_games})"
        ])

    main_table = Table(table_data, colWidths=[35, 120, 50, 75, 65, 95, 95])
    main_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0F172A')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('ALIGN', (1, 1), (1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor('#F8FAFC'), colors.HexColor('#FFFFFF')]),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
        ('FONTSIZE', (0, 1), (-1, -1), 8.5),
        ('TOPPADDING', (0, 1), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 4),
    ]))
    elements.append(main_table)
    elements.append(Spacer(1, 15))

    elements.append(Paragraph("<b>Top Performers by Side (Bayesian Weighted | Min 9 Games)</b>", section_style))
    top_side_data = [["Top Mafia Players (>= 9 Games)", "Top Citizen Players (>= 9 Games)"]]
    max_len = max(len(mafia_leaders[:5]), len(citizen_leaders[:5]))
    
    for i in range(max_len):
        m_txt = f"{i+1}. {mafia_leaders[i][0].title()} — Score: {mafia_leaders[i][1]:.1f} (Win: {mafia_leaders[i][2]}% | {mafia_leaders[i][4]}/{mafia_leaders[i][3]})" if i < len(mafia_leaders[:5]) else ""
        c_txt = f"{i+1}. {citizen_leaders[i][0].title()} — Score: {citizen_leaders[i][1]:.1f} (Win: {citizen_leaders[i][2]}% | {citizen_leaders[i][4]}/{citizen_leaders[i][3]})" if i < len(citizen_leaders[:5]) else ""
        top_side_data.append([m_txt, c_txt])

    side_table = Table(top_side_data, colWidths=[270, 270])
    side_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, 0), colors.HexColor('#DC2626')),
        ('BACKGROUND', (1, 0), (1, 0), colors.HexColor('#2563EB')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 9.5),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E2E8F0')),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
    ]))
    elements.append(side_table)

    doc.build(elements)
    return filename

# ================= ارسال پیام‌های طولانی =================
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

def get_main_keyboard():
    keyboard = [
        [KeyboardButton("📊 مشاهده رتبه‌بندی بیزی و گزارش (PDF)")],
        [KeyboardButton("🔍 جستجوی آمار بازیکن"), KeyboardButton("❓ راهنما")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

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
                f"✅ بازی‌های جدید و معتبر ثبت‌شده: {added}\n"
                f"🔁 بازی‌های تکراری رد شده: {len(messages) - added}\n"
                f"📊 مجموع کل بازی‌های ثبت‌شده: {all_stored_games}"
            ),
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
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

    if raw_content == "📊 مشاهده رتبه‌بندی بیزی و گزارش (PDF)":
        await report_command(update, context)
        return
    elif raw_content == "🔍 جستجوی آمار بازیکن":
        await update.message.reply_text("🔎 لطفاً نام انگلیسی بازیکن را ارسال کنید:")
        return SEARCH_STATE
    elif raw_content == "❓ راهنما":
        await help_command(update, context)
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
    await update.message.reply_text(
        "👋 سلام! به ربات رتبه‌بندی تحلیلی کافه مافیا خوش آمدید.\n\n"
        "سیستم رتبه‌بندی این ربات بر پایه **میانگین بیزی آماری** تنظیم شده است که علاوه بر درصد برد، ثبات در تعداد بازی‌ها را نیز در امتیاز نهایی لحاظ می‌کند.",
        reply_markup=get_main_keyboard()
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📖 **راهنمای سیستم رتبه‌بندی و فرمول بیزی:**\n\n"
        "🔹 **فرمول بیزی:** برای برقراری عدالت بین بازیکنان پرتعداد و بازیکنانی که تعداد کمی بازی داشته‌اند، امتیاز رتبه با فرمول زیر محاسبه می‌شود:\n"
        "`Score = (بردها + 5) / (تعداد بازی + 10) * 100`\n"
        "🔹 **حد نصاب‌ها:** حداقل ۱۸ بازی برای رتبه‌بندی اصلی و حداقل ۹ بازی در هر ساید برای رتبه‌بندی آن ساید.\n"
        "🔹 نام **Ali** از جدول حذف شده است."
    )
    await update.message.reply_text(help_text, parse_mode="Markdown", reply_markup=get_main_keyboard())

# ================= گزارش کلی بر اساس فرمول بیزی =================
async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with DB_LOCK:
        conn = sqlite3.connect('mafia_stats.db', timeout=60.0)
        c = conn.cursor()

        # کوئری بیزی: محاسبه امتیاز بیزی کل و سورت بر مبنای آن
        c.execute('''
            SELECT 
                p.name,
                COUNT(m.id) as total_games,
                (SUM(CASE WHEN m.is_win = 1 THEN 1 ELSE 0 END) * 100.0 / COUNT(m.id)) as overall_win_rate,
                ((SUM(CASE WHEN m.is_win = 1 THEN 1 ELSE 0 END) + 5.0) * 100.0 / (COUNT(m.id) + 10.0)) as bayes_score,
                SUM(CASE WHEN m.side = 'Mafia' THEN 1 ELSE 0 END) as mafia_games,
                SUM(CASE WHEN m.side = 'Mafia' AND m.is_win = 1 THEN 1 ELSE 0 END) as mafia_wins,
                SUM(CASE WHEN m.side = 'Citizen' THEN 1 ELSE 0 END) as citizen_games,
                SUM(CASE WHEN m.side = 'Citizen' AND m.is_win = 1 THEN 1 ELSE 0 END) as citizen_wins
            FROM players p
            JOIN matches m ON p.id = m.player_id
            WHERE LOWER(p.name) != 'ali'
            GROUP BY p.id
            HAVING total_games >= 18
            ORDER BY bayes_score DESC, total_games DESC
        ''')
        results = c.fetchall()
        conn.close()

    if not results:
        await update.message.reply_text("هنوز بازیکنی به حد نصاب حداقل ۱۸ بازی نرسیده است.", reply_markup=get_main_keyboard())
        return

    report = "📊 **رتبه‌بندی بازیکنان (بر اساس فرمول بیزی | حداقل ۱۸ بازی)**\n\n"
    mafia_leaders = []
    citizen_leaders = []

    for idx, row in enumerate(results, 1):
        name, total_g, raw_win, bayes_score, m_games, m_wins, c_games, c_wins = row
        m_rate = (m_wins * 100 // m_games) if m_games > 0 else 0
        c_rate = (c_wins * 100 // c_games) if c_games > 0 else 0

        # محاسبه بیزی اختصاصی ساید مافیا: C=4, m=50%
        if m_games >= 9:
            m_bayes = ((m_wins + 2.0) / (m_games + 4.0)) * 100.0
            mafia_leaders.append((name, m_bayes, m_rate, m_games, m_wins))

        # محاسبه بیزی اختصاصی ساید شهروند: C=4, m=50%
        if c_games >= 9:
            c_bayes = ((c_wins + 2.0) / (c_games + 4.0)) * 100.0
            citizen_leaders.append((name, c_bayes, c_rate, c_games, c_wins))

        report += f"🎖 **رتبه {idx}. {name.title()}**\n"
        report += f"⭐️ **امتیاز بیزی:** {bayes_score:.1f} | 🎮 بازی‌ها: {total_g}\n"
        report += f"🏆 درصد برد واقعی: {raw_win:.1f}%\n"
        report += f"🔪 مافیا: {m_rate}% ({m_wins}/{m_games}) | 🛡 شهر: {c_rate}% ({c_wins}/{c_games})\n"
        report += "─────────────────\n"

    # سورت بر اساس امتیاز بیزی ساید مافیا
    mafia_leaders.sort(key=lambda x: x[1], reverse=True)
    report += "\n🔥 **۵ بازیکن برتر ساید مافیا (وزن‌دهی بیزی | حداقل ۹ بازی):**\n"
    if mafia_leaders:
        for r, (n, score, rate, games, wins) in enumerate(mafia_leaders[:5], 1):
            report += f"{r}. {n.title()} ⟵ نمره: {score:.1f} (برد: {rate}% | {wins}/{games})\n"
    else:
        report += "بازیکنی به ۹ بازی مافیا نرسیده است.\n"

    # سورت بر اساس امتیاز بیزی ساید شهروند
    citizen_leaders.sort(key=lambda x: x[1], reverse=True)
    report += "\n🛡 **۵ بازیکن برتر ساید شهروند (وزن‌دهی بیزی | حداقل ۹ بازی):**\n"
    if citizen_leaders:
        for r, (n, score, rate, games, wins) in enumerate(citizen_leaders[:5], 1):
            report += f"{r}. {n.title()} ⟵ نمره: {score:.1f} (برد: {rate}% | {wins}/{games})\n"
    else:
        report += "بازیکنی به ۹ بازی شهروندی نرسیده است.\n"

    await send_large_text(update, report, context)

    pdf_path = generate_pdf_report(results, mafia_leaders, citizen_leaders)
    try:
        with open(pdf_path, 'rb') as pdf_file:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=pdf_file,
                filename="CafeMafia_Bayesian_Leaderboard.pdf",
                caption="📄 نسخه PDF رتبه‌بندی با مدل میانگین بیزی (حداقل ۱۸ بازی | بدون ali)",
                reply_markup=get_main_keyboard()
            )
    except Exception as e:
        print(f"Error sending PDF: {e}")

# ================= جستجوی اختصاصی بازیکن =================
async def search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔎 لطفاً نام بازیکن مورد نظر را ارسال کنید:")
    return SEARCH_STATE

async def search_perform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip().lower()
    
    if query == 'ali':
        await update.message.reply_text("⚠️ این نام در لیست سیاه آماری قرار دارد و نمایش داده نمی‌شود.", reply_markup=get_main_keyboard())
        return ConversationHandler.END

    async with DB_LOCK:
        conn = sqlite3.connect('mafia_stats.db', timeout=60.0)
        c = conn.cursor()

        c.execute('''
            SELECT 
                p.id,
                p.name,
                COUNT(m.id) as total_games,
                (SUM(CASE WHEN m.is_win = 1 THEN 1 ELSE 0 END) * 100.0 / COUNT(m.id)) as overall_win_rate,
                ((SUM(CASE WHEN m.is_win = 1 THEN 1 ELSE 0 END) + 5.0) * 100.0 / (COUNT(m.id) + 10.0)) as bayes_score,
                SUM(CASE WHEN m.side = 'Mafia' THEN 1 ELSE 0 END) as mafia_games,
                SUM(CASE WHEN m.side = 'Mafia' AND m.is_win = 1 THEN 1 ELSE 0 END) as mafia_wins,
                SUM(CASE WHEN m.side = 'Citizen' THEN 1 ELSE 0 END) as citizen_games,
                SUM(CASE WHEN m.side = 'Citizen' AND m.is_win = 1 THEN 1 ELSE 0 END) as citizen_wins
            FROM players p
            JOIN matches m ON p.id = m.player_id
            WHERE LOWER(p.name) != 'ali'
            GROUP BY p.id
            ORDER BY bayes_score DESC, total_games DESC
        ''')
        all_players = c.fetchall()
        conn.close()

    if not all_players:
        await update.message.reply_text("دیتابیس خالی است.", reply_markup=get_main_keyboard())
        return ConversationHandler.END

    matched_player = None
    rank = 0
    best_score = 0

    for idx, row in enumerate(all_players, 1):
        p_name = row[1]
        score = fuzz.ratio(query, p_name)
        if query == p_name:
            matched_player = row
            rank = idx
            break
        elif score > best_score and score >= 75:
            best_score = score
            matched_player = row
            rank = idx

    if not matched_player:
        await update.message.reply_text(f"❌ بازیکنی با نام «{query}» پیدا نشد.", reply_markup=get_main_keyboard())
        return ConversationHandler.END

    pid, name, total_g, win_rate, bayes_score, m_games, m_wins, c_games, c_wins = matched_player
    m_rate = (m_wins * 100 // m_games) if m_games > 0 else 0
    c_rate = (c_wins * 100 // c_games) if c_games > 0 else 0

    profile_text = (
        f"👤 **پروفایل آماری بازیکن:** `{name.title()}`\n"
        f"─────────────────────\n"
        f"🎖 **رتبه بیزی در جدول:** #{rank} (از بین {len(all_players)} بازیکن)\n"
        f"⭐️ **امتیاز عملکرد بیزی:** {bayes_score:.1f}\n"
        f"🎮 **مجموع بازی‌ها:** {total_g}\n"
        f"🏆 **درصد برد واقعی کل:** {win_rate:.1f}%\n\n"
        f"🔪 **عملکرد در ساید مافیا:**\n"
        f"   ▫️ تعداد بازی: {m_games}\n"
        f"   ▫️ پیروزی‌ها: {m_wins}\n"
        f"   ▫️ درصد برد: {m_rate}%\n\n"
        f"🛡 **عملکرد در ساید شهروند:**\n"
        f"   ▫️ تعداد بازی: {c_games}\n"
        f"   ▫️ پیروزی‌ها: {c_wins}\n"
        f"   ▫️ درصد برد: {c_rate}%\n"
        f"─────────────────────"
    )

    await update.message.reply_text(profile_text, parse_mode="Markdown", reply_markup=get_main_keyboard())
    return ConversationHandler.END

async def search_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("جستجو لغو شد.", reply_markup=get_main_keyboard())
    return ConversationHandler.END

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.warning(f"شبکه با اختلال موقت مواجه شد: {context.error}")

# ================= اجرای برنامه =================
if __name__ == '__main__':
    init_db()
    print("ربات با موتور محاسباتی بیزی، کیبورد دکمه‌ای و سرچ اختصاصی فعال شد...")
    
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
    
    search_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^🔍 جستجوی آمار بازیکن$"), search_start),
            CommandHandler("search", search_start)
        ],
        states={
            SEARCH_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_perform)]
        },
        fallbacks=[CommandHandler("cancel", search_cancel)]
    )

    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("report", report_command))
    app.add_handler(search_conv)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_incoming_messages))

    app.run_polling()