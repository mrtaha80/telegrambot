import os
import re
import math
import hashlib
import sqlite3
import asyncio
import logging
import unicodedata
import io
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

# پردازش تصویر و OCR
from PIL import Image, ImageEnhance, ImageFilter
try:
    import pytesseract
    default_tess_path = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
    if os.path.exists(default_tess_path):
        pytesseract.pytesseract.tesseract_cmd = default_tess_path
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

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

# مراحل مکالمه
SEARCH_STATE = 1
LINK_PROFILE_STATE = 2
ADD_CHANNEL_STATE = 3

SEAT_SYMBOLS = "➊➋➌➍➎➏➐➑➒➓❶❷❸❹❺❻❼❽❾❿⓫⓬⓭⓮⓯"

# فقط ali و sara نادیده گرفته می‌شوند
EXCLUDED_PLAYERS = {'ali', 'sara'}

PLAYER_ALIASES = {
    'mohammad a': 'omid',
    'mohamad a': 'omid',
    'mohammad akbar': 'omid',
    'mohamad akbar': 'omid',
    'mohamad akbarnasab': 'omid',
    'alireza': 'alireza kamali',
    'alireza k': 'alireza kamali',
    'hossein': 'hossein ss',
    'hosein': 'hossein ss',
    'hosein ss': 'hossein ss',
    'mmd': 'mmd4030',
    'mmd 4030': 'mmd4030',
    'mmd-4030': 'mmd4030',
    'mmd_4030': 'mmd4030',
    'mohamad': 'mmd4030',
    'mohammad': 'mmd4030',
    'mohammad 4030': 'mmd4030',
    'mohamad 4030': 'mmd4030',
    'mohammad4030': 'mmd4030',
    'mohamad4030': 'mmd4030',
}

def resolve_player_name(raw_name):
    name = raw_name.strip().lower()
    name = re.sub(rf'[{SEAT_SYMBOLS}]', '', name)
    name = re.sub(r'[\.\-_:]', ' ', name)
    name = " ".join(name.split())

    if not name:
        return ""

    if name in PLAYER_ALIASES:
        return PLAYER_ALIASES[name]

    if re.search(r'^(mmd|moham+ad)(\s*4030)?$', name):
        return 'mmd4030'

    if fuzz.ratio(name, 'mmd4030') >= 80 or fuzz.ratio(name, 'mmd 4030') >= 80:
        return 'mmd4030'

    return name

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

def extract_roles_from_image(image_bytes):
    if not OCR_AVAILABLE:
        return {}
    try:
        image = Image.open(io.BytesIO(image_bytes)).convert('L')
        enhancer = ImageEnhance.Contrast(image)
        image = enhancer.enhance(2.0).filter(ImageFilter.SHARPEN)

        try:
            text = pytesseract.image_to_string(image, lang='fas+eng')
        except Exception:
            text = pytesseract.image_to_string(image)

        text = normalize_text(text)
        roles_by_seat = {}

        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            seat_match = re.search(r'(?:^|[^\d])([1-9]|10)[\s\.\:\-\/•]*(.+)$', line)
            if seat_match:
                seat_num = int(seat_match.group(1))
                role_candidate = re.sub(r'[\(\)\[\]👈👉]', '', seat_match.group(2)).strip()
                if len(role_candidate) >= 2:
                    roles_by_seat[seat_num] = role_candidate

        return roles_by_seat
    except Exception as e:
        print(f"Error during OCR extraction: {e}")
        return {}

