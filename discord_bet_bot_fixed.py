import os
import threading
from flask import Flask

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running!"

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_flask).start()

import os
import time
from typing import Optional, List, Tuple
import discord
from discord.ext import commands
from discord import app_commands
from supabase import create_client, Client
import logging
import asyncio
from dotenv import load_dotenv
from functools import wraps
from discord.ui import View, Button, Modal, TextInput
import random
import aiohttp
import re
# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Explicitly load .env file from the script's directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
dotenv_path = os.path.join(BASE_DIR, '.env')

if os.path.exists(dotenv_path):
    load_dotenv(dotenv_path)
    logger.info(f".env loaded from {dotenv_path}")
else:
    logger.warning(f".env file not found at {dotenv_path}. Relying on environment variables.")

# Environment variables (теперь без exit, если не заданы — логгер предупредит)
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
DEFAULT_BURN = float(os.getenv("DEFAULT_BURN", "0.25"))
TEAM_ANNOUNCEMENT_CHANNEL = os.getenv("TEAM_ANNOUNCEMENT_CHANNEL")

# Добавь проверки без exit — пусть бот запустится, но выдаст ошибку в логах
if not DISCORD_BOT_TOKEN:
    logger.error("DISCORD_BOT_TOKEN environment variable is missing or empty")
    # Не exit, а просто не запусти бот: bot.run не вызовется ниже
if not SUPABASE_URL or not SUPABASE_KEY:
    logger.error("SUPABASE_URL or SUPABASE_KEY environment variable is missing or empty")

# Initialize Supabase client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Discord bot setup
INTENTS = discord.Intents.default()
INTENTS.message_content = False
INTENTS.members = True
bot = commands.Bot(command_prefix="!", intents=INTENTS)
balance_lock = asyncio.Lock()

def safe_int(value, min_value=0, max_value=2**63 - 1):
    try:
        val = int(value)
        if val < min_value:
            raise ValueError(f"Value too small (min {min_value})")
        if val > max_value:
            raise ValueError("Value too large")
        return val
    except (ValueError, TypeError):
        raise ValueError("Invalid integer value")
    
def admin_only(func):
    """Декоратор для проверки, что команда выполняется только админом на сервере."""
    @wraps(func)
    async def wrapper(interaction: discord.Interaction, *args, **kwargs):
        if interaction.guild is None:
            await interaction.response.send_message("❌ Эта команда работает только на сервере.", ephemeral=True)
            return
        if not interaction.user.guild_permissions.manage_guild:
            await interaction.response.send_message("❌ У вас нет прав для этой команды.", ephemeral=True)
            return
        return await func(interaction, *args, **kwargs)
    return wrapper

# ---------------------- DB HELPERS ----------------------

def get_rank_emoji(mmr: int) -> str:
    """Возвращает эмодзи + название ранга по MMR (диапазоны Dota 2)."""
    if mmr == 0 or mmr < 770:
        return "🐛 Herald"  # Червяк для Herald (Tango)
    elif 770 <= mmr < 1540:
        return "🛡️ Guardian"  # Щит для Guardian
    elif 1540 <= mmr < 2310:
        return "⚔️ Crusader"  # Меч для Crusader (твой ранг!)
    elif 2310 <= mmr < 3080:
        return "🏛️ Archon"  # Колонна для Archon
    elif 3080 <= mmr < 3850:
        return "👑 Legend"  # Корона для Legend
    elif 3850 <= mmr < 4620:
        return "🏺 Ancient"  # Ваза для Ancient
    elif 4620 <= mmr < 6000:
        return "✨ Divine"  # Звезда для Divine
    else:
        return "☠️ Immortal"  # Череп для Immortal (элита)
    
def get_rank_emoji_from_tier(rank_tier: int) -> str:
    """Возвращает эмодзи + ранг по rank_tier из OpenDota (Dota 2, 2025)."""
    if rank_tier == 0:
        return "❓ Unranked"
    
    # Mapping по тьерам (Herald 10-14, Guardian 20-24, etc.)
    if 10 <= rank_tier <= 14:  # Herald
        sub = rank_tier - 9  # 10=1, 11=2, ...
        return f"🐛 Herald {sub}"
    elif 20 <= rank_tier <= 24:  # Guardian
        sub = rank_tier - 19
        return f"🛡️ Guardian {sub}"
    elif 30 <= rank_tier <= 34:  # Crusader (твой ~32)
        sub = rank_tier - 29
        return f"⚔️ Crusader {sub}"
    elif 40 <= rank_tier <= 44:  # Archon
        sub = rank_tier - 39
        return f"🏛️ Archon {sub}"
    elif 50 <= rank_tier <= 54:  # Legend
        sub = rank_tier - 49
        return f"👑 Legend {sub}"
    elif 60 <= rank_tier <= 64:  # Ancient
        sub = rank_tier - 59
        return f"🏺 Ancient {sub}"
    elif 70 <= rank_tier <= 74:  # Divine
        sub = rank_tier - 69
        return f"✨ Divine {sub}"
    elif rank_tier >= 80:  # Immortal
        return "☠️ Immortal"
    else:
        return "❓ Unknown Rank"

async def get_rank_tier_from_steamid(steam_id: str) -> Optional[int]:
    """Тянет rank_tier из OpenDota API."""
    try:
        # Парсим SteamID в 64-bit (тот же код, что в get_mmr_from_steamid)
        if steam_id.startswith("STEAM_0:"):
            parts = re.match(r'STEAM_0:(\d):(\d+)', steam_id)
            if parts:
                universe = 1  # Default
                auth = int(parts.group(1))
                account = int(parts.group(2))
                steamid64 = 76561197960265728 + (account * 2) + auth
            else:
                return None
        else:
            # Assume 64-bit
            steamid64 = int(steam_id)
        
        account_id = steamid64 - 76561197960265728
        if account_id <= 0:
            return None
        
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.opendota.com/api/players/{account_id}") as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data.get("rank_tier")
    except Exception as e:
        logger.error(f"Error fetching rank_tier for {steam_id}: {e}")
        return None

async def get_mmr_from_steamid(steam_input: str, user_id: Optional[int] = None) -> Optional[int]:
    """Парсит MMR из OpenDota API по SteamID или account ID, и обновляет БД если user_id предоставлен."""
    try:
        steam_id = steam_input.strip()
        
        # Новый парсинг: поддержка account ID (digits, <2^32), STEAM_0 или 64-bit
        if re.match(r'^\d{7,10}$', steam_id):  # Просто account ID (e.g., 933834754)
            account_id = int(steam_id)
            steamid64 = 76561197960265728 + account_id  # Конверт в 64-bit (предполагаем STEAM_0:0)
            logger.info(f"Converted account_id {account_id} to Steam64: {steamid64}")
        elif steam_id.startswith("STEAM_0:"):
            parts = re.match(r'STEAM_0:(\d):(\d+)', steam_id)
            if parts:
                auth = int(parts.group(1))
                account = int(parts.group(2))
                steamid64 = 76561197960265728 + (account * 2) + auth
            else:
                logger.warning(f"Invalid STEAM_0 format: {steam_id}")
                return None
        else:
            # Assume 64-bit
            try:
                steamid64 = int(steam_id)
            except ValueError:
                logger.warning(f"Invalid SteamID: {steam_id}")
                return None
        
        account_id = steamid64 - 76561197960265728
        if account_id <= 0:
            logger.warning(f"Invalid account_id: {account_id} from {steam_id}")
            return None
        
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.opendota.com/api/players/{account_id}") as resp:
                if resp.status != 200:
                    logger.warning(f"API error {resp.status} for account {account_id}")
                    return None
                data = await resp.json()
                
                mmr = data.get("solo_mmr")
                rank_tier = data.get("rank_tier")
                logger.info(f"API data for {steam_id}: solo_mmr={mmr}, rank_tier={rank_tier}")

                if mmr is not None:
                    # Сохраняем solo_mmr и tier
                    if user_id:
                        update_data = {"mmr": int(mmr), "steam_id": steam_id}  # Сохраняем оригинальный input
                        if rank_tier is not None:
                            update_data["rank_tier"] = rank_tier
                        response = supabase.table("users").update(update_data).eq("user_id", str(user_id)).execute()
                        if response.data:  # Check response
                            logger.info(f"Updated DB for {user_id}: mmr={mmr}, rank_tier={rank_tier}")
                        else:
                            logger.warning(f"DB update failed for {user_id}")
                    return int(mmr)
                elif rank_tier is not None:
                    approx_mmr = _get_approx_mmr_from_rank_tier(rank_tier)
                    if approx_mmr:
                        if user_id:
                            update_data = {"mmr": approx_mmr, "steam_id": steam_id}
                            update_data["rank_tier"] = rank_tier
                            response = supabase.table("users").update(update_data).eq("user_id", str(user_id)).execute()
                            if response.data:
                                logger.info(f"Updated DB for {user_id}: approx_mmr={approx_mmr}, rank_tier={rank_tier}")
                            else:
                                logger.warning(f"DB update failed for {user_id}")
                        return approx_mmr
                else:
                    logger.warning(f"No MMR data for {steam_id}")
                    if user_id:
                        # Сохраняем только steam_id, даже без MMR
                        response = supabase.table("users").update({"steam_id": steam_id}).eq("user_id", str(user_id)).execute()
                return None
    except Exception as e:
        logger.error(f"Error fetching MMR for {steam_input}: {e}")
        return None
    
def _get_approx_mmr_from_rank_tier(rank_tier: int) -> Optional[int]:
    """Возвращает средний MMR для rank_tier (по диапазонам Dota 2, 2025)."""
    if rank_tier == 0:
        return 0  # Unranked
    
    # Mapping rank_tier -> tier/sub (OpenDota: Crusader 30-34, etc.)
    if 10 <= rank_tier <= 14:  # Herald
        base, step = 0, 192
        sub = rank_tier - 10
    elif 20 <= rank_tier <= 24:  # Guardian
        base, step = 770, 153
        sub = rank_tier - 20
    elif 30 <= rank_tier <= 34:  # Crusader
        base, step = 1540, 153
        sub = rank_tier - 30  # 0=1, 1=2 (31), 2=3 (32)
    elif 40 <= rank_tier <= 44:  # Archon
        base, step = 2310, 153
        sub = rank_tier - 40
    elif 50 <= rank_tier <= 54:  # Legend
        base, step = 3080, 153
        sub = rank_tier - 50
    elif 60 <= rank_tier <= 64:  # Ancient
        base, step = 3850, 153
        sub = rank_tier - 60
    elif 70 <= rank_tier <= 74:  # Divine
        base, step = 4620, 400
        sub = rank_tier - 70
    elif rank_tier >= 80:  # Immortal
        return 6000  # Approx mid Immortal
    else:
        return None
    
    # Mid-range: base + (sub + 0.5) * step
    mid_mmr = base + (sub + 0.5) * step
    return int(round(mid_mmr))

async def get_free_dota_account() -> Optional[int]:
    """Get ID of a free Dota account."""
    try:
        response = supabase.table("dota_accounts").select("id").eq("busy", False).limit(1).execute()
        return int(response.data[0]["id"]) if response.data else None
    except Exception as e:
        logger.error(f"Error getting free account: {e}")
        return None

async def remove_team_role_from_all(guild: discord.Guild, team_name: str):
    """Снять роль у всех участников и удалить её"""
    role = discord.utils.get(guild.roles, name=team_name)
    if role:
        for member in role.members:
            try:
                await member.remove_roles(role)
            except discord.Forbidden:
                print(f"⚠️ Не могу убрать роль {team_name} у {member}")
        try:
            await role.delete(reason="Удаление команды")
        except discord.Forbidden:
            print(f"⚠️ Нет прав на удаление роли {team_name}")
async def remove_team_role(guild, member, team_name):
    role = discord.utils.get(guild.roles, name=team_name)
    if role is None:
        print(f"Role {team_name} not found in guild {guild.name}")
        return
    try:
        await member.remove_roles(role)
        print(f"Removed role {team_name} from {member.id}")
    except discord.Forbidden:
        print(f"Bot lacks permissions to remove role {team_name} from {member.id}")
    except Exception as e:
        print(f"Error removing role: {e}")

async def ensure_team_role(guild: discord.Guild, team_name: str) -> discord.Role:
    """Найти или создать роль для команды с рандомным цветом и отдельным отображением в списке участников"""
    role = discord.utils.get(guild.roles, name=team_name)
    if not role:
        rand_color = discord.Color(random.randint(0x000000, 0xFFFFFF))
        try:
            role = await guild.create_role(name=team_name, colour=rand_color, hoist=True)
        except discord.Forbidden:
            print(f"⚠️ Нет прав на создание роли {team_name}")
            return None
    return role


async def assign_team_role(guild, member, team_name):
    role = discord.utils.get(guild.roles, name=team_name)
    if role:
        try:
            await member.add_roles(role)
            print(f"Assigned role {team_name} to {member.id}")
        except discord.Forbidden:
            print("Bot lacks permissions to assign roles")
        except Exception as e:
            print(f"Error assigning role: {e}")
    else:
        print(f"Role {team_name} not found")

async def ensure_user(user_id: int, name: str = None):
    """Ensure a user exists in the database with a default balance and last duel time of 0."""
    try:
        response = await asyncio.to_thread(
            supabase.table("users").select("*").eq("user_id", str(user_id)).execute
        )
        if not response.data:
            await asyncio.to_thread(
                supabase.table("users").insert({
                    "user_id": str(user_id),
                    "name": name,  # сохраняем никнейм
                    "balance": 0,
                    "last_duel_time": 0
                }).execute
            )
        else:
            # Обновляем никнейм, если он изменился
            if name and response.data[0].get("name") != name:
                await asyncio.to_thread(
                    supabase.table("users").update({"name": name}).eq("user_id", str(user_id)).execute
                )
    except Exception as e:
        logger.error(f"Error ensuring user {user_id}: {e}")
        raise

async def get_balance(user_id: int) -> int:
    """Get the balance of a user."""
    await ensure_user(user_id)
    try:
        response = await asyncio.to_thread(
            supabase.table("users").select("balance").eq("user_id", str(user_id)).execute
        )
        return int(response.data[0]["balance"]) if response.data else 0
    except Exception as e:
        logger.error(f"Error getting balance for user {user_id}: {e}")
        raise

async def add_balance(user_id: int, delta: int):
    async with balance_lock:
        await ensure_user(user_id)
        try:
            current_balance = await get_balance(user_id)
            new_balance = current_balance + int(delta)
            await asyncio.to_thread(
                supabase.table("users").update({"balance": new_balance}).eq("user_id", str(user_id)).execute
            )
            logger.info(f"Balance updated for user {user_id}: {current_balance} -> {new_balance}")
        except Exception as e:
            logger.error(f"Error updating balance for user {user_id}: {e}")
            raise

async def add_moderator(user_id: int, guild_id: str) -> bool:
    """Добавить модератора для guild."""
    try:
        response = await asyncio.to_thread(
            supabase.table("moderators").insert({
                "user_id": str(user_id),
                "guild_id": guild_id
            }).execute
        )
        return bool(response.data)
    except Exception as e:
        logger.error(f"Error adding moderator {user_id}: {e}")
        return False

async def remove_moderator(user_id: int, guild_id: str) -> bool:
    """Удалить модератора."""
    try:
        response = await asyncio.to_thread(
            supabase.table("moderators").delete().eq("user_id", str(user_id)).eq("guild_id", guild_id).execute
        )
        return bool(response.data)
    except Exception as e:
        logger.error(f"Error removing moderator {user_id}: {e}")
        return False

async def get_moderators(guild_id: str) -> List[int]:
    """Получить список ID модераторов для guild."""
    try:
        response = await asyncio.to_thread(
            supabase.table("moderators").select("user_id").eq("guild_id", guild_id).execute
        )
        return [int(row["user_id"]) for row in response.data] if response.data else []
    except Exception as e:
        logger.error(f"Error getting moderators for guild {guild_id}: {e}")
        return []

async def is_moderator(user_id: int, guild_id: str) -> bool:
    try:
        response = await asyncio.to_thread(
            supabase.table("moderators").select("user_id").eq("guild_id", guild_id).eq("user_id", str(user_id)).execute
        )
        is_mod = bool(response.data)
        logger.info(f"is_moderator: user={user_id}, guild={guild_id}, found={is_mod}, data_len={len(response.data) if response.data else 0}")  # ✅ Расширь лог
        return is_mod
    except Exception as e:
        logger.error(f"Error in is_moderator {user_id}/{guild_id}: {e}")
        return False

async def check_duel_limit(user_id: int) -> bool:
    """Check if a user can participate in a duel (24-hour cooldown)."""
    try:
        response = supabase.table("users").select("last_duel_time").eq("user_id", str(user_id)).execute()
        last_duel = int(response.data[0]["last_duel_time"]) if response.data and response.data[0]["last_duel_time"] is not None else 0
        now = int(time.time())
        return now - last_duel >= 1  # 24 hours
    except Exception as e:
        logger.error(f"Error checking duel limit for user {user_id}: {e}")
        raise

