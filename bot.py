import os
import re
import math
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

# ================= ساخت فایل PDF (رتبه‌بندی بیزی و پاداش تعداد بازی) =================
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

    elements.append(Paragraph("<b>CAFE MAFIA OFFICIAL LEADERBOARD</b>", title_style))
    elements.append(Paragraph("Bayesian Regularized Score with Volume Weighting (Min 18 Games | Per Side Min 9 Games)", subtitle_style))

    table_data = [["Rank", "Player", "Matches", "Score Pts", "Raw Win%", "Mafia", "Citizen"]]
    for idx, p in enumerate(results, 1):
        m_rate = (p['m_wins'] * 100 // p['m_games']) if p['m_games'] > 0 else 0
        c_rate = (p['c_wins'] * 100 // p['c_games']) if p['c_games'] > 0 else 0

        table_data.append([
            str(idx),
            p['name'].title(),
            str(p['total_games']),
            f"{p['bayes_score']:.2f}",
            f"{p['raw_win']:.1f}%",
            f"{m_rate}% ({p['m_wins']}/{p['m_games']})",
            f"{c_rate}% ({p['c_wins']}/{p['c_games']})"
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

    elements.append(Paragraph("<b>Top Performers by Side (Volume Weighted | Min 9 Games)</b>", section_style))
    top_side_data = [["Top Mafia Players (>= 9 Games)", "Top Citizen Players (>= 9 Games)"]]
    max_len = max(len(mafia_leaders[:5]), len(citizen_leaders[:5]))
    
    for i in range(max_len):
        m_txt = f"{i+1}. {mafia_leaders[i]['name'].title()} — Pts: {mafia_leaders[i]['bayes']:.2f} (Win: {mafia_leaders[i]['rate']}% | {mafia_leaders[i]['wins']}/{mafia_leaders[i]['games']})" if i < len(mafia_leaders[:5]) else ""
        c_txt = f"{i+1}. {citizen_leaders[i]['name'].title()} — Pts: {citizen_leaders[i]['bayes']:.2f} (Win: {citizen_leaders[i]['rate']}% | {citizen_leaders[i]['wins']}/{citizen_leaders[i]['games']})" if i < len(citizen_leaders[:5]) else ""
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
                f"✅ بازی‌های جدید اضافه شده: {added}\n"
                f"🔁 بازی‌های تکراری رد شده: {len(messages) - added}\n"
                f"📊 مجموع کل بازی‌های ثبت‌شده در سیستم: {all_stored_games}"
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
        "سیستم رتبه‌بندی بر پایه فرمول بیزی وزن‌دار همراه با پاداش پایداری در تعداد بازی‌ها تنظیم شده است.",
        reply_markup=get_main_keyboard()
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📖 **راهنمای سیستم رتبه‌بندی و فرمول بیزی:**\n\n"
        "🔹 **فرمول بیزی با پاداش حجم بازی:**\n"
        "برای برقراری عدالت، امتیاز رتبه‌بندی هم به درصد برد و هم به تعداد کل بازی‌ها وابسته است:\n"
        "`Score = Base_Bayes * (1 + 0.08 * log10(Matches / 18 + 1))`\n\n"
        "🔹 **حد نصاب‌ها:**\n"
        "▫️ حداقل ۱۸ بازی کل برای ورود به جدول رنکینگ.\n"
        "▫️ حداقل ۹ بازی در هر ساید برای ورود به تاپ ۵ آن ساید.\n"
        "▫️ نام **Ali** از آمار کل کنار گذاشته شده است."
    )
    await update.message.reply_text(help_text, parse_mode="Markdown", reply_markup=get_main_keyboard())

# ================= گزارش رسمی با امتیاز بیزی و بوست بازی‌ها =================
async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with DB_LOCK:
        conn = sqlite3.connect('mafia_stats.db', timeout=60.0)
        c = conn.cursor()

        c.execute('''
            SELECT 
                AVG(is_win) as global_win_mean,
                AVG(CASE WHEN side = 'Mafia' THEN is_win END) as mafia_win_mean,
                AVG(CASE WHEN side = 'Citizen' THEN is_win END) as citizen_win_mean
            FROM matches m
            JOIN players p ON p.id = m.player_id
            WHERE LOWER(p.name) != 'ali'
        ''')
        global_stats = c.fetchone()
        
        m_global = global_stats[0] if (global_stats and global_stats[0] is not None) else 0.50
        m_mafia = global_stats[1] if (global_stats and global_stats[1] is not None) else 0.50
        m_citizen = global_stats[2] if (global_stats and global_stats[2] is not None) else 0.50

        c.execute('''
            SELECT 
                p.name,
                COUNT(m.id) as total_games,
                SUM(CASE WHEN m.is_win = 1 THEN 1 ELSE 0 END) as total_wins,
                SUM(CASE WHEN m.side = 'Mafia' THEN 1 ELSE 0 END) as mafia_games,
                SUM(CASE WHEN m.side = 'Mafia' AND m.is_win = 1 THEN 1 ELSE 0 END) as mafia_wins,
                SUM(CASE WHEN m.side = 'Citizen' THEN 1 ELSE 0 END) as citizen_games,
                SUM(CASE WHEN m.side = 'Citizen' AND m.is_win = 1 THEN 1 ELSE 0 END) as citizen_wins
            FROM players p
            JOIN matches m ON p.id = m.player_id
            WHERE LOWER(p.name) != 'ali'
            GROUP BY p.id
            HAVING total_games >= 18
        ''')
        rows = c.fetchall()
        conn.close()

    if not rows:
        await update.message.reply_text("هنوز بازیکنی به حد نصاب حداقل ۱۸ بازی نرسیده است.", reply_markup=get_main_keyboard())
        return

    C_GLOBAL = 10.0
    C_SIDE = 5.0

    processed_list = []
    mafia_candidates = []
    citizen_candidates = []

    for row in rows:
        name, total_g, total_w, m_games, m_wins, c_games, c_wins = row
        
        raw_win = (total_w * 100.0 / total_g)
        base_bayes = ((total_w + (C_GLOBAL * m_global)) / (total_g + C_GLOBAL)) * 100.0
        # اعمال پاداش محسوس برای پایداری در بازی‌های بیشتر
        vol_boost = 1.0 + (0.08 * math.log10((total_g / 18.0) + 1.0))
        bayes_score = base_bayes * vol_boost

        p_data = {
            'name': name,
            'total_games': total_g,
            'total_wins': total_w,
            'raw_win': raw_win,
            'bayes_score': bayes_score,
            'm_games': m_games,
            'm_wins': m_wins,
            'c_games': c_games,
            'c_wins': c_wins
        }
        processed_list.append(p_data)

        # ارزیابی ساید مافیا
        if m_games >= 9:
            base_m = ((m_wins + (C_SIDE * m_mafia)) / (m_games + C_SIDE)) * 100.0
            m_boost = 1.0 + (0.08 * math.log10((m_games / 9.0) + 1.0))
            m_bayes = base_m * m_boost
            m_rate = (m_wins * 100 // m_games)
            mafia_candidates.append({
                'name': name,
                'bayes': m_bayes,
                'rate': m_rate,
                'games': m_games,
                'wins': m_wins
            })

        # ارزیابی ساید شهروند
        if c_games >= 9:
            base_c = ((c_wins + (C_SIDE * m_citizen)) / (c_games + C_SIDE)) * 100.0
            c_boost = 1.0 + (0.08 * math.log10((c_games / 9.0) + 1.0))
            c_bayes = base_c * c_boost
            c_rate = (c_wins * 100 // c_games)
            citizen_candidates.append({
                'name': name,
                'bayes': c_bayes,
                'rate': c_rate,
                'games': c_games,
                'wins': c_wins
            })

    # مرتب‌سازی مطلق بر پایه نمره نهایی
    processed_list.sort(key=lambda x: (x['bayes_score'], x['total_games']), reverse=True)
    mafia_candidates.sort(key=lambda x: (x['bayes'], x['games']), reverse=True)
    citizen_candidates.sort(key=lambda x: (x['bayes'], x['games']), reverse=True)

    report = "📊 **رتبه‌بندی رسمی بازیکنان (فرمول بیزی با پاداش پایداری | حداقل ۱۸ بازی)**\n\n"
    for idx, p in enumerate(processed_list, 1):
        m_rate = (p['m_wins'] * 100 // p['m_games']) if p['m_games'] > 0 else 0
        c_rate = (p['c_wins'] * 100 // p['c_games']) if p['c_games'] > 0 else 0

        report += f"🎖 **رتبه {idx}. {p['name'].title()}**\n"
        report += f"⭐️ **امتیاز نهایی:** {p['bayes_score']:.2f} | 🎮 بازی‌ها: {p['total_games']}\n"
        report += f"🏆 درصد برد واقعی: {p['raw_win']:.1f}%\n"
        report += f"🔪 مافیا: {m_rate}% ({p['m_wins']}/{p['m_games']}) | 🛡 شهر: {c_rate}% ({p['c_wins']}/{p['c_games'])}\n"
        report += "─────────────────\n"

    report += "\n🔥 **۵ بازیکن برتر ساید مافیا (حداقل ۹ بازی):**\n"
    if mafia_candidates:
        for r, m in enumerate(mafia_candidates[:5], 1):
            report += f"{r}. {m['name'].title()} ⟵ امتیاز: {m['bayes']:.2f} (برد: {m['rate']}% | {m['wins']}/{m['games']})\n"
    else:
        report += "بازیکنی با حداقل ۹ بازی مافیا یافت نشد.\n"

    report += "\n🛡 **۵ بازیکن برتر ساید شهروند (حداقل ۹ بازی):**\n"
    if citizen_candidates:
        for r, c_item in enumerate(citizen_candidates[:5], 1):
            report += f"{r}. {c_item['name'].title()} ⟵ امتیاز: {c_item['bayes']:.2f} (برد: {c_item['rate']}% | {c_item['wins']}/{c_item['games']})\n"
    else:
        report += "بازیکنی با حداقل ۹ بازی شهروندی یافت نشد.\n"

    await send_large_text(update, report, context)

    pdf_path = generate_pdf_report(processed_list, mafia_candidates, citizen_candidates)
    try:
        with open(pdf_path, 'rb') as pdf_file:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=pdf_file,
                filename="CafeMafia_Bayesian_Leaderboard.pdf",
                caption="📄 نسخه PDF رتبه‌بندی بیزی رسمی (حداقل ۱۸ بازی | بدون ali)",
                reply_markup=get_main_keyboard()
            )
    except Exception as e:
        print(f"Error sending PDF: {e}")

# ================= سرچ اختصاصی با فرمول وزن‌دار =================
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
            SELECT AVG(is_win) FROM matches m
            JOIN players p ON p.id = m.player_id
            WHERE LOWER(p.name) != 'ali'
        ''')
        global_avg_row = c.fetchone()
        m_global = global_avg_row[0] if (global_avg_row and global_avg_row[0] is not None) else 0.50

        c.execute('''
            SELECT 
                p.id,
                p.name,
                COUNT(m.id) as total_games,
                SUM(CASE WHEN m.is_win = 1 THEN 1 ELSE 0 END) as total_wins,
                SUM(CASE WHEN m.side = 'Mafia' THEN 1 ELSE 0 END) as mafia_games,
                SUM(CASE WHEN m.side = 'Mafia' AND m.is_win = 1 THEN 1 ELSE 0 END) as mafia_wins,
                SUM(CASE WHEN m.side = 'Citizen' THEN 1 ELSE 0 END) as citizen_games,
                SUM(CASE WHEN m.side = 'Citizen' AND m.is_win = 1 THEN 1 ELSE 0 END) as citizen_wins
            FROM players p
            JOIN matches m ON p.id = m.player_id
            WHERE LOWER(p.name) != 'ali'
            GROUP BY p.id
        ''')
        all_players_raw = c.fetchall()
        conn.close()

    if not all_players_raw:
        await update.message.reply_text("دیتابیس خالی است.", reply_markup=get_main_keyboard())
        return ConversationHandler.END

    C_GLOBAL = 10.0
    all_players_calculated = []
    for row in all_players_raw:
        pid, name, tg, tw, mg, mw, cg, cw = row
        base_b = ((tw + (C_GLOBAL * m_global)) / (tg + C_GLOBAL)) * 100.0
        vol_boost = 1.0 + (0.08 * math.log10((tg / 18.0) + 1.0)) if tg >= 18 else 1.0
        b_score = base_b * vol_boost
        r_win = (tw * 100.0 / tg) if tg > 0 else 0
        all_players_calculated.append({
            'id': pid,
            'name': name,
            'total_games': tg,
            'total_wins': tw,
            'bayes_score': b_score,
            'raw_win': r_win,
            'm_games': mg,
            'm_wins': mw,
            'c_games': cg,
            'c_wins': cw
        })

    all_players_calculated.sort(key=lambda x: (x['bayes_score'], x['total_games']), reverse=True)

    matched_player = None
    rank = 0
    best_score = 0

    for idx, p in enumerate(all_players_calculated, 1):
        score = fuzz.ratio(query, p['name'])
        if query == p['name']:
            matched_player = p
            rank = idx
            break
        elif score > best_score and score >= 75:
            best_score = score
            matched_player = p
            rank = idx

    if not matched_player:
        await update.message.reply_text(f"❌ بازیکنی با نام «{query}» پیدا نشد.", reply_markup=get_main_keyboard())
        return ConversationHandler.END

    p = matched_player
    m_rate = (p['m_wins'] * 100 // p['m_games']) if p['m_games'] > 0 else 0
    c_rate = (p['c_wins'] * 100 // p['c_games']) if p['c_games'] > 0 else 0

    profile_text = (
        f"👤 **پروفایل تحلیلی بازیکن:** `{p['name'].title()}`\n"
        f"─────────────────────\n"
        f"🎖 **رتبه در کل لیگ:** #{rank} (از بین {len(all_players_calculated)} بازیکن)\n"
        f"⭐️ **امتیاز نهایی:** {p['bayes_score']:.2f}\n"
        f"🎮 **مجموع بازی‌ها:** {p['total_games']}\n"
        f"🏆 **درصد برد واقعی:** {p['raw_win']:.1f}%\n\n"
        f"🔪 **عملکرد ساید مافیا:**\n"
        f"   ▫️ بازی: {p['m_games']} | برد: {p['m_wins']} ({m_rate}%)\n\n"
        f"🛡 **عملکرد ساید شهروند:**\n"
        f"   ▫️ بازی: {p['c_games']} | برد: {p['c_wins']} ({c_rate}%)\n"
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
    print("ربات با فرمول بیزی وزن‌دار، پاداش پایداری و منوی کامل فعال شد...")
    
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