# ================= دیتابیس =================
def init_db():
    conn = sqlite3.connect('mafia_stats.db', timeout=60.0)
    c = conn.cursor()
    c.execute('PRAGMA journal_mode=WAL;')

    c.execute('''
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE COLLATE NOCASE
        )
    ''')

    c.execute("INSERT OR IGNORE INTO channels (id, name) VALUES (1, 'cafe mafia')")
    c.execute("SELECT id FROM channels WHERE LOWER(name) = 'cafe mafia'")
    cafe_mafia_id = c.fetchone()[0]

    c.execute('''
        CREATE TABLE IF NOT EXISTS user_active_channel (
            telegram_user_id INTEGER PRIMARY KEY,
            channel_id INTEGER,
            FOREIGN KEY(channel_id) REFERENCES channels(id)
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS user_linked_players (
            telegram_user_id INTEGER PRIMARY KEY,
            player_name TEXT COLLATE NOCASE
        )
    ''')

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
            channel_id INTEGER DEFAULT 1,
            game_signature TEXT,
            event_id TEXT,
            scenario TEXT,
            side TEXT,
            is_win INTEGER,
            UNIQUE(player_id, game_signature),
            FOREIGN KEY(player_id) REFERENCES players(id)
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS processed_games (
            game_signature TEXT,
            channel_id INTEGER,
            PRIMARY KEY(game_signature, channel_id)
        )
    ''')

    target_names = {'omid', 'alireza kamali', 'hossein ss', 'mmd4030'}
    for target in target_names:
        c.execute("INSERT OR IGNORE INTO players (name) VALUES (?)", (target,))

    conn.commit()
    conn.close()

def get_user_channel(c, user_id):
    c.execute("SELECT channel_id FROM user_active_channel WHERE telegram_user_id = ?", (user_id,))
    row = c.fetchone()
    if row:
        c.execute("SELECT id, name FROM channels WHERE id = ?", (row[0],))
        ch = c.fetchone()
        if ch:
            return ch[0], ch[1]

    c.execute("SELECT id, name FROM channels WHERE LOWER(name) = 'cafe mafia'")
    default_ch = c.fetchone()
    if not default_ch:
        c.execute("SELECT id, name FROM channels ORDER BY id ASC LIMIT 1")
        default_ch = c.fetchone()
    return default_ch[0], default_ch[1]

def get_or_create_player(cursor, raw_name):
    clean_name = resolve_player_name(raw_name)

    if not clean_name or len(clean_name) < 2 or clean_name.isdigit() or clean_name == 'god':
        return None, None

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

# ================= ثبت داده بازی =================
def process_game_data(raw_text, image_bytes=None, fallback_id="0", channel_id=1):
    try:
        norm = normalize_text(raw_text)

        event_match = re.search(r'(?:event|ایونت)\s*[:#•\-_ ]*([0-9]+)', norm, re.IGNORECASE)
        scenario_match = re.search(r'(?:scenario|سناریو)\s*[:•\-_ ]*([^\n\r]+)', norm, re.IGNORECASE)
        win_match = re.search(r'(?:winner|win|برنده|برد)\s*[:•\-_ ]*([^\n\r]+)', norm, re.IGNORECASE)

        if not scenario_match or not win_match:
            return False, "عدم یافتن سناریو یا برنده در پیام"

        event_id = event_match.group(1).strip() if event_match else str(fallback_id)
        scenario = scenario_match.group(1).strip()
        win_text = win_match.group(1).strip().lower()

        winning_side = None
        if any(w in win_text for w in ['مافیا', 'mafia']):
            winning_side = "Mafia"
        elif any(w in win_text for w in ['شهر', 'citizen', 'کی اس', 'ks']):
            winning_side = "Citizen"

        if not winning_side:
            return False, "ساید برنده (مافیا یا شهروند) مشخص نیست"

        players_match = re.search(r'(?:players|بازیکنان|پلیرها)([\s\S]*?)(?:winner|win|🏆|$)', norm, re.IGNORECASE)
        if not players_match:
            return False, "لیست بازیکنان پیدا نشد"

        players_block = players_match.group(1)
        temp_players = []
        seat_counter = 1
        needs_image_ocr = False

        for line in players_block.strip().splitlines():
            line = line.strip()
            if not line or any(sym in line for sym in ['━', '┄', '─', '🥀', '🎭', '🕯']):
                continue

            seat_find = re.search(r'^[✦\s\/\•\:\.\-]*([0-9]+|[➊-➓]|[❶-⓫])', line)
            current_seat = seat_counter
            if seat_find:
                seat_raw = seat_find.group(1)
                if seat_raw in SEAT_SYMBOLS:
                    current_seat = SEAT_SYMBOLS.index(seat_raw) % 10 + 1
                elif seat_raw.isdigit():
                    current_seat = int(seat_raw)

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
                    role = ""

            if not name or len(name) < 2 or not re.search(r'[a-zA-Z\u0600-\u06FF]', name):
                continue

            name_lower = resolve_player_name(name)
            if name_lower in EXCLUDED_PLAYERS:
                seat_counter += 1
                continue

            if not role:
                needs_image_ocr = True

            temp_players.append({
                'seat': current_seat,
                'name': name_lower,
                'role': role
            })
            seat_counter += 1

        if len(temp_players) < 5:
            return False, f"تعداد بازیکنان شناسایی شده کمتر از ۵ نفر بود ({len(temp_players)} نفر)"

        roles_from_image = {}
        ocr_used = False
        if needs_image_ocr and image_bytes:
            roles_from_image = extract_roles_from_image(image_bytes)
            if roles_from_image:
                ocr_used = True

        parsed_players = []
        for p in temp_players:
            final_role = p['role']
            if not final_role:
                if p['seat'] in roles_from_image:
                    final_role = roles_from_image[p['seat']]
                else:
                    final_role = "ساده"

            side = detect_side(scenario, final_role)
            if side != "Independent":
                parsed_players.append((p['name'], final_role.lower(), side))

        if len(parsed_players) < 5:
            return False, "تعداد بازیکنان معتبر غیرمستقل کمتر از ۵ نفر بود"

        game_signature = f"ev_{event_id}_sc_{scenario.lower()[:5]}"

        conn = sqlite3.connect('mafia_stats.db', timeout=60.0)
        c = conn.cursor()

        for name, role, side in parsed_players:
            player_id, _ = get_or_create_player(c, name)
            if not player_id:
                continue

            is_win = 1 if side == winning_side else 0
            c.execute('''
                INSERT OR IGNORE INTO matches (player_id, channel_id, game_signature, event_id, scenario, side, is_win)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (player_id, channel_id, game_signature, event_id, scenario, side, is_win))

        c.execute("INSERT OR REPLACE INTO processed_games (game_signature, channel_id) VALUES (?, ?)", (game_signature, channel_id))
        conn.commit()
        conn.close()

        detail_info = {
            'event_id': event_id,
            'scenario': scenario,
            'winning_side': winning_side,
            'players_count': len(parsed_players),
            'ocr_used': ocr_used
        }
        return True, detail_info

    except Exception as e:
        print(f"Error parsing event: {e}")
        return False, str(e)