async def update_duel_time(user_id: int):
    """Update the last duel time for a user."""
    try:
        supabase.table("users").update({"last_duel_time": int(time.time())}).eq("user_id", str(user_id)).execute()
    except Exception as e:
        logger.error(f"Error updating duel time for user {user_id}: {e}")
        raise

async def create_duel(channel_id: int, player1_id: Optional[int] = None, player2_id: Optional[int] = None, team1_id: Optional[int] = None, team2_id: Optional[int] = None, points: int = 0, duel_type: str = "1v1", is_public: bool = False, creator_user_id: Optional[int] = None) -> int:
    """Create a new duel and invite the opponent."""
    now = int(time.time())
    try:
        insert_data = {
            "channel_id": int(channel_id),
            "points": int(points),
            "status": "public" if is_public else "waiting",
            "type": duel_type,
            "is_public": is_public,
            "created_at": now,
            "announced": False,
            "steam_team_a": None,
            "steam_team_b": None,
            "dota_account_id": None,
            "dota_match_id": None,
            "stats": None,
            "reason": None,
            "completed_at": None,
            "creator_id": str(creator_user_id) if creator_user_id else None,  # ✅ Добавлено: ID создателя дуэли
        }
        logger.info(f"Creating duel with data: {insert_data}")
        if duel_type == "1v1":
            if player1_id is None:
                raise ValueError("player1_id required for 1v1")
            insert_data["player1_id"] = str(player1_id)
            insert_data["player2_id"] = str(player2_id) if player2_id else None
        else:  # 5v5
            insert_data["team1_id"] = str(team1_id) if team1_id else None
            insert_data["team2_id"] = str(team2_id) if team2_id else None

        duel_response = await asyncio.to_thread(supabase.table("duels").insert(insert_data).execute)
        logger.info(f"Created duel ID: {duel_response.data[0]['id']}, is_public: {duel_response.data[0].get('is_public', 'NOT SET')}, status: {duel_response.data[0].get('status')}")
        duel_id = int(duel_response.data[0]["id"])
        
        if not is_public and player2_id:  # Only for private 1v1
            supabase.table("duel_invites").insert({
                "duel_id": duel_id,
                "user_id": str(player2_id),
                "status": "pending",
                "created_at": now
            }).execute()
        elif not is_public and team2_id:  # For private 5v5
            leader2 = await get_team_leader(team2_id)
            if leader2:
                supabase.table("duel_invites").insert({
                    "duel_id": duel_id,
                    "user_id": str(leader2),
                    "status": "pending",
                    "created_at": now
                }).execute()
        return duel_id
    except Exception as e:
        logger.error(f"Error creating duel: {e}")
        raise

async def set_duel_message(duel_id: int, message_id: int):
    """Set the message ID for a duel."""
    try:
        supabase.table("duels").update({"message_id": int(message_id)}).eq("id", int(duel_id)).execute()
    except Exception as e:
        logger.error(f"Error setting duel message ID {duel_id}: {e}")
        raise

async def update_duel_status(duel_id: int, new_status: str):
    try:
        supabase.table("duels").update({"status": new_status}).eq("id", int(duel_id)).execute()
        duel = await get_duel(duel_id)
        if duel and duel.get("message_id"):
            channel = bot.get_channel(int(duel["channel_id"]))
            if channel:
                try:
                    msg = await channel.fetch_message(int(duel["message_id"]))
                    await refresh_duel_message(msg, duel)
                except Exception as e:
                    logger.error(f"Error refreshing duel message {duel_id}: {e}")
    except Exception as e:
        logger.error(f"Error updating duel status {duel_id}: {e}")

async def get_duel(duel_id: int) -> Optional[dict]:
    """Get details of a duel by ID."""
    try:
        response = supabase.table("duels").select("*").eq("id", int(duel_id)).execute()
        return response.data[0] if response.data else None
    except Exception as e:
        logger.error(f"Error getting duel {duel_id}: {e}")
        raise

async def has_pending_duel(user_id: int) -> Optional[int]:
    """Проверяет, есть ли у пользователя открытая pending дуэль (waiting/public)."""
    try:
        # Check 1v1: creator as player1
        response = await asyncio.to_thread(
            supabase.table("duels")
            .select("id")
            .eq("player1_id", str(user_id))
            .in_("status", ["waiting", "public"])
            .execute
        )
        if response.data:
            return int(response.data[0]["id"])
        
        # Check 5v5: creator as team1 leader
        user_team = await get_user_team(user_id)
        if user_team and str(user_id) == user_team["leader_id"]:
            response = await asyncio.to_thread(
                supabase.table("duels")
                .select("id")
                .eq("team1_id", str(user_team["id"]))
                .in_("status", ["waiting", "public"])
                .execute
            )
            if response.data:
                return int(response.data[0]["id"])
        
        return None
    except Exception as e:
        logger.error(f"Error checking pending duel for {user_id}: {e}")
        return None

async def update_duel_invite_status(duel_id: int, user_id: int, status: str):
    """Update the status of a duel invite."""
    try:
        await asyncio.to_thread(
            supabase.table("duel_invites").update({"status": status}).eq("duel_id", int(duel_id)).eq("user_id", str(user_id)).execute
        )
        duel = await get_duel(duel_id)
        if status == "accepted":
            await asyncio.to_thread(
                supabase.table("duels").update({"status": "active"}).eq("id", int(duel_id)).execute
            )
            # Deduct points from second player/leader
            if duel["type"] == "1v1":
                await add_balance(int(duel["player2_id"]), -int(duel["points"]))
                # ✅ Cooldown только после accepted
                await update_duel_time(int(duel["player1_id"]))
                await update_duel_time(int(duel["player2_id"]))
            else:  # 5v5
                leader2 = await get_team_leader(int(duel["team2_id"]))
                if leader2:
                    await add_balance(leader2, -int(duel["points"]))
                    # ✅ Cooldown для лидеров только после accepted
                    leader1 = await get_team_leader(int(duel["team1_id"]))
                    if leader1:
                        await update_duel_time(leader1)
                    await update_duel_time(leader2)
            # Refresh message
            if duel.get("message_id"):
                channel = bot.get_channel(int(duel["channel_id"]))
                if channel:
                    try:
                        msg = await channel.fetch_message(int(duel["message_id"]))
                        await refresh_duel_message(msg, duel)
                    except Exception as e:
                        logger.error(f"Error refreshing after accept {duel_id}: {e}")
        elif status == "declined":
            await asyncio.to_thread(
                supabase.table("duels").update({"status": "cancelled"}).eq("id", int(duel_id)).execute
            )
            # Refund first player/leader (без cooldown)
            if duel["type"] == "1v1":
                await add_balance(int(duel["player1_id"]), int(duel["points"]))
            else:
                leader1 = await get_team_leader(int(duel["team1_id"]))
                if leader1:
                    await add_balance(leader1, int(duel["points"]))
            # Refresh message
            if duel.get("message_id"):
                channel = bot.get_channel(int(duel["channel_id"]))
                if channel:
                    try:
                        msg = await channel.fetch_message(int(duel["message_id"]))
                        await refresh_duel_message(msg, duel)
                    except Exception as e:
                        logger.error(f"Error refreshing after decline {duel_id}: {e}")
    except Exception as e:
        logger.error(f"Error updating duel invite status for duel {duel_id}, user {user_id}: {e}")

async def join_public_duel(duel_id: int, joining_user_id: int, joining_team_id: Optional[int] = None, points: int = 0):
    """Handle joining a public duel (1v1 or 5v5)."""
    try:
        duel = await get_duel(duel_id)
        if not duel or duel["status"] != "public" or duel["is_public"] is False:
            return False, "Дуэль недоступна для присоединения."
        
        if duel["type"] == "1v1":
            if duel["player2_id"] is not None:
                return False, "Дуэль уже заполнена."
            bal = await get_balance(joining_user_id)
            if bal < points:
                return False, f"Недостаточно поинтов: {bal}."
            # ✅ Cooldown проверка перед join, но update только после
            if not await check_duel_limit(joining_user_id):
                return False, "Вы уже дуэлились сегодня."
            if str(joining_user_id) == duel["player1_id"]:
                return False, "Вы уже в дуэли."
            await asyncio.to_thread(
                supabase.table("duels").update({"player2_id": str(joining_user_id), "status": "active"}).eq("id", duel_id).execute
            )
            await add_balance(joining_user_id, -points)
            # ✅ Cooldown стартует только после join
            await update_duel_time(joining_user_id)
            await update_duel_time(int(duel["player1_id"]))
            # Refresh message
            if duel.get("message_id"):
                channel = bot.get_channel(int(duel["channel_id"]))
                if channel:
                    try:
                        msg = await channel.fetch_message(int(duel["message_id"]))
                        await refresh_duel_message(msg, duel)
                    except Exception as e:
                        logger.error(f"Error refreshing after public join 1v1 {duel_id}: {e}")
            return True, "Вы присоединились к дуэли. Она активна!"
        else:  # 5v5
            team = await get_team(joining_team_id)
            if not team or not await is_team_full_and_confirmed(team):
                return False, "Ваша команда должна быть полной и подтвержденной."
            if str(joining_user_id) != team["leader_id"]:
                return False, "Только лидер может присоединить команду."
            bal = await get_balance(joining_user_id)
            if bal < points:
                return False, f"Недостаточно поинтов у лидера: {bal}."
            # ✅ Cooldown проверка перед join
            if not await check_duel_limit(joining_user_id):
                return False, "Лидер уже участвовал в дуэли сегодня."
            if duel["team1_id"] and str(joining_team_id) == duel["team1_id"]:
                return False, "Нельзя присоединиться к своей дуэли."
            if duel["team1_id"] is None:
                await asyncio.to_thread(
                    supabase.table("duels").update({"team1_id": str(joining_team_id), "status": "active"}).eq("id", duel_id).execute
                )
                await add_balance(joining_user_id, -points)
                # ✅ Cooldown для обоих лидеров после join
                await update_duel_time(joining_user_id)
                creator_leader = await get_team_leader(int(duel["team1_id"]))
                if creator_leader:
                    await update_duel_time(creator_leader)
                # Refresh message
                if duel.get("message_id"):
                    channel = bot.get_channel(int(duel["channel_id"]))
                    if channel:
                        try:
                            msg = await channel.fetch_message(int(duel["message_id"]))
                            await refresh_duel_message(msg, duel)
                        except Exception as e:
                            logger.error(f"Error refreshing after public join 5v5 {duel_id}: {e}")
                return True, "Ваша команда присоединилась к дуэли. Она активна!"
            else:
                return False, "Дуэль уже заполнена."
    except Exception as e:
        logger.error(f"Error joining public duel {duel_id}: {e}")
        return False, "Ошибка присоединения."

async def auto_refund_public_duel(duel_id: int, creator_id: int, points: int):
    """Auto-refund public duel after 1 hour if no join."""
    await asyncio.sleep(3600)  # 1 hour
    try:
        duel = await get_duel(duel_id)
        if not duel or duel["status"] != "public":
            logger.info(f"Auto-refund skipped for {duel_id}: status {duel['status'] if duel else 'None'}")
            return
        # Refund creator
        await add_balance(creator_id, points)
        # Update status
        await update_duel_status(duel_id, "cancelled")
        # Notify in channel
        channel = bot.get_channel(int(duel["channel_id"]))
        if channel:
            embed = await build_duel_embed(duel)
            embed.add_field(name="Статус", value="⏰ Отменена (никто не присоединился)", inline=False)
            view = discord.ui.View()  # No buttons
            if duel.get("message_id"):
                try:
                    msg = await channel.fetch_message(int(duel["message_id"]))
                    await msg.edit(embed=embed, view=view)
                except:
                    await channel.send(embed=embed)
            else:
                await channel.send(embed=embed)
        logger.info(f"Auto-refund for duel {duel_id}: {points} returned to {creator_id}")
    except Exception as e:
        logger.error(f"Error in auto-refund {duel_id}: {e}")

async def settle_duel(duel_id: int, winner_side: str) -> Tuple[bool, str]:
    logger.info(f"Starting settle_duel for {duel_id}, winner {winner_side}")
    if winner_side not in ("A", "B"):
        return False, "Победитель должен быть 'A' или 'B'."
    
    try:
        duel = await get_duel(duel_id)
        if not duel:
            return False, "Дуэль не найдена."
        if duel["status"] not in ("processing", "result_pending"):  # Allow manual on processing too
            return False, "Дуэль не в состоянии для завершения."
        
        logger.info(f"Duel {duel_id} current status: {duel['status']}")
        points = int(duel["points"])
        total_pot = points * 2  # Общий банк
        burn_rate = DEFAULT_BURN  # 0.25
        burned_amount = int(total_pot * burn_rate)  # 25% сгорает
        payout = total_pot - burned_amount  # Остаток победителю
        
        winner_leader = None
        if winner_side == "A":
            if duel["type"] == "1v1":
                winner_leader = int(duel["player1_id"])
            else:
                winner_leader = await get_team_leader(int(duel["team1_id"]))
        else:  # winner_side == "B"
            if duel["type"] == "1v1":
                winner_leader = int(duel["player2_id"])
            else:
                winner_leader = await get_team_leader(int(duel["team2_id"]))
        
        if not winner_leader:
            return False, "Не удалось определить лидера победителя."
        
        # Передача точек в лог
        logger.info(f"Winner leader {winner_leader}, total_pot {total_pot}, burned {burned_amount}, payout {payout}, updating to settled")
        
        # Обновление статуса и winner_side
        supabase.table("duels").update({"status": "settled", "winner_side": winner_side}).eq("id", int(duel_id)).execute()
        
        # Немедленная проверка: повторный запрос для обхода кэша
        response = supabase.table("duels").select("status", "winner_side").eq("id", int(duel_id)).execute()
        if not response.data:
            logger.error(f"No data after update for duel {duel_id}")
            return False, "Обновление не применилось."
        updated_db = response.data[0]
        logger.info(f"DB check after update: status {updated_db['status']}, winner_side {updated_db.get('winner_side', 'None')}")
        if updated_db["status"] != "settled" or updated_db.get("winner_side") != winner_side:
            logger.error(f"DB update failed: expected settled/{winner_side}, got {updated_db['status']}/{updated_db.get('winner_side')}")
            return False, "Ошибка обновления БД."
        
        # Передача payout лидеру победителя (учитывая, что у него уже -points)
        await add_balance(winner_leader, payout)
        logger.info(f"Balance updated for {winner_leader}: +{payout} (netto +{payout - points})")
        
        await update_duel_status(duel_id, "settled")
        return True, f"Дуэль завершена! Победитель: {winner_side} ({payout} поинтов лидеру, сгорело {burned_amount})."
    except Exception as e:
        logger.error(f"Error settling duel {duel_id}: {e}")
        raise


async def create_team(leader_id: int, players: List[int], name: str) -> int:
    """Create a new team with the specified players."""
    now = int(time.time())
    try:
        team_response = supabase.table("teams").insert({
            "leader_id": str(leader_id),
            "player1_id": str(players[0]),
            "player2_id": str(players[1]),
            "player3_id": str(players[2]),
            "player4_id": str(players[3]),
            "player5_id": str(players[4]),
            "name": name,               # ✅ сохраняем название
            "status": "pending",
            "created_at": now
        }).execute()
        team_id = int(team_response.data[0]["id"])
        
        for player_id in players:
            supabase.table("team_invites").insert({
                "team_id": team_id,
                "user_id": str(player_id),
                "status": "pending",
                "created_at": now
            }).execute()
        return team_id
    except Exception as e:
        logger.error(f"Error creating team: {e}")
        raise


async def get_team(team_id: int) -> Optional[dict]:
    """Get details of a team by ID."""
    try:
        response = supabase.table("teams").select("*").eq("id", int(team_id)).execute()
        return response.data[0] if response.data else None
    except Exception as e:
        logger.error(f"Error getting team {team_id}: {e}")
        raise

async def get_user_team(user_id: int) -> Optional[dict]:
    """Get the team a user is part of."""
    try:
        response = supabase.table("teams").select("*").or_(f"leader_id.eq.{str(user_id)},player1_id.eq.{str(user_id)},player2_id.eq.{str(user_id)},player3_id.eq.{str(user_id)},player4_id.eq.{str(user_id)},player5_id.eq.{str(user_id)}").execute()
        return response.data[0] if response.data else None
    except Exception as e:
        logger.error(f"Error getting team for user {user_id}: {e}")
        raise
async def get_team_leader(team_id: int) -> Optional[int]:
    """Get the leader ID of a team."""
    team = await get_team(team_id)
    return int(team["leader_id"]) if team else None

async def is_user_in_team(team: dict, user_id: int) -> bool:
    """Check if a user is in a team (leader or player)."""
    if str(user_id) == team["leader_id"]:
        return True
    for i in range(1, 6):
        if team.get(f"player{i}_id") == str(user_id):
            return True
    return False

async def is_team_full_and_confirmed(team: dict) -> bool:
    """Check if a team is full (5 players) and confirmed."""
    if team["status"] != "confirmed":
        return False
    player_count = sum(1 for i in range(1, 6) if team.get(f"player{i}_id"))
    return player_count >= 5


