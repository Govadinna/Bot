import discord
from discord.ext import commands
import os
from dotenv import load_dotenv
import asyncio

load_dotenv()

intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

CREATE_LOBBY_ID = int(os.getenv("CREATE_LOBBY_CHANNEL_ID"))
LOBBY_CATEGORY_ID = int(os.getenv("LOBBY_CATEGORY_ID"))
ANNOUNCE_CHANNEL_ID = int(os.getenv("ANNOUNCEMENT_CHANNEL_ID"))

lobby_messages = {}  # channel_id -> message_id

class PersistentJoinView(discord.ui.View):
    def __init__(self, lobby_channel_id: int):
        super().__init__(timeout=None)  # обязательно для персистентности
        self.lobby_channel_id = lobby_channel_id

        # Добавляем кнопку с фиксированным custom_id
        self.add_item(discord.ui.Button(
            label="Подключиться",
            style=discord.ButtonStyle.green,
            emoji="🔊",
            custom_id=f"join_lobby_{lobby_channel_id}"
        ))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        channel = bot.get_channel(self.lobby_channel_id)
        if not channel or len(channel.members) >= 5:
            await interaction.response.send_message("Лобби заполнено или удалено!", ephemeral=True)
            return False
        return True

@bot.event
async def on_ready():
    print(f"Бот {bot.user} онлайн и готов!")

    # Регистрируем все персистентные кнопки для существующих лобби
    category = bot.get_channel(LOBBY_CATEGORY_ID)
    if category:
        for voice_channel in category.voice_channels:
            if voice_channel.name.startswith("Лобби") and len(voice_channel.members) < 5:
                bot.add_view(PersistentJoinView(voice_channel.id))

    # Регистрируем обработчик для всех кнопок (один раз)
    @bot.event
    async def on_interaction(interaction: discord.Interaction):
        if not interaction.type == discord.InteractionType.component:
            return
        if not interaction.data or interaction.data.get("custom_id", "").startswith("join_lobby_"):
            return

        channel_id = int(interaction.data["custom_id"].split("_")[-1])
        channel = bot.get_channel(channel_id)
        if not channel or not isinstance(channel, discord.VoiceChannel):
            await interaction.response.send_message("Лобби больше не существует.", ephemeral=True)
            return

        if len(channel.members) >= 5:
            await interaction.response.send_message("Лобби уже заполнено!", ephemeral=True)
            return

        await interaction.user.move_to(channel)
        await interaction.response.defer()
        await update_lobby_message(channel)

async def update_lobby_message(channel: discord.VoiceChannel):
    members = channel.members
    free = 5 - len(members)
    color = discord.Color.green() if free > 0 else discord.Color.red()

    if members:
        participants = "\n".join(f"{i+1}. {m.mention}" for i, m in enumerate(members))
    else:
        participants = "Пока никого нет"

    embed = discord.Embed(title="Парти Гейм 208", color=color)
    embed.add_field(name="Участники:", value=participants, inline=False)
    embed.add_field(name="Доступ:", value="Любой ранг", inline=False)
    embed.add_field(name=" ", value=f"**+ {free}**" if free > 0 else "**Заполнено**", inline=False)

    announce = bot.get_channel(ANNOUNCE_CHANNEL_ID)

    if channel.id in lobby_messages:
        msg = await announce.fetch_message(lobby_messages[channel.id])
        view = PersistentJoinView(channel.id) if free > 0 else None
        await msg.edit(embed=embed, view=view)
    else:
        view = PersistentJoinView(channel.id) if free > 0 else None
        msg = await announce.send(embed=embed, view=view)
        lobby_messages[channel.id] = msg.id

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    # Создание лобби
    if after.channel and after.channel.id == CREATE_LOBBY_ID:
        category = bot.get_channel(LOBBY_CATEGORY_ID)
        new_lobby = await category.create_voice_channel(
            name=f"Лобби #{len([c for c in category.voice_channels if c.name.startswith('Лобби')]) + 1}",
            user_limit=5
        )
        await member.move_to(new_lobby)
        await update_lobby_message(new_lobby)

    # Удаление пустого лобби
    if before.channel and before.channel.category_id == LOBBY_CATEGORY_ID:
        if before.channel.name.startswith("Лобби") and len(before.channel.members) == 0:
            if before.channel.id in lobby_messages:
                msg = await bot.get_channel(ANNOUNCE_CHANNEL_ID).fetch_message(lobby_messages[before.channel.id])
                await msg.delete()
                del lobby_messages[before.channel.id]
            await before.channel.delete()

bot.run(os.getenv("TOKEN"))