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

# اسامی که به طور کامل از سیستم حذف و فیلتر می‌شوند
EXCLUDED_PLAYERS = {'ali', 'sara', 'mohammad', 'mohamad'}

# نگاشت ادغام به نام omid
PLAYER_ALIASES = {
    'mohammad a': 'omid',
    'mohamad a': 'omid',
    'mohammad akbar': 'omid',
    'mohamad akbar': 'omid',
}

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

def make_bar(percent, length=8):
    filled = int(round(length * (percent / 100.0)))
    return "▰" * filled + "▱" * (length - filled)

# ================= دیتابیس و ادغام هوشمند =================
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

    # ۱. ادغام داده‌ها در حساب omid
    c.execute("INSERT OR IGNORE INTO players (name) VALUES ('omid')")
    c.execute("SELECT id FROM players WHERE LOWER(name) = 'omid'")
    omid_row = c.fetchone()
    
    if omid_row:
        omid_id = omid_row[0]
        aliases_to_merge = list(PLAYER_ALIASES.keys())
        alias_placeholders = ','.join(['?'] * len(aliases_to_merge))
        
        c.execute(f'''
            SELECT id FROM players 
            WHERE LOWER(name) IN ({alias_placeholders})
        ''', aliases_to_merge)
        alias_players = c.fetchall()
        
        for (a_id,) in alias_players:
            c.execute('''
                UPDATE OR IGNORE matches 
                SET player_id = ? 
                WHERE player_id = ?
            ''', (omid_id, a_id))
            
            c.execute("DELETE FROM matches WHERE player_id = ?", (a_id,))
            c.execute("DELETE FROM players WHERE id = ?", (a_id,))

    # ۲. حذف مطلق داده‌های اسامی فیلتر شده
    placeholders = ','.join(['?'] * len(EXCLUDED_PLAYERS))
    c.execute(f'''
        DELETE FROM matches 
        WHERE player_id IN (
            SELECT id FROM players WHERE LOWER(name) IN ({placeholders})
        )
    ''', list(EXCLUDED_PLAYERS))
    
    c.execute(f'''
        DELETE FROM players WHERE LOWER(name) IN ({placeholders})
    ''', list(EXCLUDED_PLAYERS))

    conn.commit()
    conn.close()

def get_or_create_player(cursor, raw_name):
    clean_name = raw_name.strip().lower()
    clean_name = re.sub(rf'[{SEAT_SYMBOLS}]', '', clean_name)
    clean_name = re.sub(r'[\.\-_:]', ' ', clean_name)
    clean_name = " ".join(clean_name.split())

    if not clean_name or len(clean_name) < 2 or clean_name.isdigit() or clean_name == 'god':
        return None, None

    if clean_name in PLAYER_ALIASES:
        clean_name = PLAYER_ALIASES[clean_name]

    if clean_name in EXCLUDED_PLAYERS:
        return None, None

    if not re.search(r'[a-zA-Z\u0600-\u06FF]', clean_name):
        return None, None

    cursor.execute("SELECT id, LOWER(name) FROM players")
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
    cursor.execute("SELECT id FROM players WHERE LOWER(name) = ?", (clean_name,))
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

# ================= استخراج اطلاعات =================
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

            name_lower = name.strip().lower()
            name_lower = re.sub(rf'[{SEAT_SYMBOLS}]', '', name_lower)
            name_lower = " ".join(name_lower.split())

            if name_lower in PLAYER_ALIASES:
                name_lower = PLAYER_ALIASES[name_lower]

            if name_lower in EXCLUDED_PLAYERS:
                continue

            side = detect_side(scenario, role)
            if side != "Independent":
                parsed_players.append((name_lower, role.lower(), side))

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

