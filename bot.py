import discord
from discord.ext import commands
import aiosqlite
import os
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

if TOKEN is None:
    print("❌ Token not found!")
    exit()

print(f"✅ Token loaded, length: {len(TOKEN)} characters")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(
    command_prefix='!',
    intents=intents,
    activity=discord.Activity(
        type=discord.ActivityType.watching,
        name="0 people on shift"
    ),
    status=discord.Status.online
)

# --- SETTINGS ---
REPORT_CHANNEL_ID = 1533758067513098408
SLOT_NOTIFICATION_CHANNEL_ID = 1544455513964675143
REPORT_USER_ID = 775396551936704533

REMINDER_HOURS = 4
URGENT_HOURS = 8
CHECK_INTERVAL = 30

NOTIFICATION_TIME_HOUR = 9
NOTIFICATION_TIME_MINUTE = 0

TOMORROW_NOTIFICATION_HOUR = 12
TOMORROW_NOTIFICATION_MINUTE = 0

AUTO_PUBLISH_ENABLED = True
AUTO_PUBLISH_DAY = 6
AUTO_PUBLISH_HOUR = 20
AUTO_PUBLISH_MINUTE = 0
AUTO_PUBLISH_PING_ROLE_ID = 1533758065323540565

ADMIN_ROLE_IDS = [
    1533758065352900822,
]

# ✅ ТОЛЬКО ЗА 15 МИНУТ
SLOT_REMINDERS = [15]
DEFAULT_MAX_PEOPLE = 3

# --- TIMEZONE (GMT+3 Moscow) ---
TIMEZONE_OFFSET = 3

def now_tz():
    """Текущее время по GMT+3"""
    return datetime.utcnow() + timedelta(hours=TIMEZONE_OFFSET)

# --- DATABASE ---
async def init_db():
    async with aiosqlite.connect('shifts.db') as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS shifts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, username TEXT, start_time TEXT, end_time TEXT,
                guild_id INTEGER, is_active INTEGER DEFAULT 1, last_reminder INTEGER DEFAULT 0
            )
        ''')
        await db.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER, start_time TEXT, end_time TEXT,
                max_people INTEGER, day_of_week INTEGER, is_active INTEGER DEFAULT 1
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slot_id INTEGER, user_id INTEGER, username TEXT,
                booked_at TEXT, status TEXT DEFAULT 'booked'
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS notification_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER, channel_id INTEGER, message_id INTEGER, date TEXT,
                UNIQUE(guild_id, date)
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS slot_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER, name TEXT, start_time TEXT, end_time TEXT,
                max_people INTEGER, days TEXT,
                UNIQUE(guild_id, name)
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS slot_reminders_sent (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                booking_id INTEGER, minutes_before INTEGER, sent_at TEXT,
                UNIQUE(booking_id, minutes_before)
            )
        ''')
        await db.commit()

# --- HELPERS ---
def format_time(seconds):
    if seconds is None or seconds == 0: return "0h 0min"
    h = int(seconds / 3600); m = int((seconds % 3600) / 60)
    if h == 0: return f"{m} min"
    if m == 0: return f"{h} h"
    return f"{h} h {m} min"

def day_name(day):
    return ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][day] if day is not None else "Every day"

def parse_days(days_str):
    day_map = {"mon":0,"tue":1,"wed":2,"thu":3,"fri":4,"sat":5,"sun":6,
               "пн":0,"вт":1,"ср":2,"чт":3,"пт":4,"сб":5,"вс":6}
    if days_str.lower() == 'all':
        return list(range(7))
    result = []
    for d in [x.strip().lower() for x in days_str.split(',')]:
        if d in day_map:
            result.append(day_map[d])
    return sorted(set(result))

def is_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild: return False
    if interaction.user.guild_permissions.administrator: return True
    user_role_ids = [role.id for role in interaction.user.roles]
    for role_id in ADMIN_ROLE_IDS:
        if role_id in user_role_ids: return True
    return False

# --- SHIFT FUNCTIONS ---
async def get_active_shifts(guild_id):
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute('SELECT user_id, username, start_time FROM shifts WHERE guild_id = ? AND is_active = 1', (guild_id,))
        return await cursor.fetchall()

async def get_all_active_shifts():
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute('SELECT user_id, username, start_time, guild_id, id FROM shifts WHERE is_active = 1')
        return await cursor.fetchall()

async def start_shift(user_id, username, guild_id):
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute('SELECT id FROM shifts WHERE user_id = ? AND guild_id = ? AND is_active = 1', (user_id, guild_id))
        if await cursor.fetchone(): return False
        await db.execute('INSERT INTO shifts (user_id, username, start_time, guild_id, is_active, last_reminder) VALUES (?, ?, ?, ?, 1, 0)',
                         (user_id, username, now_tz().isoformat(), guild_id))
        await db.commit()
        return True

async def end_shift(user_id, guild_id):
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute('SELECT id, start_time FROM shifts WHERE user_id = ? AND guild_id = ? AND is_active = 1', (user_id, guild_id))
        shift = await cursor.fetchone()
        if not shift: return None
        now = now_tz()
        duration = now - datetime.fromisoformat(shift[1])
        await db.execute('UPDATE shifts SET end_time = ?, is_active = 0 WHERE id = ?', (now.isoformat(), shift[0]))
        await db.commit()
        hours = duration.total_seconds() / 3600
        return f"{int(hours)}h {int((hours % 1) * 60)}min"

# --- SLOT FUNCTIONS ---
async def get_today_slots(guild_id):
    today = now_tz().weekday()
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute('''
            SELECT s.id, s.start_time, s.end_time, s.max_people,
                (SELECT COUNT(*) FROM bookings WHERE slot_id = s.id AND status IN ('booked', 'started')) as booked
            FROM slots s
            WHERE s.guild_id = ? AND s.is_active = 1 AND (s.day_of_week = ? OR s.day_of_week IS NULL)
            ORDER BY s.start_time
        ''', (guild_id, today))
        return await cursor.fetchall()

async def get_tomorrow_slots(guild_id):
    tomorrow = (now_tz().weekday() + 1) % 7
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute('''
            SELECT s.id, s.start_time, s.end_time, s.max_people,
                (SELECT COUNT(*) FROM bookings WHERE slot_id = s.id AND status IN ('booked', 'started')) as booked
            FROM slots s
            WHERE s.guild_id = ? AND s.is_active = 1 AND (s.day_of_week = ? OR s.day_of_week IS NULL)
            ORDER BY s.start_time
        ''', (guild_id, tomorrow))
        return await cursor.fetchall()

async def get_all_active_slots(guild_id):
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute('''
            SELECT s.id, s.start_time, s.end_time, s.max_people, s.day_of_week,
                (SELECT COUNT(*) FROM bookings WHERE slot_id = s.id AND status IN ('booked', 'started')) as booked
            FROM slots s WHERE s.guild_id = ? AND s.is_active = 1
            ORDER BY s.day_of_week, s.start_time
        ''', (guild_id,))
        return await cursor.fetchall()

async def get_slot_bookings(slot_id):
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute('SELECT user_id, username FROM bookings WHERE slot_id = ? AND status IN ("booked", "started")', (slot_id,))
        return await cursor.fetchall()

async def get_user_bookings(user_id, guild_id):
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute('''
            SELECT b.id, b.slot_id, s.start_time, s.end_time, s.day_of_week
            FROM bookings b JOIN slots s ON b.slot_id = s.id
            WHERE b.user_id = ? AND s.guild_id = ? AND b.status = 'booked'
            ORDER BY s.day_of_week, s.start_time
        ''', (user_id, guild_id))
        return await cursor.fetchall()