# ================= ساخت فایل PDF =================
def generate_pdf_report(results, mafia_leaders, citizen_leaders, channel_name="cafe mafia", filename="Mafia_Leaderboard.pdf"):
    doc = SimpleDocTemplate(filename, pagesize=letter, rightMargin=32, leftMargin=32, topMargin=32, bottomMargin=32)
    elements = []
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle('MainTitle', parent=styles['Heading1'], fontSize=20, leading=24, textColor=colors.HexColor('#0F172A'), alignment=1, spaceAfter=4)
    subtitle_style = ParagraphStyle('SubTitle', parent=styles['Normal'], fontSize=10, textColor=colors.HexColor('#475569'), alignment=1, spaceAfter=16)
    section_style = ParagraphStyle('SectionHeading', parent=styles['Heading2'], fontSize=12, leading=15, textColor=colors.HexColor('#0F172A'), spaceBefore=12, spaceAfter=8)

    elements.append(Paragraph(f"👑 <b>CAFE MAFIA GRAND CHAMPIONSHIP</b> 👑", title_style))
    elements.append(Paragraph(f"League / Channel: <b>{channel_name.upper()}</b> • Bayesian Volume Regularization", subtitle_style))

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
    elements.append(Spacer(1, 14))

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
        [KeyboardButton("👤 کارنامه من"), KeyboardButton("🔍 جستجوی کارت بازیکن")],
        [KeyboardButton("📢 انتخاب / تغییر کانال"), KeyboardButton("🔗 اتصال نام بازی من")],
        [KeyboardButton("📜 راهنمای رتبه‌بندی")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ================= هندلرهای تلگرام =================
async def flush_batch(chat_id, context: ContextTypes.DEFAULT_TYPE):
    global TOTAL_PROCESSED_COUNT
    await asyncio.sleep(2.5)

    batch_data = BATCH_STORAGE.pop(chat_id, [])
    BATCH_TASKS.pop(chat_id, None)

    if not batch_data:
        return

    conn = sqlite3.connect('mafia_stats.db', timeout=60.0)
    c = conn.cursor()
    ch_id, ch_name = get_user_channel(c, chat_id)
    conn.close()

    added = 0
    accepted_details = []
    rejected_reasons = []

    async with DB_LOCK:
        for text, img_bytes, msg_id in batch_data:
            ok, res = process_game_data(text, img_bytes, msg_id, channel_id=ch_id)
            if ok:
                added += 1
                accepted_details.append(res)
            else:
                rejected_reasons.append(str(res))

    TOTAL_PROCESSED_COUNT += added

    conn = sqlite3.connect('mafia_stats.db', timeout=60.0)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM processed_games WHERE (channel_id = ? OR channel_id IS NULL)", (ch_id,))
    all_stored_games = c.fetchone()[0]
    conn.close()

    summary_text = (
        f"⚡️ **نتیجه بررسی و ثبت بسته ارسالی**\n"
        f"📍 کانال فعال: `{ch_name}`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📥 کل فایل‌ها و پیام‌ها: `{len(batch_data)}`\n"
        f"✨ بازی‌های تایید شده: `{added}`\n"
        f"🔁 رد شده‌ها: `{len(batch_data) - added}`\n"
        f"🏛 کل بازی‌های ثبت‌شده در این کانال: `{all_stored_games}`\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
    )

    if accepted_details:
        summary_text += "📋 **جزئیات بازی‌های ثبت‌شده:**\n"
        for idx, g in enumerate(accepted_details, 1):
            ocr_status = "📷 نقش‌ها با OCR تصویر" if g['ocr_used'] else "📝 نقش‌ها از متن"
            winner_icon = "🔪 مافیا" if g['winning_side'] == "Mafia" else "🛡 شهروند"
            summary_text += f"{idx}. ایونت `{g['event_id']}` | سناریو: `{g['scenario']}`\n   ↳ برنده: {winner_icon} | پلیرها: `{g['players_count']}` نفر | منبع: {ocr_status}\n"

    if rejected_reasons:
        summary_text += "\n⚠️ **علت رد شدن سایر موارد:**\n"
        for idx, reason in enumerate(rejected_reasons, 1):
            summary_text += f"• مورد {idx}: {reason}\n"

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=summary_text,
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
    except Exception as e:
        print(f"Error sending batch summary: {e}")

async def search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔎 **نام انگلیسی بازیکن را وارد کنید:**\n*(مثال: Omid, Alireza Kamali, Hossein SS, Mmd4030)*")
    return SEARCH_STATE

async def link_profile_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔗 **اتصال نام بازیکن در بازی:**\n"
        "نام انگلیسی خود را که در بازی‌ها ثبت می‌شود وارد کنید:\n"
        "*(مثال: Omid, Alireza Kamali, Hossein SS, Mmd4030)*"
    )
    return LINK_PROFILE_STATE