async def remove_from_team(user_id: int):
    """Remove a user from their team."""
    try:
        team = await get_user_team(user_id)
        if team:
            team_id = team["id"]
            updates = {}
            for i in range(1, 6):
                if team[f"player{i}_id"] == str(user_id):
                    updates[f"player{i}_id"] = None
            if updates:
                supabase.table("teams").update(updates).eq("id", team_id).execute()
                supabase.table("team_invites").update({"status": "left"}).eq("team_id", team_id).eq("user_id", str(user_id)).execute()
                supabase.table("teams").update({"status": "pending"}).eq("id", team_id).execute()
    except Exception as e:
        logger.error(f"Error removing user {user_id} from team: {e}")
        raise

async def create_match(channel_id: int, team_a: str, team_b: str, burn: float) -> int:
    """Create a new match for betting."""
    now = int(time.time())
    try:
        response = supabase.table("matches").insert({
            "channel_id": int(channel_id),
            "team_a": team_a.strip(),
            "team_b": team_b.strip(),
            "burn": float(burn),
            "status": "Открыта",
            "total_a": 0,
            "total_b": 0,
            "created_at": now
        }).execute()
        return int(response.data[0]["id"])
    except Exception as e:
        logger.error(f"Error creating match: {e}")
        raise

async def set_match_message(match_id: int, message_id: int):
    """Set the message ID for a match."""
    try:
        supabase.table("matches").update({"message_id": int(message_id)}).eq("id", int(match_id)).execute()
    except Exception as e:
        logger.error(f"Error setting match message ID {match_id}: {e}")
        raise

async def get_match(match_id: int) -> Optional[dict]:
    """Get details of a match by ID."""
    try:
        response = await asyncio.to_thread(
            supabase.table("matches").select("*").eq("id", int(match_id)).execute
        )
        return response.data[0] if response.data else None
    except Exception as e:
        logger.error(f"Error getting match {match_id}: {e}")
        raise

async def sum_bets(match_id: int, team: str) -> int:
    """Sum the total bets for a team in a match."""
    try:
        response = supabase.table("bets").select("amount").eq("match_id", int(match_id)).eq("team", team).execute()
        return sum(int(bet["amount"]) for bet in response.data) if response.data else 0
    except Exception as e:
        logger.error(f"Error summing bets for match {match_id}, team {team}: {e}")
        raise

async def place_bet(match_id: int, user_id: int, team: str, amount: int) -> Tuple[bool, str]:
    """Place a bet on a match for a specific team."""
    assert team in ("A", "B")
    try:
        amount = safe_int(amount)
    except ValueError as e:
        return False, str(e)
    if amount <= 0:
        return False, "Сумма должна быть > 0."

    try:
        match = await get_match(match_id)
        if not match:
            return False, "Матч не найден."
        if match["status"] != "Открыта":
            return False, "Ставки закрыты."

        await ensure_user(user_id)
        current_balance = await get_balance(user_id)
        if amount > current_balance:
            return False, f"Недостаточно поинтов. Ваш баланс: {current_balance}."

        async with balance_lock:  # Ensure atomic transaction
            # Deduct balance directly
            new_balance = current_balance - amount
            await asyncio.to_thread(
                supabase.table("users").update({"balance": new_balance}).eq("user_id", str(user_id)).execute
            )

            # Insert bet
            await asyncio.to_thread(
                supabase.table("bets").insert({
                    "match_id": int(match_id),
                    "user_id": str(user_id),
                    "team": team,
                    "amount": amount,
                    "created_at": int(time.time())
                }).execute
            )

            # Update match total
            if team == "A":
                new_total = int(match["total_a"]) + amount
                await asyncio.to_thread(
                    supabase.table("matches").update({"total_a": new_total}).eq("id", int(match_id)).execute
                )
            else:
                new_total = int(match["total_b"]) + amount
                await asyncio.to_thread(
                    supabase.table("matches").update({"total_b": new_total}).eq("id", int(match_id)).execute
                )

        return True, "Ставка принята!"
    except Exception as e:
        logger.error(f"Error placing bet for match {match_id}, user {user_id}: {e}")
        # Potential refund if deducted but failed
        try:
            await add_balance(user_id, amount)
        except:
            pass
        return False, f"Ошибка при размещении ставки: {str(e)}"


async def close_bet(match_id: int) -> Tuple[bool, str]:
    """Close betting for a match."""
    try:
        match = (supabase.table("matches").select("status").eq("id", int(match_id)).execute()).data
        if not match:
            return False, "Матч не найден."
        if match[0]["status"] != "Открыта":
            return False, "Матч уже не открыт."
        supabase.table("matches").update({"status": "Закрыта"}).eq("id", int(match_id)).execute()
        return True, "Прием ставок закрыт."
    except Exception as e:
        logger.error(f"Error closing bet for match {match_id}: {e}")
        raise

async def cancel_bet(match_id: int) -> Tuple[bool, str, int]:
    """Cancel a match and refund all bets."""
    refunded = 0
    try:
        # получаем матч через helper (await безопасен, т.к. get_match — async)
        m = await get_match(match_id)
        if not m:
            return False, "Матч не найден.", refunded

        status = (m.get("status") or "").lower()
        if status in ("cancelled", "settled", "cancelling"):
            return False, "Матч уже завершен или в процессе отмены.", refunded

        # помечаем матч как 'cancelling' чтобы предотвратить повторные вызовы
        supabase.table("matches").update({"status": "cancelling"}).eq("id", int(match_id)).execute()

        # получаем ставки (синхронный вызов execute — НЕ await)
        bets_res = supabase.table("bets").select("user_id,amount").eq("match_id", int(match_id)).execute()
        bets = bets_res.data or []

        # делаем возвраты
        for bet in bets:
            try:
                uid = int(bet["user_id"])
                amt = int(bet["amount"])
            except Exception:
                logger.warning(f"[cancel_bet] некорректные данные ставки для match={match_id}: {bet}")
                continue

            if amt <= 0:
                continue

            await add_balance(uid, amt)
            refunded += amt
            logger.info(f"[cancel_bet] refunded {amt} to user {uid} for match {match_id}")

        # помечаем матч как отменённый
        supabase.table("matches").update({"status": "cancelled"}).eq("id", int(match_id)).execute()
        logger.info(f"[cancel_bet] match={match_id} cancelled, total_refunded={refunded}")

        return True, f"Матч отменен. Возвращено {refunded} поинтов.", refunded

    except Exception as e:
        logger.exception(f"Error cancelling bet for match {match_id}: {e}")
        # пробуем в стандартном варианте пометить матч как cancelled (best-effort)
        try:
            supabase.table("matches").update({"status": "cancelled"}).eq("id", int(match_id)).execute()
        except Exception:
            pass
        return False, "Не удалось отменить матч. Проверь логи.", refunded


async def settle_bet(match_id: int, winner: str) -> Tuple[bool, str]:
    """Settle a match and distribute winnings."""
    winner = (winner or "").strip().upper()
    if winner not in ("A", "B"):
        return False, "winner должен быть 'A' или 'B'"

    try:
        # 1) читаем матч (без await для supabase-py)
        m_res = supabase.table("matches").select("*").eq("id", int(match_id)).execute()
        rows = m_res.data or []
        if not rows:
            return False, "Матч не найден."
        m = rows[0]

        status = (m.get("status") or "").lower()
        if status in ("cancelled", "settled"):
            return False, "Матч уже завершен."

        burn = float(m["burn"]) if m.get("burn") is not None else DEFAULT_BURN
        total_a = int(m.get("total_a") or 0)
        total_b = int(m.get("total_b") or 0)

        # Банк победителей / проигравших
        W = total_a if winner == "A" else total_b
        L = total_b if winner == "A" else total_a

        # Никто не ставил на победителя — всё сгорает
        if W <= 0:
            supabase.table("matches").update({"status": "settled"}).eq("id", int(match_id)).execute()
            return True, "Никто не ставил на победителя. Весь проигрыш сгорел."

        # 2) ставки победителей
        bets_res = supabase.table("bets").select("id,user_id,amount").eq("match_id", int(match_id)).eq("team", winner).execute()
        winners = bets_res.data or []
        if not winners:
            # Страховка от несогласованности данных
            supabase.table("matches").update({"status": "settled"}).eq("id", int(match_id)).execute()
            return True, "Ставки победителей не найдены; пометил матч как settled."

        distribute = int(round((1.0 - burn) * L))

        # 3) делим L*(1-burn) методом наибольших остатков
        shares = []
        acc_int = 0
        W = int(W)
        for wb in winners:
            amt = int(wb["amount"])
            raw = (amt / W) * distribute
            part_int = int(raw)                 # целая часть
            frac = float(raw - part_int)        # дробная часть
            shares.append((wb["id"], wb["user_id"], amt, part_int, frac))
            acc_int += part_int

        remainder = distribute - acc_int
        if remainder > 0:
            shares.sort(key=lambda x: x[4], reverse=True)
            for i in range(min(remainder, len(shares))):
                bid, uid, amt, pi, fr = shares[i]
                shares[i] = (bid, uid, amt, pi + 1, fr)

        # 4) выплаты: ставка + доля из проигравшего банка
        paid_total = 0
        for _, uid, amt, part_int, _ in shares:
            payout = int(amt) + int(part_int)
            paid_total += payout
            await add_balance(int(uid), payout)

        supabase.table("matches").update({"status": "settled"}).eq("id", int(match_id)).execute()
        burned = L - distribute
        logger.info(f"[settle_bet] match={match_id} winner={winner} distribute={distribute} burned={burned} paid_total={paid_total}")
        return True, f"Выплаты завершены. Раздали {distribute} из банка проигравших."
    except Exception as e:
        logger.exception(f"Error settling bet for match {match_id}: {e}")
        return False, "Не удалось завершить матч. Проверь логи."

def create_disabled_view(original_view_type: str) -> discord.ui.View:
    """Создает view с отключенными кнопками для разных типов взаимодействий."""
    view = discord.ui.View()
    
    if original_view_type == "duel_invite":
        view.add_item(discord.ui.Button(label="Принять", style=discord.ButtonStyle.success, disabled=True))
        view.add_item(discord.ui.Button(label="Отклонить", style=discord.ButtonStyle.danger, disabled=True))
    elif original_view_type == "team_invite":
        view.add_item(discord.ui.Button(label="Принять", style=discord.ButtonStyle.success, disabled=True))
        view.add_item(discord.ui.Button(label="Отклонить", style=discord.ButtonStyle.danger, disabled=True))
    
    return view

def get_dotabuff_account_id(steam_input: str) -> Optional[int]:
    """Извлекает account_id (32-bit) из steam_id для Dotabuff."""
    try:
        steam_id = steam_input.strip()
        
        # Парсинг как в get_mmr_from_steamid
        if re.match(r'^\d{7,10}$', steam_id):  # Account ID
            return int(steam_id)
        elif steam_id.startswith("STEAM_0:"):
            parts = re.match(r'STEAM_0:(\d):(\d+)', steam_id)
            if parts:
                auth = int(parts.group(1))
                account = int(parts.group(2))
                return (account * 2) + auth  # Account ID из STEAM_0
        else:  # 64-bit
            try:
                steamid64 = int(steam_id)
                account_id = steamid64 - 76561197960265728
                if account_id > 0:
                    return account_id
            except ValueError:
                pass
        return None
    except Exception as e:
        logger.error(f"Error extracting account_id from {steam_input}: {e}")
        return None
# ---------------------- UI ----------------------



class ManualMMRView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)  # 5 мин
        self.user_id = user_id
        self.add_item(discord.ui.Button(label="Ввести MMR", style=discord.ButtonStyle.primary, custom_id=f"manual_mmr:{user_id}"))

        

class LeaderboardView(discord.ui.View):
    def __init__(self, data, per_page=10):
        super().__init__(timeout=120)
        self.data = data
        self.per_page = per_page
        self.page = 0
        self.max_page = (len(data) - 1) // per_page

        self.prev_button = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary)
        self.next_button = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary)
        self.prev_button.callback = self.prev_page
        self.next_button.callback = self.next_page

        self.add_item(self.prev_button)
        self.add_item(self.next_button)

    async def update_message(self, interaction):
        start = self.page * self.per_page
        end = start + self.per_page
        chunk = self.data[start:end]

        desc = "\n".join(
            [f"**{i+1}.** <@{row['user_id']}> — {row['balance']}💰" for i, row in enumerate(chunk, start=start)]
        )

        embed = discord.Embed(
            title=f"🏆 Лидерборд (стр. {self.page+1}/{self.max_page+1})",
            description=desc,
            color=discord.Color.gold()
        )
        await interaction.response.edit_message(embed=embed, view=self)

    async def prev_page(self, interaction):
        if self.page > 0:
            self.page -= 1
        await self.update_message(interaction)

    async def next_page(self, interaction):
        if self.page < self.max_page:
            self.page += 1
        await self.update_message(interaction)

class BetAmountModal(discord.ui.Modal, title="Введите сумму ставки"):
    def __init__(self, match_id: int, team: str):
        super().__init__()
        self.match_id = match_id
        self.team = team
        self.amount = discord.ui.TextInput(
            label="Сумма поинтов",
            placeholder="Например, 100",
            required=True,
            min_length=1,
            max_length=12,
        )
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amt = safe_int(self.amount.value.strip())
        except ValueError:
            await interaction.response.send_message("Введите целое число.", ephemeral=True)
            return
        
        try:
            ok, msg = await place_bet(self.match_id, interaction.user.id, self.team, amt)
            if ok:
                await refresh_match_message(interaction, self.match_id)
            await interaction.response.send_message(msg, ephemeral=True)
        except Exception as e:
            logger.error(f"Error in bet modal: {e}")
            await interaction.response.send_message("Произошла ошибка при размещении ставки.", ephemeral=True)

class TeamInviteView(discord.ui.View):
    def __init__(self, invite_id: int, user_id: int):
        super().__init__(timeout=None)  # Persistent view - no timeout
        self.invite_id = invite_id
        self.user_id = user_id

        self.accept_button = discord.ui.Button(
            label="Принять", 
            style=discord.ButtonStyle.success, 
            custom_id=f"team_accept:{invite_id}:{user_id}"
        )
        self.add_item(self.accept_button)

        self.decline_button = discord.ui.Button(
            label="Отклонить", 
            style=discord.ButtonStyle.danger, 
            custom_id=f"team_decline:{invite_id}:{user_id}"
        )
        self.add_item(self.decline_button)

    async def accept_team(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Это не ваше приглашение.", ephemeral=True)
            return
        # Проверяем статус
        invite_resp = supabase.table("team_invites").select("status, team_id").eq("id", self.invite_id).execute()
        if not invite_resp.data or invite_resp.data[0]["status"] != "pending":
            await interaction.response.send_message("Приглашение устарело.", ephemeral=True)
            return
        team_id = int(invite_resp.data[0]["team_id"])
        team = supabase.table("teams").select("*").eq("id", team_id).execute().data[0]
        player_count = sum(1 for i in range(1, 6) if team.get(f"player{i}_id"))
        if player_count >= 5:
            await interaction.response.send_message("Команда заполнена.", ephemeral=True)
            return
        # Обновляем
        supabase.table("team_invites").update({"status": "accepted"}).eq("id", self.invite_id).execute()
        updates = {}
        for i in range(1, 6):
            if team.get(f"player{i}_id") is None:
                updates[f"player{i}_id"] = str(self.user_id)
                break
        if updates:
            supabase.table("teams").update(updates).eq("id", team_id).execute()
            # Check all invites
            invites = supabase.table("team_invites").select("status").eq("team_id", team_id).execute().data
            if all(invite["status"] == "accepted" for invite in invites):
                supabase.table("teams").update({"status": "confirmed"}).eq("id", team_id).execute()
        # Assign role
        guild = interaction.guild
        if guild:
            member = guild.get_member(self.user_id)
            if member:
                await assign_team_role(guild, member, team["name"])
        # Уведомить лидера
        leader_id = int(team["leader_id"])
        leader = bot.get_user(leader_id)
        if leader:
            await safe_send(leader, f"<@{self.user_id}> присоединился к вашей команде!")
        # Ответ и disable
        await interaction.response.send_message("Вы присоединились к команде!", ephemeral=True)
        button.disabled = True
        self.decline_button.disabled = True
        await interaction.edit_original_response(view=self)

    async def decline_team(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Это не ваше приглашение.", ephemeral=True)
            return
        supabase.table("team_invites").update({"status": "declined"}).eq("id", self.invite_id).execute()
        await interaction.response.send_message("Вы отклонили приглашение.", ephemeral=True)
        self.accept_button.disabled = True
        button.disabled = True
        await interaction.edit_original_response(view=self)
    
class TeamsView(discord.ui.View):
    def __init__(self, data, per_page=10):
        super().__init__(timeout=120)
        self.data = data
        self.per_page = per_page
        self.page = 0
        self.max_page = (len(data) - 1) // per_page

        self.prev_button = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary)
        self.next_button = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary)
        self.prev_button.callback = self.prev_page
        self.next_button.callback = self.next_page

        self.add_item(self.prev_button)
        self.add_item(self.next_button)

    async def update_message(self, interaction):
        start = self.page * self.per_page
        end = start + self.per_page
        chunk = self.data[start:end]

        players_list = []
        for i, row in enumerate(chunk, start=start):
            players = [row.get(f'player{j}_id') for j in range(1, 6) if row.get(f'player{j}_id')]
            participants_str = " ".join([f"<@{p}>" for p in players if p != row['leader_id']]) if players else "❌ Нет участников"
            status_emoji = "✅" if row['status'] == "confirmed" else "⏳"
            type_str = "🌍 Публичная" if row['is_public'] else "🔒 Приватная"
            players_list.append(
                f"**{start + i + 1}. {row['name']}** {status_emoji} {type_str}\n"
                f"👑 **Лидер:** <@{row['leader_id']}>\n"
                f"👥 **Участники:** {participants_str}"
            )

        desc = "\n\n".join(players_list) or "Нет команд"

        embed = discord.Embed(
            title=f"👥 Команды (стр. {self.page+1}/{self.max_page+1})",
            description=desc,
            color=discord.Color.blue()
        )
        await interaction.response.edit_message(embed=embed, view=self)

    async def prev_page(self, interaction):
        if self.page > 0:
            self.page -= 1
        await self.update_message(interaction)

    async def next_page(self, interaction):
        if self.page < self.max_page:
            self.page += 1
        await self.update_message(interaction)