async def book_slot(slot_id, user_id, username):
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute('SELECT max_people, (SELECT COUNT(*) FROM bookings WHERE slot_id = ? AND status IN (\'booked\', \'started\')) FROM slots WHERE id = ?', (slot_id, slot_id))
        row = await cursor.fetchone()
        if not row: return "not_found"
        max_p, booked = row
        if booked >= max_p: return "full"
        cursor = await db.execute('SELECT id FROM bookings WHERE slot_id = ? AND user_id = ? AND status = \'booked\'', (slot_id, user_id))
        if await cursor.fetchone(): return "already_booked"
        await db.execute('INSERT INTO bookings (slot_id, user_id, username, booked_at, status) VALUES (?, ?, ?, ?, "booked")',
                         (slot_id, user_id, username, now_tz().isoformat()))
        await db.commit()
        return "success"

async def cancel_booking(booking_id, user_id):
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute('UPDATE bookings SET status = "cancelled" WHERE id = ? AND user_id = ? AND status = "booked"', (booking_id, user_id))
        await db.commit()
        return cursor.rowcount > 0

# --- NOTIFICATION MESSAGES ---
async def save_notification_message(guild_id, channel_id, message_id, is_tomorrow=False):
    today = now_tz().date().isoformat()
    key = f"tomorrow_{today}" if is_tomorrow else f"today_{today}"
    async with aiosqlite.connect('shifts.db') as db:
        await db.execute('INSERT OR REPLACE INTO notification_messages (guild_id, channel_id, message_id, date) VALUES (?, ?, ?, ?)',
                         (guild_id, channel_id, message_id, key))
        await db.commit()

async def get_notification_message(guild_id, is_tomorrow=False):
    today = now_tz().date().isoformat()
    key = f"tomorrow_{today}" if is_tomorrow else f"today_{today}"
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute('SELECT channel_id, message_id FROM notification_messages WHERE guild_id = ? AND date = ?', (guild_id, key))
        return await cursor.fetchone()

# --- NOTIFICATIONS ---
async def send_shift_start_notification(user_id, username, guild_id):
    try:
        channel = bot.get_channel(REPORT_CHANNEL_ID)
        if channel:
            embed = discord.Embed(title="🟢 Shift Started", description=f"**{username}** started their shift at {now_tz().strftime('%H:%M')}",
                                  color=discord.Color.green(), timestamp=now_tz())
            embed.set_footer(text=f"User ID: {user_id}")
            await channel.send(embed=embed)
    except Exception as e: print(f"❌ Start notification error: {e}")

async def send_shift_end_notification(user_id, username, duration, guild_id):
    try:
        channel = bot.get_channel(REPORT_CHANNEL_ID)
        if channel:
            embed = discord.Embed(title="🔴 Shift Ended", description=f"**{username}** ended their shift",
                                  color=discord.Color.red(), timestamp=now_tz())
            embed.add_field(name="⏱️ Duration", value=duration, inline=True)
            embed.set_footer(text=f"User ID: {user_id}")
            await channel.send(embed=embed)
    except Exception as e: print(f"❌ End notification error: {e}")

# --- LONG SHIFT REMINDERS ---
async def check_long_shifts():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            for user_id, username, start_time_str, guild_id, shift_id in await get_all_active_shifts():
                hours = (now_tz() - datetime.fromisoformat(start_time_str)).total_seconds() / 3600
                async with aiosqlite.connect('shifts.db') as db:
                    cursor = await db.execute('SELECT last_reminder FROM shifts WHERE id = ?', (shift_id,))
                    result = await cursor.fetchone()
                    last_reminder = result[0] if result else 0
                rtype = ""
                if hours >= URGENT_HOURS and last_reminder < 2: rtype = "urgent"
                elif hours >= REMINDER_HOURS and last_reminder == 0: rtype = "normal"
                if rtype:
                    try:
                        user = await bot.fetch_user(user_id)
                        if user:
                            if rtype == "normal":
                                embed = discord.Embed(title="⏰ Shift Reminder", description=f"⚠️ You've been on shift for **{int(hours)} hours**!",
                                                      color=discord.Color.orange(), timestamp=now_tz())
                                embed.add_field(name="💡 Tip", value="Don't forget to take breaks!", inline=False)
                            else:
                                embed = discord.Embed(title="🚨 URGENT: Long Shift Warning", description=f"⚠️ You've been on shift for **{int(hours)} hours**!",
                                                      color=discord.Color.red(), timestamp=now_tz())
                                embed.add_field(name="💡 Important", value="Please consider ending your shift.", inline=False)
                            await user.send(embed=embed)
                            new_val = 2 if rtype == "urgent" else 1
                            async with aiosqlite.connect('shifts.db') as db:
                                await db.execute('UPDATE shifts SET last_reminder = ? WHERE id = ?', (new_val, shift_id))
                                await db.commit()
                    except discord.Forbidden: pass
                    except Exception as e: print(f"❌ Reminder error: {e}")
            await asyncio.sleep(CHECK_INTERVAL * 60)
        except Exception as e:
            print(f"❌ check_long_shifts error: {e}")
            await asyncio.sleep(60)

# --- SLOT REMINDERS (только за 15 минут) ---
async def check_slot_reminders():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            now = now_tz()
            today = now.weekday()
            async with aiosqlite.connect('shifts.db') as db:
                cursor = await db.execute('''
                    SELECT id, start_time, end_time, guild_id FROM slots
                    WHERE is_active = 1 AND (day_of_week = ? OR day_of_week IS NULL)
                ''', (today,))
                slots = await cursor.fetchall()
            for slot_id, start, end, guild_id in slots:
                try:
                    start_dt = datetime.strptime(start, "%H:%M").replace(year=now.year, month=now.month, day=now.day)
                except: continue
                if start_dt < now: continue
                minutes_until = (start_dt - now).total_seconds() / 60
                for remind_min in SLOT_REMINDERS:
                    if remind_min - 1 <= minutes_until <= remind_min + 1:
                        async with aiosqlite.connect('shifts.db') as db:
                            cursor = await db.execute('SELECT b.id, b.user_id FROM bookings b WHERE b.slot_id = ? AND b.status = "booked"', (slot_id,))
                            bookings = await cursor.fetchall()
                        for booking_id, user_id in bookings:
                            async with aiosqlite.connect('shifts.db') as db:
                                cursor = await db.execute('SELECT id FROM slot_reminders_sent WHERE booking_id = ? AND minutes_before = ?',
                                                          (booking_id, remind_min))
                                if await cursor.fetchone(): continue
                            try:
                                user = await bot.fetch_user(user_id)
                                if user:
                                    embed = discord.Embed(
                                        title=f"⏰ Shift Reminder — {remind_min} min",
                                        description=f"Your shift starts at **{start}** ({remind_min} minutes left!)",
                                        color=discord.Color.gold(), timestamp=now_tz()
                                    )
                                    embed.add_field(name="📅 Slot", value=f"{start} - {end}", inline=False)
                                    await user.send(embed=embed)
                                    async with aiosqlite.connect('shifts.db') as db:
                                        await db.execute('INSERT OR IGNORE INTO slot_reminders_sent (booking_id, minutes_before, sent_at) VALUES (?, ?, ?)',
                                                         (booking_id, remind_min, now.isoformat()))
                                        await db.commit()
                            except discord.Forbidden: pass
                            except Exception as e: print(f"❌ Slot reminder error: {e}")
            await asyncio.sleep(60)
        except Exception as e:
            print(f"❌ check_slot_reminders error: {e}")
            await asyncio.sleep(60)

# --- BUILD EMBED: TODAY ---
async def build_slot_notification_embed(guild):
    today_slots = await get_today_slots(guild.id)
    embed = discord.Embed(
        title="📢 Sign Up for Shifts!",
        description="**Available slots for today (00:00 - 23:00):**\nClick the menu below to book your shift!",
        color=discord.Color.blurple(), timestamp=now_tz()
    )
    if not today_slots:
        embed.description = "📭 No slots available for today. Check back later!"
        return embed, []
    free_slots = []
    for slot_id, start, end, max_p, booked in today_slots:
        free = max_p - booked
        booked_users = await get_slot_bookings(slot_id)
        mentions = " ".join([f"<@{uid}>" for uid, _ in booked_users]) if booked_users else "—"
        status = "🟢" if free > 0 else "🔴"
        embed.add_field(name=f"{status} **{start} - {end}** ({booked}/{max_p})", value=f"👥 {mentions}", inline=False)
        if free > 0:
            free_slots.append((slot_id, start, end, free))
    return embed, free_slots

