# --- УВЕДОМЛЕНИЕ О НАЧАЛЕ СМЕНЫ (РУССКИЙ) ---
async def send_shift_start_notification(user_id, username, guild_id):
    """Отправляет уведомление о начале смены в канал"""
    try:
        channel = bot.get_channel(REPORT_CHANNEL_ID)
        if channel:
            embed = discord.Embed(
                title="🟢 Смена начата",
                description=f"**{username}** начал(а) смену в {datetime.now().strftime('%H:%M')}",
                color=discord.Color.green(),
                timestamp=datetime.now()
            )
            embed.set_footer(text=f"ID пользователя: {user_id}")
            await channel.send(embed=embed)
            print(f"✅ Уведомление о начале отправлено для {username}")
    except Exception as e:
        print(f"❌ Ошибка отправки уведомления о начале: {e}")

# --- УВЕДОМЛЕНИЕ О КОНЦЕ СМЕНЫ (РУССКИЙ) ---
async def send_shift_end_notification(user_id, username, duration, guild_id):
    """Отправляет уведомление о завершении смены в канал"""
    try:
        channel = bot.get_channel(REPORT_CHANNEL_ID)
        if channel:
            embed = discord.Embed(
                title="🔴 Смена завершена",
                description=f"**{username}** завершил(а) смену",
                color=discord.Color.red(),
                timestamp=datetime.now()
            )
            embed.add_field(name="⏱️ Длительность", value=duration, inline=True)
            embed.set_footer(text=f"ID пользователя: {user_id}")
            await channel.send(embed=embed)
            print(f"✅ Уведомление о завершении отправлено для {username}")
    except Exception as e:
        print(f"❌ Ошибка отправки уведомления о завершении: {e}")
