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

bot = commands.Bot(command_prefix='!', intents=intents)

# --- НАСТРОЙКИ ---
REPORT_CHANNEL_ID = 1533758067513098408   # ID канала для уведомлений и отчётов
REPORT_USER_ID = 775396551936704533      # ID пользователя для отчётов

# Настройки напоминаний
REMINDER_HOURS = 4   # Через сколько часов отправлять первое напоминание
URGENT_HOURS = 8     # Через сколько часов отправлять срочное напоминание
CHECK_INTERVAL = 30  # Проверять каждые 30 минут

# --- БАЗА ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect('shifts.db') as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS shifts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                start_time TEXT,
                end_time TEXT,
                guild_id INTEGER,
                is_active INTEGER DEFAULT 1,
                last_reminder INTEGER DEFAULT 0
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        await db.commit()

async def get_active_shifts(guild_id):
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute(
            'SELECT user_id, username, start_time FROM shifts WHERE guild_id = ? AND is_active = 1',
            (guild_id,)
        )
        return await cursor.fetchall()

async def get_all_active_shifts():
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute(
            'SELECT user_id, username, start_time, guild_id, id FROM shifts WHERE is_active = 1'
        )
        return await cursor.fetchall()

async def start_shift(user_id, username, guild_id):
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute(
            'SELECT id FROM shifts WHERE user_id = ? AND guild_id = ? AND is_active = 1',
            (user_id, guild_id)
        )
        if await cursor.fetchone():
            return False
        now = datetime.now().isoformat()
        await db.execute(
            'INSERT INTO shifts (user_id, username, start_time, guild_id, is_active, last_reminder) VALUES (?, ?, ?, ?, 1, 0)',
            (user_id, username, now, guild_id)
        )
        await db.commit()
        return True

async def end_shift(user_id, guild_id):
    async with aiosqlite.connect('shifts.db') as db:
        cursor = await db.execute(
            'SELECT id, start_time FROM shifts WHERE user_id = ? AND guild_id = ? AND is_active = 1',
            (user_id, guild_id)
        )
        shift = await cursor.fetchone()
        if not shift:
            return None
        now = datetime.now().isoformat()
        duration = datetime.now() - datetime.fromisoformat(shift[1])
        await db.execute(
            'UPDATE shifts SET end_time = ?, is_active = 0 WHERE id = ?',
            (now, shift[0])
        )
        await db.commit()
        hours = duration.total_seconds() / 3600
        return f"{int(hours)}h {int((hours % 1) * 60)}min"

# --- ФУНКЦИЯ ДЛЯ ФОРМАТИРОВАНИЯ ВРЕМЕНИ ---
def format_time(seconds):
    if seconds is None or seconds == 0:
        return "0h 0min"
    hours = int(seconds / 3600)
    minutes = int((seconds % 3600) / 60)
    if hours == 0:
        return f"{minutes} min"
    if minutes == 0:
        return f"{hours} h"
    return f"{hours} h {minutes} min"

# --- УВЕДОМЛЕНИЕ О НАЧАЛЕ СМЕНЫ ---
async def send_shift_start_notification(user_id, username, guild_id):
    """Отправляет уведомление о начале смены в канал"""
    try:
        channel = bot.get_channel(REPORT_CHANNEL_ID)
        if channel:
            embed = discord.Embed(
                title="🟢 Shift Started",
                description=f"**{username}** started their shift at {datetime.now().strftime('%H:%M')}",
                color=discord.Color.green(),
                timestamp=datetime.now()
            )
            embed.set_footer(text=f"User ID: {user_id}")
            await channel.send(embed=embed)
            print(f"✅ Start notification sent for {username}")
    except Exception as e:
        print(f"❌ Error sending start notification: {e}")

# --- УВЕДОМЛЕНИЕ О КОНЦЕ СМЕНЫ ---
async def send_shift_end_notification(user_id, username, duration, guild_id):
    """Отправляет уведомление о завершении смены в канал"""
    try:
        channel = bot.get_channel(REPORT_CHANNEL_ID)
        if channel:
            embed = discord.Embed(
                title="🔴 Shift Ended",
                description=f"**{username}** ended their shift",
                color=discord.Color.red(),
                timestamp=datetime.now()
            )
            embed.add_field(name="⏱️ Duration", value=duration, inline=True)
            embed.set_footer(text=f"User ID: {user_id}")
            await channel.send(embed=embed)
            print(f"✅ End notification sent for {username}")
    except Exception as e:
        print(f"❌ Error sending end notification: {e}")

