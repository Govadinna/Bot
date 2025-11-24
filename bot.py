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

load_dotenv()

intents = discord.Intents.default()
intents.members = True
intents.voice_states = True

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
    if member.voice and member.voice.channel:
        await member.move_to(channel)
    else:
        await channel.connect()  # Discord сам затянет пользователя в канал

async def update_message(channel: discord.VoiceChannel):
    free = 5 - len(channel.members)
    color = discord.Color.green() if free > 0 else discord.Color.red()
    players = "\n".join(f"• {m.display_name}" for m in channel.members) or "Никого нет"
    status = f"Свободно: {free}/5" if free > 0 else "Заполнено"

    embed = discord.Embed(title=f"{channel.name}", color=color)
    embed.add_field(name="Игроки", value=players, inline=False)
    embed.add_field(name="Статус", value=status, inline=False)

    view = JoinView(channel.id) if free > 0 else None
    announce = bot.get_channel(ANNOUNCE_CHANNEL_ID)

    if channel.id in lobby_messages:
        msg = await announce.fetch_message(lobby_messages[channel.id])
        await msg.edit(embed=embed, view=view)
    else:
        msg = await announce.send(embed=embed, view=view)
        lobby_messages[channel.id] = msg.id

@bot.event
async def on_interaction(interaction: discord.Interaction):
    if not interaction.data or interaction.data.get("component_type") != 2:
        return
    if not interaction.data["custom_id"].startswith("join:"):
        return

    channel_id = int(interaction.data["custom_id"].split(":")[1])
    channel = bot.get_channel(channel_id)
    if not channel or not isinstance(channel, discord.VoiceChannel):
        return await interaction.response.send_message("Лобби удалено.", ephemeral=True)

    if len(channel.members) >= 5:
        return await interaction.response.send_message("Лобби уже заполнено!", ephemeral=True)

    await interaction.response.defer(ephemeral=True)

    try:
        await join_lobby(interaction.user, channel)
        await interaction.followup.send("Ты в лобби!", ephemeral=True)
    except:
        await interaction.followup.send("Ошибка подключения. Проверь права бота.", ephemeral=True)

    await update_message(channel)

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    category = bot.get_channel(LOBBY_CATEGORY_ID)

    # Создание лобби
    if after and after.channel and after.channel.id == CREATE_LOBBY_ID:
        num = len([c for c in category.voice_channels if c.name.startswith("Лобби")]) + 1
        lobby = await category.create_voice_channel(
            name=f"Лобби #{num}",
            user_limit=5
        )
        await join_lobby(member, lobby)
        await update_message(lobby)

    # Удаление пустого лобби
    if before and before.channel and before.channel.category_id == LOBBY_CATEGORY_ID:
        if before.channel.name.startswith("Лобби") and len(before.channel.members) == 0:
            if before.channel.id in lobby_messages:
                msg = await bot.get_channel(ANNOUNCE_CHANNEL_ID).fetch_message(lobby_messages[before.channel.id])
                await msg.delete()
                del lobby_messages[before.channel.id]
            await before.channel.delete()

@bot.event
async def on_ready():
    print(f"Бот {bot.user} запущен и работает!")
    category = bot.get_channel(LOBBY_CATEGORY_ID)
    if category:
        for ch in category.voice_channels:
            if ch.name.startswith("Лобби") and len(ch.members) < 5:
                bot.add_view(JoinView(ch.id))
    print("Кнопки восстановлены.")

bot.run(os.getenv("TOKEN"))