# ================= ساخت فایل PDF =================
def generate_pdf_report(results, mafia_leaders, citizen_leaders, filename="Mafia_Leaderboard.pdf"):
    doc = SimpleDocTemplate(
        filename,
        pagesize=letter,
        rightMargin=32,
        leftMargin=32,
        topMargin=32,
        bottomMargin=32
    )
    elements = []
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        'MainTitle',
        parent=styles['Heading1'],
        fontSize=22,
        leading=26,
        textColor=colors.HexColor('#0F172A'),
        alignment=1,
        spaceAfter=6
    )
    subtitle_style = ParagraphStyle(
        'SubTitle',
        parent=styles['Normal'],
        fontSize=10,
        textColor=colors.HexColor('#475569'),
        alignment=1,
        spaceAfter=18
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

    elements.append(Paragraph("👑 <b>CAFE MAFIA GRAND CHAMPIONSHIP</b> 👑", title_style))
    elements.append(Paragraph("Official Bayesian Rating System • Enhanced Volume Regularization (Min 18 Games)", subtitle_style))

    table_data = [["Rank", "Player", "Matches", "Bayesian Pts", "Win Rate", "Mafia (W/G)", "Citizen (W/G)"]]
    for idx, p in enumerate(results, 1):
        m_rate = (p['m_wins'] * 100 // p['m_games']) if p['m_games'] > 0 else 0
        c_rate = (p['c_wins'] * 100 // p['c_games']) if p['c_games'] > 0 else 0

        badge = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else f"#{idx}"

        table_data.append([
            badge,
            p['name'].title(),
            str(p['total_games']),
            f"{p['bayes_score']:.2f}",
            f"{p['raw_win']:.1f}%",
            f"{m_rate}% ({p['m_wins']}/{p['m_games']})",
            f"{c_rate}% ({p['c_wins']}/{p['c_games']})"
        ])

    main_table = Table(table_data, colWidths=[40, 125, 52, 78, 65, 95, 95])
    main_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0B132B')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#F8FAFC')),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('ALIGN', (1, 1), (1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 7),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor('#F8FAFC'), colors.HexColor('#EDF2F7')]),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#CBD5E1')),
        ('FONTSIZE', (0, 1), (-1, -1), 8.5),
        ('TOPPADDING', (0, 1), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 5),
    ]))
    elements.append(main_table)
    elements.append(Spacer(1, 15))

    elements.append(Paragraph("⚔️ <b>Elite Side Specialists (Minimum 9 Side Games)</b>", section_style))
    top_side_data = [["🔥 Top Mafia Syndicate", "🛡 Top Citizen Alliance"]]
    max_len = max(len(mafia_leaders[:5]), len(citizen_leaders[:5]))
    
    for i in range(max_len):
        m_txt = f"{i+1}. {mafia_leaders[i]['name'].title()} — <b>{mafia_leaders[i]['bayes']:.2f} Pts</b> ({mafia_leaders[i]['wins']}/{mafia_leaders[i]['games']} W)" if i < len(mafia_leaders[:5]) else ""
        c_txt = f"{i+1}. {citizen_leaders[i]['name'].title()} — <b>{citizen_leaders[i]['bayes']:.2f} Pts</b> ({citizen_leaders[i]['wins']}/{citizen_leaders[i]['games']} W)" if i < len(citizen_leaders[:5]) else ""
        top_side_data.append([Paragraph(m_txt, styles['Normal']), Paragraph(c_txt, styles['Normal'])])

    side_table = Table(top_side_data, colWidths=[275, 275])
    side_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, 0), colors.HexColor('#991B1B')),
        ('BACKGROUND', (1, 0), (1, 0), colors.HexColor('#1E40AF')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 9.5),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#CBD5E1')),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
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
        [KeyboardButton("🏆 تالار افتخارات و رتبه‌بندی بیزی (PDF)")],
        [KeyboardButton("🔍 جستجوی کارت بازیکن"), KeyboardButton("📜 راهنمای رتبه‌بندی")]
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
                f"⚡️ **بسته با موفقیت آنالیز شد!**\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"📥 کل پیام‌های دریافتی: `{len(messages)}`\n"
                f"✨ بازی‌های جدید تایید شده: `{added}`\n"
                f"🔁 بازی‌های تکراری رد شده: `{len(messages) - added}`\n"
                f"🏛 کل نبردهای ثبت‌شده دیتابیس: `{all_stored_games}`"
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

    norm_lower = raw_content.strip().lower()

    if norm_lower in ["🏆 تالار افتخارات و رتبه‌بندی بیزی (pdf)", "📊 مشاهده رتبه‌بندی بیزی و گزارش (pdf)"]:
        await report_command(update, context)
        return
    elif norm_lower in ["🔍 جستجوی کارت بازیکن", "🔍 جستجوی آمار بازیکن"]:
        await update.message.reply_text("🔎 **نام انگلیسی بازیکن را وارد کنید:**\n*(مثال: Omid, Hooman, Ebi)*")
        return SEARCH_STATE
    elif norm_lower in ["📜 راهنمای رتبه‌بندی", "❓ راهنما"]:
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