# --- НАПОМИНАНИЯ О ДЛИТЕЛЬНОЙ СМЕНЕ ---
async def check_long_shifts():
    """Проверяет активные смены и отправляет напоминания в ЛС"""
    await bot.wait_until_ready()
    
    while not bot.is_closed():
        try:
            active_shifts = await get_all_active_shifts()
            
            for user_id, username, start_time_str, guild_id, shift_id in active_shifts:
                start_time = datetime.fromisoformat(start_time_str)
                duration = datetime.now() - start_time
                hours = duration.total_seconds() / 3600
                
                async with aiosqlite.connect('shifts.db') as db:
                    cursor = await db.execute(
                        'SELECT last_reminder FROM shifts WHERE id = ?',
                        (shift_id,)
                    )
                    result = await cursor.fetchone()
                    last_reminder = result[0] if result else 0
                
                should_remind = False
                reminder_type = ""
                
                if hours >= URGENT_HOURS and last_reminder < 2:
                    should_remind = True
                    reminder_type = "urgent"
                elif hours >= REMINDER_HOURS and last_reminder == 0:
                    should_remind = True
                    reminder_type = "normal"
                
                if should_remind:
                    try:
                        user = await bot.fetch_user(user_id)
                        if user:
                            if reminder_type == "normal":
                                embed = discord.Embed(
                                    title="⏰ Shift Reminder",
                                    description=f"⚠️ You've been on shift for **{int(hours)} hours**!",
                                    color=discord.Color.orange(),
                                    timestamp=datetime.now()
                                )
                                embed.add_field(
                                    name="💡 Tip",
                                    value=f"You've been working for over {REMINDER_HOURS} hours. Make sure to take breaks!",
                                    inline=False
                                )
                                await user.send(embed=embed)
                                print(f"✅ Sent normal reminder to {username}")
                                
                            elif reminder_type == "urgent":
                                embed = discord.Embed(
                                    title="🚨 URGENT: Long Shift Warning",
                                    description=f"⚠️ You've been on shift for **{int(hours)} hours**!",
                                    color=discord.Color.red(),
                                    timestamp=datetime.now()
                                )
                                embed.add_field(
                                    name="💡 Important",
                                    value=f"This is a serious reminder! You've been working for over {URGENT_HOURS} hours. Please consider ending your shift or taking a longer break.",
                                    inline=False
                                )
                                await user.send(embed=embed)
                                print(f"✅ Sent urgent reminder to {username}")
                            
                            new_reminder_value = 2 if reminder_type == "urgent" else 1
                            async with aiosqlite.connect('shifts.db') as db:
                                await db.execute(
                                    'UPDATE shifts SET last_reminder = ? WHERE id = ?',
                                    (new_reminder_value, shift_id)
                                )
                                await db.commit()
                    except discord.Forbidden:
                        print(f"❌ Cannot send DM to {username} (DMs disabled)")
                    except Exception as e:
                        print(f"❌ Error sending reminder to {username}: {e}")
            
            await asyncio.sleep(CHECK_INTERVAL * 60)
            
        except Exception as e:
            print(f"❌ Error in check_long_shifts: {e}")
            await asyncio.sleep(60)

# --- ОБНОВЛЕНИЕ СТАТУСА ---
async def update_status():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            total_active = 0
            for guild in bot.guilds:
                active = await get_active_shifts(guild.id)
                total_active += len(active)
            activity = discord.Activity(
                type=discord.ActivityType.watching,
                name=f"{total_active} people on shift"
            )
            await bot.change_presence(activity=activity)
        except:
            pass
        await asyncio.sleep(30)