class DuelInviteView(discord.ui.View):
    def __init__(self, duel_id: int, invitee_id: Optional[int] = None):
        super().__init__(timeout=None)  # Persistent
        self.duel_id = duel_id
        self.invitee_id = invitee_id or 0  # Dummy для регистрации

        # Добавляем кнопки только если invitee_id задан (для реальных инстансов)
        if invitee_id:
            self.add_item(discord.ui.Button(
                label="Принять", 
                style=discord.ButtonStyle.success, 
                custom_id=f"duel_accept:{duel_id}:{invitee_id}"
            ))
            self.add_item(discord.ui.Button(
                label="Отклонить", 
                style=discord.ButtonStyle.danger, 
                custom_id=f"duel_decline:{duel_id}:{invitee_id}"
            ))
class PublicDuelView(discord.ui.View):
    def __init__(self, duel_id: int, creator_id: int):
        super().__init__(timeout=None)  # Persistent
        self.duel_id = duel_id
        self.creator_id = creator_id

        self.add_item(discord.ui.Button(
            label="Присоединиться", 
            style=discord.ButtonStyle.success, 
            custom_id=f"join_public_duel:{duel_id}"
        ))
        self.add_item(discord.ui.Button(
            label="Отменить дуэль", 
            style=discord.ButtonStyle.danger, 
            custom_id=f"cancel_public_duel:{duel_id}:{creator_id}"
        ))

class CancelDuelView(discord.ui.View):
    def __init__(self, duel_id: int, creator_id: int):
        super().__init__(timeout=None)  # Persistent
        self.duel_id = duel_id
        self.creator_id = creator_id

        self.add_item(discord.ui.Button(
            label="Отменить дуэль", 
            style=discord.ButtonStyle.danger, 
            custom_id=f"cancel_duel:{duel_id}:{creator_id}"
        ))
    
class MatchView(discord.ui.View):
    def __init__(self, match_id: int, team_a: str, team_b: str, status: str):
        super().__init__(timeout=86400)  # 24-hour timeout
        self.match_id = match_id
        self.team_a = team_a
        self.team_b = team_b
        disabled = status != "Открыта"
        self.add_item(discord.ui.Button(label=f"Поставить на {team_a}", style=discord.ButtonStyle.success, custom_id=f"bet:A:{match_id}", disabled=disabled))
        self.add_item(discord.ui.Button(label=f"Поставить на {team_b}", style=discord.ButtonStyle.success, custom_id=f"bet:B:{match_id}", disabled=disabled))

class JoinTeamView(discord.ui.View):
    def __init__(self, team_id: int):
        super().__init__(timeout=None)  # Persistent
        self.team_id = team_id
        self.add_item(discord.ui.Button(
            label="Присоединиться", 
            style=discord.ButtonStyle.success, 
            custom_id=f"join_team:{team_id}"
        ))
        # Если refresh_btn нужен: self.add_item(Button(label="Обновить", style=discord.ButtonStyle.primary, custom_id="refresh")) — но обработка в on_interaction?

class ModeratorDuelView(discord.ui.View):
    def __init__(self, duel_id: int, guild_id: str):
        super().__init__(timeout=None)  # Persistent
        self.duel_id = duel_id
        self.guild_id = guild_id
        
        # ✅ Добавляем 4 кнопки с custom_id (без дубликатов)
        self.add_item(discord.ui.Button(
            label="Победил A", 
            style=discord.ButtonStyle.primary, 
            custom_id=f"mod_settle_a:{duel_id}:{guild_id}"
        ))
        self.add_item(discord.ui.Button(
            label="Победил B", 
            style=discord.ButtonStyle.primary, 
            custom_id=f"mod_settle_b:{duel_id}:{guild_id}"
        ))
        self.add_item(discord.ui.Button(
            label="Отменить результат", 
            style=discord.ButtonStyle.danger, 
            custom_id=f"mod_cancel_result:{duel_id}:{guild_id}"
        ))
        self.add_item(discord.ui.Button(
            label="Отменить дуэль", 
            style=discord.ButtonStyle.secondary, 
            custom_id=f"mod_cancel_duel:{duel_id}:{guild_id}"
        ))
        logger.info(f"Created ModeratorDuelView for duel={duel_id}, guild={guild_id}, children={len(self.children)}")  # Дебаг: должно быть 4

    async def on_interaction(self, interaction: discord.Interaction):
        """Handle all button clicks by parsing custom_id."""
        custom_id = interaction.data.get('custom_id', '')
        await interaction.response.defer(ephemeral=True)  # Defer всегда
        
        logger.info(f"Button clicked: custom_id={custom_id}, user={interaction.user.id}")
        
        if not await is_moderator(interaction.user.id, self.guild_id):
            await interaction.followup.send("❌ Вы не модератор.", ephemeral=True)
            self.disable_all_items()
            await interaction.edit_original_response(view=self)
            return
        
        if custom_id.startswith("mod_settle_a:"):
            await self._handle_settle(interaction, "A")
        elif custom_id.startswith("mod_settle_b:"):
            await self._handle_settle(interaction, "B")
        elif custom_id.startswith("mod_cancel_result:"):
            await self._handle_cancel_result(interaction)
        elif custom_id.startswith("mod_cancel_duel:"):
            await self._handle_cancel_duel(interaction)
        else:
            await interaction.followup.send("❌ Неизвестная кнопка.", ephemeral=True)
            self.disable_all_items()
            await interaction.edit_original_response(view=self)

    async def _handle_settle(self, interaction: discord.Interaction, winner_side: str):
        ok, msg = await settle_duel(self.duel_id, winner_side)
        if ok:
            await interaction.followup.send(f"✅ {msg}", ephemeral=True)
            # Обновляем публичное сообщение
            duel = await get_duel(self.duel_id)
            if duel and duel.get("message_id"):
                channel = bot.get_channel(int(duel["channel_id"]))
                if channel:
                    try:
                        pub_msg = await channel.fetch_message(int(duel["message_id"]))
                        await refresh_duel_message(pub_msg, duel)
                    except Exception as e:
                        logger.error(f"Failed to refresh public after settle {self.duel_id}: {e}")
        else:
            await interaction.followup.send(f"❌ {msg}", ephemeral=True)
        self.disable_all_items()
        await interaction.edit_original_response(view=self)

    async def _handle_cancel_result(self, interaction: discord.Interaction):
        await update_duel_status(self.duel_id, "result_canceled")
        duel = await get_duel(self.duel_id)
        await interaction.followup.send("✅ Результат отменён.", ephemeral=True)
        if duel and duel.get("message_id"):
            channel = bot.get_channel(int(duel["channel_id"]))
            if channel:
                try:
                    pub_msg = await channel.fetch_message(int(duel["message_id"]))
                    await refresh_duel_message(pub_msg, duel)
                except Exception as e:
                    logger.error(f"Failed to refresh after cancel_result {self.duel_id}: {e}")
        self.disable_all_items()
        await interaction.edit_original_response(view=self)

    async def _handle_cancel_duel(self, interaction: discord.Interaction):
        duel = await get_duel(self.duel_id)
        if duel:
            points = int(duel["points"])
            if duel["type"] == "1v1":
                await add_balance(int(duel["player1_id"]), points)
                if duel["player2_id"]:
                    await add_balance(int(duel["player2_id"]), points)
            else:
                l1 = await get_team_leader(int(duel["team1_id"]))
                l2 = await get_team_leader(int(duel["team2_id"]))
                if l1: await add_balance(l1, points)
                if l2: await add_balance(l2, points)
            await update_duel_status(self.duel_id, "cancelled")
            await interaction.followup.send("✅ Дуэль отменена, поинты возвращены.", ephemeral=True)
            if duel.get("message_id"):
                channel = bot.get_channel(int(duel["channel_id"]))
                if channel:
                    try:
                        pub_msg = await channel.fetch_message(int(duel["message_id"]))
                        await refresh_duel_message(pub_msg, duel)
                    except Exception as e:
                        logger.error(f"Failed to refresh after cancel_duel {self.duel_id}: {e}")
        self.disable_all_items()
        await interaction.edit_original_response(view=self)

    def disable_all_items(self):
        for item in self.children:
            item.disabled = True

async def refresh_match_message(interaction: discord.Interaction, match_id: int, edit_message: bool = False):
    """Refresh the match message with updated data."""
    m = await get_match(match_id)
    if not m:
        return
    total_a = int(m["total_a"]) if m["total_a"] else 0
    total_b = int(m["total_b"]) if m["total_b"] else 0
    team_a = m["team_a"]
    team_b = m["team_b"]
    status = m["status"]
    burn = float(m["burn"]) if m["burn"] is not None else DEFAULT_BURN

    EH_EMOJI = "<:EH:1412492188809560196>"

    embed = discord.Embed(title=f"Матч: {team_a} vs {team_b}", color=discord.Color.dark_gray() if status in ["settled", "cancelled"] else discord.Color.blurple())
    embed.add_field(name=f"Банк {team_a}", value=f"{total_a} {EH_EMOJI}", inline=True)
    embed.add_field(name=f"Банк {team_b}", value=f"{total_b} {EH_EMOJI}", inline=True)
    embed.add_field(name="Статус", value=status, inline=False)
    embed.set_footer(text=f"match:{match_id}")


    view = MatchView(match_id, team_a, team_b, status)

    try:
        if edit_message and interaction.message:
            await interaction.message.edit(embed=embed, view=view)
        else:
            channel = bot.get_channel(int(m["channel_id"]))
            if channel is not None:
                msg = await channel.fetch_message(int(m["message_id"]))
                await msg.edit(embed=embed, view=view)
    except discord.HTTPException as e:
        logger.error(f"Failed to edit message for match {match_id}: {e}")
        if interaction.response.is_done():
            await interaction.followup.send("❌ Не удалось обновить сообщение матча.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Не удалось обновить сообщение матча.", ephemeral=True)

async def refresh_duel_message(message: discord.Message, duel: dict):
    """Refresh the duel message with updated data."""
    # Сохраняем старую картинку, если была
    old_embed = message.embeds[0] if message.embeds else None
    screenshot_url = old_embed.image.url if old_embed and old_embed.image else None
    
    embed = await build_duel_embed(duel)
    if screenshot_url:
        embed.set_image(url=screenshot_url)

    guild = message.guild  # Получаем guild из сообщения

    # ---------- Отладка ----------
    logger.info(f"DEBUG duel dict: {duel}")
    logger.info(f"DEBUG duel type: {duel.get('type')} status: {duel.get('status')}")

    # ---------- Определение имён ----------
    winner_a_name = f"Игрок {duel.get('player1_id', '?')}"
    winner_b_name = f"Игрок {duel.get('player2_id', '?')}"

    try:
        if duel["type"] == "1v1":
            player1_id = duel.get("player1_id")
            player2_id = duel.get("player2_id")

            if player1_id:
                try:
                    member1 = guild.get_member(int(player1_id)) if guild else None
                    if member1:
                        winner_a_name = member1.display_name
                except Exception as e:
                    logger.warning(f"Ошибка при обработке player1_id {player1_id}: {e}")

            if player2_id:
                try:
                    member2 = guild.get_member(int(player2_id)) if guild else None
                    if member2:
                        winner_b_name = member2.display_name
                except Exception as e:
                    logger.warning(f"Ошибка при обработке player2_id {player2_id}: {e}")

        elif duel["type"] == "5v5":
            team1_leader = await get_team_leader(int(duel.get("team1_id", 0)))
            if team1_leader:
                member1 = guild.get_member(team1_leader) if guild else None
                winner_a_name = member1.display_name if member1 else f"Лидер {team1_leader}"

            team2_leader = await get_team_leader(int(duel.get("team2_id", 0)))
            if team2_leader:
                member2 = guild.get_member(team2_leader) if guild else None
                winner_b_name = member2.display_name if member2 else f"Лидер {team2_leader}"

    except Exception as e:
        logger.error(f"Ошибка при определении имён: {e}")

    logger.info(f"Names for duel {duel['id']}: A='{winner_a_name}', B='{winner_b_name}'")

    # ---------- Цвет и подсказка для отменённого результата ----------
    if duel.get("status") == "result_canceled":
        embed.color = discord.Color.grey()  # Серая линия
        embed.add_field(name="Статус", value="Результат отменён", inline=False)  # Подсказка

    # ---------- Кнопки ----------
    view = discord.ui.View()
    if duel["status"] == "waiting" and not duel["is_public"]:
        invitee_id = duel.get("player2_id") if duel["type"] == "1v1" else await get_team_leader(int(duel.get("team2_id", 0)))
        if invitee_id:
            view.add_item(discord.ui.Button(label="Присоединиться", style=discord.ButtonStyle.success, custom_id=f"duel_accept:{duel['id']}:{invitee_id}"))
            view.add_item(discord.ui.Button(label="Отклонить", style=discord.ButtonStyle.danger, custom_id=f"duel_decline:{duel['id']}:{invitee_id}"))
    elif duel["status"] == "public" and duel["type"] in ["1v1", "5v5"]:
        view.add_item(discord.ui.Button(label="Присоединиться", style=discord.ButtonStyle.success, custom_id=f"join_public_duel:{duel['id']}"))
    elif duel["status"] == "result_pending":
        view = discord.ui.View()
    # ✅ Убрана кнопка "Отменить дуэль" полностью
    # Если статус "result_canceled" — view пустой (все кнопки пропадают)

    # ---------- Обновление сообщения ----------
    try:
        await message.edit(embed=embed, view=view)
        logger.info(f"Successfully edited duel message {duel['id']}")
    except Exception as e:
        logger.error(f"Failed to edit duel message {duel['id']}: {e}")




# ---------------------- SLASH COMMANDS ----------------------

async def safe_send(target, **kwargs):
    """Safely send message to channel or user, handling Forbidden and rate limits."""
    if not isinstance(target, discord.abc.Messageable):
        logger.warning(f"Invalid target for safe_send: {type(target)}")
        return None
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            return await target.send(**kwargs)
        except discord.Forbidden:
            logger.warning(f"Cannot send to {target}: DMs closed or no perms")
            return None
        except discord.HTTPException as e:
            if e.status == 429:  # Rate limit
                retry_after = getattr(e, 'retry_after', 1)
                logger.warning(f"Rate limited, retrying in {retry_after}s (attempt {attempt + 1})")
                await asyncio.sleep(retry_after)
                continue
            logger.error(f"HTTP error sending message: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error sending message: {e}")
            if attempt == max_retries - 1:
                return None
            await asyncio.sleep(2 ** attempt)  # Exponential backoff
    return None

