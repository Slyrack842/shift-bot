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

# --- DATABASE ---
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
                is_active INTEGER DEFAULT 1
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
            'INSERT INTO shifts (user_id, username, start_time, guild_id, is_active) VALUES (?, ?, ?, ?, 1)',
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

# --- STATUS UPDATE ---
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

# --- CREATE SHIFT PANEL ---
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

# --- BUTTONS ---
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

# --- COMMANDS ---
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

# --- MAIN COMMANDS ---
@bot.tree.command(name='shift', description='📋 Manage your shift')
async def shift_panel_command(interaction: discord.Interaction):
    await create_shift_panel(interaction, edit=False)

# --- UPDATED /ONSHIFT COMMAND WITH TIME ---
@bot.tree.command(name='onshift', description='👥 Who is on shift right now (with time)')
async def on_shift(interaction: discord.Interaction):
    """Shows everyone on shift with their time"""
    
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
            
            # Total stats
            total_hours = int(total_seconds / 3600)
            total_minutes = int((total_seconds % 3600) / 60)
            embed.set_footer(text=f"Total: {len(active_users)} people | Total time: {total_hours}h {total_minutes}min")
        else:
            embed.description = "🟢 No one is on shift right now"
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)

# --- MY STATS COMMAND ---
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

# --- NEW COMMAND: USER STATS ---
@bot.tree.command(name='user-stats', description='📊 Statistics of a specific employee')
async def user_stats(interaction: discord.Interaction, user: discord.Member):
    """Shows statistics of the selected user"""
    
    await interaction.response.defer(ephemeral=True)
    
    try:
        async with aiosqlite.connect('shifts.db') as db:
            # Get all completed shifts of the user
            cursor = await db.execute('''
                SELECT start_time, end_time FROM shifts 
                WHERE user_id = ? AND guild_id = ? AND is_active = 0
                ORDER BY start_time DESC
            ''', (user.id, interaction.guild_id))
            shifts = await cursor.fetchall()
            
            if not shifts:
                await interaction.followup.send(f"📭 User {user.mention} has no completed shifts.", ephemeral=True)
                return
            
            # Calculate statistics
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
            
            # Format time
            def format_time(seconds):
                if seconds < 60:
                    return f"{int(seconds)} sec"
                hours = int(seconds / 3600)
                minutes = int((seconds % 3600) / 60)
                if hours == 0:
                    return f"{minutes} min"
                if minutes == 0:
                    return f"{hours} h"
                return f"{hours} h {minutes} min"
            
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

# --- NEW COMMAND: TOP EMPLOYEES ---
@bot.tree.command(name='top', description='🏆 Top employees by hours worked')
async def top_employees(interaction: discord.Interaction):
    """Shows top 10 employees by total hours worked"""
    
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
            
            def format_time(seconds):
                if seconds is None or seconds == 0:
                    return "0 h"
                hours = int(seconds / 3600)
                minutes = int((seconds % 3600) / 60)
                if hours == 0:
                    return f"{minutes} min"
                if minutes == 0:
                    return f"{hours} h"
                return f"{hours} h {minutes} min"
            
            embed = discord.Embed(
                title="🏆 Top Employees by Hours Worked",
                color=discord.Color.gold(),
                timestamp=datetime.now()
            )
            
            medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
            
            for i, (user_id, username, shifts, seconds) in enumerate(stats):
                # Try to get nickname if user is on the server
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

# --- BUTTON HANDLERS ---
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

# --- LAUNCH ---
if __name__ == '__main__':
    try:
        bot.run(TOKEN)
    except Exception as e:
        print(f"❌ Launch error: {e}")