# --- BUILD EMBED: TOMORROW ---
async def build_tomorrow_notification_embed(guild):
    tomorrow_slots = await get_tomorrow_slots(guild.id)
    tomorrow_day = (now_tz().weekday() + 1) % 7
    tomorrow_name = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][tomorrow_day]
    embed = discord.Embed(
        title=f"📢 Sign Up for Tomorrow ({tomorrow_name})!",
        description="**Available slots for tomorrow (00:00 - 23:00):**\nClick the menu below to book your shift in advance!",
        color=discord.Color.purple(), timestamp=now_tz()
    )
    if not tomorrow_slots:
        embed.description = f"📭 No slots available for tomorrow ({tomorrow_name})."
        return embed, []
    free_slots = []
    for slot_id, start, end, max_p, booked in tomorrow_slots:
        free = max_p - booked
        booked_users = await get_slot_bookings(slot_id)
        mentions = " ".join([f"<@{uid}>" for uid, _ in booked_users]) if booked_users else "—"
        status = "🟢" if free > 0 else "🔴"
        embed.add_field(name=f"{status} **{start} - {end}** ({booked}/{max_p})", value=f"👥 {mentions}", inline=False)
        if free > 0:
            free_slots.append((slot_id, start, end, free))
    embed.set_footer(text=f"Tomorrow: {tomorrow_name}")
    return embed, free_slots

# --- SEND NOTIFICATIONS ---
async def send_slot_notification():
    try:
        for guild in bot.guilds:
            channel = bot.get_channel(SLOT_NOTIFICATION_CHANNEL_ID)
            if not channel: continue
            embed, free_slots = await build_slot_notification_embed(guild)
            view = SlotNotificationView(guild.id, free_slots)
            msg = await channel.send(embed=embed, view=view)
            await save_notification_message(guild.id, channel.id, msg.id, is_tomorrow=False)
            print(f"✅ Today notification sent to {guild.name}")
    except Exception as e: print(f"❌ Error sending today notification: {e}")

async def send_tomorrow_notification():
    try:
        for guild in bot.guilds:
            channel = bot.get_channel(SLOT_NOTIFICATION_CHANNEL_ID)
            if not channel: continue
            embed, free_slots = await build_tomorrow_notification_embed(guild)
            view = SlotNotificationView(guild.id, free_slots)
            msg = await channel.send(embed=embed, view=view)
            await save_notification_message(guild.id, channel.id, msg.id, is_tomorrow=True)
            print(f"✅ Tomorrow notification sent to {guild.name}")
    except Exception as e: print(f"❌ Error sending tomorrow notification: {e}")

async def update_tomorrow_notification(guild_id):
    try:
        msg_data = await get_notification_message(guild_id, is_tomorrow=True)
        if not msg_data: return
        channel_id, message_id = msg_data
        channel = bot.get_channel(channel_id)
        if not channel: return
        try: message = await channel.fetch_message(message_id)
        except discord.NotFound: return
        guild = bot.get_guild(guild_id)
        if not guild: return
        embed, free_slots = await build_tomorrow_notification_embed(guild)
        view = SlotNotificationView(guild_id, free_slots)
        await message.edit(embed=embed, view=view)
    except Exception as e: print(f"❌ Error updating tomorrow notification: {e}")

async def update_slot_notification(guild_id):
    try:
        msg_data = await get_notification_message(guild_id, is_tomorrow=False)
        if msg_data:
            channel_id, message_id = msg_data
            channel = bot.get_channel(channel_id)
            if channel:
                try: message = await channel.fetch_message(message_id)
                except discord.NotFound: message = None
                if message:
                    guild = bot.get_guild(guild_id)
                    if guild:
                        embed, free_slots = await build_slot_notification_embed(guild)
                        view = SlotNotificationView(guild_id, free_slots)
                        await message.edit(embed=embed, view=view)
        await update_tomorrow_notification(guild_id)
    except Exception as e: print(f"❌ Error updating notifications: {e}")

# --- NOTIFICATION LOOP ---
async def slot_notification_loop():
    await bot.wait_until_ready()
    await asyncio.sleep(30)
    while not bot.is_closed():
        try:
            now = now_tz()
            today_key = now.date().isoformat()
            if now.hour == NOTIFICATION_TIME_HOUR and now.minute < 5:
                key = f"daily_notification_{today_key}"
                async with aiosqlite.connect('shifts.db') as db:
                    cursor = await db.execute('SELECT value FROM settings WHERE key = ?', (key,))
                    if await cursor.fetchone():
                        await asyncio.sleep(60); continue
                await send_slot_notification()
                async with aiosqlite.connect('shifts.db') as db:
                    await db.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, "done"))
                    await db.commit()
                print(f"✅ Today notification sent at {now.strftime('%H:%M')}")
                await asyncio.sleep(3600); continue
            if now.hour == TOMORROW_NOTIFICATION_HOUR and now.minute < 5:
                key = f"tomorrow_notification_{today_key}"
                async with aiosqlite.connect('shifts.db') as db:
                    cursor = await db.execute('SELECT value FROM settings WHERE key = ?', (key,))
                    if await cursor.fetchone():
                        await asyncio.sleep(60); continue
                await send_tomorrow_notification()
                async with aiosqlite.connect('shifts.db') as db:
                    await db.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, "done"))
                    await db.commit()
                print(f"✅ Tomorrow notification sent at {now.strftime('%H:%M')}")
                await asyncio.sleep(3600); continue
            await asyncio.sleep(60)
        except Exception as e:
            print(f"❌ slot_notification_loop error: {e}")
            await asyncio.sleep(60)

