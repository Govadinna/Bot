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

intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
intents.message_content = False  # не нужен

bot = commands.Bot(command_prefix="!", intents=intents)

# === КОНФИГИ ===
CREATE_LOBBY_ID = int(os.getenv("CREATE_LOBBY_CHANNEL_ID"))      # голосовой канал "Создать лобби"
LOBBY_CATEGORY_ID = int(os.getenv("LOBBY_CATEGORY_ID"))          # категория для лобби
ANNOUNCE_CHANNEL_ID = int(os.getenv("ANNOUNCEMENT_CHANNEL_ID"))  # канал с объявлениями

# Храним сообщения с кнопками: {voice_channel_id: message_id}
lobby_messages = {}

# === Персистентная кнопка ===
class JoinLobbyButton(discord.ui.View):
    def __init__(self, lobby_channel_id: int):
        super().__init__(timeout=None)
        self.lobby_channel_id = lobby_channel_id

        self.add_item(discord.ui.Button(
            label="Подключиться",
            style=discord.ButtonStyle.green,
            emoji="mic",
            custom_id=f"join_lobby:{lobby_channel_id}"
        ))

# === Подключение к лобби (главная магия) ===
async def connect_member_to_lobby(member: discord.Member, lobby_channel: discord.VoiceChannel):
    """Подключает участника в лобби — даже если он не в голосе"""
    try:
        # Если уже в голосе — просто перемещаем
        if member.voice and member.voice.channel:
            await member.move_to(lobby_channel)
        else:
            # Если не в голосе — подключаем бота, Discord сам "затянет" юзера
            await lobby_channel.connect(timeout=10, reconnect=True)
    except discord.Forbidden:
        raise discord.Forbidden("Нет прав на перемещение или подключение")
    except Exception as e:
        print(f"[ОШИБКА ПОДКЛЮЧЕНИЯ] {e}")
        raise e

# === Обновление объявления ===
async def update_lobby_message(lobby_channel: discord.VoiceChannel):
    members = lobby_channel.members
    slots_free = 5 - len(members)
    color = discord.Color.green() if slots_free > 0 else discord.Color.red()

    participants = "\n".join([f"`{i+1}.` {m.display_name}" for i, m in enumerate(members)]) if members else "*Пусто*"
    status = f"**+ {slots_free} свободно**" if slots_free > 0 else "**Заполнено**"

    embed = discord.Embed(
        title=f"{lobby_channel.name}",
        description=f"**Игроки:**\n{participants}\n\n{status}",
        color=color
    )
    embed.set_footer(text="Нажми кнопку ниже, чтобы присоединиться")

    announce_channel = bot.get_channel(ANNOUNCE_CHANNEL_ID)

    view = JoinLobbyButton(lobby_channel.id) if slots_free > 0 else None

    if lobby_channel.id in lobby_messages:
        msg = await announce_channel.fetch_message(lobby_messages[lobby_channel.id])
        await msg.edit(embed=embed, view=view)
    else:
        msg = await announce_channel.send(embed=embed, view=view)
        lobby_messages[lobby_channel.id] = msg.id

# === Обработка нажатий на кнопку ===
@bot.event
async def on_interaction(interaction: discord.Interaction):
    if not interaction.data or interaction.data.get("component_type") != 2:  # не кнопка
        return

    custom_id = interaction.data.get("custom_id", "")
    if not custom_id.startswith("join_lobby:"):
        return

    channel_id = int(custom_id.split(":")[1])
    lobby_channel = bot.get_channel(channel_id)

    if not lobby_channel or not isinstance(lobby_channel, discord.VoiceChannel):
        await interaction.response.send_message("Лобби удалено.", ephemeral=True)
        return

    if len(lobby_channel.members) >= 5:
        await interaction.response.send_message("Лобби уже заполнено!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        await connect_member_to_lobby(interaction.user, lobby_channel)
        await interaction.followup.send(f"Ты в лобби {lobby_channel.name}!", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("Ошибка: у бота нет прав на перемещение или подключение.", ephemeral=True)
    except Exception:
        await interaction.followup.send("Не удалось подключить. Попробуй ещё раз.", ephemeral=True)

    await update_lobby_message(lobby_channel)

# === Создание и удаление лобби ===
@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    category = bot.get_channel(LOBBY_CATEGORY_ID)

    # === СОЗДАНИЕ ЛОББИ ===
    if after.channel and after.channel.id == CREATE_LOBBY_ID:
        lobby_num = len([c for c in category.voice_channels if c.name.startswith("Лобби")]) + 1
        new_lobby = await category.create_voice_channel(
            name=f"Лобби #{lobby_num}",
            user_limit=5,
            bitrate=96000
        )
        await connect_member_to_lobby(member, new_lobby)
        await update_lobby_message(new_lobby)

    # === УДАЛЕНИЕ ПУСТОГО ЛОББИ ===
    if before.channel and before.channel.category_id == LOBBY_CATEGORY_ID:
        if before.channel.name.startswith("Лобби") and len(before.channel.members) == 0:
            if before.channel.id in lobby_messages:
                msg = await bot.get_channel(ANNOUNCE_CHANNEL_ID).fetch_message(lobby_messages[before.channel.id])
                await msg.delete()
                del lobby_messages[before.channel.id]
            await before.channel.delete()

# === Запуск бота и восстановление кнопок ===
@bot.event
async def on_ready():
    print(f"{bot.user} онлайн и готов к работе!")

    # Восстанавливаем персистентные кнопки для всех существующих лобби
    category = bot.get_channel(LOBBY_CATEGORY_ID)
    if category:
        for channel in category.voice_channels:
            if channel.name.startswith("Лобби") and len(channel.members) < 5:
                bot.add_view(JoinLobbyButton(channel.id))

    print("Персистентные кнопки восстановлены.")

# === ЗАПУСК ===
bot.run(os.getenv("TOKEN"))
