import os
import threading
from flask import Flask

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running! 🚀"

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, use_reloader=False)

# Запускаем Flask в фоне
threading.Thread(target=run_flask, daemon=True).start()

import discord
from discord.ext import commands
import os
from dotenv import load_dotenv
import asyncio

load_dotenv()

# --- НАСТРОЙКИ INTENTS ---
intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
intents.message_content = True  # Исправляет предупреждение в логах

bot = commands.Bot(command_prefix="!", intents=intents)

CREATE_LOBBY_ID = int(os.getenv("CREATE_LOBBY_CHANNEL_ID"))
LOBBY_CATEGORY_ID = int(os.getenv("LOBBY_CATEGORY_ID"))
ANNOUNCE_CHANNEL_ID = int(os.getenv("ANNOUNCEMENT_CHANNEL_ID"))

lobby_messages = {}  # {voice_channel_id: message_id}

class JoinView(discord.ui.View):
    def __init__(self, lobby_id):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(
            label="Подключиться",
            style=discord.ButtonStyle.green,
            custom_id=f"join:{lobby_id}"
        ))

async def join_lobby(member: discord.Member, channel: discord.VoiceChannel):
    """Перемещает участника, если он находится в голосовом канале."""
    if member.voice:
        await member.move_to(channel)
    else:
        # Бот не может затянуть пользователя, если тот не в войсе.
        # channel.connect() подключает БОТА, а не человека, поэтому это мы убрали.
        pass 

async def update_message(channel: discord.VoiceChannel):
    # Проверка, существует ли канал (на случай быстрого удаления)
    if not channel.guild.get_channel(channel.id):
        return

    free = 5 - len(channel.members)
    color = discord.Color.green() if free > 0 else discord.Color.red()
    players = "\n".join(f"• {m.display_name}" for m in channel.members) or "Никого нет"
    status = f"Свободно: {free}/5" if free > 0 else "Заполнено"

    embed = discord.Embed(title=f"{channel.name}", color=color)
    embed.add_field(name="Игроки", value=players, inline=False)
    embed.add_field(name="Статус", value=status, inline=False)

    view = JoinView(channel.id) if free > 0 else None
    announce = bot.get_channel(ANNOUNCE_CHANNEL_ID)

    try:
        if channel.id in lobby_messages:
            try:
                msg = await announce.fetch_message(lobby_messages[channel.id])
                await msg.edit(embed=embed, view=view)
            except discord.NotFound:
                # Если сообщение удалено ручками, отправляем новое
                msg = await announce.send(embed=embed, view=view)
                lobby_messages[channel.id] = msg.id
        else:
            msg = await announce.send(embed=embed, view=view)
            lobby_messages[channel.id] = msg.id
    except Exception as e:
        print(f"Ошибка обновления сообщения: {e}")

@bot.event
async def on_interaction(interaction: discord.Interaction):
    if not interaction.data or interaction.data.get("component_type") != 2:
        return
    if not interaction.data["custom_id"].startswith("join:"):
        return

    channel_id = int(interaction.data["custom_id"].split(":")[1])
    channel = bot.get_channel(channel_id)
    
    # Проверяем, существует ли канал
    if not channel or not isinstance(channel, discord.VoiceChannel):
        return await interaction.response.send_message("Лобби больше не существует.", ephemeral=True)

    if len(channel.members) >= 5:
        return await interaction.response.send_message("Лобби уже заполнено!", ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    try:
        if interaction.user.voice:
            await join_lobby(interaction.user, channel)
            await interaction.followup.send("Ты в лобби!", ephemeral=True)
        else:
            await interaction.followup.send("Сначала зайди в любой голосовой канал!", ephemeral=True)
    except Exception as e:
        await interaction.followup.send("Ошибка перемещения.", ephemeral=True)
        print(e)

    await update_message(channel)

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    category = bot.get_channel(LOBBY_CATEGORY_ID)

    # --- СОЗДАНИЕ ЛОББИ ---
    if after and after.channel and after.channel.id == CREATE_LOBBY_ID:
        # Считаем только каналы, начинающиеся с "Лобби"
        voice_channels = [c for c in category.voice_channels if c.name.startswith("Лобби")]
        num = len(voice_channels) + 1
        
        try:
            lobby = await category.create_voice_channel(
                name=f"Лобби #{num}",
                user_limit=5
            )
            await join_lobby(member, lobby)
            await update_message(lobby)
        except Exception as e:
            print(f"Ошибка создания лобби: {e}")

    # --- УДАЛЕНИЕ ПУСТОГО ЛОББИ ---
    if before and before.channel and before.channel.category_id == LOBBY_CATEGORY_ID:
        # ВАЖНО: Не удаляем канал создания лобби
        if before.channel.id == CREATE_LOBBY_ID:
            return

        if before.channel.name.startswith("Лобби") and len(before.channel.members) == 0:
            # Удаляем сообщение об этом лобби
            if before.channel.id in lobby_messages:
                try:
                    msg_id = lobby_messages[before.channel.id]
                    msg = await bot.get_channel(ANNOUNCE_CHANNEL_ID).fetch_message(msg_id)
                    await msg.delete()
                except discord.NotFound:
                    pass # Сообщение уже удалено
                except Exception as e:
                    print(f"Ошибка удаления сообщения: {e}")
                finally:
                    if before.channel.id in lobby_messages:
                        del lobby_messages[before.channel.id]
            
            # Удаляем сам канал (с защитой от ошибки 404)
            try:
                await before.channel.delete()
            except discord.NotFound:
                pass # Канал уже удален (например, другим событием)
            except Exception as e:
                print(f"Ошибка удаления канала: {e}")

    # Обновляем статус старого лобби (если из него кто-то вышел, но оно не пустое)
    if before and before.channel and before.channel.category_id == LOBBY_CATEGORY_ID:
        if before.channel.id != CREATE_LOBBY_ID and len(before.channel.members) > 0:
             await update_message(before.channel)

@bot.event
async def on_ready():
    print(f"Бот {bot.user} запущен и работает!")
    category = bot.get_channel(LOBBY_CATEGORY_ID)
    if category:
        for ch in category.voice_channels:
            if ch.name.startswith("Лобби"):
                if len(ch.members) == 0:
                    # Очистка пустых лобби при перезапуске
                    await ch.delete()
                else:
                    # Восстановление кнопок для активных лобби
                    bot.add_view(JoinView(ch.id))
    print("Система лобби инициализирована.")

bot.run(os.getenv("TOKEN"))