# --- ФУНКЦИЯ ДЛЯ ОТПРАВКИ ОТЧЁТА ---
async def send_monthly_report():
    """Отправляет отчёт за месяц в указанный канал"""
    await bot.wait_until_ready()
    
    await asyncio.sleep(10)
    
    while not bot.is_closed():
        try:
            now = datetime.now()
            
            is_last_day = now.day == 1 and now.hour == 0 and now.minute < 5
            
            if is_last_day:
                month_start = (now - timedelta(days=1)).replace(day=1, hour=0, minute=0, second=0)
                month_end = now.replace(day=1, hour=0, minute=0, second=0)
                
                async with aiosqlite.connect('shifts.db') as db:
                    cursor = await db.execute('''
                        SELECT 
                            user_id,
                            username,
                            COUNT(*) as shift_count,
                            SUM(strftime('%s', end_time) - strftime('%s', start_time)) as total_seconds
                        FROM shifts 
                        WHERE guild_id IS NOT NULL 
                        AND is_active = 0 
                        AND end_time IS NOT NULL
                        AND start_time >= ?
                        AND end_time <= ?
                        GROUP BY user_id, username
                        ORDER BY total_seconds DESC
                    ''', (month_start.isoformat(), month_end.isoformat()))
                    stats = await cursor.fetchall()
                    
                    await db.execute(
                        'DELETE FROM shifts WHERE end_time < ? AND is_active = 0',
                        (month_end.isoformat(),)
                    )
                    await db.commit()
                
                embed = discord.Embed(
                    title=f"📊 Monthly Report - {month_start.strftime('%B %Y')}",
                    color=discord.Color.gold(),
                    timestamp=datetime.now()
                )
                
                if stats:
                    total_employees = len(stats)
                    total_shifts = sum(s[2] for s in stats)
                    total_seconds = sum(s[3] for s in stats if s[3])
                    
                    embed.add_field(
                        name="📈 Summary",
                        value=f"Employees: {total_employees}\nTotal Shifts: {total_shifts}\nTotal Hours: {format_time(total_seconds)}",
                        inline=False
                    )
                    
                    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
                    top_list = []
                    for i, (user_id, username, shifts, seconds) in enumerate(stats[:10], 1):
                        try:
                            guild = bot.get_guild(1478824197357703281)
                            member = guild.get_member(user_id) if guild else None
                            display_name = member.display_name if member else username
                        except:
                            display_name = username
                        
                        top_list.append(f"{medals[i-1]} **{display_name}**\n   └ {shifts} shifts, {format_time(seconds)}")
                    
                    embed.add_field(
                        name="🏆 Top 10 Employees",
                        value="\n\n".join(top_list),
                        inline=False
                    )
                else:
                    embed.description = "📭 No shifts recorded this month"
                
                embed.set_footer(text="Monthly cleanup completed")
                
                try:
                    if REPORT_CHANNEL_ID:
                        channel = bot.get_channel(REPORT_CHANNEL_ID)
                        if channel:
                            await channel.send(embed=embed)
                    
                    if REPORT_USER_ID:
                        user = await bot.fetch_user(REPORT_USER_ID)
                        if user:
                            await user.send(embed=embed)
                except Exception as e:
                    print(f"❌ Error sending report: {e}")
                
                print(f"✅ Monthly report sent and old shifts deleted")
                
                await asyncio.sleep(86400)
            
            await asyncio.sleep(3600)
            
        except Exception as e:
            print(f"❌ Error in monthly report: {e}")
            await asyncio.sleep(3600)

# --- СОЗДАНИЕ ПАНЕЛИ ---
async def create_shift_panel(interaction: discord.Interaction, edit: bool = False):
    try:
        async with aiosqlite.connect('shifts.db') as db:
            cursor = await db.execute(
                'SELECT id FROM shifts WHERE user_id = ? AND guild_id = ? AND is_active = 1',
                (interaction.user.id, interaction.guild_id)
            )
            active_shift = await cursor.fetchone()
        
        active_users = await get_active_shifts(interaction.guild_id)
        
        embed = discord.Embed(
            title="📋 Shift Management",
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )
        
        status = "🟢 On Shift" if active_shift else "🔴 Off Shift"
        embed.add_field(
            name="Your Status",
            value=f"{status}\n{interaction.user.mention}",
            inline=False
        )
        
        if active_users:
            user_list = []
            for u in active_users:
                start = datetime.fromisoformat(u[2])
                duration = datetime.now() - start
                hours = int(duration.total_seconds() / 3600)
                minutes = int((duration.total_seconds() % 3600) / 60)
                user_list.append(f"👤 {u[1]} — {hours}h {minutes}min (since {u[2][11:16]})")
            
            if len(user_list) > 15:
                user_list = user_list[:15]
                user_list.append(f"... and {len(active_users) - 15} more")
            
            embed.add_field(
                name=f"👥 On Shift ({len(active_users)} people)",
                value="\n".join(user_list),
                inline=False
            )
        else:
            embed.add_field(
                name="👥 On Shift (0 people)",
                value="🟢 No one is on shift right now",
                inline=False
            )
        
        view = ShiftButtons(active_shift is not None, interaction.user.id)
        
        if edit:
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            
    except Exception as e:
        print(f"❌ Error creating panel: {e}")
        if not edit:
            await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)