async def build_duel_embed(duel: dict) -> discord.Embed:
    embed = discord.Embed(title=f"⚔️ Дуэль: {duel['type']}", color=discord.Color.blue())  # Default blue
    
    # Логика цветов по статусу
    if duel["status"] in ["waiting", "public"]:
        embed.color = discord.Color.green()  # Зеленый: новая/открытая
    elif duel["status"] == "active":
        embed.color = discord.Color.yellow()  # Желтый: активна
    elif duel["status"] == "settled":
        embed.color = discord.Color.red()     # Красный: завершена успешно
    elif duel["status"] == "cancelled":
        embed.color = discord.Color.dark_grey()  # Серый/черный: отменена
    
    if duel.get("screenshot_url"):
        embed.set_image(url=duel["screenshot_url"])
    
    if duel["type"] == "1v1":
        p1 = f"<@{duel.get('player1_id', 'N/A')}>"
        p2 = f"<@{duel.get('player2_id', 'N/A')}> " if duel.get('player2_id') else "Свободно"
        embed.add_field(name="Участники", value=f"{p1} vs {p2}", inline=False)
    else:  # 5v5
        team1_id = int(duel.get('team1_id', 0))
        team2_id = int(duel.get('team2_id', 0))
        team1 = await get_team(team1_id)
        team2 = await get_team(team2_id)
        team1_name = team1["name"] if team1 else "Свободно"
        team2_name = team2["name"] if team2 else "Свободно"
        embed.add_field(name="Команды", value=f"{team1_name} vs {team2_name}", inline=False)
    
    embed.add_field(name="Ставка", value=f"{duel['points']} EH Points", inline=True)
    
    # Унифицированный статус на русский
    status_display = {
        "waiting": "Ожидание оппонента",
        "active": "Активна",
        "result_pending": "Ожидание результата",
        "settled": "Завершена",
        "cancelled": "Отменена",
        "public": "Открыта для присоединения",
        "queued": "В очереди",
        "processing": "Обработка"
    }
    embed.add_field(name="Статус", value=status_display.get(duel["status"], duel["status"]), inline=True)
    
    if duel["status"] == "settled":
        winner_side = duel.get("winner_side", "N/A")
        points = duel.get("points", 0)
        total_pot = points * 2
        burned_amount = int(total_pot * DEFAULT_BURN)
        payout = total_pot - burned_amount
        embed.add_field(name="Победитель", value=f"{winner_side} (+{payout} поинтов)", inline=False)
        embed.add_field(name="Сгорело", value=f"{burned_amount} поинтов", inline=True)
    else:
        hints = {
            "waiting": "Ожидаем второго участника.",
            "active": "Дуэль идёт! Загрузите скриншот через /submit_duel.",
            "result_pending": "Ожидаем подтверждения от админа.",
            "cancelled": "Дуэль отменена.",
            "public": "Открыто для присоединения.",
            "queued": "Ожидание бота Dota."
        }
        hint = hints.get(duel["status"], "")
        if hint:
            embed.add_field(name="Подсказка", value=hint, inline=False)

    embed.timestamp = discord.utils.utcnow()
    embed.set_footer(text=f"ID дуэли: {duel['id']}")
    return embed

async def get_team_steam_ids(team_id: int) -> List[str]:
    """Get SteamIDs for all members of a team. Returns available ones, logs warnings if incomplete."""
    team = await get_team(team_id)
    if not team:
        logger.warning(f"Team {team_id} not found")
        return []
    
    uids = [team["leader_id"]]
    for i in range(1, 6):
        pid = team.get(f"player{i}_id")
        if pid:
            uids.append(pid)
    
    steam_ids = []
    missing = []
    for uid in uids:
        resp = supabase.table("users").select("steam_id").eq("user_id", uid).execute()
        steam_id = resp.data[0].get("steam_id") if resp.data else None
        if steam_id:
            steam_ids.append(steam_id)
        else:
            missing.append(uid)
            logger.warning(f"Missing SteamID for team {team_id} member {uid}")
    
    if len(steam_ids) < 5:
        logger.error(f"Team {team_id} has only {len(steam_ids)}/5 SteamIDs: missing {missing}")
        # Не raise – пусть queue_duel рефандит, если incomplete
    else:
        logger.info(f"Full SteamIDs collected for team {team_id}: {len(steam_ids)}")
    
    return steam_ids


@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        logger.info(f"Синхронизировано слэш-команд: {len(synced)}")
    except Exception as e:
        logger.error(f"Ошибка синхронизации команд: {e}")

@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type != discord.InteractionType.component:
        return
    if interaction.data['component_type'] != 2:  # Button
        return
    cid = interaction.data['custom_id']
    if cid.startswith("duel_accept:"):
        parts = cid.split(":")
        if len(parts) < 3:
            await interaction.response.send_message("Неверный формат.", ephemeral=True)
            return
        _, duel_id_str, user_id_str = parts
        duel_id = int(duel_id_str)
        user_id = int(user_id_str)
        
        if interaction.user.id != user_id:
            await interaction.response.send_message("Это не ваше приглашение.", ephemeral=True)
            return
            
        duel = await get_duel(duel_id)
        if not duel or duel["status"] != "waiting":
            await interaction.response.send_message("Приглашение устарело.", ephemeral=True)
            return
            
        await interaction.response.defer(ephemeral=True)
        await update_duel_invite_status(duel_id, user_id, "accepted")
        updated_duel = await get_duel(duel_id)
        
        # Создаем новый embed и disabled view
        new_embed = await build_duel_embed(updated_duel)
        new_embed.add_field(name="Статус", value="Принято! Дуэль активна.", inline=False)
        disabled_view = create_disabled_view("duel_invite")
        
        try:
            await interaction.edit_original_response(embed=new_embed, view=disabled_view)
        except discord.NotFound:
            logger.warning(f"Original message not found for duel accept {duel_id}")
        except Exception as e:
            logger.error(f"Failed to edit duel accept message: {e}")
        
        # Обновляем канал
        if updated_duel.get("message_id"):
            channel = bot.get_channel(int(updated_duel["channel_id"]))
            if channel:
                try:
                    msg = await channel.fetch_message(int(updated_duel["message_id"]))
                    await refresh_duel_message(msg, updated_duel)
                except Exception as e:
                    logger.error(f"Error refreshing channel message {duel_id}: {e}")
        
        await interaction.followup.send("✅ Дуэль активирована!", ephemeral=True)
        return

    elif cid.startswith("duel_decline:"):
        parts = cid.split(":")
        if len(parts) < 3:
            await interaction.response.send_message("Неверный формат.", ephemeral=True)
            return
        _, duel_id_str, user_id_str = parts
        duel_id = int(duel_id_str)
        user_id = int(user_id_str)
        
        if interaction.user.id != user_id:
            await interaction.response.send_message("Это не ваше приглашение.", ephemeral=True)
            return
            
        duel = await get_duel(duel_id)
        if not duel or duel["status"] != "waiting":
            await interaction.response.send_message("Приглашение устарело.", ephemeral=True)
            return
            
        await interaction.response.defer(ephemeral=True)
        await update_duel_invite_status(duel_id, user_id, "declined")
        updated_duel = await get_duel(duel_id)
        
        # Создаем новый embed и disabled view
        new_embed = await build_duel_embed(updated_duel)
        new_embed.add_field(name="Статус", value="Отклонено. Дуэль отменена.", inline=False)
        disabled_view = create_disabled_view("duel_invite")
        
        try:
            await interaction.edit_original_response(embed=new_embed, view=disabled_view)
        except discord.NotFound:
            logger.warning(f"Original message not found for duel decline {duel_id}")
        except Exception as e:
            logger.error(f"Failed to edit duel decline message: {e}")
        
        # Обновляем канал
        if updated_duel.get("message_id"):
            channel = bot.get_channel(int(updated_duel["channel_id"]))
            if channel:
                try:
                    msg = await channel.fetch_message(int(updated_duel["message_id"]))
                    await refresh_duel_message(msg, updated_duel)
                except Exception as e:
                    logger.error(f"Error refreshing channel message {duel_id}: {e}")
        
        await interaction.followup.send("❌ Дуэль отменена.", ephemeral=True)
        return

    elif cid.startswith("team_accept:"):
        parts = cid.split(":")
        if len(parts) < 3:
            await interaction.response.send_message("Неверный формат.", ephemeral=True)
            return
        _, invite_id_str, user_id_str = parts[:3]
        invite_id = int(invite_id_str)
        user_id = int(user_id_str)
        
        if interaction.user.id != user_id:
            await interaction.response.send_message("Это не ваше приглашение.", ephemeral=True)
            return
            
        # Проверяем статус
        invite_resp = supabase.table("team_invites").select("status, team_id").eq("id", invite_id).execute()
        if not invite_resp.data or invite_resp.data[0]["status"] != "pending":
            await interaction.response.send_message("Приглашение устарело.", ephemeral=True)
            return
            
        team_id = int(invite_resp.data[0]["team_id"])
        team = supabase.table("teams").select("*").eq("id", team_id).execute().data[0]
        player_count = sum(1 for i in range(1, 6) if team.get(f"player{i}_id"))
        if player_count >= 5:
            await interaction.response.send_message("Команда заполнена.", ephemeral=True)
            return
            
        await interaction.response.defer()
        
        # Обновляем данные команды
        supabase.table("team_invites").update({"status": "accepted"}).eq("id", invite_id).execute()
        updates = {}
        for i in range(1, 6):
            if team.get(f"player{i}_id") is None:
                updates[f"player{i}_id"] = str(user_id)
                break
        if updates:
            supabase.table("teams").update(updates).eq("id", team_id).execute()
            invites = supabase.table("team_invites").select("status").eq("team_id", team_id).execute().data
            if all(invite["status"] == "accepted" for invite in invites):
                supabase.table("teams").update({"status": "confirmed"}).eq("id", team_id).execute()
        
        # Назначаем роль
        guild_id = team.get("guild_id")
        if guild_id:
            guild = bot.get_guild(int(guild_id))
            if guild:
                member = guild.get_member(user_id)
                if member:
                    await assign_team_role(guild, member, team["name"])
        
        # Уведомляем лидера
        leader_id = int(team["leader_id"])
        leader = bot.get_user(leader_id)
        if leader:
            await safe_send(leader, content=f"<@{user_id}> присоединился к вашей команде!")
        
        # Убираем view и отправляем подтверждение
        empty_view = discord.ui.View()
        try:
            await interaction.edit_original_response(view=empty_view)
        except Exception as e:
            logger.error(f"Failed to edit team accept message: {e}")
            
        await interaction.followup.send("Вы присоединились к команде!", ephemeral=True)
        return

    elif cid.startswith("team_decline:"):
        parts = cid.split(":")
        if len(parts) < 3:
            await interaction.response.send_message("Неверный формат.", ephemeral=True)
            return
        _, invite_id_str, user_id_str = parts[:3]
        invite_id = int(invite_id_str)
        user_id = int(user_id_str)
        
        if interaction.user.id != user_id:
            await interaction.response.send_message("Это не ваше приглашение.", ephemeral=True)
            return
            
        invite_resp = supabase.table("team_invites").select("status").eq("id", invite_id).execute()
        if not invite_resp.data or invite_resp.data[0]["status"] != "pending":
            await interaction.response.send_message("Приглашение устарело.", ephemeral=True)
            return
            
        await interaction.response.defer()
        supabase.table("team_invites").update({"status": "declined"}).eq("id", invite_id).execute()
        
        # Уведомляем лидера
        invite_data = supabase.table("team_invites").select("team_id").eq("id", invite_id).execute().data[0]
        team_id = int(invite_data["team_id"])
        team = supabase.table("teams").select("leader_id").eq("id", team_id).execute().data[0]
        leader_id = int(team["leader_id"])
        leader = bot.get_user(leader_id)
        if leader:
            await safe_send(leader, content=f"<@{user_id}> отклонил приглашение в вашу команду.")
        
        # Убираем view
        empty_view = discord.ui.View()
        try:
            await interaction.edit_original_response(view=empty_view)
        except Exception as e:
            logger.error(f"Failed to edit team decline message: {e}")
            
        await interaction.followup.send("Вы отклонили приглашение.", ephemeral=True)
        return
    elif cid.startswith("join_public_duel:"):
        _, duel_id_str = cid.split(":", 1)
        duel_id = int(duel_id_str)
        user_id = interaction.user.id
        duel = await get_duel(duel_id)
        points = duel["points"]
        if duel["type"] == "1v1":
            ok, msg = await join_public_duel(duel_id, user_id, None, points)
        else:  # 5v5
            team = await get_user_team(user_id)
            if not team or str(user_id) != team["leader_id"]:
                await interaction.response.send_message("Только лидер команды может присоединиться.", ephemeral=True)
                return
            if not await is_team_full_and_confirmed(team):
                await interaction.response.send_message("Команда должна быть полной и подтвержденной.", ephemeral=True)
                return
            ok, msg = await join_public_duel(duel_id, user_id, team["id"], points)
        await interaction.response.send_message(msg, ephemeral=True)
        if ok:
            updated_duel = await get_duel(duel_id)
            await refresh_duel_message(interaction.message, updated_duel)
        return
    elif cid.startswith("cancel_duel:"):
        parts = cid.split(":")
        if len(parts) < 3:
            await interaction.response.send_message("Неверный формат.", ephemeral=True)
            return
        _, duel_id_str, creator_id_str = parts
        duel_id = int(duel_id_str)
        creator_id = int(creator_id_str)
        if interaction.user.id != creator_id:
            await interaction.response.send_message("❌ Только создатель дуэли может её отменить.", ephemeral=True)
            return
        duel = await get_duel(duel_id)
        if duel["status"] not in ["waiting", "public"]:
            await interaction.response.send_message("Дуэль уже идет, загрузите скриншот конца игры.", ephemeral=True)
            return
        
        await add_balance(creator_id, int(duel["points"]))  # Refund создателю
        supabase.table("duels").update({"status": "cancelled", "reason": "cancelled_by_creator"}).eq("id", duel_id).execute()
        updated_duel = await get_duel(duel_id)
        await refresh_duel_message(interaction.message, updated_duel)
        
        # Если есть invitee, уведомить в DM
        invitee_id = duel.get("player2_id") if duel["type"] == "1v1" else await get_team_leader(int(duel.get("team2_id", 0)))
        if invitee_id:
            invitee_user = bot.get_user(int(invitee_id))
            if invitee_user:
                await safe_send(invitee_user, f"Дуэль отменена создателем <@{creator_id}>.")
        
        await interaction.response.send_message("✅ Дуэль отменена. Поинты возвращены.", ephemeral=True)
        
        # ИСПРАВЛЕНИЕ: Создаем новый view с отключенными кнопками вместо редактирования старого
        try:
            # Создаем новый view на основе типа дуэли
            if duel.get("is_public"):
                # Для публичной дуэли
                new_view = discord.ui.View()
                new_view.add_item(discord.ui.Button(
                    label="Присоединиться", 
                    style=discord.ButtonStyle.success, 
                    disabled=True
                ))
                new_view.add_item(discord.ui.Button(
                    label="Отменить дуэль", 
                    style=discord.ButtonStyle.danger, 
                    disabled=True
                ))
            else:
                # Для приватной дуэли
                new_view = discord.ui.View()
                new_view.add_item(discord.ui.Button(
                    label="Отменить дуэль", 
                    style=discord.ButtonStyle.danger, 
                    disabled=True
                ))
            
            await interaction.message.edit(view=new_view)
        except Exception as e:
            logger.error(f"Failed to disable cancel button: {e}")
        
        return
    elif cid.startswith("settle_a:"):
        _, duel_id_str = cid.split(":", 1)
        duel_id = int(duel_id_str)
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Только админ.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        logger.info(f"Admin {interaction.user.id} pressed settle_a for duel {duel_id}")
        try:
            ok, msg = await settle_duel(duel_id, "A")
            updated_duel = await get_duel(duel_id)
            logger.info(f"After settle, duel {duel_id} status: {updated_duel['status'] if updated_duel else 'None'}")
            if ok and updated_duel and updated_duel.get("message_id"):
                channel = bot.get_channel(int(updated_duel["channel_id"]))
                if channel:
                    try:
                        msg = await channel.fetch_message(int(updated_duel["message_id"]))
                        await refresh_duel_message(msg, updated_duel)
                        logger.info(f"Message refreshed for duel {duel_id}")
                    except Exception as e:
                        logger.error(f"Error refreshing duel message {duel_id} in settle_a: {e}")
            await interaction.followup.send(msg if 'msg' in locals() else "Дуэль завершена.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error in settle_a for duel {duel_id}: {e}")
            await interaction.followup.send("Произошла ошибка при завершении дуэли.", ephemeral=True)
        return
    elif cid.startswith("settle_b:"):
        _, duel_id_str = cid.split(":", 1)
        duel_id = int(duel_id_str)
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Только админ.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        logger.info(f"Admin {interaction.user.id} pressed settle_b for duel {duel_id}")
        try:
            ok, msg = await settle_duel(duel_id, "B")
            updated_duel = await get_duel(duel_id)
            logger.info(f"After settle, duel {duel_id} status: {updated_duel['status'] if updated_duel else 'None'}")
            if ok and updated_duel and updated_duel.get("message_id"):
                channel = bot.get_channel(int(updated_duel["channel_id"]))
                if channel:
                    try:
                        msg_obj = await channel.fetch_message(int(updated_duel["message_id"]))
                        await refresh_duel_message(msg_obj, updated_duel)
                        logger.info(f"Message refreshed for duel {duel_id}")
                    except Exception as e:
                        logger.error(f"Error refreshing duel message {duel_id} in settle_b: {e}")
            await interaction.followup.send(msg if 'msg' in locals() else "Дуэль завершена.", ephemeral=True)
        except Exception as e:
            logger.error(f"Error in settle_b for duel {duel_id}: {e}")
            await interaction.followup.send("Произошла ошибка при завершении дуэли.", ephemeral=True)
        return
    elif cid.startswith("cancel_result:"):
        _, duel_id_str = cid.split(":", 1)
        duel_id = int(duel_id_str)
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Только админ.", ephemeral=True)
            return
        duel = await get_duel(duel_id)
        if not duel or duel["status"] != "result_pending":
            await interaction.response.send_message("Дуэль не в статусе для отмены результата.", ephemeral=True)
            return
        # Устанавливаем новый статус
        supabase.table("duels").update({"status": "result_canceled"}).eq("id", duel_id).execute()
        updated_duel = await get_duel(duel_id)
        if updated_duel and updated_duel.get("message_id"):
            channel = bot.get_channel(int(updated_duel["channel_id"]))
            if channel:
                try:
                    msg = await channel.fetch_message(int(updated_duel["message_id"]))
                    await refresh_duel_message(msg, updated_duel)  # Теперь embed серый, view пустой, статус "Результат отменён"
                    logger.info(f"Result canceled for duel {duel_id}")
                except Exception as e:
                    logger.error(f"Error updating message after cancel_result {duel_id}: {e}")
        await interaction.response.send_message("Результат отменён. Дуэль закрыта.", ephemeral=True)
        return
    elif cid.startswith("team_accept:"):
        parts = cid.split(":")
        if len(parts) < 3:
            await interaction.response.send_message("Неверный формат.", ephemeral=True)
            return
        _, invite_id_str, user_id_str = parts[:3]
        invite_id = int(invite_id_str)
        user_id = int(user_id_str)
        if interaction.user.id != user_id:
            await interaction.response.send_message("Это не ваше приглашение.", ephemeral=True)
            return
        # Проверяем статус
        invite_resp = supabase.table("team_invites").select("status, team_id").eq("id", invite_id).execute()
        if not invite_resp.data or invite_resp.data[0]["status"] != "pending":
            await interaction.response.send_message("Приглашение устарело.", ephemeral=True)
            return
        team_id = int(invite_resp.data[0]["team_id"])
        team = supabase.table("teams").select("*").eq("id", team_id).execute().data[0]
        player_count = sum(1 for i in range(1, 6) if team.get(f"player{i}_id"))
        if player_count >= 5:
            await interaction.response.send_message("Команда заполнена.", ephemeral=True)
            return
        # Defer to allow editing
        await interaction.response.defer()
        # Обновляем
        supabase.table("team_invites").update({"status": "accepted"}).eq("id", invite_id).execute()
        updates = {}
        for i in range(1, 6):
            if team.get(f"player{i}_id") is None:
                updates[f"player{i}_id"] = str(user_id)
                break
        if updates:
            supabase.table("teams").update(updates).eq("id", team_id).execute()
            # Check if full now
            invites = supabase.table("team_invites").select("status").eq("team_id", team_id).execute().data
            if all(invite["status"] == "accepted" for invite in invites):
                supabase.table("teams").update({"status": "confirmed"}).eq("id", team_id).execute()
        # Assign role
        guild_id = team.get("guild_id")
        if guild_id:
            guild = bot.get_guild(int(guild_id))
            if guild:
                member = guild.get_member(user_id)
                if member:
                    await assign_team_role(guild, member, team["name"])
        # Уведомить лидера
        leader_id = int(team["leader_id"])
        leader = bot.get_user(leader_id)
        if leader:
            await safe_send(leader, content=f"<@{user_id}> присоединился к вашей команде!")
        # Edit original to remove view
        await interaction.edit_original_response(view=None)
        # Followup confirmation
        await interaction.followup.send("Вы присоединились к команде!", ephemeral=True)
        return
    elif cid.startswith("team_decline:"):
        parts = cid.split(":")
        if len(parts) < 3:
            await interaction.response.send_message("Неверный формат.", ephemeral=True)
            return
        _, invite_id_str, user_id_str = parts[:3]
        invite_id = int(invite_id_str)
        user_id = int(user_id_str)
        if interaction.user.id != user_id:
            await interaction.response.send_message("Это не ваше приглашение.", ephemeral=True)
            return
        # Проверяем статус
        invite_resp = supabase.table("team_invites").select("status").eq("id", invite_id).execute()
        if not invite_resp.data or invite_resp.data[0]["status"] != "pending":
            await interaction.response.send_message("Приглашение устарело.", ephemeral=True)
            return
        # Defer
        await interaction.response.defer()
        supabase.table("team_invites").update({"status": "declined"}).eq("id", invite_id).execute()
        # Уведомить лидера
        invite_data = supabase.table("team_invites").select("team_id").eq("id", invite_id).execute().data[0]
        team_id = int(invite_data["team_id"])
        team = supabase.table("teams").select("leader_id").eq("id", team_id).execute().data[0]
        leader_id = int(team["leader_id"])
        leader = bot.get_user(leader_id)
        if leader:
            await safe_send(leader, content=f"<@{user_id}> отклонил приглашение в вашу команду.")
        # Edit original to remove view
        await interaction.edit_original_response(view=None)
        # Followup
        await interaction.followup.send("Вы отклонили приглашение.", ephemeral=True)
        return
    elif cid.startswith("join_team:"):
        _, team_id_str = cid.split(":", 1)
        team_id = int(team_id_str)
        user_id = interaction.user.id
        # Проверяем SteamID
        resp = supabase.table("users").select("steam_id").eq("user_id", str(user_id)).execute()
        steam_id = resp.data[0]["steam_id"] if resp.data and resp.data[0].get("steam_id") else None
        if not steam_id:
            await interaction.response.send_message("❌ Для присоединения к команде нужно зарегистрировать SteamID.", ephemeral=True)
            return
        # Проверяем, что не в другой команде
        if await get_user_team(user_id):
            await interaction.response.send_message("❌ Вы уже состоите в другой команде.", ephemeral=True)
            return
        # Проверяем команду
        team = await get_team(team_id)
        if not team or not team["is_public"]:
            await interaction.response.send_message("❌ Команда не найдена или не публичная.", ephemeral=True)
            return
        player_count = sum(1 for i in range(1, 6) if team.get(f"player{i}_id"))
        if player_count >= 5:
            await interaction.response.send_message("❌ Команда заполнена.", ephemeral=True)
            return
        # Создаём invite и сразу accept
        now = int(time.time())
        invite_response = supabase.table("team_invites").insert({
            "team_id": team_id,
            "user_id": str(user_id),
            "status": "accepted",
            "created_at": now
        }).execute()
        invite_id = int(invite_response.data[0]["id"])
        # Добавляем в слот
        updates = {}
        for i in range(1, 6):
            if team.get(f"player{i}_id") is None:
                updates[f"player{i}_id"] = str(user_id)
                break
        if updates:
            supabase.table("teams").update(updates).eq("id", team_id).execute()
            # Check if full
            invites = supabase.table("team_invites").select("status").eq("team_id", team_id).execute().data
            if all(invite["status"] == "accepted" for invite in invites):
                supabase.table("teams").update({"status": "confirmed"}).eq("id", team_id).execute()
        # Assign role
        guild_id = team.get("guild_id")
        if guild_id:
            guild = bot.get_guild(int(guild_id))
            if guild:
                member = guild.get_member(user_id)
                if member:
                    await assign_team_role(guild, member, team["name"])
        # Уведомить лидера
        leader_id = int(team["leader_id"])
        leader = bot.get_user(leader_id)
        if leader:
            await safe_send(leader, content=f"<@{user_id}> присоединился к вашей публичной команде через объявление!")
        # Отключить кнопку если full
        if player_count + 1 >= 5:
            view = interaction.message.view
            for item in view.children:
                if isinstance(item, discord.ui.Button) and item.label == "Присоединиться":
                    item.disabled = True
            await interaction.message.edit(view=view)
        await interaction.response.send_message("✅ Вы присоединились к команде!", ephemeral=True)
        return
    # ✅ Добавлено: Обработка неизвестных custom_id - defer чтобы избежать ошибок Discord
    elif cid.startswith("cancel_public_duel:"):
        parts = cid.split(":")
        if len(parts) < 3:
            await interaction.response.send_message("Неверный формат.", ephemeral=True)
            return
        _, duel_id_str, creator_id_str = parts
        duel_id = int(duel_id_str)
        creator_id = int(creator_id_str)
        if interaction.user.id != creator_id:
            await interaction.response.send_message("❌ Только создатель дуэли может её отменить.", ephemeral=True)
            return
        # Логика отмены (как в callback)
        duel = await get_duel(duel_id)
        if duel["status"] != "public":
            await interaction.response.send_message("Дуэль уже идет, загрузите скриншот конца игры.", ephemeral=True)
            return
        await add_balance(creator_id, int(duel["points"]))
        supabase.table("duels").update({"status": "cancelled", "reason": "cancelled_by_creator"}).eq("id", duel_id).execute()
        updated_duel = await get_duel(duel_id)
        await refresh_duel_message(interaction.message, updated_duel)
        await interaction.response.send_message("✅ Дуэль отменена. Поинты возвращены.", ephemeral=True)
    # Обработчик в on_interaction (добавьте elif):
    elif cid.startswith("manual_mmr:"):
            _, user_id_str = cid.split(":", 1)
            user_id = int(user_id_str)
            if interaction.user.id != user_id:
                await interaction.response.send_message("Это не ваше.", ephemeral=True)
                return
            modal = MMRModal()  # Ваш существующий MMR модал
            await interaction.response.send_modal(modal)
    elif cid.startswith("bet:"):
        parts = cid.split(":")
        if len(parts) == 3:
            _, team, match_id_str = parts
            match_id = int(match_id_str)
            modal = BetAmountModal(match_id, team)
            await interaction.response.send_modal(modal)
            return
    else:
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            pass  # Уже acknowledged
    
