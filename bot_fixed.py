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
    print("❌ Токен не найден!")
    exit()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

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
        return f"{int(hours)}ч {int((hours % 1) * 60)}мин"

# --- КНОПКИ (БЕЗ ДЕКОРАТОРОВ!) ---
class ShiftButtons(discord.ui.View):
    def __init__(self, is_active):
        super().__init__(timeout=60)
        
        if not is_active:
            self.add_item(discord.ui.Button(
                label="▶️ Начать смену",
                style=discord.ButtonStyle.success,
                custom_id="start_shift_button"
            ))
        else:
            self.add_item(discord.ui.Button(
                label="⏹️ Закончить смену",
                style=discord.ButtonStyle.danger,
                custom_id="end_shift_button"
            ))
        
        self.add_item(discord.ui.Button(
            label="🔄 Обновить",
            style=discord.ButtonStyle.secondary,
            custom_id="refresh_button"
        ))
    
    async def interaction_check(self, interaction: discord.Interaction):
        if interaction.user.id != interaction.message.interaction.user.id:
            await interaction.response.send_message("❌ Это не ваша панель!", ephemeral=True)
            return False
        return True

# --- КОМАНДЫ ---
@bot.event
async def on_ready():
    await init_db()
    try:
        synced = await bot.tree.sync()
        print(f'✅ Синхронизировано {len(synced)} команд')
    except Exception as e:
        print(f'❌ Ошибка: {e}')
    print(f'✅ Бот {bot.user} запущен!')

@bot.tree.command(name='shift', description='Управление сменами')
async def shift_panel(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        async with aiosqlite.connect('shifts.db') as db:
            cursor = await db.execute(
                'SELECT id FROM shifts WHERE user_id = ? AND guild_id = ? AND is_active = 1',
                (interaction.user.id, interaction.guild_id)
            )
            active_shift = await cursor.fetchone()
        
        active_users = await get_active_shifts(interaction.guild_id)
        
        embed = discord.Embed(title="📋 Управление сменами", color=discord.Color.blue())
        status = "🟢 На смене" if active_shift else "🔴 Не на смене"
        embed.add_field(name="Ваш статус", value=f"{status}\n{interaction.user.mention}", inline=False)
        
        if active_users:
            user_list = "\n".join([f"👤 {u[1]} (с {u[2][11:16]})" for u in active_users[:10]])
            embed.add_field(name=f"👥 На смене ({len(active_users)} чел.)", value=user_list, inline=False)
        else:
            embed.add_field(name="👥 На смене (0 чел.)", value="Сейчас никого нет на смене", inline=False)
        
        view = ShiftButtons(active_shift is not None)
        await interaction.followup.send(embed=embed, view=view)
    except Exception as e:
        await interaction.followup.send(f"❌ Ошибка: {str(e)}")

# --- ОБРАБОТЧИКИ КНОПОК (ОТДЕЛЬНО) ---
@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type != discord.InteractionType.component:
        return
    
    custom_id = interaction.data.get('custom_id')
    
    if custom_id == 'start_shift_button':
        await interaction.response.defer()
        try:
            success = await start_shift(
                interaction.user.id,
                interaction.user.name,
                interaction.guild_id
            )
            if success:
                embed = discord.Embed(
                    title="✅ Смена начата!",
                    color=discord.Color.green()
                )
                await interaction.edit_original_response(embed=embed, view=None)
                await asyncio.sleep(3)
                await shift_panel.callback(interaction)
            else:
                await interaction.followup.send("❌ У вас уже есть активная смена!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка: {str(e)}", ephemeral=True)
    
    elif custom_id == 'end_shift_button':
        await interaction.response.defer()
        try:
            duration = await end_shift(interaction.user.id, interaction.guild_id)
            if duration:
                embed = discord.Embed(
                    title="⏹️ Смена завершена!",
                    color=discord.Color.red()
                )
                await interaction.edit_original_response(embed=embed, view=None)
                await asyncio.sleep(3)
                await shift_panel.callback(interaction)
            else:
                await interaction.followup.send("❌ У вас нет активной смены!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка: {str(e)}", ephemeral=True)
    
    elif custom_id == 'refresh_button':
        await interaction.response.defer()
        try:
            await shift_panel.callback(interaction)
        except Exception as e:
            print(f"❌ Ошибка обновления: {e}")

# --- ЗАПУСК ---
if __name__ == '__main__':
    bot.run(TOKEN)