# --- AUTO-PUBLISH ---
async def auto_publish_next_week():
    await bot.wait_until_ready()
    await asyncio.sleep(60)
    while not bot.is_closed():
        try:
            if not AUTO_PUBLISH_ENABLED:
                await asyncio.sleep(3600); continue
            now = now_tz()
            if now.weekday() != AUTO_PUBLISH_DAY or now.hour != AUTO_PUBLISH_HOUR or now.minute >= 5:
                await asyncio.sleep(60); continue
            week_key = f"auto_publish_{now.date().isoformat()}"
            async with aiosqlite.connect('shifts.db') as db:
                cursor = await db.execute('SELECT value FROM settings WHERE key = ?', (week_key,))
                if await cursor.fetchone():
                    await asyncio.sleep(3600); continue
            async with aiosqlite.connect('shifts.db') as db:
                cursor = await db.execute('SELECT guild_id, name, start_time, end_time, max_people, days FROM slot_templates')
                templates = await cursor.fetchall()
            if not templates:
                async with aiosqlite.connect('shifts.db') as db:
                    await db.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (week_key, "done"))
                    await db.commit()
                await asyncio.sleep(86400); continue
            created_by_guild = {}
            for guild_id, name, start, end, max_p, days_str in templates:
                async with aiosqlite.connect('shifts.db') as db:
                    cursor = await db.execute('SELECT COUNT(*) FROM slots WHERE guild_id = ? AND start_time = ? AND end_time = ?',
                                              (guild_id, start, end))
                    if (await cursor.fetchone())[0] > 0: continue
                days = parse_days(days_str)
                count = 0
                async with aiosqlite.connect('shifts.db') as db:
                    for day in days:
                        await db.execute('''
                            INSERT INTO slots (guild_id, start_time, end_time, max_people, day_of_week, is_active)
                            VALUES (?, ?, ?, ?, ?, 1)
                        ''', (guild_id, start, end, max_p, day))
                        count += 1
                    await db.commit()
                created_by_guild[guild_id] = created_by_guild.get(guild_id, 0) + count
            async with aiosqlite.connect('shifts.db') as db:
                await db.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (week_key, "done"))
                await db.commit()
            channel = bot.get_channel(SLOT_NOTIFICATION_CHANNEL_ID)
            if channel:
                total = sum(created_by_guild.values())
                embed = discord.Embed(
                    title="📢 Новая неделя — новые слоты!",
                    description=f"🗓️ Слоты на следующую неделю **уже доступны**!\n✅ Создано слотов: **{total}**\n\n👉 Открой `/shift` чтобы записаться",
                    color=discord.Color.green(), timestamp=now_tz()
                )
                embed.set_footer(text="Auto-published every Sunday at 20:00")
                async with aiosqlite.connect('shifts.db') as db:
                    cursor = await db.execute('SELECT start_time, end_time, max_people, day_of_week FROM slots WHERE guild_id = ? AND is_active = 1 ORDER BY day_of_week, start_time',
                                              (channel.guild.id,))
                    slots = await cursor.fetchall()
                if slots:
                    slots_by_day = {}
                    for start, end, maxp, day in slots:
                        slots_by_day.setdefault(day, []).append(f"{start}-{end} ({maxp} мест)")
                    for day in sorted(slots_by_day.keys()):
                        embed.add_field(name=f"📅 {day_name(day)}", value="\n".join(slots_by_day[day][:10]), inline=False)
                try:
                    await channel.send(content=f"<@&{AUTO_PUBLISH_PING_ROLE_ID}>", embed=embed)
                    print(f"✅ Auto-publish sent")
                except Exception as e:
                    print(f"❌ Send error: {e}")
            try:
                if REPORT_USER_ID:
                    u = await bot.fetch_user(REPORT_USER_ID)
                    if u: await u.send(f"✅ Авто-публикация: создано **{sum(created_by_guild.values())}** слотов.")
            except: pass
            await asyncio.sleep(86400)
        except Exception as e:
            print(f"❌ auto_publish error: {e}")
            await asyncio.sleep(3600)

# --- SLOT VIEW ---
class SlotNotificationView(discord.ui.View):
    def __init__(self, guild_id, free_slots):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        if free_slots:
            options = [discord.SelectOption(label=f"{s[1]} - {s[2]}", description=f"Free: {s[3]} spots", value=str(s[0])) for s in free_slots[:25]]
            select = discord.ui.Select(placeholder="📅 Choose your shift...", options=options)
            select.callback = self.select_callback
            self.add_item(select)
    async def select_callback(self, interaction: discord.Interaction):
        slot_id = int(interaction.data['values'][0])
        result = await book_slot(slot_id, interaction.user.id, interaction.user.name)
        if result == "success":
            await interaction.response.send_message("✅ Booked! Your tag is now shown in the list.", ephemeral=True)
            await update_slot_notification(interaction.guild_id)
        elif result == "full":
            await interaction.response.send_message("❌ Slot is now full.", ephemeral=True)
        elif result == "already_booked":
            await interaction.response.send_message("❌ You already booked this slot.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Slot not found.", ephemeral=True)

# --- STATUS ---
async def update_status():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            total = 0
            for guild in bot.guilds:
                total += len(await get_active_shifts(guild.id))
            await bot.change_presence(
                status=discord.Status.online,
                activity=discord.Activity(type=discord.ActivityType.watching, name=f"{total} people on shift")
            )
        except Exception as e:
            print(f"❌ Status update error: {e}")
        await asyncio.sleep(30)

# --- MONTHLY REPORT ---
async def send_monthly_report():
    await bot.wait_until_ready()
    await asyncio.sleep(10)
    while not bot.is_closed():
        try:
            now = now_tz()
            if now.day == 1 and now.hour == 0 and now.minute < 5:
                month_start = (now - timedelta(days=1)).replace(day=1, hour=0, minute=0, second=0)
                month_end = now.replace(day=1, hour=0, minute=0, second=0)
                async with aiosqlite.connect('shifts.db') as db:
                    cursor = await db.execute('''SELECT user_id, username, COUNT(*), SUM(strftime('%s', end_time) - strftime('%s', start_time))
                        FROM shifts WHERE guild_id IS NOT NULL AND is_active = 0 AND end_time IS NOT NULL
                        AND start_time >= ? AND end_time <= ? GROUP BY user_id, username ORDER BY 4 DESC''',
                        (month_start.isoformat(), month_end.isoformat()))
                    stats = await cursor.fetchall()
                    await db.execute('DELETE FROM shifts WHERE end_time < ? AND is_active = 0', (month_end.isoformat(),))
                    await db.commit()
                embed = discord.Embed(title=f"📊 Monthly Report - {month_start.strftime('%B %Y')}", color=discord.Color.gold(), timestamp=now_tz())
                if stats:
                    total_s = sum(s[3] for s in stats if s[3])
                    embed.add_field(name="📈 Summary", value=f"Employees: {len(stats)}\nShifts: {sum(s[2] for s in stats)}\nHours: {format_time(total_s)}", inline=False)
                    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
                    for i, (uid, uname, sh, secs) in enumerate(stats[:10], 1):
                        embed.add_field(name=f"{medals[i-1]} {uname}", value=f"{sh} shifts, {format_time(secs)}", inline=False)
                try:
                    ch = bot.get_channel(REPORT_CHANNEL_ID)
                    if ch: await ch.send(embed=embed)
                    if REPORT_USER_ID:
                        u = await bot.fetch_user(REPORT_USER_ID)
                        if u: await u.send(embed=embed)
                except Exception as e: print(f"❌ Report error: {e}")
                await asyncio.sleep(86400)
            await asyncio.sleep(3600)
        except Exception as e:
            print(f"❌ Monthly report error: {e}")
            await asyncio.sleep(3600)

# --- SHIFT PANEL ---
async def create_shift_panel(interaction: discord.Interaction, edit: bool = False):
    try:
        async with aiosqlite.connect('shifts.db') as db:
            cursor = await db.execute('SELECT id FROM shifts WHERE user_id = ? AND guild_id = ? AND is_active = 1', (interaction.user.id, interaction.guild_id))
            active_shift = await cursor.fetchone()
        active_users = await get_active_shifts(interaction.guild_id)
        today_slots = await get_today_slots(interaction.guild_id)
        my_bookings = await get_user_bookings(interaction.user.id, interaction.guild_id)
        embed = discord.Embed(title="📋 Shift Management", color=discord.Color.blue(), timestamp=now_tz())
        status = "🟢 On Shift" if active_shift else "🔴 Off Shift"
        embed.add_field(name="Your Status", value=f"{status}\n{interaction.user.mention}", inline=False)
        if today_slots:
            text = []
            for sid, start, end, maxp, booked in today_slots:
                free = maxp - booked
                booked_users = await get_slot_bookings(sid)
                mentions = " ".join([f"<@{uid}>" for uid, _ in booked_users]) if booked_users else "—"
                icon = "🟢" if free > 0 else "🔴"
                text.append(f"{icon} **{start}-{end}** ({booked}/{maxp}) — {mentions}")
            embed.add_field(name="📅 Today's Slots", value="\n".join(text[:10]), inline=False)
        else:
            embed.add_field(name="📅 Today's Slots", value="No slots available.", inline=False)
        if my_bookings:
            text = [f"✅ {day_name(d)} {s}-{e} (ID: {bid})" for bid, sid, s, e, d in my_bookings]
            embed.add_field(name="📝 My Bookings", value="\n".join(text[:10]), inline=False)
        if active_users:
            text = [f"👤 {u[1]} — {int((now_tz()-datetime.fromisoformat(u[2])).total_seconds()/3600)}h" for u in active_users[:10]]
            embed.add_field(name=f"👥 On Shift ({len(active_users)})", value="\n".join(text), inline=False)
        else:
            embed.add_field(name="👥 On Shift (0)", value="No one is on shift", inline=False)
        view = ShiftPanelView(interaction.user.id, active_shift is not None, today_slots, my_bookings)
        if edit: await interaction.edit_original_response(embed=embed, view=view)
        else: await interaction.followup.send(embed=embed, view=view)
    except Exception as e:
        print(f"❌ Panel error: {e}")
        if not edit: await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