# --- КНОПКИ ---
class ShiftButtons(discord.ui.View):
    def __init__(self, is_active, user_id):
        super().__init__(timeout=120)
        self.user_id = user_id
        
        if not is_active:
            self.add_item(discord.ui.Button(
                label="▶️ Start Shift",
                style=discord.ButtonStyle.success,
                custom_id="start_shift_button"
            ))
        else:
            self.add_item(discord.ui.Button(
                label="⏹️ End Shift",
                style=discord.ButtonStyle.danger,
                custom_id="end_shift_button"
            ))
        
        self.add_item(discord.ui.Button(
            label="🔄 Refresh",
            style=discord.ButtonStyle.secondary,
            custom_id="refresh_button"
        ))
    
    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This is not your panel!", ephemeral=True)
            return False
        return True

# --- КОМАНДЫ ---
@bot.event
async def on_ready():
    await init_db()
    try:
        synced = await bot.tree.sync()
        print(f'✅ Synced {len(synced)} commands')
        for cmd in synced:
            print(f'   - /{cmd.name}')
    except Exception as e:
        print(f'❌ Sync error: {e}')
    print(f'✅ Bot {bot.user} is ready!')
    print(f'📊 On servers: {len(bot.guilds)}')
    
    bot.loop.create_task(update_status())
    bot.loop.create_task(check_long_shifts())

# --- ОСНОВНЫЕ КОМАНДЫ ---
@bot.tree.command(name='shift', description='📋 Manage your shift')
async def shift_panel_command(interaction: discord.Interaction):
    await create_shift_panel(interaction, edit=False)

@bot.tree.command(name='onshift', description='👥 Who is on shift right now')
async def on_shift(interaction: discord.Interaction):
    try:
        active_users = await get_active_shifts(interaction.guild_id)
        
        embed = discord.Embed(
            title="👥 Who is on shift",
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )
        
        if active_users:
            user_list = []
            total_seconds = 0
            
            for u in active_users:
                start = datetime.fromisoformat(u[2])
                duration = datetime.now() - start
                seconds = duration.total_seconds()
                total_seconds += seconds
                
                hours = int(seconds / 3600)
                minutes = int((seconds % 3600) / 60)
                
                if hours > 0:
                    time_str = f"{hours}h {minutes}min"
                else:
                    time_str = f"{minutes}min"
                
                user_list.append(f"👤 {u[1]} — **{time_str}** (since {u[2][11:16]})")
            
            embed.description = "\n".join(user_list)
            
            total_hours = int(total_seconds / 3600)
            total_minutes = int((total_seconds % 3600) / 60)
            embed.set_footer(text=f"Total: {len(active_users)} people | Total time: {total_hours}h {total_minutes}min")
        else:
            embed.description = "🟢 No one is on shift right now"
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)

@bot.tree.command(name='stats', description='📊 My shift statistics')
async def my_stats(interaction: discord.Interaction):
    try:
        async with aiosqlite.connect('shifts.db') as db:
            today = datetime.now().date().isoformat()
            cursor = await db.execute(
                '''SELECT start_time, end_time FROM shifts 
                   WHERE user_id = ? AND guild_id = ? AND is_active = 0 
                   AND date(start_time) = ?''',
                (interaction.user.id, interaction.guild_id, today)
            )
            today_shifts = await cursor.fetchall()
            
            week_ago = (datetime.now() - timedelta(days=7)).isoformat()
            cursor = await db.execute(
                '''SELECT start_time, end_time FROM shifts 
                   WHERE user_id = ? AND guild_id = ? AND is_active = 0 
                   AND start_time > ?''',
                (interaction.user.id, interaction.guild_id, week_ago)
            )
            week_shifts = await cursor.fetchall()
        
        def calc_total(shifts):
            total = 0
            for s in shifts:
                if s[1]:
                    total += (datetime.fromisoformat(s[1]) - datetime.fromisoformat(s[0])).total_seconds() / 3600
            return total
        
        embed = discord.Embed(
            title=f"📊 Statistics for {interaction.user.name}",
            color=discord.Color.gold(),
            timestamp=datetime.now()
        )
        embed.add_field(
            name="📅 Today",
            value=f"{calc_total(today_shifts):.1f} hours\n({len(today_shifts)} shifts)",
            inline=True
        )
        embed.add_field(
            name="📅 This Week",
            value=f"{calc_total(week_shifts):.1f} hours\n({len(week_shifts)} shifts)",
            inline=True
        )
        embed.set_footer(text=f"ID: {interaction.user.id}")
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)