async def link_profile_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    player_name = resolve_player_name(update.message.text)
    user_id = update.effective_user.id

    if player_name in EXCLUDED_PLAYERS:
        await update.message.reply_text("❌ این نام در لیست سیاه آماری قرار دارد.", reply_markup=get_main_keyboard())
        return ConversationHandler.END

    conn = sqlite3.connect('mafia_stats.db', timeout=60.0)
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO user_linked_players (telegram_user_id, player_name)
        VALUES (?, ?)
    ''', (user_id, player_name))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ نام بازی شما با موفقیت روی **{player_name.title()}** ذخیره شد!\n"
        f"از این پس با زدن دکمه **«👤 کارنامه من»** آمار و رتبه خود را مشاهده خواهید کرد.",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )
    return ConversationHandler.END

async def channel_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = sqlite3.connect('mafia_stats.db', timeout=60.0)
    c = conn.cursor()
    c.execute("SELECT id, name FROM channels ORDER BY id ASC")
    all_channels = c.fetchall()
    active_id, active_name = get_user_channel(c, update.effective_user.id)
    conn.close()

    msg = f"📢 **مدیریت و تعیین کانال / لیگ فعال:**\n"
    msg += f"🔹 کانال فعال فعلی شما: **{active_name}**\n\n"
    msg += "لیست کانال‌های موجود:\n"
    for idx, (cid, cname) in enumerate(all_channels, 1):
        mark = " 👈 (انتخاب‌شده)" if cid == active_id else ""
        msg += f"{idx}. `{cname}`{mark}\n"

    msg += "\nبرای انتخاب کانال موجود یا ساخت کانال جدید، نام کانال را بفرستید:"

    await update.message.reply_text(msg, parse_mode="Markdown")
    return ADD_CHANNEL_STATE

async def channel_save_or_switch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_ch_name = update.message.text.strip().lower()
    user_id = update.effective_user.id

    if len(new_ch_name) < 2:
        await update.message.reply_text("❌ نام کانال نامعتبر است.", reply_markup=get_main_keyboard())
        return ConversationHandler.END

    conn = sqlite3.connect('mafia_stats.db', timeout=60.0)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO channels (name) VALUES (?)", (new_ch_name,))
    c.execute("SELECT id FROM channels WHERE LOWER(name) = ?", (new_ch_name,))
    ch_id = c.fetchone()[0]

    c.execute('''
        INSERT OR REPLACE INTO user_active_channel (telegram_user_id, channel_id)
        VALUES (?, ?)
    ''', (user_id, ch_id))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ کانال فعال شما به **{new_ch_name}** تغییر یافت.\n"
        f"تمام بازی‌های جدید و گزارش‌های این کانال در این بخش اعمال می‌شوند.",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )
    return ConversationHandler.END

async def my_profile_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    conn = sqlite3.connect('mafia_stats.db', timeout=60.0)
    c = conn.cursor()
    c.execute("SELECT player_name FROM user_linked_players WHERE telegram_user_id = ?", (user_id,))
    row = c.fetchone()
    ch_id, ch_name = get_user_channel(c, user_id)
    conn.close()

    if not row:
        await update.message.reply_text(
            "⚠️ هنوز نام بازی خود را متصل نکرده‌اید!\n"
            "لطفاً ابتدا روی دکمه **«🔗 اتصال نام بازی من»** بزنید و اسم درون بازی خود را ثبت کنید.",
            reply_markup=get_main_keyboard()
        )
        return

    player_name = row[0]
    await show_player_card(update, player_name, ch_id, ch_name)

async def handle_incoming_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post or update.message
    if not msg:
        return

    raw_content = msg.text or msg.caption or ""
    norm_lower = raw_content.strip().lower()

    if norm_lower in ["🏆 تالار افتخارات و رتبه‌بندی بیزی (pdf)", "📊 مشاهده رتبه‌بندی بیزی و گزارش (pdf)"]:
        await report_command(update, context)
        return
    elif norm_lower == "👤 کارنامه من":
        await my_profile_handler(update, context)
        return
    elif norm_lower in ["📜 راهنمای رتبه‌بندی", "❓ راهنما"]:
        await help_command(update, context)
        return

    image_bytes = None
    if msg.photo:
        try:
            photo_file = await msg.photo[-1].get_file()
            f_io = io.BytesIO()
            await photo_file.download_to_memory(out=f_io)
            image_bytes = f_io.getvalue()
        except Exception as e:
            print(f"Error downloading photo: {e}")

    norm_content = normalize_text(raw_content).lower()

    if (any(k in norm_content for k in ['player', 'بازیکن', 'سیت', 'ساده', 'مافیا']) and 
        any(w in norm_content for w in ['win', 'برد', 'شهروند', 'مافیا'])) or image_bytes:

        chat_id = msg.chat_id
        if chat_id not in BATCH_STORAGE:
            BATCH_STORAGE[chat_id] = []

        BATCH_STORAGE[chat_id].append((raw_content, image_bytes, msg.message_id))

        if chat_id in BATCH_TASKS:
            BATCH_TASKS[chat_id].cancel()

        BATCH_TASKS[chat_id] = asyncio.create_task(flush_batch(chat_id, context))

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name if update.effective_user else "همراه گرامی"
    user_id = update.effective_user.id

    conn = sqlite3.connect('mafia_stats.db', timeout=60.0)
    c = conn.cursor()
    ch_id, ch_name = get_user_channel(c, user_id)
    conn.close()

    welcome_text = (
        f"👑 **درود {user_name} عزیز! به سامانه تحلیل و رتبه‌بندی کافه مافیا خوش آمدید.** 👑\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📍 کانال فعال شما: **{ch_name}**\n\n"
        f"🌟 **ویژگی‌های سامانه:**\n\n"
        f"🔹 **پشتیبانی از تفکیک کانال‌ها:**\n"
        f"داده‌های دیتابیس در کانال **cafe mafia** ثبت هستند و می‌توانید کانال جدید ایجاد یا انتخاب کنید.\n\n"
        f"🔹 **پروفایل شخصی خودکار:**\n"
        f"با زدن «🔗 اتصال نام بازی من»، نام خود را متصل کنید تا با زدن «👤 کارنامه من» آمار اختصاصی‌تان را ببینید.\n\n"
        f"🔹 **الگوریتم بیزی با ضریب ثبات سنگین:**\n"
        f"ثبات در تعداد بازی‌های بالا ارزش‌گذاری می‌شود.\n\n"
        f"🔹 **موتور OCR تطبیق تصویر پویا:**\n"
        f"استخراج هوشمند نقش‌های فارسی و متن و تطبیق از تصویر.\n\n"
        f"⚖️ **حد نصاب:** حداقل ۱۸ بازی کل | حداقل ۹ بازی در هر ساید.\n\n"
        f"👇 *جهت شروع، از دکمه‌های زیر استفاده کنید:* "
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown", reply_markup=get_main_keyboard())

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📜 **راهنمای جامع سامانه:**\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "▫️ **تغییر کانال:** قبل از ارسال بازی، با زدن «📢 انتخاب / تغییر کانال» مشخص کنید داده‌ها متعلق به کدام کانال است.\n"
        "▫️ **کارنامه شخصی:** با زدن «🔗 اتصال نام بازی من» اسمتان را متصل کنید تا با «👤 کارنامه من» آمار خود را ببینید.\n"
        "▫️ **ارسال بازی:** متن و عکس ایونت را ارسال کنید تا در کانال فعال ثبت شود."
    )
    await update.message.reply_text(help_text, parse_mode="Markdown", reply_markup=get_main_keyboard())

# ================= گزارش رسمی و لیدربرد =================
async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with DB_LOCK:
        conn = sqlite3.connect('mafia_stats.db', timeout=60.0)
        c = conn.cursor()
        ch_id, ch_name = get_user_channel(c, user_id)

        placeholders = ','.join(['?'] * len(EXCLUDED_PLAYERS))
        
        c.execute(f'''
            SELECT 
                AVG(is_win) as global_win_mean,
                AVG(CASE WHEN side = 'Mafia' THEN is_win END) as mafia_win_mean,
                AVG(CASE WHEN side = 'Citizen' THEN is_win END) as citizen_win_mean
            FROM matches m
            JOIN players p ON p.id = m.player_id
            WHERE LOWER(p.name) NOT IN ({placeholders}) 
              AND (m.channel_id = ? OR (? = 1 AND m.channel_id IS NULL))
        ''', list(EXCLUDED_PLAYERS) + [ch_id, ch_id])
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
              AND (m.channel_id = ? OR (? = 1 AND m.channel_id IS NULL))
            GROUP BY LOWER(p.name)
            HAVING total_games >= 18
        ''', list(EXCLUDED_PLAYERS) + [ch_id, ch_id])
        rows = c.fetchall()
        conn.close()

    if not rows:
        await update.message.reply_text(
            f"هنوز در کانال **{ch_name}** بازیکنی به حد نصاب حداقل ۱۸ بازی نرسیده است.\n"
            f"برای مشاهده داده‌های کانال اصلی، کانال فعال را روی **cafe mafia** قرار دهید.",
            parse_mode="Markdown",
            reply_markup=get_main_keyboard()
        )
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

    report = f"👑 **جدول برترین‌های لیگ: {ch_name.upper()}** 👑\n"
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

    report += "\n🔥 **۵ شکارچی برتر ساید مافیا:**\n"
    if mafia_candidates:
        medals = ["👑", "🩸", "💀", "🗡", "🎯"]
        for r, m in enumerate(mafia_candidates[:5], 1):
            report += f"{medals[r-1]} {r}. **{m['name'].title()}** ⟵ نمره: `{m['bayes']:.2f}` (برد: `{m['rate']}%` در `{m['games']}` بازی)\n"
    else:
        report += "بازیکنی با حداقل ۹ بازی مافیا یافت نشد.\n"

    report += "\n🛡 **۵ قهرمان برتر ساید شهروند:**\n"
    if citizen_candidates:
        shields = ["🌟", "💎", "✨", "🛡", "⚜️"]
        for r, c_item in enumerate(citizen_candidates[:5], 1):
            report += f"{shields[r-1]} {r}. **{c_item['name'].title()}** ⟵ نمره: `{c_item['bayes']:.2f}` (برد: `{c_item['rate']}%` در `{c_item['games']}` بازی)\n"
    else:
        report += "بازیکنی با حداقل ۹ بازی شهروندی یافت نشد.\n"

    await send_large_text(update, report, context)

    pdf_path = generate_pdf_report(processed_list, mafia_candidates, citizen_candidates, channel_name=ch_name)
    try:
        with open(pdf_path, 'rb') as pdf_file:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=pdf_file,
                filename=f"CafeMafia_{ch_name}_Leaderboard.pdf",
                caption=f"📜 **نسخه رسمی تالار افتخارات ({ch_name})**",
                reply_markup=get_main_keyboard()
            )
    except Exception as e:
        print(f"Error sending PDF: {e}")