# --- SHIFT PANEL VIEW ---
class ShiftPanelView(discord.ui.View):
    def __init__(self, user_id, is_active, today_slots, my_bookings):
        super().__init__(timeout=300)
        self.user_id = user_id
        if not is_active:
            self.add_item(discord.ui.Button(label="▶️ Start Shift Now", style=discord.ButtonStyle.success, custom_id="start_shift_button", row=0))
        else:
            self.add_item(discord.ui.Button(label="⏹️ End Shift", style=discord.ButtonStyle.danger, custom_id="end_shift_button", row=0))
        self.add_item(discord.ui.Button(label="🔄 Refresh", style=discord.ButtonStyle.secondary, custom_id="refresh_button", row=0))
        free_slots = [(s[0], s[1], s[2], s[3] - s[4]) for s in today_slots if s[3] - s[4] > 0]
        if free_slots:
            options = [discord.SelectOption(label=f"Book: {s[1]}-{s[2]}", description=f"{s[3]} spots left", value=f"book_{s[0]}") for s in free_slots[:25]]
            select = discord.ui.Select(placeholder="📅 Book a shift slot...", options=options, row=1)
            select.callback = self.book_callback
            self.add_item(select)
        if my_bookings:
            options = [discord.SelectOption(label=f"Cancel: {b[2]}-{b[3]}", description=f"{day_name(b[4])}", value=f"cancel_{b[0]}") for b in my_bookings[:25]]
            cs = discord.ui.Select(placeholder="❌ Cancel booking...", options=options, row=2)
            cs.callback = self.cancel_callback
            self.add_item(cs)
    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Not your panel!", ephemeral=True)
            return False
        return True
    async def book_callback(self, interaction):
        slot_id = int(interaction.data['values'][0].replace('book_', ''))
        await interaction.response.defer(ephemeral=True)
        result = await book_slot(slot_id, interaction.user.id, interaction.user.name)
        msgs = {"success": "✅ Slot booked!", "full": "❌ Slot is full.", "already_booked": "❌ Already booked.", "not_found": "❌ Not found."}
        await interaction.followup.send(msgs.get(result, "❌ Error"), ephemeral=True)
        await update_slot_notification(interaction.guild_id)
        await asyncio.sleep(1)
        await create_shift_panel(interaction, edit=True)
    async def cancel_callback(self, interaction):
        booking_id = int(interaction.data['values'][0].replace('cancel_', ''))
        await interaction.response.defer(ephemeral=True)
        if await cancel_booking(booking_id, interaction.user.id):
            await interaction.followup.send("✅ Booking cancelled.", ephemeral=True)
            await update_slot_notification(interaction.guild_id)
        else:
            await interaction.followup.send("❌ Could not cancel.", ephemeral=True)
        await asyncio.sleep(1)
        await create_shift_panel(interaction, edit=True)

# --- READY ---
@bot.event
async def on_ready():
    await init_db()
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Activity(type=discord.ActivityType.watching, name="0 people on shift")
    )
    try:
        synced = await bot.tree.sync()
        print(f'✅ Synced {len(synced)} commands')
        for c in synced: print(f'   - /{c.name}')
    except Exception as e: print(f'❌ Sync error: {e}')
    print(f'✅ Bot {bot.user} is ready!')
    bot.loop.create_task(update_status())
    bot.loop.create_task(check_long_shifts())
    bot.loop.create_task(check_slot_reminders())
    bot.loop.create_task(send_monthly_report())
    bot.loop.create_task(slot_notification_loop())
    bot.loop.create_task(auto_publish_next_week())

# ==================== USER COMMANDS ====================
@bot.tree.command(name='shift', description='📋 Manage your shift and book slots')
async def shift_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try: await create_shift_panel(interaction, edit=False)
    except Exception as e: await interaction.followup.send(f"❌ {str(e)}", ephemeral=True)

@bot.tree.command(name='onshift', description='👥 Who is on shift right now')
async def onshift_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    users = await get_active_shifts(interaction.guild_id)
    embed = discord.Embed(title="👥 Who is on shift", color=discord.Color.blue(), timestamp=now_tz())
    if users:
        text = []
        for u in users:
            secs = (now_tz() - datetime.fromisoformat(u[2])).total_seconds()
            text.append(f"👤 {u[1]} — **{int(secs/3600)}h {int((secs%3600)/60)}min** (since {u[2][11:16]})")
        embed.description = "\n".join(text)
    else:
        embed.description = "🟢 No one is on shift"
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name='stats', description='📊 My shift statistics')
async def stats_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect('shifts.db') as db:
        today = now_tz().date().isoformat()
        cursor = await db.execute('SELECT start_time, end_time FROM shifts WHERE user_id = ? AND guild_id = ? AND is_active = 0 AND date(start_time) = ?',
                                  (interaction.user.id, interaction.guild_id, today))
        today_shifts = await cursor.fetchall()
        week_ago = (now_tz() - timedelta(days=7)).isoformat()
        cursor = await db.execute('SELECT start_time, end_time FROM shifts WHERE user_id = ? AND guild_id = ? AND is_active = 0 AND start_time > ?',
                                  (interaction.user.id, interaction.guild_id, week_ago))
        week_shifts = await cursor.fetchall()
    def calc(sh):
        return sum((datetime.fromisoformat(s[1]) - datetime.fromisoformat(s[0])).total_seconds()/3600 for s in sh if s[1])
    embed = discord.Embed(title=f"📊 Statistics for {interaction.user.name}", color=discord.Color.gold(), timestamp=now_tz())
    embed.add_field(name="📅 Today", value=f"{calc(today_shifts):.1f} h\n({len(today_shifts)} shifts)", inline=True)
    embed.add_field(name="📅 Week", value=f"{calc(week_shifts):.1f} h\n({len(week_shifts)} shifts)", inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name='user-stats', description='📊 Statistics of a specific employee')
async def userstats_cmd(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute('SELECT start_time, end_time FROM shifts WHERE user_id = ? AND guild_id = ? AND is_active = 0 ORDER BY start_time DESC',
                                  (user.id, interaction.guild_id))
        shifts = await cursor.fetchall()
    if not shifts:
        await interaction.followup.send(f"📭 {user.mention} has no shifts.", ephemeral=True); return
    total = today_s = week_s = month_s = 0
    td = now_tz().date(); wk = now_tz()-timedelta(days=7); mo = now_tz()-timedelta(days=30)
    for s, e in shifts:
        start = datetime.fromisoformat(s); end = datetime.fromisoformat(e)
        d = (end-start).total_seconds(); total += d
        if start.date() == td: today_s += d
        if start >= wk: week_s += d
        if start >= mo: month_s += d
    embed = discord.Embed(title=f"📊 Statistics for {user.display_name}", color=discord.Color.blue(), timestamp=now_tz())
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="📅 Total Shifts", value=f"**{len(shifts)}**", inline=False)
    embed.add_field(name="⏱️ Total", value=format_time(total), inline=True)
    embed.add_field(name="📅 Today", value=format_time(today_s), inline=True)
    embed.add_field(name="📅 Week", value=format_time(week_s), inline=True)
    embed.add_field(name="📅 Month", value=format_time(month_s), inline=True)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name='top', description='🏆 Top employees by hours')