@bot.tree.command(name='user-stats', description='📊 Statistics of a specific employee')
async def user_stats(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)
    
    try:
        async with aiosqlite.connect('shifts.db') as db:
            cursor = await db.execute('''
                SELECT start_time, end_time FROM shifts 
                WHERE user_id = ? AND guild_id = ? AND is_active = 0
                ORDER BY start_time DESC
            ''', (user.id, interaction.guild_id))
            shifts = await cursor.fetchall()
            
            if not shifts:
                await interaction.followup.send(f"📭 User {user.mention} has no completed shifts.", ephemeral=True)
                return
            
            total_seconds = 0
            today_seconds = 0
            week_seconds = 0
            month_seconds = 0
            
            today = datetime.now().date()
            week_ago = datetime.now() - timedelta(days=7)
            month_ago = datetime.now() - timedelta(days=30)
            
            for start_str, end_str in shifts:
                start = datetime.fromisoformat(start_str)
                end = datetime.fromisoformat(end_str)
                duration = (end - start).total_seconds()
                
                total_seconds += duration
                
                if start.date() == today:
                    today_seconds += duration
                if start >= week_ago:
                    week_seconds += duration
                if start >= month_ago:
                    month_seconds += duration
            
            embed = discord.Embed(
                title=f"📊 Statistics for {user.display_name}",
                color=discord.Color.blue(),
                timestamp=datetime.now()
            )
            embed.set_thumbnail(url=user.display_avatar.url)
            
            embed.add_field(
                name="📅 Total Shifts",
                value=f"**{len(shifts)}** shifts",
                inline=False
            )
            
            embed.add_field(
                name="⏱️ Total Time",
                value=format_time(total_seconds),
                inline=True
            )
            embed.add_field(
                name="📅 Today",
                value=format_time(today_seconds),
                inline=True
            )
            embed.add_field(
                name="📅 This Week",
                value=format_time(week_seconds),
                inline=True
            )
            embed.add_field(
                name="📅 This Month",
                value=format_time(month_seconds),
                inline=True
            )
            
            embed.set_footer(text=f"Requested by: {interaction.user.name}")
            await interaction.followup.send(embed=embed)
            
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