# ================= نمایش کارت اختصاصی بازیکن =================
async def show_player_card(update: Update, query_name: str, ch_id: int, ch_name: str):
    query = resolve_player_name(query_name)

    if query in EXCLUDED_PLAYERS:
        await update.message.reply_text(f"❌ بازیکنی با نام «{query}» در لیست سیاه آماری قرار دارد.", reply_markup=get_main_keyboard())
        return

    async with DB_LOCK:
        conn = sqlite3.connect('mafia_stats.db', timeout=60.0)
        c = conn.cursor()

        placeholders = ','.join(['?'] * len(EXCLUDED_PLAYERS))
        c.execute(f'''
            SELECT AVG(is_win) FROM matches m
            JOIN players p ON p.id = m.player_id
            WHERE LOWER(p.name) NOT IN ({placeholders}) 
              AND (m.channel_id = ? OR (? = 1 AND m.channel_id IS NULL))
        ''', list(EXCLUDED_PLAYERS) + [ch_id, ch_id])
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
              AND (m.channel_id = ? OR (? = 1 AND m.channel_id IS NULL))
            GROUP BY LOWER(p.name)
        ''', list(EXCLUDED_PLAYERS) + [ch_id, ch_id])
        all_players_raw = c.fetchall()
        conn.close()

    if not all_players_raw:
        await update.message.reply_text(f"دیتابیس کانال **{ch_name}** هنوز داده‌ای ندارد.", parse_mode="Markdown", reply_markup=get_main_keyboard())
        return

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
        await update.message.reply_text(f"❌ بازیکنی با نام «{query}» در کانال **{ch_name}** پیدا نشد.", parse_mode="Markdown", reply_markup=get_main_keyboard())
        return

    p = matched_player
    m_rate = (p['m_wins'] * 100 // p['m_games']) if p['m_games'] > 0 else 0
    c_rate = (p['c_wins'] * 100 // p['c_games']) if p['c_games'] > 0 else 0
    bar_m = make_bar(m_rate, length=6)
    bar_c = make_bar(c_rate, length=6)

    profile_text = (
        f"🎖 **کارت شناسنامه آماری بازیکن** 🎖\n"
        f"📍 کانال: **{ch_name}**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 **نام:** `{p['name'].title()}`\n"
        f"👑 **جایگاه در این کانال:** `#{rank}` (از میان {len(all_players_calculated)} بازیکن)\n"
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

async def search_perform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip().lower()
    user_id = update.effective_user.id

    conn = sqlite3.connect('mafia_stats.db', timeout=60.0)
    c = conn.cursor()
    ch_id, ch_name = get_user_channel(c, user_id)
    conn.close()

    await show_player_card(update, query, ch_id, ch_name)
    return ConversationHandler.END

async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("عملیات لغو شد.", reply_markup=get_main_keyboard())
    return ConversationHandler.END

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.warning(f"خطای موقت در ارتباط شبکه: {context.error}")

# ================= اجرای برنامه =================
if __name__ == '__main__':
    init_db()
    print("ربات با سیستم گزارش تحلیلی کامل بسته فعال شد...")

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
        fallbacks=[CommandHandler("cancel", cancel_handler)]
    )

    link_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^🔗 اتصال نام بازی من$"), link_profile_start)
        ],
        states={
            LINK_PROFILE_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, link_profile_save)]
        },
        fallbacks=[CommandHandler("cancel", cancel_handler)]
    )

    channel_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^📢 انتخاب / تغییر کانال$"), channel_menu_handler)
        ],
        states={
            ADD_CHANNEL_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, channel_save_or_switch)]
        },
        fallbacks=[CommandHandler("cancel", cancel_handler)]
    )

    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("report", report_command))

    app.add_handler(search_conv)
    app.add_handler(link_conv)
    app.add_handler(channel_conv)

    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_incoming_messages))

    try:
        app.run_polling(drop_pending_updates=False)
    except KeyboardInterrupt:
        print("\nربات با درخواست کاربر خاموش شد.")