async def top_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute('''SELECT user_id, username, COUNT(*), SUM(strftime('%s', end_time)-strftime('%s', start_time))
            FROM shifts WHERE guild_id = ? AND is_active = 0 AND end_time IS NOT NULL
            GROUP BY user_id, username ORDER BY 4 DESC LIMIT 10''', (interaction.guild_id,))
        stats = await cursor.fetchall()
    if not stats:
        await interaction.followup.send("📭 No data.", ephemeral=True); return
    embed = discord.Embed(title="🏆 Top Employees", color=discord.Color.gold(), timestamp=now_tz())
    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    for i, (uid, un, sh, secs) in enumerate(stats):
        m = interaction.guild.get_member(uid)
        name = m.display_name if m else un
        embed.add_field(name=f"{medals[i]} {name}", value=f"📊 {sh} shifts | ⏱️ {format_time(secs)}", inline=False)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name='slots', description='📅 View available shift slots')
async def slots_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    embed, _ = await build_slot_notification_embed(interaction.guild)
    await interaction.followup.send(embed=embed, ephemeral=True)

# ==================== ADMIN COMMANDS ====================
@bot.tree.command(name='create-hourly-slots', description='⏰ Создать почасовые слоты 00:00-23:00 (по 3 места)')
async def create_hourly_slots(interaction: discord.Interaction, days: str = "all", max_people: int = DEFAULT_MAX_PEOPLE):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ No permission!", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    selected = parse_days(days)
    if not selected:
        await interaction.followup.send("❌ Неверный формат дней.", ephemeral=True); return
    hours = [f"{h:02d}:00" for h in range(24)]
    created = 0
    async with aiosqlite.connect('shifts.db') as db:
        for day in selected:
            for i, start in enumerate(hours):
                end = hours[i + 1] if i + 1 < len(hours) else "00:00"
                await db.execute('INSERT INTO slots (guild_id, start_time, end_time, max_people, day_of_week, is_active) VALUES (?, ?, ?, ?, ?, 1)',
                                 (interaction.guild_id, start, end, max_people, day))
                created += 1
        await db.commit()
    names = [["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][d] for d in selected]
    embed = discord.Embed(title="✅ Почасовые слоты созданы!",
                          description=f"**Создано:** {created}\n**Мест:** {max_people}\n**Дни:** {', '.join(names)}",
                          color=discord.Color.green())
    await interaction.followup.send(embed=embed)
    await update_slot_notification(interaction.guild_id)

@bot.tree.command(name='set-slots-to-3', description='🔧 Установить 3 места для всех слотов (админ)')
async def set_slots_to_3(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ No permission!", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute('UPDATE slots SET max_people = 3 WHERE guild_id = ?', (interaction.guild_id,))
        updated = cursor.rowcount
        await db.commit()
    await interaction.followup.send(f"✅ Обновлено **{updated}** слотов — теперь по **3 места**.", ephemeral=True)
    await update_slot_notification(interaction.guild_id)

@bot.tree.command(name='create-week-slots', description='📅 Create slots for the week (admin)')
async def create_week_slots(interaction: discord.Interaction, start_time: str, end_time: str, max_people: int, days: str):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ No permission!", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    selected = parse_days(days)
    if not selected:
        await interaction.followup.send("❌ Unknown days format.", ephemeral=True); return
    async with aiosqlite.connect('shifts.db') as db:
        for day in selected:
            await db.execute('INSERT INTO slots (guild_id, start_time, end_time, max_people, day_of_week, is_active) VALUES (?, ?, ?, ?, ?, 1)',
                             (interaction.guild_id, start_time, end_time, max_people, day))
        await db.commit()
    names = [["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][d] for d in selected]
    embed = discord.Embed(title="✅ Slots Created!", description=f"Created **{len(selected)}** slots\nTime: **{start_time}-{end_time}**\nSpots: **{max_people}**\nDays: {', '.join(names)}",
                          color=discord.Color.green())
    await interaction.followup.send(embed=embed)

@bot.tree.command(name='create-template', description='💾 Save a slot template (admin)')
async def create_template(interaction: discord.Interaction, name: str, start_time: str, end_time: str, max_people: int, days: str):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ No permission!", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect('shifts.db') as db:
        try:
            await db.execute('INSERT INTO slot_templates (guild_id, name, start_time, end_time, max_people, days) VALUES (?, ?, ?, ?, ?, ?)',
                             (interaction.guild_id, name, start_time, end_time, max_people, days))
            await db.commit()
        except aiosqlite.IntegrityError:
            await db.execute('UPDATE slot_templates SET start_time = ?, end_time = ?, max_people = ?, days = ? WHERE guild_id = ? AND name = ?',
                             (start_time, end_time, max_people, days, interaction.guild_id, name))
            await db.commit()
    embed = discord.Embed(title="✅ Template Saved!", description=f"**Name:** {name}\n**Time:** {start_time}-{end_time}\n**Spots:** {max_people}\n**Days:** {days}",
                          color=discord.Color.green())
    await interaction.followup.send(embed=embed)

@bot.tree.command(name='list-templates', description='📋 List all slot templates (admin)')
async def list_templates(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ No permission!", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute('SELECT name, start_time, end_time, max_people, days FROM slot_templates WHERE guild_id = ?', (interaction.guild_id,))
        templates = await cursor.fetchall()
    if not templates:
        await interaction.followup.send("📭 No templates saved.", ephemeral=True); return
    embed = discord.Embed(title="📋 Slot Templates", color=discord.Color.blue(), timestamp=now_tz())
    for name, start, end, maxp, days in templates:
        embed.add_field(name=f"💾 {name}", value=f"Time: {start}-{end}\nSpots: {maxp}\nDays: {days}", inline=False)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name='apply-template', description='✨ Apply a template (admin)')
async def apply_template(interaction: discord.Interaction, name: str):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ No permission!", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute('SELECT start_time, end_time, max_people, days FROM slot_templates WHERE guild_id = ? AND name = ?',
                                  (interaction.guild_id, name))
        tpl = await cursor.fetchone()
        if not tpl:
            await interaction.followup.send(f"❌ Template '{name}' not found.", ephemeral=True); return
        start, end, maxp, days_str = tpl
        days = parse_days(days_str)
        for day in days:
            await db.execute('INSERT INTO slots (guild_id, start_time, end_time, max_people, day_of_week, is_active) VALUES (?, ?, ?, ?, ?, 1)',
                             (interaction.guild_id, start, end, maxp, day))
        await db.commit()
    await interaction.followup.send(f"✅ Template '{name}' applied — created {len(days)} slots.", ephemeral=True)

@bot.tree.command(name='delete-template', description='🗑️ Delete a template (admin)')
async def delete_template(interaction: discord.Interaction, name: str):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ No permission!", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute('DELETE FROM slot_templates WHERE guild_id = ? AND name = ?', (interaction.guild_id, name))
        if cursor.rowcount == 0:
            await interaction.followup.send("❌ Template not found.", ephemeral=True); return
        await db.commit()
    await interaction.followup.send(f"✅ Template '{name}' deleted.", ephemeral=True)

@bot.tree.command(name='mass-edit-slots', description='✏️ Mass edit slots by ID range (admin)')
async def mass_edit_slots(interaction: discord.Interaction, from_id: int, to_id: int, max_people: int = None, start_time: str = None, end_time: str = None):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ No permission!", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    updates, params = [], []
    if max_people is not None: updates.append("max_people = ?"); params.append(max_people)
    if start_time is not None: updates.append("start_time = ?"); params.append(start_time)
    if end_time is not None: updates.append("end_time = ?"); params.append(end_time)
    if not updates:
        await interaction.followup.send("❌ Nothing to update.", ephemeral=True); return
    params.extend([interaction.guild_id, from_id, to_id])
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute(f'UPDATE slots SET {", ".join(updates)} WHERE guild_id = ? AND id >= ? AND id <= ?', params)
        updated = cursor.rowcount
        await db.commit()
    await interaction.followup.send(f"✅ Updated **{updated}** slots in range ID {from_id}-{to_id}.", ephemeral=True)
    await update_slot_notification(interaction.guild_id)

@bot.tree.command(name='mass-edit-by-time', description='✏️ Mass edit slots by start time (admin)')
async def mass_edit_by_time(interaction: discord.Interaction, old_time: str, max_people: int = None, new_start_time: str = None, new_end_time: str = None):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ No permission!", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    updates, params = [], []
    if max_people is not None: updates.append("max_people = ?"); params.append(max_people)
    if new_start_time is not None: updates.append("start_time = ?"); params.append(new_start_time)
    if new_end_time is not None: updates.append("end_time = ?"); params.append(new_end_time)
    if not updates:
        await interaction.followup.send("❌ Nothing to update.", ephemeral=True); return
    params.extend([interaction.guild_id, old_time])
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute(f'UPDATE slots SET {", ".join(updates)} WHERE guild_id = ? AND start_time = ?', params)
        updated = cursor.rowcount
        await db.commit()
    await interaction.followup.send(f"✅ Updated **{updated}** slots with start time `{old_time}`.", ephemeral=True)
    await update_slot_notification(interaction.guild_id)

@bot.tree.command(name='mass-delete-slots', description='🗑️ Mass delete slots by day (admin)')
async def mass_delete_slots(interaction: discord.Interaction, day: int):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ No permission!", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute('SELECT id FROM slots WHERE guild_id = ? AND day_of_week = ?', (interaction.guild_id, day))
        slot_ids = [row[0] for row in await cursor.fetchall()]
        for sid in slot_ids:
            await db.execute('DELETE FROM bookings WHERE slot_id = ?', (sid,))
        cursor = await db.execute('DELETE FROM slots WHERE guild_id = ? AND day_of_week = ?', (interaction.guild_id, day))
        deleted = cursor.rowcount
        await db.commit()
    await interaction.followup.send(f"✅ Deleted **{deleted}** slots for {day_name(day)}.", ephemeral=True)
    await update_slot_notification(interaction.guild_id)

@bot.tree.command(name='edit-slot', description='✏️ Edit a single slot (admin)')
async def edit_slot(interaction: discord.Interaction, slot_id: int, max_people: int = None, start_time: str = None, end_time: str = None):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ No permission!", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    updates, params = [], []
    if max_people is not None: updates.append("max_people = ?"); params.append(max_people)
    if start_time is not None: updates.append("start_time = ?"); params.append(start_time)
    if end_time is not None: updates.append("end_time = ?"); params.append(end_time)
    if not updates:
        await interaction.followup.send("❌ Nothing to update.", ephemeral=True); return
    params.extend([slot_id, interaction.guild_id])
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute(f'UPDATE slots SET {", ".join(updates)} WHERE id = ? AND guild_id = ?', params)
        if cursor.rowcount == 0:
            await interaction.followup.send("❌ Slot not found.", ephemeral=True); return
        await db.commit()
    await interaction.followup.send(f"✅ Slot {slot_id} updated.", ephemeral=True)
    await update_slot_notification(interaction.guild_id)

@bot.tree.command(name='list-slots', description='📋 List all slots (admin)')
async def list_slots(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ No permission!", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    slots = await get_all_active_slots(interaction.guild_id)
    if not slots:
        await interaction.followup.send("📭 No slots.", ephemeral=True); return
    embed = discord.Embed(title="📋 All Slots", color=discord.Color.blue(), timestamp=now_tz())
    for sid, start, end, maxp, day, booked in slots[:25]:
        embed.add_field(name=f"ID:{sid} {day_name(day)} {start}-{end}", value=f"Booked: {booked}/{maxp}", inline=False)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name='delete-slot', description='🗑️ Delete a slot (admin)')
async def delete_slot(interaction: discord.Interaction, slot_id: int):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ No permission!", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect('shifts.db') as db:
        await db.execute('DELETE FROM bookings WHERE slot_id = ?', (slot_id,))
        cursor = await db.execute('DELETE FROM slots WHERE id = ? AND guild_id = ?', (slot_id, interaction.guild_id))
        if cursor.rowcount == 0:
            await interaction.followup.send("❌ Slot not found.", ephemeral=True); return
        await db.commit()
    await interaction.followup.send(f"✅ Slot {slot_id} deleted.", ephemeral=True)
    await update_slot_notification(interaction.guild_id)

@bot.tree.command(name='delete-shift', description='🗑️ Delete a specific shift by ID (admin)')
async def delete_shift(interaction: discord.Interaction, shift_id: int):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ No permission!", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute('SELECT user_id, start_time, is_active FROM shifts WHERE id = ? AND guild_id = ?',
                                  (shift_id, interaction.guild_id))
        shift = await cursor.fetchone()
        if not shift:
            await interaction.followup.send("❌ Shift not found.", ephemeral=True); return
        await db.execute('DELETE FROM shifts WHERE id = ?', (shift_id,))
        await db.commit()
    uid, start, active = shift
    status = "active" if active else "completed"
    await interaction.followup.send(f"✅ Shift `{shift_id}` ({status}) deleted.\nUser: <@{uid}>\nStart: `{start[:16]}`", ephemeral=True)

@bot.tree.command(name='delete-user-shifts', description='🗑️ Delete all shifts of a user (admin)')
async def delete_user_shifts(interaction: discord.Interaction, user: discord.Member, period: str = "all"):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ No permission!", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    query = "DELETE FROM shifts WHERE user_id = ? AND guild_id = ?"
    params = [user.id, interaction.guild_id]
    if period == "today":
        query += " AND date(start_time) = ?"; params.append(now_tz().date().isoformat())
    elif period == "week":
        query += " AND start_time >= ?"; params.append((now_tz() - timedelta(days=7)).isoformat())
    elif period == "month":
        query += " AND start_time >= ?"; params.append((now_tz() - timedelta(days=30)).isoformat())
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute(query, params)
        deleted = cursor.rowcount
        await db.commit()
    await interaction.followup.send(f"✅ Deleted **{deleted}** shifts for {user.mention} (period: `{period}`).", ephemeral=True)

@bot.tree.command(name='cancel-user-booking', description="❌ Cancel a user's booking (admin)")
async def cancel_user_booking(interaction: discord.Interaction, user: discord.Member, slot_id: int):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ No permission!", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute('UPDATE bookings SET status = "cancelled" WHERE user_id = ? AND slot_id = ? AND status = "booked"',
                                  (user.id, slot_id))
        if cursor.rowcount == 0:
            await interaction.followup.send("❌ Booking not found.", ephemeral=True); return
        await db.commit()
    await interaction.followup.send(f"✅ Cancelled booking of {user.mention} for slot `{slot_id}`.", ephemeral=True)
    await update_slot_notification(interaction.guild_id)

@bot.tree.command(name='send-slots-notification', description='📢 Send slot notification now (admin)')
async def send_notif(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ No permission!", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    await send_slot_notification()
    await interaction.followup.send("✅ Notification sent!", ephemeral=True)

@bot.tree.command(name='send-tomorrow-notification', description='📢 Send tomorrow notification now (admin)')
async def send_tomorrow_notif(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ No permission!", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    await send_tomorrow_notification()
    await interaction.followup.send("✅ Tomorrow notification sent!", ephemeral=True)

@bot.tree.command(name='test-report', description='🧪 Test monthly report (admin)')
async def test_rep(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ No permission!", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    now = now_tz(); month_start = now.replace(day=1, hour=0, minute=0, second=0)
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute('''SELECT user_id, username, COUNT(*), SUM(strftime('%s',end_time)-strftime('%s',start_time))
            FROM shifts WHERE guild_id IS NOT NULL AND is_active = 0 AND end_time IS NOT NULL AND start_time >= ?
            GROUP BY user_id, username ORDER BY 4 DESC''', (month_start.isoformat(),))
        stats = await cursor.fetchall()
    embed = discord.Embed(title=f"📊 Test Report - {month_start.strftime('%B %Y')}", color=discord.Color.gold(), timestamp=now_tz())
    if stats:
        total_s = sum(s[3] for s in stats if s[3])
        embed.add_field(name="📈 Summary", value=f"Employees: {len(stats)}\nShifts: {sum(s[2] for s in stats)}\nHours: {format_time(total_s)}", inline=False)
        medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
        for i, (uid, uname, sh, secs) in enumerate(stats[:10], 1):
            embed.add_field(name=f"{medals[i-1]} {uname}", value=f"{sh} shifts, {format_time(secs)}", inline=False)
    else:
        embed.description = "📭 No shifts this month"
    await interaction.followup.send(embed=embed)

# ==================== FORCE END SHIFTS (ADMIN) ====================
@bot.tree.command(name='force-end-shift', description='🔧 Принудительно завершить смену сотрудника (админ)')
async def force_end_shift(interaction: discord.Interaction, user: discord.Member):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Нет доступа!", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute(
            'SELECT id, start_time FROM shifts WHERE user_id = ? AND guild_id = ? AND is_active = 1',
            (user.id, interaction.guild_id))
        shift = await cursor.fetchone()
        if not shift:
            await interaction.followup.send(f"❌ У {user.mention} нет активной смены.", ephemeral=True)
            return
        shift_id, start_time = shift
        now = now_tz()
        duration = now - datetime.fromisoformat(start_time)
        duration_str = f"{int(duration.total_seconds()/3600)}h {int((duration.total_seconds()%3600)/60)}min"
        await db.execute(
            'UPDATE shifts SET end_time = ?, is_active = 0 WHERE id = ?',
            (now.isoformat(), shift_id))
        await db.commit()
    try:
        channel = bot.get_channel(REPORT_CHANNEL_ID)
        if channel:
            embed = discord.Embed(
                title="🔴 Shift Force Ended",
                description=f"**{user.name}**'s shift was ended by {interaction.user.mention}",
                color=discord.Color.dark_red(), timestamp=now_tz())
            embed.add_field(name="⏱️ Duration", value=duration_str, inline=True)
            embed.add_field(name="👮 Ended by", value=interaction.user.name, inline=True)
            await channel.send(embed=embed)
    except Exception as e:
        print(f"❌ Notification error: {e}")
    try:
        embed_dm = discord.Embed(
            title="🔴 Your shift was ended",
            description=f"Ваша смена была завершена администратором {interaction.user.name}.",
            color=discord.Color.red(), timestamp=now_tz())
        embed_dm.add_field(name="⏱️ Duration", value=duration_str, inline=True)
        await user.send(embed=embed_dm)
    except discord.Forbidden:
        pass
    except Exception as e:
        print(f"❌ DM error: {e}")
    embed = discord.Embed(
        title="✅ Shift Ended",
        description=f"Смена {user.mention} завершена.\n**Длительность:** {duration_str}",
        color=discord.Color.green(), timestamp=now_tz())
    embed.set_footer(text=f"Ended by: {interaction.user.name}")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name='force-end-all', description='🔧 Завершить ВСЕ активные смены на сервере (админ)')
async def force_end_all(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Нет доступа!", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute(
            'SELECT id, user_id, username, start_time FROM shifts WHERE guild_id = ? AND is_active = 1',
            (interaction.guild_id,))
        shifts = await cursor.fetchall()
        if not shifts:
            await interaction.followup.send("📭 Нет активных смен.", ephemeral=True)
            return
        now = now_tz()
        ended_users = []
        for shift_id, user_id, username, start_time in shifts:
            duration = now - datetime.fromisoformat(start_time)
            duration_str = f"{int(duration.total_seconds()/3600)}h {int((duration.total_seconds()%3600)/60)}min"
            await db.execute(
                'UPDATE shifts SET end_time = ?, is_active = 0 WHERE id = ?',
                (now.isoformat(), shift_id))
            ended_users.append((user_id, username, duration_str))
        await db.commit()
    try:
        channel = bot.get_channel(REPORT_CHANNEL_ID)
        if channel:
            embed = discord.Embed(
                title="🔴 All Shifts Force Ended",
                description=f"Завершено **{len(ended_users)}** смен администратором {interaction.user.mention}",
                color=discord.Color.dark_red(), timestamp=now_tz())
            user_list = []
            for uid, uname, dur in ended_users[:15]:
                user_list.append(f"👤 {uname} — {dur}")
            if len(ended_users) > 15:
                user_list.append(f"... и ещё {len(ended_users) - 15}")
            embed.add_field(name="👥 Ended shifts", value="\n".join(user_list), inline=False)
            await channel.send(embed=embed)
    except Exception as e:
        print(f"❌ Notification error: {e}")
    embed = discord.Embed(
        title="✅ All Shifts Ended",
        description=f"Завершено **{len(ended_users)}** смен.",
        color=discord.Color.green(), timestamp=now_tz())
    embed.set_footer(text=f"Ended by: {interaction.user.name}")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name='force-end-shift-id', description='🔧 Завершить смену по ID (админ)')
async def force_end_shift_id(interaction: discord.Interaction, shift_id: int):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Нет доступа!", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute(
            'SELECT id, user_id, username, start_time, is_active FROM shifts WHERE id = ? AND guild_id = ?',
            (shift_id, interaction.guild_id))
        shift = await cursor.fetchone()
        if not shift:
            await interaction.followup.send(f"❌ Смена `{shift_id}` не найдена.", ephemeral=True)
            return
        sid, user_id, username, start_time, is_active = shift
        if not is_active:
            await interaction.followup.send(f"⚠️ Смена `{shift_id}` уже завершена.", ephemeral=True)
            return
        now = now_tz()
        duration = now - datetime.fromisoformat(start_time)
        duration_str = f"{int(duration.total_seconds()/3600)}h {int((duration.total_seconds()%3600)/60)}min"
        await db.execute(
            'UPDATE shifts SET end_time = ?, is_active = 0 WHERE id = ?',
            (now.isoformat(), shift_id))
        await db.commit()
    await interaction.followup.send(
        f"✅ Смена `{shift_id}` завершена.\n"
        f"👤 Пользователь: <@{user_id}>\n"
        f"⏱️ Длительность: {duration_str}",
        ephemeral=True)

# --- BUTTON HANDLERS ---
@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type != discord.InteractionType.component: return
    cid = interaction.data.get('custom_id')
    if not cid: return
    try:
        if cid == 'start_shift_button':
            if await start_shift(interaction.user.id, interaction.user.name, interaction.guild_id):
                await interaction.response.defer()
                await interaction.followup.send("✅ Shift started!", ephemeral=True)
                await send_shift_start_notification(interaction.user.id, interaction.user.name, interaction.guild_id)
                await asyncio.sleep(1)
                await create_shift_panel(interaction, edit=True)
            else:
                await interaction.response.send_message("❌ Already on shift!", ephemeral=True)
        elif cid == 'end_shift_button':
            duration = await end_shift(interaction.user.id, interaction.guild_id)
            if duration:
                await interaction.response.defer()
                await interaction.followup.send(f"✅ Shift ended! **{duration}**", ephemeral=True)
                await send_shift_end_notification(interaction.user.id, interaction.user.name, duration, interaction.guild_id)
                await asyncio.sleep(1)
                await create_shift_panel(interaction, edit=True)
            else:
                await interaction.response.send_message("❌ No active shift!", ephemeral=True)
        elif cid == 'refresh_button':
            await interaction.response.defer()
            await create_shift_panel(interaction, edit=True)
    except discord.errors.InteractionResponded: pass
    except Exception as e: print(f"❌ on_interaction error: {e}")

# --- LAUNCH ---
if __name__ == '__main__':
    try: bot.run(TOKEN)
    except Exception as e: print(f"❌ Launch error: {e}")