@bot.tree.command(name='top', description='🏆 Top employees by hours worked')
async def top_employees(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    try:
        async with aiosqlite.connect('shifts.db') as db:
            cursor = await db.execute('''
                SELECT 
                    user_id,
                    username,
                    COUNT(*) as shift_count,
                    SUM(strftime('%s', end_time) - strftime('%s', start_time)) as total_seconds
                FROM shifts 
                WHERE guild_id = ? AND is_active = 0 AND end_time IS NOT NULL
                GROUP BY user_id, username
                ORDER BY total_seconds DESC
                LIMIT 10
            ''', (interaction.guild_id,))
            stats = await cursor.fetchall()
            
            if not stats:
                await interaction.followup.send("📭 No shift data available.", ephemeral=True)
                return
            
            embed = discord.Embed(
                title="🏆 Top Employees by Hours Worked",
                color=discord.Color.gold(),
                timestamp=datetime.now()
            )
            
            medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
            
            for i, (user_id, username, shifts, seconds) in enumerate(stats):
                try:
                    member = interaction.guild.get_member(user_id)
                    display_name = member.display_name if member else username
                except:
                    display_name = username
                
                embed.add_field(
                    name=f"{medals[i]} {display_name}",
                    value=f"📊 {shifts} shifts | ⏱️ {format_time(seconds)}",
                    inline=False
                )
            
            embed.set_footer(text=f"Requested by: {interaction.user.name}")
            await interaction.followup.send(embed=embed)
            
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

@bot.tree.command(name='test-report', description='🧪 Test monthly report (admin only)')
async def test_report(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ You don't have permission! Admin only.", ephemeral=True)
        return
    
    await interaction.response.defer(ephemeral=True)
    
    try:
        now = datetime.now()
        month_start = now.replace(day=1, hour=0, minute=0, second=0)
        month_end = now
        
        async with aiosqlite.connect('shifts.db') as db:
            cursor = await db.execute('''
                SELECT 
                    user_id,
                    username,
                    COUNT(*) as shift_count,
                    SUM(strftime('%s', end_time) - strftime('%s', start_time)) as total_seconds
                FROM shifts 
                WHERE guild_id IS NOT NULL 
                AND is_active = 0 
                AND end_time IS NOT NULL
                AND start_time >= ?
                AND end_time <= ?
                GROUP BY user_id, username
                ORDER BY total_seconds DESC
            ''', (month_start.isoformat(), month_end.isoformat()))
            stats = await cursor.fetchall()
        
        embed = discord.Embed(
            title=f"📊 Test Report - {month_start.strftime('%B %Y')}",
            color=discord.Color.gold(),
            timestamp=datetime.now()
        )
        
        if stats:
            total_employees = len(stats)
            total_shifts = sum(s[2] for s in stats)
            total_seconds = sum(s[3] for s in stats if s[3])
            
            embed.add_field(
                name="📈 Summary",
                value=f"Employees: {total_employees}\nTotal Shifts: {total_shifts}\nTotal Hours: {format_time(total_seconds)}",
                inline=False
            )
            
            medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
            top_list = []
            for i, (user_id, username, shifts, seconds) in enumerate(stats[:10], 1):
                try:
                    guild = bot.get_guild(interaction.guild_id)
                    member = guild.get_member(user_id) if guild else None
                    display_name = member.display_name if member else username
                except:
                    display_name = username
                
                top_list.append(f"{medals[i-1]} **{display_name}**\n   └ {shifts} shifts, {format_time(seconds)}")
            
            embed.add_field(
                name="🏆 Top 10 Employees",
                value="\n\n".join(top_list),
                inline=False
            )
        else:
            embed.description = "📭 No shifts recorded this month"
        
        embed.set_footer(text="Test report (no data deleted)")
        
        await interaction.followup.send(embed=embed)
        print(f"✅ Test report sent to {interaction.user.name}")
        
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {str(e)}")

# --- ОБРАБОТЧИКИ КНОПОК ---
@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type != discord.InteractionType.component:
        return
    
    custom_id = interaction.data.get('custom_id')
    
    try:
        if custom_id == 'start_shift_button':
            success = await start_shift(
                interaction.user.id,
                interaction.user.name,
                interaction.guild_id
            )
            if success:
                await interaction.response.defer()
                embed = discord.Embed(
                    title="✅ Shift Started!",
                    description=f"{interaction.user.mention} started shift at {datetime.now().strftime('%H:%M')}",
                    color=discord.Color.green()
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                
                # Отправляем уведомление в канал
                await send_shift_start_notification(
                    interaction.user.id,
                    interaction.user.name,
                    interaction.guild_id
                )
                
                await asyncio.sleep(2)
                await create_shift_panel(interaction, edit=True)
            else:
                await interaction.response.send_message("❌ You already have an active shift!", ephemeral=True)
        
        elif custom_id == 'end_shift_button':
            duration = await end_shift(interaction.user.id, interaction.guild_id)
            if duration:
                await interaction.response.defer()
                embed = discord.Embed(
                    title="⏹️ Shift Ended!",
                    description=f"{interaction.user.mention} worked for **{duration}**",
                    color=discord.Color.red()
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                
                # Отправляем уведомление в канал
                await send_shift_end_notification(
                    interaction.user.id,
                    interaction.user.name,
                    duration,
                    interaction.guild_id
                )
                
                await asyncio.sleep(2)
                await create_shift_panel(interaction, edit=True)
            else:
                await interaction.response.send_message("❌ You don't have an active shift!", ephemeral=True)
        
        elif custom_id == 'refresh_button':
            await interaction.response.defer()
            await create_shift_panel(interaction, edit=True)
    
    except discord.errors.InteractionResponded:
        pass
    except Exception as e:
        print(f"❌ Error in on_interaction: {e}")
        try:
            await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)
        except:
            pass

# --- ЗАПУСК ---
if __name__ == '__main__':
    try:
        bot.run(TOKEN)
    except Exception as e:
        print(f"❌ Launch error: {e}")