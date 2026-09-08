import re
import sqlite3
import asyncio
from telethon import TelegramClient, events
from fuzzywuzzy import process

# ================= تنظیمات ربات =================
API_ID = 6
API_HASH = 'eb06d4de3521142fa6e3d24247465352'
BOT_TOKEN = '8936060141:AAHD7N56eK7FtIq_FBy8E1txGNKkV2lWQjI'
CHANNEL_USERNAME = 'https://t.me/+L8Svnjw-I_0wMzU0'

client = TelegramClient('mafia_bot_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# ================= دیتابیس =================
def init_db():
    conn = sqlite3.connect('mafia_stats.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS players (id INTEGER PRIMARY KEY, name TEXT UNIQUE)''')
    c.execute('''CREATE TABLE IF NOT EXISTS matches (
                    id INTEGER PRIMARY KEY,
                    player_id INTEGER,
                    side TEXT,
                    is_win BOOLEAN,
                    FOREIGN KEY(player_id) REFERENCES players(id))''')
    conn.commit()
    conn.close()

def get_standard_name(name):
    conn = sqlite3.connect('mafia_stats.db')
    c = conn.cursor()
    c.execute("SELECT name FROM players")
    existing_names = [row[0] for row in c.fetchall()]
    conn.close()

    if not existing_names:
        return name

    # تشخیص هوشمند اسامی مشابه (حساسیت ۸۵ درصد برای چشم‌پوشی از غلط‌های املایی)
    best_match, score = process.extractOne(name, existing_names)
    if score >= 85:
        return best_match
    return name

def save_player_stats(name, side, is_win):
    if side == "Independent": # نقش‌های مستقل در آمار حساب نمی‌شوند
        return

    standard_name = get_standard_name(name)
    
    conn = sqlite3.connect('mafia_stats.db')
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO players (name) VALUES (?)", (standard_name,))
    c.execute("SELECT id FROM players WHERE name = ?", (standard_name,))
    player_id = c.fetchone()[0]
    
    c.execute("INSERT INTO matches (player_id, side, is_win) VALUES (?, ?, ?)", (player_id, side, is_win))
    conn.commit()
    conn.close()

# ================= منطق نقش‌ها و سناریوها =================
def detect_side(scenario, role):
    scenario = scenario.lower()
    role = role.lower()
    
    # 1. مستقل‌ها
    independents = ['jack', 'جک', 'nostra', 'نوسترا', 'sherlock', 'شرلوک', 'churchill', 'چرچیل']
    if any(ind in role for ind in independents):
        return "Independent"

    # 2. مافیاهای پایه و اضافه شونده در بازی‌های 13/15 نفره
    mafia_roles = ['don', 'دن', 'nato', 'ناتو', 'mafia sade', 'مافیا ساده']

    # 3. مافیاهای اختصاصی هر سناریو
    if 'takavar' in scenario or 'تکاور' in scenario:
        mafia_roles.extend(['grogangir', 'گروگانگیر'])
    elif 'bazpors' in scenario or 'بازپرس' in scenario:
        mafia_roles.extend(['shayad', 'شیاد'])
    elif 'mozakere' in scenario or 'مذاکره' in scenario:
        mafia_roles.extend(['mozakere', 'مذاکره کننده', 'خریداری کننده'])
    elif 'kapo' in scenario or 'کاپو' in scenario:
        mafia_roles.extend(['jadogar', 'جادوگر', 'jalad', 'جلاد'])
    elif 'hanibal' in scenario or 'هانیبال' in scenario:
        mafia_roles.extend(['hanibal', 'هانیبال', 'saye', 'سایه'])
    elif 'namayande' in scenario or 'نماینده' in scenario:
        mafia_roles.extend(['yaghi', 'یاغی', 'hacker', 'هکر']) 
        # نقش وکیل اینجا خودکار شهروند حساب می‌شود چون به لیست مافیا اضافه نشد.
    elif 'pishrafte' in scenario or 'پیشرفته' in scenario:
        mafia_roles.extend(['vakil', 'وکیل', 'terrorist', 'تروریست', 'natasha', 'ناتاشا'])
    elif any(s in scenario for s in ['nostra', 'نوسترا', 'jack', 'جک', 'sherlock', 'شرلوک', 'پدرخوانده', 'pedarkhande']):
        mafia_roles.extend(['pedarkhande', 'پدرخوانده', 'matador', 'ماتادور', 'saul goodman', 'سال گودمن', 'سال'])
    elif 'elclassico' in scenario or 'الکلاسیکو' in scenario:
        mafia_roles.extend(['khoan', 'خوان', 'blanco', 'بلانکو', 'pablo scobar', 'پابلو اسکوبار', 'پابلو'])

    # بررسی تطابق نقش با لیست مافیاها
    for m in mafia_roles:
        if m in role:
            return "Mafia"
            
    # اگر مستقل یا مافیا نباشد، قطعا شهروند است
    return "Citizen"

# ================= استخراج اطلاعات از متن =================
def process_message(text):
    try:
        scenario_match = re.search(r'𝐒𝐂𝐄𝐍𝐀𝐑𝐈𝐎\s*•\s*(.+)', text)
        win_match = re.search(r'𝐖𝐈𝐍\s*•\s*(.+)', text)
        
        if not scenario_match or not win_match:
            return
            
        scenario = scenario_match.group(1).strip()
        win_text = win_match.group(1).strip().lower()
        
        # تشخیص ساید برنده
        winning_side = "Unknown"
        if 'شهر' in win_text or 'citizen' in win_text:
            winning_side = "Citizen"
        elif 'مافیا' in win_text or 'mafia' in win_text:
            winning_side = "Mafia"
            
        # استخراج بازیکنان (پشتیبانی از نام انگلیسی + نقش فارسی)
        # الگوی شناسایی خطوطی که با شماره شروع میشوند
        players_lines = re.findall(r'[✦]*[➊-➓0-9]+:\s*([a-zA-Z0-9_\-\s]+)\s+(.+)', text)
        
        for name_part, role_part in players_lines:
            name = name_part.strip().lower()
            role = role_part.strip().lower()
            
            # گاد بازی محاسبه نمیشود
            if name == "god":
                continue
                
            player_side = detect_side(scenario, role)
            is_win = (player_side == winning_side)
            
            save_player_stats(name, player_side, is_win)
            
    except Exception as e:
        print(f"Error parsing message: {e}")

# ================= دستورات ربات =================

@client.on(events.NewMessage(pattern='/scan_channel'))
async def scan_channel(event):
    await event.reply("در حال اسکن کانال و استخراج اطلاعات بازی‌ها. لطفا صبر کنید... ⏳")
    count = 0
    async for message in client.iter_messages(CHANNEL_USERNAME):
        if message.text and "𝐏𝐋𝐀𝐘𝐄𝐑𝐒" in message.text and "𝐖𝐈𝐍" in message.text:
            process_message(message.text)
            count += 1
    await event.reply(f"اسکن با موفقیت انجام شد! اطلاعات {count} بازی استخراج و در دیتابیس ذخیره شد. ✅")

@client.on(events.NewMessage(pattern='/report'))
async def generate_report(event):
    conn = sqlite3.connect('mafia_stats.db')
    c = conn.cursor()
    
    # کوئری بازیکنان با بالای 30 بازی (درصد کلی و درصد هر ساید)
    c.execute('''
        SELECT p.name, 
               COUNT(m.id) as total_games,
               SUM(CASE WHEN m.is_win = 1 THEN 1 ELSE 0 END) * 100 / COUNT(m.id) as overall_win_rate,
               SUM(CASE WHEN m.side = 'Mafia' THEN 1 ELSE 0 END) as mafia_games,
               SUM(CASE WHEN m.side = 'Mafia' AND m.is_win = 1 THEN 1 ELSE 0 END) as mafia_wins,
               SUM(CASE WHEN m.side = 'Citizen' THEN 1 ELSE 0 END) as city_games,
               SUM(CASE WHEN m.side = 'Citizen' AND m.is_win = 1 THEN 1 ELSE 0 END) as city_wins
        FROM players p
        JOIN matches m ON p.id = m.player_id
        GROUP BY p.id
        HAVING total_games > 30
        ORDER BY overall_win_rate DESC
    ''')
    top_players = c.fetchall()
    
    if not top_players:
        await event.reply("هنوز بازیکنی با بیش از 30 بازی ثبت نشده است!")
        return

    report = "🏆 **رتبه‌بندی نهایی (بالای 30 بازی)** 🏆\n\n"
    
    mafia_best = []
    city_best = []

    for rank, p in enumerate(top_players, 1):
        name, t_games, overall_rate, m_games, m_wins, c_games, c_wins = p
        
        m_rate = (m_wins * 100 // m_games) if m_games > 0 else 0
        c_rate = (c_wins * 100 // c_games) if c_games > 0 else 0
        
        mafia_best.append((name, m_rate, m_games))
        city_best.append((name, c_rate, c_games))
        
        report += f"🥇 {rank}. {name.title()}\n"
        report += f"🎮 بازی‌ها: {t_games} | 📈 برد کلی: {overall_rate}%\n"
        report += f"🔪 درصد برد مافیایی: {m_rate}% | 🛡 درصد برد شهروندی: {c_rate}%\n"
        report += "┄┄┄┄┄┄┄┄┄┄┄\n"
        
    # رتبه‌بندی مجزا برای مافیا و شهروند
    mafia_best = sorted(mafia_best, key=lambda x: x[1], reverse=True)[:5]
    city_best = sorted(city_best, key=lambda x: x[1], reverse=True)[:5]
    
    report += "\n🔥 **بهترین پلیرها با کارت مافیا:**\n"
    for i, (name, rate, count) in enumerate(mafia_best, 1):
        report += f"{i}. {name.title()} ({rate}% برد از {count} بازی)\n"

    report += "\n🛡 **بهترین پلیرها با کارت شهروند:**\n"
    for i, (name, rate, count) in enumerate(city_best, 1):
        report += f"{i}. {name.title()} ({rate}% برد از {count} بازی)\n"

    await event.reply(report)
    conn.close()

if __name__ == '__main__':
    init_db()
    print("ربات با موفقیت روشن شد...")
    client.run_until_disconnected()