# ================= پیام استارت و خوش‌آمدگویی کامل =================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name if update.effective_user else "همراه گرامی"
    
    welcome_text = (
        f"👑 **درود {user_name} عزیز! به سامانه تحلیل و رتبه‌بندی کافه مافیا خوش آمدید.** 👑\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"این ربات یک دستیار هوشمند، عادلانه و پیشرفته برای ثبت، آنالیز و رتبه‌بندی دقیق آمار بازی‌های مافیا است.\n\n"
        f"🌟 **ویژگی‌ها و قابلیت‌های اصلی ربات:**\n\n"
        f"🔹 **الگوریتم بیزی با ضریب ثبات سنگین:**\n"
        f"برخلاف سیستم‌های سنتی که صرفاً درصد برد خام را می‌سنجند، سیستم ما تعداد کل بازی‌ها را ارزش‌گذاری می‌کند؛ بنابراین بازیکنی با ۱۰۰ یا ۲۰۰ بازی زیر سایه شانس مقطعی بازی‌های کم‌تعداد قرار نمی‌گیرد.\n\n"
        f"🔹 **ثبت خودکار و آنی ایونت‌ها:**\n"
        f"کافی است متن یا عکس نبردهای برگزارشده را به ربات فوروارد کنید تا مشخصات بازیکنان، نقش‌ها و ساید برنده ذخیره شوند.\n\n"
        f"🔹 **پروفایل و شناسنامه بازیکنان:**\n"
        f"با جستجوی نام هر بازیکن، کارنامه تفکیکی (تعداد بازی، برد، درصد پیروزی و رتبه در کل لیگ) همراه با نمودار نواری اختصاصی صادر می‌شود.\n\n"
        f"🔹 **گزارش رسمی و صدور PDF:**\n"
        f"در هر لحظه می‌توانید جدول رده‌بندی کل و تاپ ۵ هر ساید را در قالب فایل مستند PDF دریافت کنید.\n\n"
        f"⚖️ **قوانین و حد نصاب‌های رتبه‌بندی:**\n"
        f"▫️ حداقل **۱۸ بازی** برای ورود به تالار افتخارات کل.\n"
        f"▫️ حداقل **۹ بازی** در هر ساید برای رقابت در ۵ نفر برتر مافیا یا شهروند.\n\n"
        f"👇 **جهت شروع، از دکمه‌های زیر استفاده کنید:**"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown", reply_markup=get_main_keyboard())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📜 **ساختار رتبه‌بندی و فرمول بیزی با ضریب حجم:**\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚖️ **چرا تعداد بازی تعیین‌کننده است؟**\n"
        "حفظ درصد برد بالا در ۲۰۰ مسابقه ارزش آماری به مراتب بیشتری از ۲۰ مسابقه دارد. بنابراین علاوه بر نسبت برد بیزی، یک ضریب تصاعدی روی کل امتیاز اعمال می‌شود:\n"
        "`Score = Base_Bayes × [1 + 0.18 × log10(Matches / 18 + 1)]`\n\n"
        "🎖 **نشان‌های رتبه‌بندی:**\n"
        "👑 Grandmaster: رتبه ۱ تا ۳ جدول\n"
        "💎 Master: امتیاز بالای ۶۰ با حجم بازی سنگین\n"
        "💠 Diamond: بازیکنان باثبات بالا\n\n"
        "📌 **حداقل شرط ورود به جدول:** ۱۸ بازی کل و ۹ بازی در هر ساید."
    )
    await update.message.reply_text(help_text, parse_mode="Markdown", reply_markup=get_main_keyboard())