class MMRModal(Modal, title="Введите ваш MMR"):
    mmr = TextInput(label="MMR", placeholder="Например: 2500")

    async def on_submit(self, interaction: discord.Interaction):
        value = self.mmr.value
        if not value.isdigit() or int(value) <= 0:
            await interaction.response.send_message("❌ MMR должен быть числом > 0", ephemeral=True)
            return

        supabase.table("users").update({"mmr": int(value)}).eq("user_id", str(interaction.user.id)).execute()
        await interaction.response.send_message(f"✅ Ваш MMR установлен: {value}", ephemeral=True)


class SteamIDModal(discord.ui.Modal, title="Введите SteamID (авто-MMR)"):
    steam = discord.ui.TextInput(
        label="Ваш SteamID или Account ID",
        placeholder="933834754 (Account ID) или STEAM_0:0:1234567 или 76561197960265728",
        required=True,
        min_length=1,
        max_length=20,
        custom_id="steam_id_input_unique"  # Unique для избежания дубликатов
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            steam_input = self.steam.value.strip()
            # Новый regex: account ID (7-10 digits), STEAM_0 или 64-bit
            if not re.match(r'^\d{7,10}$|STEAM_0:\d:\d+|7656119\d{10}$', steam_input):
                await interaction.response.send_message("❌ Неверный формат. Примеры: 933834754 (Account ID), STEAM_0:0:1234567 или 76561197960265728", ephemeral=True)
                return
            
            # Получаем MMR (авто, с новым парсингом)
            mmr = await get_mmr_from_steamid(steam_input, interaction.user.id)
            
            # SteamID всегда сохраняется (response check)
            response = supabase.table("users").update({"steam_id": steam_input}).eq("user_id", str(interaction.user.id)).execute()
            if not response.data:
                logger.warning(f"Failed to save steam_id for {interaction.user.id}")
            
            if mmr is not None:
                await interaction.response.send_message(f"✅ Ваш SteamID установлен: {steam_input}", ephemeral=True)
                logger.info(f"Success: SteamID {steam_input} for {interaction.user.id}, MMR {mmr}")
            else:
                await interaction.response.send_message(f"✅ Ваш SteamID установлен: {steam_input}\n❌ Не удалось получить MMR. Проверьте публичность профиля в Dota 2.", ephemeral=True)
                logger.warning(f"SteamID set but no MMR for {interaction.user.id}: {steam_input}")
        except Exception as e:
            logger.error(f"Error in SteamIDModal for {interaction.user.id}: {e}")
            await interaction.response.send_message("❌ Что-то пошло не так. Проверьте логи бота.", ephemeral=True)


class RegisterView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # Persistent

    @discord.ui.button(label="Ввести SteamID (авто-MMR)", style=discord.ButtonStyle.primary, custom_id="register_steam_unique")
    async def steam_button(self, interaction: discord.Interaction, button: Button):
        try:
            await interaction.response.send_modal(SteamIDModal())
        except discord.HTTPException as e:
            logger.error(f"HTTP error sending modal: {e}")
            if "50035" in str(e) or "duplicated" in str(e).lower():
                await interaction.response.send_message("❌ Ошибка формы. Обновите сообщение регистрации.", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Ошибка. Попробуйте позже.", ephemeral=True)
        except Exception as e:
            logger.error(f"Unexpected error in steam_button: {e}")
            await interaction.response.send_message("❌ Что-то сломалось. Проверьте логи.", ephemeral=True)


# Группа команд для модераторов
moderator_group = app_commands.Group(name="moderator", description="Управление модераторами дуэлей")

@moderator_group.command(name="add", description="Добавить модератора")
@app_commands.describe(user="Пользователь для добавления")
@admin_only
async def moderator_add(interaction: discord.Interaction, user: discord.User):
    if await is_moderator(user.id, str(interaction.guild.id)):
        await interaction.response.send_message("Этот пользователь уже модератор.", ephemeral=True)
        return
    if await add_moderator(user.id, str(interaction.guild.id)):
        await interaction.response.send_message(f"✅ {user.mention} добавлен как модератор.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Ошибка добавления.", ephemeral=True)

@moderator_group.command(name="kick", description="Удалить модератора")
@app_commands.describe(user="Пользователь для удаления")
@admin_only
async def moderator_kick(interaction: discord.Interaction, user: discord.User):
    if not await is_moderator(user.id, str(interaction.guild.id)):
        await interaction.response.send_message("Этот пользователь не модератор.", ephemeral=True)
        return
    if await remove_moderator(user.id, str(interaction.guild.id)):
        await interaction.response.send_message(f"✅ {user.mention} удалён из модераторов.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Ошибка удаления.", ephemeral=True)

@moderator_group.command(name="list", description="Список модераторов")
@admin_only
async def moderator_list(interaction: discord.Interaction):
    mods = await get_moderators(str(interaction.guild.id))
    if not mods:
        await interaction.response.send_message("❌ Нет модераторов.", ephemeral=True)
        return
    desc = "\n".join([f"<@{uid}>" for uid in mods])
    embed = discord.Embed(title="👮 Модераторы дуэлей", description=desc, color=discord.Color.orange())
    await interaction.response.send_message(embed=embed, ephemeral=True)

# Регистрация группы в on_ready или после bot=...
bot.tree.add_command(moderator_group)

# Команда для запуска регистрации
@bot.tree.command(name="setup_register", description="Отправить сообщение с кнопками регистрации")
@app_commands.describe(channel="Канал для сообщения (по умолчанию — текущий)")
@admin_only
async def setup_register(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    target_channel = channel or interaction.channel
    if not target_channel:
        await interaction.response.send_message("❌ Укажите канал или используйте в канале.", ephemeral=True)
        return

    # Проверка на дубликат (опционально: сохраните REGISTER_MESSAGE_ID в .env)
    register_msg_id = os.getenv("REGISTER_MESSAGE_ID")
    if register_msg_id:
        try:
            msg = await target_channel.fetch_message(int(register_msg_id))
            await interaction.response.send_message(f"✅ Сообщение регистрации уже существует: {msg.jump_url}", ephemeral=True)
            return
        except discord.NotFound:
            pass  # Старое сообщение удалено, создаём новое

    embed = discord.Embed(
        title="👋 Добро пожаловать на сервер!",
        description="Чтобы получить доступ к дуэлям и командам, зарегистрируйтесь:\n• Укажите SteamID\n\nПосле регистрации вы сможете участвовать в играх! 🚀",
        color=discord.Color.green()
    )
    embed.set_thumbnail(url=interaction.guild.icon.url if interaction.guild.icon else None)

    view = RegisterView()
    await target_channel.send(embed=embed, view=view)
    
    # Сохраните ID в env или БД (для примера — в файл, но лучше в Supabase)
    with open('.register_msg_id', 'w') as f:
        f.write(str(target_channel.last_message.id))
    
    await interaction.response.send_message(f"✅ Сообщение отправлено в {target_channel.mention}!", ephemeral=True)

@bot.tree.command(name="balance", description="Показать баланс поинтов")
@app_commands.describe(user="Чей баланс посмотреть (если не указать, то ваш)")
async def balance_cmd(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    target = user or interaction.user
    bal = await get_balance(target.id)
    if user:
        await interaction.response.send_message(
            f"Баланс {target.mention}: **{bal}** поинтов", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"Ваш баланс: **{bal}** поинтов", ephemeral=True
        )

@bot.tree.command(name="leaderboard", description="Показать топ игроков по балансу")
async def leaderboard_cmd(interaction: discord.Interaction):
    data = supabase.table("users").select("user_id,balance").order("balance", desc=True).limit(100).execute().data
    if not data:
        await interaction.response.send_message("❌ Лидерборд пуст.", ephemeral=True)
        return

    view = LeaderboardView(data)

    # первый ответ — теперь приватный
    await interaction.response.send_message("Загрузка...", view=view, ephemeral=True)

    # редактируем только для вызвавшего
    msg = await interaction.original_response()
    start = 0
    end = view.per_page
    chunk = data[start:end]

    desc = "\n".join(
        [f"**{i+1}.** <@{row['user_id']}> — {row['balance']}💰" for i, row in enumerate(chunk, start=start)]
    )

    embed = discord.Embed(
        title=f"🏆 Лидерборд (стр. 1/{view.max_page+1})",
        description=desc,
        color=discord.Color.gold()
    )
    await msg.edit(content=None, embed=embed, view=view)

@bot.tree.command(name="teams", description="Показать список всех команд")
async def teams_cmd(interaction: discord.Interaction):
    data = supabase.table("teams").select("id,name,leader_id,status,is_public,player1_id,player2_id,player3_id,player4_id,player5_id").order("created_at", desc=True).execute().data
    if not data:
        await interaction.response.send_message("❌ Команд нет.", ephemeral=True)
        return

    view = TeamsView(data)

    # Send initial response (private)
    await interaction.response.send_message("Загрузка...", view=view, ephemeral=True)

    # Edit response with team list
    msg = await interaction.original_response()
    start = 0
    end = view.per_page
    chunk = data[start:end]

    players_list = []
    for i, row in enumerate(chunk, start=start):
        players = [row.get(f'player{j}_id') for j in range(1, 6) if row.get(f'player{j}_id')]
        participants_str = " ".join([f"<@{p}>" for p in players if p != row['leader_id']]) if players else "❌ Нет участников"
        players_list.append(
            f"**{i + 1}. {row['name']}**"
            f"👑 **Лидер:** <@{row['leader_id']}>\n"
            f"👥 **Участники:** {participants_str}"
        )

    desc = "\n\n".join(players_list) or "Нет команд"

    embed = discord.Embed(
        title=f"👥 Команды (стр. 1/{view.max_page+1})",
        description=desc,
        color=discord.Color.blue()
    )
    await msg.edit(content=None, embed=embed, view=view)


@app_commands.default_permissions(manage_guild=True)
@bot.tree.command(name="grant", description="Выдать поинты пользователю (админ)")
@app_commands.describe(user="Кому выдать", amount="Сколько поинтов выдать (+/-)")
@admin_only
async def grant_cmd(interaction: discord.Interaction, user: discord.User, amount: int):
    if amount == 0:
        await interaction.response.send_message("Сумма должна быть ненулевой.", ephemeral=True)
        return

    await add_balance(user.id, amount)
    new_bal = await get_balance(user.id)
    await interaction.response.send_message(
        f"Выдано {amount} поинтов {user.mention}. Новый баланс: {new_bal}",
        ephemeral=True
    )


@app_commands.default_permissions(manage_guild=True)
@bot.tree.command(name="bid", description="Создать матч со ставками")
@app_commands.describe(team_a="Команда A", team_b="Команда B", burn="Доля сгорания (0.2-0.3), по умолчанию 0.25")
@admin_only
async def bid_cmd(interaction: discord.Interaction, team_a: str, team_b: str, burn: Optional[float] = None):
    b = DEFAULT_BURN if burn is None else float(burn)
    if b < 0 or b > 0.9:
        await interaction.response.send_message("burn должен быть от 0 до 0.9", ephemeral=True)
        return

    match_id = await create_match(interaction.channel_id, team_a, team_b, b)

    embed = discord.Embed(title=f"Матч: {team_a} vs {team_b}", color=discord.Color.blurple())
    embed.add_field(name=f"Банк {team_a}", value="0 поинтов", inline=True)
    embed.add_field(name=f"Банк {team_b}", value="0 поинтов", inline=True)
    embed.add_field(name="Статус", value="Открыта", inline=False)
    embed.set_footer(text=f"match:{match_id}")

    view = MatchView(match_id, team_a, team_b, "Открыта")

    await interaction.response.send_message(embed=embed, view=view)
    msg = await interaction.original_response()
    await set_match_message(match_id, msg.id)

@app_commands.default_permissions(manage_guild=True)
@bot.tree.command(name="close_bet", description="Закрыть прием ставок (без расчета)")
@app_commands.describe(match_id="ID матча")
@admin_only
async def close_bet_cmd(interaction: discord.Interaction, match_id: int):
    ok, msg = await close_bet(match_id)
    await interaction.response.send_message(msg)
    if ok:
        await refresh_match_message(interaction, match_id)

@app_commands.default_permissions(manage_guild=True)
@bot.tree.command(name="cancel_bet", description="Отменить матч и вернуть всем ставки")
@app_commands.describe(match_id="ID матча")
@admin_only
async def cancel_bet_cmd(interaction: discord.Interaction, match_id: int):
    ok, msg, _ = await cancel_bet(match_id)
    await interaction.response.send_message(msg)
    if ok:
        await refresh_match_message(interaction, match_id)

@app_commands.default_permissions(manage_guild=True)
@bot.tree.command(name="settle_bet", description="Завершить матч: указать победителя и выплатить")
@app_commands.describe(match_id="ID матча", winner="Победитель: A или B")
@admin_only
async def settle_bet_cmd(interaction: discord.Interaction, match_id: int, winner: str):
    ok, msg = await settle_bet(match_id, winner)
    await interaction.response.send_message(msg)
    if ok:
        await refresh_match_message(interaction, match_id)

# Полный duel_cmd с интеграцией CancelDuelView (для private блоков)
@bot.tree.command(name="duel", description="Создать дуэль")
@app_commands.describe(
    type="Тип дуэли (1v1 или 5v5)",
    points="Ставка поинтов (50-200)",
    opponent="Оппонент (игрок для 1v1 или лидер для 5v5; если не указать — публичная дуэль)"
)
@app_commands.choices(
    type=[
        app_commands.Choice(name="1v1", value="1v1"),
        app_commands.Choice(name="5v5", value="5v5")
    ]
)
async def duel_cmd(interaction: discord.Interaction, type: str, points: int = 100, opponent: Optional[discord.Member] = None):
    user_id = interaction.user.id
    bal = await get_balance(user_id)
    try:
        if points < 50 or points > 200:
            await interaction.response.send_message("Ставка должна быть 50-200 поинтов.", ephemeral=True)
            return

        if bal < points:
            await interaction.response.send_message(f"Недостаточно поинтов: {bal}.", ephemeral=True)
            return

        # ✅ Новый check: нет ли pending дуэли
        pending_duel_id = await has_pending_duel(user_id)
        if pending_duel_id:
            await interaction.response.send_message(f"❌ У вас уже есть открытая дуэль #{pending_duel_id}. Дождитесь ответа или отмените её.", ephemeral=True)
            return

        is_public = opponent is None

        if type == "1v1":
            if not is_public and opponent is None:
                await interaction.response.send_message("Для приватной 1v1 укажите оппонента.", ephemeral=True)
                return
            if opponent and opponent.id == user_id:
                await interaction.response.send_message("Нельзя вызвать себя.", ephemeral=True)
                return
            if not is_public:
                # Optional: check pending для opponent
                opp_pending = await has_pending_duel(opponent.id)
                if opp_pending:
                    await interaction.response.send_message(f"У оппонента уже есть открытая дуэль #{opp_pending}.", ephemeral=True)
                    return
                if not await check_duel_limit(opponent.id):
                    await interaction.response.send_message("Оппонент уже дуэлился сегодня.", ephemeral=True)
                    return
                opponent_bal = await get_balance(opponent.id)
                if opponent_bal < points:
                    await interaction.response.send_message(f"У {opponent.mention} недостаточно: {opponent_bal}.", ephemeral=True)
                    return
                await add_balance(user_id, -points)
                duel_id = await create_duel(interaction.channel_id, player1_id=user_id, player2_id=opponent.id, points=points, duel_type="1v1", is_public=False, creator_user_id=user_id)
                # DM to opponent
                opponent_user = opponent
                embed = await build_duel_embed(await get_duel(duel_id))
                view = DuelInviteView(duel_id, opponent.id)
                await safe_send(opponent_user, embed=embed, view=view)
                # Main response
                embed = await build_duel_embed(await get_duel(duel_id))
                view = CancelDuelView(duel_id, user_id)  # Или PublicDuelView для private? Adjust
                await interaction.response.send_message(embed=embed, view=view)
                msg = await interaction.original_response()
                await set_duel_message(duel_id, msg.id)
            else:  # public 1v1
                await add_balance(user_id, -points)
                duel_id = await create_duel(interaction.channel_id, player1_id=user_id, points=points, duel_type="1v1", is_public=True, creator_user_id=user_id)
                await interaction.response.defer()
                await interaction.followup.send("Создаётся публичная дуэль (без оппонента).", ephemeral=True)
                embed = await build_duel_embed(await get_duel(duel_id))
                view = PublicDuelView(duel_id, user_id)
                await interaction.edit_original_response(embed=embed, view=view)
                await set_duel_message(duel_id, interaction.message.id if interaction.message else 0)
                asyncio.create_task(auto_refund_public_duel(duel_id, user_id, points))

        else:  # 5v5
            user_team = await get_user_team(user_id)
            if not user_team or str(user_id) != user_team["leader_id"]:
                await interaction.response.send_message("Для 5v5 вы должны быть лидером команды.", ephemeral=True)
                return
            if not await is_team_full_and_confirmed(user_team):
                await interaction.response.send_message("Ваша команда должна быть полной и подтвержденной.", ephemeral=True)
                return
            if not is_public:
                if opponent is None:
                    await interaction.response.send_message("Для приватной 5v5 укажите оппонента (лидера).", ephemeral=True)
                    return
                opponent_team = await get_user_team(opponent.id)
                if not opponent_team or str(opponent.id) != opponent_team["leader_id"]:
                    await interaction.response.send_message("Оппонент должен быть лидером команды.", ephemeral=True)
                    return
                if not await is_team_full_and_confirmed(opponent_team):
                    await interaction.response.send_message("Команда оппонента не полная.", ephemeral=True)
                    return
                # Optional: check pending для opponent
                opp_pending = await has_pending_duel(opponent.id)
                if opp_pending:
                    await interaction.response.send_message(f"У оппонента уже есть открытая дуэль #{opp_pending}.", ephemeral=True)
                    return
                if not await check_duel_limit(opponent.id):
                    await interaction.response.send_message("Лидер оппонента уже дуэлился сегодня.", ephemeral=True)
                    return
                opponent_bal = await get_balance(opponent.id)
                if opponent_bal < points:
                    await interaction.response.send_message(f"У лидера оппонента недостаточно: {opponent_bal}.", ephemeral=True)
                    return
                await add_balance(user_id, -points)
                duel_id = await create_duel(interaction.channel_id, team1_id=user_team["id"], team2_id=opponent_team["id"], points=points, duel_type="5v5", is_public=False, creator_user_id=user_id)
                embed = await build_duel_embed(await get_duel(duel_id))
                view = DuelInviteView(duel_id, opponent.id)
                await safe_send(opponent, embed=embed, view=view)
                embed = await build_duel_embed(await get_duel(duel_id))
                view = CancelDuelView(duel_id, user_id)
                await interaction.response.send_message(embed=embed, view=view)
                msg = await interaction.original_response()
                await set_duel_message(duel_id, msg.id)
            else:  # public 5v5
                await add_balance(user_id, -points)
                duel_id = await create_duel(interaction.channel_id, team1_id=user_team["id"], points=points, duel_type="5v5", is_public=True, creator_user_id=user_id)
                await interaction.response.defer()
                await interaction.followup.send("Создаётся публичная дуэль (без оппонента).", ephemeral=True)
                embed = await build_duel_embed(await get_duel(duel_id))
                view = PublicDuelView(duel_id, user_id)
                await interaction.edit_original_response(embed=embed, view=view)
                await set_duel_message(duel_id, interaction.message.id if interaction.message else 0)
                asyncio.create_task(auto_refund_public_duel(duel_id, user_id, points))

    except Exception as e:
        logger.error(f"Error in duel_cmd for user {user_id}: {e}")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(f"❌ Ошибка создания дуэли: {str(e)[:100]}...", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ Ошибка создания дуэли: {str(e)[:100]}...", ephemeral=True)
        except:
            pass
        if bal >= points:
            await add_balance(user_id, points)



async def refresh_duel_message(message: discord.Message, duel: dict):
    """Refresh the duel message with updated data."""
    # Сохраняем старую картинку, если была
    old_embed = message.embeds[0] if message.embeds else None
    screenshot_url = old_embed.image.url if old_embed and old_embed.image else None
    
    embed = await build_duel_embed(duel)
    if screenshot_url:
        embed.set_image(url=screenshot_url)

    guild = message.guild  # Получаем guild из сообщения

    # Определение имён для кнопок
    winner_a_name = f"Игрок {duel.get('player1_id', '?')}"
    winner_b_name = f"Игрок {duel.get('player2_id', '?')}"

    try:
        if duel["type"] == "1v1":
            player1_id = duel.get("player1_id")
            player2_id = duel.get("player2_id")

            if player1_id:
                try:
                    member1 = guild.get_member(int(player1_id)) if guild else None
                    if member1:
                        winner_a_name = member1.display_name
                except Exception as e:
                    logger.warning(f"Ошибка при обработке player1_id {player1_id}: {e}")

            if player2_id:
                try:
                    member2 = guild.get_member(int(player2_id)) if guild else None
                    if member2:
                        winner_b_name = member2.display_name
                except Exception as e:
                    logger.warning(f"Ошибка при обработке player2_id {player2_id}: {e}")

        elif duel["type"] == "5v5":
            team1_leader = await get_team_leader(int(duel.get("team1_id", 0)))
            if team1_leader:
                member1 = guild.get_member(team1_leader) if guild else None
                winner_a_name = member1.display_name if member1 else f"Лидер {team1_leader}"

            team2_leader = await get_team_leader(int(duel.get("team2_id", 0)))
            if team2_leader:
                member2 = guild.get_member(team2_leader) if guild else None
                winner_b_name = member2.display_name if member2 else f"Лидер {team2_leader}"

    except Exception as e:
        logger.error(f"Ошибка при определении имён: {e}")

    logger.info(f"Names for duel {duel['id']}: A='{winner_a_name}', B='{winner_b_name}'")

    # Цвет и подсказка для отменённого результата
    if duel.get("status") == "result_canceled":
        embed.color = discord.Color.grey()  # Серая линия
        embed.add_field(name="Статус", value="Результат отменён", inline=False)  # Подсказка

    # ---------- Кнопки ----------
    view = discord.ui.View()
    
    if duel["status"] == "waiting" and not duel["is_public"]:
        invitee_id = duel.get("player2_id") if duel["type"] == "1v1" else await get_team_leader(int(duel.get("team2_id", 0)))
        if invitee_id:
            view.add_item(discord.ui.Button(label="Присоединиться", style=discord.ButtonStyle.success, custom_id=f"duel_accept:{duel['id']}:{invitee_id}"))
            view.add_item(discord.ui.Button(label="Отклонить", style=discord.ButtonStyle.danger, custom_id=f"duel_decline:{duel['id']}:{invitee_id}"))
            # Добавляем кнопку отмены только для waiting статуса
            view.add_item(discord.ui.Button(label="Отменить дуэль", style=discord.ButtonStyle.secondary, custom_id=f"cancel_duel:{duel['id']}:{duel.get('creator_id', 0)}"))
    
    elif duel["status"] == "public" and duel["type"] in ["1v1", "5v5"]:
        view.add_item(discord.ui.Button(label="Присоединиться", style=discord.ButtonStyle.success, custom_id=f"join_public_duel:{duel['id']}"))
        # Добавляем кнопку отмены только для public статуса
        view.add_item(discord.ui.Button(label="Отменить дуэль", style=discord.ButtonStyle.secondary, custom_id=f"cancel_public_duel:{duel['id']}:{duel.get('creator_id', 0)}"))
    
    elif duel["status"] == "result_pending":
        view.add_item(discord.ui.Button(label=f"Победил {winner_a_name}", style=discord.ButtonStyle.primary, custom_id=f"settle_a:{duel['id']}"))
        view.add_item(discord.ui.Button(label=f"Победил {winner_b_name}", style=discord.ButtonStyle.primary, custom_id=f"settle_b:{duel['id']}"))
        view.add_item(discord.ui.Button(label="Отменить результат", style=discord.ButtonStyle.danger, custom_id=f"cancel_result:{duel['id']}"))
        # НЕ добавляем кнопку "Отменить дуэль" для result_pending
    
    # Для всех остальных статусов (active, settled, cancelled, result_canceled) 
    # кнопка "Отменить дуэль" НЕ добавляется

    # ---------- Обновление сообщения ----------
    try:
        await message.edit(embed=embed, view=view)
        logger.info(f"Successfully edited duel message {duel['id']}")
    except Exception as e:
        logger.error(f"Failed to edit duel message {duel['id']}: {e}")



@bot.tree.command(name="create_team", description="Создать команду")
@app_commands.describe(name="Название команды", public="Публичная (true) или приватная (false) команда")
async def create_team_cmd(interaction: discord.Interaction, name: str, public: bool = False):
    # Check if user has SteamID
    response = supabase.table("users").select("steam_id").eq("user_id", str(interaction.user.id)).execute()
    steam_id = response.data[0]["steam_id"] if response.data and response.data[0].get("steam_id") else None
    if not steam_id:
        await interaction.response.send_message("❌ Для создания команды нужно зарегистрировать SteamID через /steamid.", ephemeral=True)
        return

    # Check if user is already in a team
    existing_team = await get_user_team(interaction.user.id)
    if existing_team:
        await interaction.response.send_message("❌ Вы уже состоите в другой команде.", ephemeral=True)
        return

    # Create team role
    role = await ensure_team_role(interaction.guild, name)
    if not role:
        await interaction.response.send_message("❌ Не удалось создать роль для команды.", ephemeral=True)
        return
    await interaction.user.add_roles(role)

    # Create team in database
    now = int(time.time())
    team_response = supabase.table("teams").insert({
        "leader_id": str(interaction.user.id),
        "player1_id": str(interaction.user.id),  # Leader as player1
        "name": name,
        "status": "pending",
        "is_public": public,
        "guild_id": str(interaction.guild.id),  # Добавляем guild_id
        "created_at": now
    }).execute()
    team_id = team_response.data[0]["id"]

    # Add leader to team_invites
    supabase.table("team_invites").insert({
        "team_id": team_id,
        "user_id": str(interaction.user.id),
        "status": "accepted",
        "created_at": now
    }).execute()

    # Send private confirmation to the leader
    embed = discord.Embed(title="Команда создана!", color=discord.Color.green())
    embed.add_field(name="Название", value=name, inline=False)
    embed.add_field(name="Лидер", value=f"<@{interaction.user.id}>", inline=False)
    embed.add_field(name="Тип", value="Публичная" if public else "Приватная", inline=False)
    embed.set_footer(text=f"team:{team_id}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

    # Announce public team in the designated channel
    if public:
        channel_id = TEAM_ANNOUNCEMENT_CHANNEL or str(interaction.channel_id)
        channel = bot.get_channel(int(channel_id))
        if not channel:
            logger.warning(f"Could not find team announcement channel {channel_id}")
            return

        embed = discord.Embed(title=f"Новая публичная команда: {name}", color=discord.Color.blue())
        embed.add_field(name="Лидер", value=f"<@{interaction.user.id}>", inline=False)
        embed.add_field(name="Игроки", value=f"<@{interaction.user.id}>", inline=False)
        embed.add_field(name="Статус", value="pending", inline=False)
        embed.set_footer(text=f"team:{team_id}")
        view = JoinTeamView(team_id)
        msg = await safe_send(channel, embed=embed, view=view)
        if msg:
            supabase.table("teams").update({"announcement_message_id": msg.id}).eq("id", team_id).execute()


@bot.tree.command(name="invite_member", description="Пригласить игроков в команду")
@app_commands.describe(
    user1="Игрок 1",
    user2="Игрок 2",
    user3="Игрок 3",
    user4="Игрок 4"
)
async def invite_member_cmd(
    interaction: discord.Interaction,
    user1: Optional[discord.Member] = None,
    user2: Optional[discord.Member] = None,
    user3: Optional[discord.Member] = None,
    user4: Optional[discord.Member] = None
):
    team = await get_user_team(interaction.user.id)
    if not team or str(interaction.user.id) != team["leader_id"]:
        await interaction.response.send_message("❌ Вы должны быть лидером команды, чтобы приглашать участников.", ephemeral=True)
        return

    users = [u for u in [user1, user2, user3, user4] if u]
    if not users:
        await interaction.response.send_message("❌ Укажите хотя бы одного игрока.", ephemeral=True)
        return

    for u in users:
        # Проверяем SteamID
        resp = supabase.table("users").select("steam_id").eq("user_id", str(u.id)).execute()
        steam_id = resp.data[0]["steam_id"] if resp.data and resp.data[0].get("steam_id") else None
        if not steam_id:
            await interaction.response.send_message(f"❌ У {u.mention} нет зарегистрированного SteamID.", ephemeral=True)
            continue

        # Проверяем, что игрок не в другой команде
        if await get_user_team(u.id):
            await interaction.response.send_message(f"❌ {u.mention} уже состоит в другой команде.", ephemeral=True)
            continue

        # Проверяем существующий invite
        existing = supabase.table("team_invites") \
            .select("id,status") \
            .eq("team_id", team["id"]) \
            .eq("user_id", str(u.id)) \
            .execute().data

        invite_id = None
        if existing:
            if existing[0]["status"] in ("pending", "accepted"):
                await interaction.response.send_message(
                    f"❌ {u.mention} уже приглашён или состоит в этой команде.",
                    ephemeral=True
                )
                continue
            else:
                # Обновляем старое приглашение
                invite_id = int(existing[0]["id"])
                supabase.table("team_invites").update({
                    "status": "pending",
                    "created_at": int(time.time())
                }).eq("id", invite_id).execute()
        else:
            # Создаём новое приглашение
            response = supabase.table("team_invites").insert({
                "team_id": team["id"],
                "user_id": str(u.id),
                "status": "pending",
                "created_at": int(time.time())
            }).execute()
            invite_id = int(response.data[0]["id"])

        # Отправляем ЛС с View
        view = TeamInviteView(invite_id, u.id)
        embed = discord.Embed(title="Приглашение в команду!", color=discord.Color.blue())
        embed.add_field(name="Команда", value=team["name"], inline=False)
        embed.add_field(name="Лидер", value=f"<@{interaction.user.id}>", inline=False)
        embed.set_footer(text=f"team:{team['id']}:{u.id}")

        try:
            await safe_send(u, embed=embed, view=view)
            await interaction.response.send_message(f"✅ Приглашение отправлено {u.mention}.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message(f"❌ Не удалось отправить приглашение {u.mention} (ЛС закрыты).", ephemeral=True)


@bot.tree.command(name="check_team", description="Показать состав вашей команды")
async def check_team_cmd(interaction: discord.Interaction):
    # Send initial response to avoid timeout
    await interaction.response.send_message("Загрузка состава команды...", ephemeral=True)

    # Fetch team
    team = await get_user_team(interaction.user.id)
    if not team:
        msg = await interaction.original_response()
        await msg.edit(content="❌ Вы не состоите в команде.")
        return

    # Create embed
    embed = discord.Embed(title=f"Команда: {team['name']}", color=discord.Color.blue())
    
    # Set leader's avatar as thumbnail
    if interaction.guild:
        leader_member = interaction.guild.get_member(int(team["leader_id"]))
        if leader_member:
            embed.set_thumbnail(url=leader_member.display_avatar.url)
    
    embed.add_field(name="Лидер", value=f"<@{team['leader_id']}>", inline=False)
    
    # Fetch players
    players = [team.get(f"player{i}_id") for i in range(1, 6) if team.get(f"player{i}_id")]
    if players:
        for i, player_id in enumerate(players, 1):
            embed.add_field(name=f"Игрок {i}", value=f"<@{player_id}>", inline=True)
    else:
        embed.add_field(name="Игроки", value="Нет активных игроков", inline=False)
    
    embed.add_field(name="Статус команды", value=team["status"], inline=False)
    embed.add_field(name="Тип", value="Публичная" if team["is_public"] else "Приватная", inline=False)
    embed.set_footer(text=f"team:{team['id']}")

    # Edit original message
    msg = await interaction.original_response()
    await msg.edit(content=None, embed=embed)

@bot.tree.command(name="profile", description="Показать профиль игрока")
@app_commands.describe(user="Чей профиль посмотреть (если не указать, то ваш)")
async def profile_cmd(interaction: discord.Interaction, user: Optional[discord.Member] = None):
    # Сразу даем ответ, чтобы не было таймаута взаимодействия
    await interaction.response.send_message("Загрузка профиля...", ephemeral=True)

    # Теперь выполняем всю работу
    target = user or interaction.user

    # Убедимся, что юзер есть в базе
    await ensure_user(target.id, target.display_name)

    # Получаем данные из базы
    resp = supabase.table("users").select("steam_id, mmr").eq("user_id", str(target.id)).execute()
    user_data = resp.data[0] if resp.data else {}
    steam_id = user_data.get("steam_id")
    mmr_value = user_data.get("mmr", 0)

    if mmr_value != 0 and mmr_value is not None:
        # Ручной MMR из БД → эмодзи по числу
        rank_display = get_rank_emoji(int(mmr_value))
    else:
        # Fallback: тянем rank_tier из API, если SteamID есть
        if steam_id:
            rank_tier = await get_rank_tier_from_steamid(steam_id)
            if rank_tier is not None:
                rank_display = get_rank_emoji_from_tier(rank_tier)
            else:
                rank_display = "❓ Не указан"
        else:
            rank_display = "❓ Не указан"

    # Проверяем команду
    team = await get_user_team(target.id)
    team_name = team["name"] if team else "Не состоит"

    # Получаем аватар: если в гильдии, пользуемся display_avatar
    avatar_url = ""
    if interaction.guild:
        member = interaction.guild.get_member(target.id)
        if member:
            avatar_url = member.display_avatar.url
    if not avatar_url:
        avatar_url = target.avatar.url if target.avatar else None

    # ✅ Dotabuff ссылка
    dotabuff_link = ""
    if steam_id:
        account_id = get_dotabuff_account_id(steam_id)
        if account_id:
            dotabuff_link = f"[Dotabuff](https://www.dotabuff.com/players/{account_id})"
        else:
            dotabuff_link = "❌ Неверный SteamID"
    else:
        dotabuff_link = "Не зарегистрирован"

    # Создаем красивый embed
    embed = discord.Embed(
        title=f"🎖️ Профиль игрока: {target.display_name}",
        color=discord.Color.blue()  # Синий цвет для профиля
    )

    if avatar_url:
        embed.set_thumbnail(url=avatar_url)

    embed.add_field(name="👤 Никнейм", value=target.mention, inline=True)
    embed.add_field(name="🎮 SteamID", value=f"`{steam_id}`" if steam_id and steam_id != "Не указан" else "Не указан", inline=True)
    embed.add_field(name="🔗 Dotabuff", value=dotabuff_link, inline=True)  # ✅ Новый field с ссылкой
    embed.add_field(name="👥 Команда", value=team_name, inline=True)
    embed.add_field(name="🏆 Ранг", value=rank_display, inline=False)
    embed.add_field(name="💰 Баланс", value=f"{await get_balance(target.id)} поинтов", inline=True)

    embed.set_footer(text=f"ID пользователя: {target.id}")

    # Редактируем сообщение на финальный embed
    final_embed = embed
    msg = await interaction.original_response()
    await msg.edit(content=None, embed=final_embed)


@bot.tree.command(name="kick", description="Кикнуть игрока из вашей команды")
@app_commands.describe(user="Игрок, которого нужно кикнуть")
async def kick_cmd(interaction: discord.Interaction, user: discord.Member):
    team = await get_user_team(interaction.user.id)
    if not team:
        await interaction.response.send_message("❌ Вы не состоите в команде.", ephemeral=True)
        return

    if str(interaction.user.id) != team["leader_id"]:
        await interaction.response.send_message("❌ Только лидер может кикать игроков.", ephemeral=True)
        return

    if str(user.id) == team["leader_id"]:
        await interaction.response.send_message("❌ Лидера нельзя кикнуть.", ephemeral=True)
        return

    # Проверяем, что игрок реально в команде
    invite_data = supabase.table("team_invites").select("status").eq("team_id", team["id"]).eq("user_id", str(user.id)).execute().data
    if not invite_data:
        await interaction.response.send_message("❌ Этот игрок не состоит в вашей команде.", ephemeral=True)
        return

    invite_status = invite_data[0]["status"]
    if invite_status not in ("pending", "accepted"):
        await interaction.response.send_message(f"❌ {user.mention} уже не состоит в вашей команде (статус: {invite_status}).", ephemeral=True)
        return

    # Обновляем статус приглашения
    supabase.table("team_invites").update({"status": "left"}).eq("team_id", team["id"]).eq("user_id", str(user.id)).execute()
    
    # Clear player slot
    team = supabase.table("teams").select("*").eq("id", team["id"]).execute().data[0]
    updates = {}
    for i in range(1, 6):
        if team[f"player{i}_id"] == str(user.id):
            updates[f"player{i}_id"] = None
            break
    if updates:
        supabase.table("teams").update(updates).eq("id", team["id"]).execute()
    
    # Убираем роль
    guild = interaction.guild
    if guild:
        await remove_team_role(guild, user, team["name"])
    
    # Уведомление игрока
    try:
        await user.send(f"❌ Вы были кикнуты из команды **{team['name']}** лидером <@{interaction.user.id}>.")
    except discord.Forbidden:
        pass

    await interaction.response.send_message(f"✅ Игрок {user.mention} кикнут из команды **{team['name']}**.", ephemeral=True)





@bot.tree.command(name="leave", description="Покинуть текущую команду")
async def leave_cmd(interaction: discord.Interaction):
    team = await get_user_team(interaction.user.id)
    if not team:
        await interaction.response.send_message("Вы не состоите в команде.", ephemeral=True)
        return
    if team["leader_id"] == str(interaction.user.id):
        await interaction.response.send_message("Лидер не может покинуть команду. Распустите команду через /delete_team.", ephemeral=True)
        return

    await remove_from_team(interaction.user.id)

    # Убираем роль
    guild = interaction.guild
    if guild:
        member = interaction.guild.get_member(interaction.user.id)
        if member:
            await remove_team_role(guild, member, team["name"])

    await interaction.response.send_message("Вы покинули команду.", ephemeral=True)

    leader = bot.get_user(int(team["leader_id"]))
    if leader:
        try:
            await safe_send(leader, content=f"<@{interaction.user.id}> покинул вашу команду (ID: {team['id']}).")
        except discord.Forbidden:
            pass



@bot.tree.command(name="delete_team", description="Удалить свою команду")
async def delete_team_cmd(interaction: discord.Interaction):
    team = await get_user_team(interaction.user.id)
    if not team:
        await interaction.response.send_message("❌ Вы не состоите в команде.", ephemeral=True)
        return
    if str(interaction.user.id) != team["leader_id"]:
        await interaction.response.send_message("❌ Только лидер может удалять команду.", ephemeral=True)
        return

    # Disable buttons on public team announcement message
    if team.get("is_public") and team.get("announcement_message_id"):
        channel = bot.get_channel(int(TEAM_ANNOUNCEMENT_CHANNEL or interaction.channel_id))
        message = await channel.fetch_message(int(team["announcement_message_id"]))
        embed = message.embeds[0] if message.embeds else discord.Embed(
            title=f"Команда: {team['name']}",
            description="Команда удалена.",
            color=discord.Color.red()
        )
        embed.set_footer(text=f"team:{team['id']}")
        await message.edit(embed=embed, view=discord.ui.View())

    # Delete team_invites first to avoid foreign key constraint violation
    supabase.table("team_invites").delete().eq("team_id", team["id"]).execute()
    # Now delete the team
    supabase.table("teams").delete().eq("id", team["id"]).execute()

    # Remove team roles
    guild = interaction.guild
    if guild:
        await remove_team_role_from_all(guild, team["name"])

    await interaction.response.send_message(f"✅ Команда **{team['name']}** была удалена.", ephemeral=True)



@app_commands.default_permissions(manage_guild=True)
@bot.tree.command(name="cleanup_db", description="Очистить старые записи в базе данных")
@app_commands.describe(days="Удалить записи старше указанного количества дней")
@admin_only
async def cleanup_db_cmd(interaction: discord.Interaction, days: int = 30):
    if days <= 0:
        await interaction.response.send_message("Количество дней должно быть больше 0.", ephemeral=True)
        return
    threshold = int(time.time()) - days * 86400
    try:
        supabase.table("matches").delete().lt("created_at", threshold).in_("status", ["settled", "cancelled"]).execute()
        supabase.table("duels").delete().lt("created_at", threshold).in_("status", ["settled", "cancelled"]).execute()
        supabase.table("teams").delete().lt("created_at", threshold).eq("status", "pending").execute()
        supabase.table("team_invites").delete().lt("created_at", threshold).execute()
        supabase.table("duel_invites").delete().lt("created_at", threshold).execute()
        await interaction.response.send_message(f"Удалены записи старше {days} дней.", ephemeral=True)
    except Exception as e:
        logger.error(f"Error cleaning up database: {e}")
        await interaction.response.send_message("Ошибка при очистке базы данных.", ephemeral=True)


@bot.event
async def on_ready():
    logger.info(f'{bot.user} has logged in!')
    # Регистрация persistent views (dummy args)
    bot.add_view(TeamInviteView(0, 0))
    bot.add_view(DuelInviteView(0, 0))
    bot.add_view(PublicDuelView(0, 0))
    bot.add_view(JoinTeamView(0))
    bot.add_view(RegisterView())
    # ✅ Dummy для ModeratorDuelView с placeholder custom_id
    dummy_mod_view = ModeratorDuelView(0, "0")
    bot.add_view(dummy_mod_view)
    try:
        synced = await bot.tree.sync()
        logger.info(f'Synced {len(synced)} command(s)')
    except Exception as e:
        logger.error(f'Failed to sync commands: {e}')


if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)
