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

# Запускаем Flask в отдельном потоке
threading.Thread(target=run_flask, daemon=True).start()

import discord
from discord.ext import commands
from dotenv import load_dotenv
import asyncio

load_dotenv()

intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# === ID каналов из .env ===
CREATE_LOBBY_ID = int(os.getenv("CREATE_LOBBY_CHANNEL_ID"))
LOBBY_CATEGORY_ID = int(os.getenv("LOBBY_CATEGORY_ID"))
ANNOUNCE_CHANNEL_ID = int(os.getenv("ANNOUNCEMENT_CHANNEL_ID"))

# Хранит сообщение в канале объявлений: channel_id → message_id
lobby_messages = {}

# === Персистентная кнопка "Подключиться" ===
class PersistentJoinView(discord.ui.View):
    def __init__(self, lobby_channel_id: int):
        super().__init__(timeout=None)  # timeout=None — обязательно для персистентности
        self.lobby_channel_id = lobby_channel_id

        self.add_item(discord.ui.Button(
            label="Подключиться",
            style=discord.ButtonStyle.green,
            emoji="🔊",
            custom_id=f"join_lobby_{lobby_channel_id}"
        ))

# === Универсальный обработчик всех кнопок (один на все лобби) ===
@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type != discord.InteractionType.component:
        return

    custom_id = interaction.data.get("custom_id", "")
    if not custom_id.startswith("join_lobby_"):
        return

    try:
        channel_id = int(custom_id.split("_")[-1])
    except:
        return

    channel = bot.get_channel(channel_id)
    if not channel or not isinstance(channel, discord.VoiceChannel):
        await interaction.response.send_message("❌ Лобби больше не существует.", ephemeral=True)
        return

    if len(channel.members) >= 5:
        await interaction.response.send_message("❌ Лобби уже заполнено!", ephemeral=True)
        return

    await interaction.user.move_to(channel)
    await interaction.response.defer()  # скрываем "взаимодействие не удалось"
    await update_lobby_message(channel)

# === Обновление/создание embed-сообщения в канале объявлений ===
async def update_lobby_message(channel: discord.VoiceChannel):
    members = channel.members
    free_slots = 5 - len(members)
    color = discord.Color.green() if free_slots > 0 else discord.Color.red()

    if members:
        participants = "\n".join(f"{i+1}. {m.mention}" for i, m in enumerate(members))
    else:
        participants = "Пока никого нет"

    embed = discord.Embed(title="🎮 Открытое лобби", color=color)
    embed.add_field(name="Участники:", value=participants, inline=False)
    embed.add_field(name="Свободно мест:", value=f"**+{free_slots}**" if free_slots > 0 else "**Заполнено**", inline=False)

    announce_channel = bot.get_channel(ANNOUNCE_CHANNEL_ID)

    # Если кнопка не нужна (заполнено) — view=None
    view = PersistentJoinView(channel.id) if free_slots > 0 else None

    if channel.id in lobby_messages:
        msg = await announce_channel.fetch_message(lobby_messages[channel.id])
        await msg.edit(embed=embed, view=view)
    else:
        msg = await announce_channel.send(embed=embed, view=view)
        lobby_messages[channel.id] = msg.id

# === Регистрация всех существующих лобби при старте бота ===
async def register_persistent_views():
    await bot.wait_until_ready()
    category = bot.get_channel(LOBBY_CATEGORY_ID)
    if not category:
        print("⚠️ Категория лобби не найдена! Проверь LOBBY_CATEGORY_ID в .env")
        return

    registered = 0
    for voice_channel in category.voice_channels:
        if voice_channel.name.startswith("Лобби") and len(voice_channel.members) < 5:
            bot.add_view(PersistentJoinView(voice_channel.id))
            registered += 1
    print(f"✅ Зарегистрировано персистентных кнопок: {registered}")

# Запускаем задачу регистрации сразу после подключения
bot.loop.create_task(register_persistent_views())

# === Бот готов ===
@bot.event
async def on_ready():
    print(f"🚀 Бот {bot.user} онлайн и готов к работе!")

# === Основная логика создания и удаления лобби ===
@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    # ——— Создание нового лобби ———
    if after.channel and after.channel.id == CREATE_LOBBY_ID:
        category = bot.get_channel(LOBBY_CATEGORY_ID)
        lobby_number = len([c for c in category.voice_channels if c.name.startswith("Лобби")]) + 1

        new_lobby = await category.create_voice_channel(
            name=f"Лобби #{lobby_number}",
            user_limit=5
        )
        await member.move_to(new_lobby)
        await update_lobby_message(new_lobby)

    # ——— Удаление пустого лобби ———
    if before.channel and before.channel.category_id == LOBBY_CATEGORY_ID:
        if before.channel.name.startswith("Лобби") and len(before.channel.members) == 0:
            # Удаляем сообщение в канале объявлений
            if before.channel.id in lobby_messages:
                announce = bot.get_channel(ANNOUNCE_CHANNEL_ID)
                msg = await announce.fetch_message(lobby_messages[before.channel.id])
                await msg.delete()
                del lobby_messages[before.channel.id]

            await before.channel.delete()

# === Запуск бота ===
bot.run(os.getenv("TOKEN"))