# ================= گزارش رسمی =================
async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with DB_LOCK:
        conn = sqlite3.connect('mafia_stats.db', timeout=60.0)
        c = conn.cursor()

        placeholders = ','.join(['?'] * len(EXCLUDED_PLAYERS))
        c.execute(f'''
            SELECT 
                AVG(is_win) as global_win_mean,
                AVG(CASE WHEN side = 'Mafia' THEN is_win END) as mafia_win_mean,
                AVG(CASE WHEN side = 'Citizen' THEN is_win END) as citizen_win_mean
            FROM matches m
            JOIN players p ON p.id = m.player_id
            WHERE LOWER(p.name) NOT IN ({placeholders})
        ''', list(EXCLUDED_PLAYERS))
        global_stats = c.fetchone()
        
        m_global = global_stats[0] if (global_stats and global_stats[0] is not None) else 0.50
        m_mafia = global_stats[1] if (global_stats and global_stats[1] is not None) else 0.50
        m_citizen = global_stats[2] if (global_stats and global_stats[2] is not None) else 0.50

        c.execute(f'''
            SELECT 
                LOWER(p.name),
                COUNT(m.id) as total_games,
                SUM(CASE WHEN m.is_win = 1 THEN 1 ELSE 0 END) as total_wins,
                SUM(CASE WHEN m.side = 'Mafia' THEN 1 ELSE 0 END) as mafia_games,
                SUM(CASE WHEN m.side = 'Mafia' AND m.is_win = 1 THEN 1 ELSE 0 END) as mafia_wins,
                SUM(CASE WHEN m.side = 'Citizen' THEN 1 ELSE 0 END) as citizen_games,
                SUM(CASE WHEN m.side = 'Citizen' AND m.is_win = 1 THEN 1 ELSE 0 END) as citizen_wins
            FROM players p
            JOIN matches m ON p.id = m.player_id
            WHERE LOWER(p.name) NOT IN ({placeholders})
            GROUP BY LOWER(p.name)
            HAVING total_games >= 18
        ''', list(EXCLUDED_PLAYERS))
        rows = c.fetchall()
        conn.close()

    if not rows:
        await update.message.reply_text("هنوز بازیکنی به حد نصاب حداقل ۱۸ بازی نرسیده است.", reply_markup=get_main_keyboard())
        return

    C_GLOBAL = 12.0
    C_SIDE = 6.0
    VOLUME_POWER = 0.18

    processed_list = []
    mafia_candidates = []
    citizen_candidates = []

    for row in rows:
        name, total_g, total_w, m_games, m_wins, c_games, c_wins = row
        
        raw_win = (total_w * 100.0 / total_g)
        base_bayes = ((total_w + (C_GLOBAL * m_global)) / (total_g + C_GLOBAL)) * 100.0
        vol_boost = 1.0 + (VOLUME_POWER * math.log10((total_g / 18.0) + 1.0))
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

        if m_games >= 9:
            base_m = ((m_wins + (C_SIDE * m_mafia)) / (m_games + C_SIDE)) * 100.0
            m_boost = 1.0 + (VOLUME_POWER * math.log10((m_games / 9.0) + 1.0))
            m_bayes = base_m * m_boost
            m_rate = (m_wins * 100 // m_games)
            mafia_candidates.append({
                'name': name,
                'bayes': m_bayes,
                'rate': m_rate,
                'games': m_games,
                'wins': m_wins
            })

        if c_games >= 9:
            base_c = ((c_wins + (C_SIDE * m_citizen)) / (c_games + C_SIDE)) * 100.0
            c_boost = 1.0 + (VOLUME_POWER * math.log10((c_games / 9.0) + 1.0))
            c_bayes = base_c * c_boost
            c_rate = (c_wins * 100 // c_games)
            citizen_candidates.append({
                'name': name,
                'bayes': c_bayes,
                'rate': c_rate,
                'games': c_games,
                'wins': c_wins
            })

    processed_list.sort(key=lambda x: (x['bayes_score'], x['total_games']), reverse=True)
    mafia_candidates.sort(key=lambda x: (x['bayes'], x['games']), reverse=True)
    citizen_candidates.sort(key=lambda x: (x['bayes'], x['games']), reverse=True)

    report = "👑 **جدول برترین‌های کافه مافیا (رتبه‌بندی بیزی با ضریب استقامت)** 👑\n"
    report += "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

    for idx, p in enumerate(processed_list, 1):
        m_rate = (p['m_wins'] * 100 // p['m_games']) if p['m_games'] > 0 else 0
        c_rate = (p['c_wins'] * 100 // p['c_games']) if p['c_games'] > 0 else 0
        bar = make_bar(p['raw_win'], length=8)

        if idx == 1:
            icon = "🥇 𝐆𝐑𝐀𝐍𝐃𝐌𝐀𝐒𝐓𝐄𝐑"
        elif idx == 2:
            icon = "🥈 𝐌𝐀𝐒𝐓𝐄𝐑"
        elif idx == 3:
            icon = "🥉 𝐃𝐈𝐀𝐌𝐎𝐍𝐃"
        elif idx <= 10:
            icon = f"⚜️ رتبه #{idx}"
        else:
            icon = f"🎖 رتبه #{idx}"

        report += f"{icon} • **{p['name'].title()}**\n"
        report += f"💎 **امتیاز عملکرد:** `{p['bayes_score']:.2f}` | 🎮 **نبردها:** `{p['total_games']}`\n"
        report += f"📊 وین‌ریت کل: {bar} `{p['raw_win']:.1f}%`\n"
        report += f"🔪 مافیا: `{m_rate}%` ({p['m_wins']}/{p['m_games']}) | 🛡 شهر: `{c_rate}%` ({p['c_wins']}/{p['c_games']})\n"
        report += "──────────────────────────\n"

    report += "\n🔥 **۵ شکارچی برتر ساید مافیا (حداقل ۹ بازی):**\n"
    if mafia_candidates:
        medals = ["👑", "🩸", "💀", "🗡", "🎯"]
        for r, m in enumerate(mafia_candidates[:5], 1):
            report += f"{medals[r-1]} {r}. **{m['name'].title()}** ⟵ نمره: `{m['bayes']:.2f}` (برد: `{m['rate']}%` در `{m['games']}` بازی)\n"
    else:
        report += "بازیکنی با حداقل ۹ بازی مافیا یافت نشد.\n"

    report += "\n🛡 **۵ قهرمان برتر ساید شهروند (حداقل ۹ بازی):**\n"
    if citizen_candidates:
        shields = ["🌟", "💎", "✨", "🛡", "⚜️"]
        for r, c_item in enumerate(citizen_candidates[:5], 1):
            report += f"{shields[r-1]} {r}. **{c_item['name'].title()}** ⟵ نمره: `{c_item['bayes']:.2f}` (برد: `{c_item['rate']}%` در `{c_item['games']}` بازی)\n"
    else:
        report += "بازیکنی با حداقل ۹ بازی شهروندی یافت نشد.\n"

    await send_large_text(update, report, context)

    pdf_path = generate_pdf_report(processed_list, mafia_candidates, citizen_candidates)
    try:
        with open(pdf_path, 'rb') as pdf_file:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=pdf_file,
                filename="CafeMafia_Official_Leaderboard.pdf",
                caption="📜 **نسخه رسمی و تفکیکی تالار افتخارات (PDF مستند)**",
                reply_markup=get_main_keyboard()
            )
    except Exception as e:
        print(f"Error sending PDF: {e}")

# ================= سرچ اختصاصی بازیکن =================
async def search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔎 **نام بازیکن مورد نظر را وارد کنید:**")
    return SEARCH_STATE

async def search_perform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip().lower()
    
    if query in PLAYER_ALIASES:
        query = PLAYER_ALIASES[query]

    if query in EXCLUDED_PLAYERS:
        await update.message.reply_text(f"❌ بازیکنی با نام «{query}» پیدا نشد.", reply_markup=get_main_keyboard())
        return ConversationHandler.END

    async with DB_LOCK:
        conn = sqlite3.connect('mafia_stats.db', timeout=60.0)
        c = conn.cursor()

        placeholders = ','.join(['?'] * len(EXCLUDED_PLAYERS))
        c.execute(f'''
            SELECT AVG(is_win) FROM matches m
            JOIN players p ON p.id = m.player_id
            WHERE LOWER(p.name) NOT IN ({placeholders})
        ''', list(EXCLUDED_PLAYERS))
        global_avg_row = c.fetchone()
        m_global = global_avg_row[0] if (global_avg_row and global_avg_row[0] is not None) else 0.50

        c.execute(f'''
            SELECT 
                p.id,
                LOWER(p.name),
                COUNT(m.id) as total_games,
                SUM(CASE WHEN m.is_win = 1 THEN 1 ELSE 0 END) as total_wins,
                SUM(CASE WHEN m.side = 'Mafia' THEN 1 ELSE 0 END) as mafia_games,
                SUM(CASE WHEN m.side = 'Mafia' AND m.is_win = 1 THEN 1 ELSE 0 END) as mafia_wins,
                SUM(CASE WHEN m.side = 'Citizen' THEN 1 ELSE 0 END) as citizen_games,
                SUM(CASE WHEN m.side = 'Citizen' AND m.is_win = 1 THEN 1 ELSE 0 END) as citizen_wins
            FROM players p
            JOIN matches m ON p.id = m.player_id
            WHERE LOWER(p.name) NOT IN ({placeholders})
            GROUP BY LOWER(p.name)
        ''', list(EXCLUDED_PLAYERS))
        all_players_raw = c.fetchall()
        conn.close()

    if not all_players_raw:
        await update.message.reply_text("دیتابیس خالی است.", reply_markup=get_main_keyboard())
        return ConversationHandler.END

    C_GLOBAL = 12.0
    VOLUME_POWER = 0.18

    all_players_calculated = []
    for row in all_players_raw:
        pid, name, tg, tw, mg, mw, cg, cw = row
        base_b = ((tw + (C_GLOBAL * m_global)) / (tg + C_GLOBAL)) * 100.0
        vol_boost = 1.0 + (VOLUME_POWER * math.log10((tg / 18.0) + 1.0)) if tg >= 18 else 1.0
        b_score = base_b * vol_boost
        r_win = (tw * 100.0 / tg) if tg > 0 else 0
        all_players_calculated.append({
            'id': pid,
            'name': name.lower(),
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
        await update.message.reply_text(f"❌ بازیکنی با نام «{query}» در تالار افتخارات پیدا نشد.", reply_markup=get_main_keyboard())
        return ConversationHandler.END

    p = matched_player
    m_rate = (p['m_wins'] * 100 // p['m_games']) if p['m_games'] > 0 else 0
    c_rate = (p['c_wins'] * 100 // p['c_games']) if p['c_games'] > 0 else 0
    bar_m = make_bar(m_rate, length=6)
    bar_c = make_bar(c_rate, length=6)

    profile_text = (
        f"🎖 **کارت شناسنامه آماری بازیکن** 🎖\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 **نام:** `{p['name'].title()}`\n"
        f"👑 **جایگاه در لیگ:** `#{rank}` (از میان {len(all_players_calculated)} بازیکن)\n"
        f"⭐️ **امتیاز نهایی:** `{p['bayes_score']:.2f}`\n"
        f"⚔️ **تعداد کل نبردها:** `{p['total_games']}` بازی\n"
        f"🏆 **وین‌ریت قطعی:** `{p['raw_win']:.1f}%` ({p['total_wins']} برد)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔪 **ساید مافیا:**\n"
        f"   ▫️ بازی: `{p['m_games']}` | برد: `{p['m_wins']}`\n"
        f"   ▫️ نرخ برد: {bar_m} `{m_rate}%`\n\n"
        f"🛡 **ساید شهروند:**\n"
        f"   ▫️ بازی: `{p['c_games']}` | برد: `{p['c_wins']}`\n"
        f"   ▫️ نرخ برد: {bar_c} `{c_rate}%`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
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
    print("ربات فعال شد...")
    
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
            MessageHandler(filters.Regex("^(🔍 جستجوی کارت بازیکن|🔍 جستجوی آمار بازیکن)$"), search_start),
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