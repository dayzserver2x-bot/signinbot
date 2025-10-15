import sys
import types
# 🩹 Patch for Python 3.13 — prevents discord.py from trying to import the removed 'audioop' module
if 'audioop' not in sys.modules:
    sys.modules['audioop'] = types.ModuleType('audioop')

import discord
from discord import app_commands
from discord.ext import commands
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import csv
import io
import os
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
BUTTON_CHANNEL_ID = int(os.getenv("BUTTON_CHANNEL_ID"))
ADMIN_ROLE_IDS = [int(rid.strip()) for rid in os.getenv("ADMIN_ROLE_IDS", "").split(",") if rid.strip()]

CENTRAL_TZ = ZoneInfo("America/Chicago")
AUTO_DELETE_TIME = 60
ADMIN_AUTO_DELETE_TIME = 60
HOURLY_PAY = 2500

# --- Database Setup ---
conn = sqlite3.connect("clockbot.db")
cursor = conn.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS time_tracking (
    user_id INTEGER,
    username TEXT,
    clock_in TEXT,
    clock_out TEXT
)
""")
conn.commit()

# --- Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


def is_admin(interaction: discord.Interaction) -> bool:
    if interaction.user.guild_permissions.administrator:
        return True
    return any(role.id in ADMIN_ROLE_IDS for role in interaction.user.roles)


# --- Buttons View (User Panel) ---
class ClockButtons(discord.ui.View):
    def __init__(self, cog: "TimeTracker"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="🟢 Clock In", style=discord.ButtonStyle.success, custom_id="persistent_clock_in_btn")
    async def clock_in_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.clockin_func(interaction)

    @discord.ui.button(label="🔴 Clock Out", style=discord.ButtonStyle.danger, custom_id="persistent_clock_out_btn")
    async def clock_out_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.clockout_func(interaction)

    @discord.ui.button(label="📊 Status", style=discord.ButtonStyle.primary, custom_id="persistent_status_btn")
    async def status_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.status_func(interaction)

    @discord.ui.button(label="🕒 My Hours", style=discord.ButtonStyle.secondary, custom_id="persistent_myhours_btn")
    async def myhours_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.myhours_func(interaction)


# --- Buttons View (Admin Panel) ---
class AdminClockButtons(discord.ui.View):
    def __init__(self, cog: "TimeTracker"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="👥 Clock Status", style=discord.ButtonStyle.primary, custom_id="persistent_admin_status_btn")
    async def clock_status_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await interaction.response.send_message("❌ You don’t have permission to use this.", ephemeral=False)
            return
        await self.cog.clockstatus_func(interaction)

    @discord.ui.button(label="🧾 All Hours", style=discord.ButtonStyle.success, custom_id="persistent_admin_allhours_btn")
    async def all_hours_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await interaction.response.send_message("❌ You don’t have permission to use this.", ephemeral=False)
            return
        await self.cog.allhours_func(interaction, export=False)

    @discord.ui.button(label="📅 7-Day Report", style=discord.ButtonStyle.secondary, custom_id="persistent_admin_weekly_btn")
    async def weekly_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await interaction.response.send_message("❌ You don’t have permission to use this.", ephemeral=False)
            return
        await self.cog.weeklyreport_func(interaction)

    @discord.ui.button(label="✏️ Change Hours", style=discord.ButtonStyle.blurple, custom_id="persistent_admin_changehours_btn")
    async def change_hours_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await interaction.response.send_message("❌ You don’t have permission to use this.", ephemeral=False)
            return
        await interaction.response.send_modal(ChangeHoursModal())

    @discord.ui.button(label="🧹 Purge Data", style=discord.ButtonStyle.danger, custom_id="persistent_admin_purge_btn")
    async def purge_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await interaction.response.send_message("❌ You don’t have permission to use this.", ephemeral=False)
            return
        await self.cog.purge_func(interaction)


# --- Modal: Change Hours ---
class ChangeHoursModal(discord.ui.Modal, title="✏️ Adjust User Hours"):
    user_input = discord.ui.TextInput(
        label="User (mention, username, or ID)",
        placeholder="e.g. @JohnDoe, JohnDoe#1234, or 123456789012345678",
        required=True
    )
    adjustment = discord.ui.TextInput(
        label="Adjustment (hours)",
        placeholder="e.g. +2.5 or -1.0",
        required=True
    )
    reason = discord.ui.TextInput(
        label="Reason",
        style=discord.TextStyle.paragraph,
        placeholder="Why are you adjusting this?",
        required=False
    )

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        raw_input = self.user_input.value.strip()
        member = None
        user_id = None

        # --- Resolve user input ---
        if raw_input.startswith("<@") and raw_input.endswith(">"):
            user_id = int(raw_input.strip("<@!>"))
            member = guild.get_member(user_id)
        elif raw_input.isdigit():
            user_id = int(raw_input)
            member = guild.get_member(user_id)
        else:
            member = discord.utils.find(
                lambda m: str(m) == raw_input or m.name.lower() == raw_input.lower(),
                guild.members
            )
            if member:
                user_id = member.id

        if not user_id:
            await interaction.response.send_message("❌ Could not find that user in this server.", ephemeral=True)
            return

        # --- Parse adjustment hours ---
        try:
            delta_hours = float(self.adjustment.value.strip())
        except ValueError:
            await interaction.response.send_message("❌ Invalid hour value. Please enter a number like `+2.5` or `-1.0`.", ephemeral=True)
            return

        username = str(member) if member else f"User {user_id}"

        # --- Find and adjust latest record ---
        cursor.execute(
            "SELECT rowid, clock_in, clock_out FROM time_tracking WHERE user_id = ? AND clock_out IS NOT NULL ORDER BY clock_out DESC LIMIT 1",
            (user_id,)
        )
        row = cursor.fetchone()

        now = datetime.now(CENTRAL_TZ)

        if row:
            rowid, clock_in_str, clock_out_str = row
            clock_in = datetime.fromisoformat(clock_in_str)
            clock_out = datetime.fromisoformat(clock_out_str)
            duration = (clock_out - clock_in).total_seconds() / 3600

            adjusted_duration = max(0, duration + delta_hours)
            new_clock_out = clock_in + timedelta(hours=adjusted_duration)

            cursor.execute(
                "UPDATE time_tracking SET clock_out = ? WHERE rowid = ?",
                (new_clock_out.isoformat(), rowid)
            )
            action = "updated last session"
        else:
            fake_start = now - timedelta(hours=max(delta_hours, 0))
            fake_end = now if delta_hours >= 0 else now - timedelta(hours=abs(delta_hours))
            cursor.execute(
                "INSERT INTO time_tracking (user_id, username, clock_in, clock_out) VALUES (?, ?, ?, ?)",
                (user_id, username, fake_start.isoformat(), fake_end.isoformat())
            )
            action = "created new adjustment record"

        conn.commit()

        embed = discord.Embed(
            title="✏️ Hours Adjusted",
            color=discord.Color.orange(),
            description=(
                f"👤 **User:** {username} (`{user_id}`)\n"
                f"⏱️ **Adjustment:** {delta_hours:+.2f} hours\n"
                f"📄 **Action:** {action}\n"
                f"📝 **Reason:** {self.reason.value or 'No reason provided'}\n"
                f"🕓 **Processed by:** {interaction.user.mention}"
            )
        )
        embed.set_footer(text=f"Time: {now.strftime('%I:%M %p %Z')}")
        await interaction.response.send_message(embed=embed, ephemeral=False)


# --- Main Cog: Time Tracker ---
class TimeTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # (keep your existing functions here – clockin_func, clockout_func, status_func, etc.)

# --- Creator Info Embed ---
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    await bot.tree.sync(guild=discord.Object(id=GUILD_ID))

    guild = bot.get_guild(GUILD_ID)
    channel = guild.get_channel(BUTTON_CHANNEL_ID)
    if not channel:
        print("❌ Button channel not found!")
        return

    # Creator Info Embed
    creator_embed = discord.Embed(
        title="🧠 Creator Info",
        description="Created by **Rebeldude86** | Version 1.5 | © 2025",
        color=discord.Color.blue()
    )
    creator_embed.set_footer(text="!!2x custom bot for the traders! ❤️")

    # User Panel
    user_embed = discord.Embed(
        title="⏰ Employee Time Clock",
        description="Use the buttons below to clock in/out and check your hours.",
        color=discord.Color.green()
    )

    # Admin Panel
    admin_embed = discord.Embed(
        title="⚙️ Admin Panel",
        description="Manage employee hours and data.",
        color=discord.Color.gold()
    )

    # Send or update panels
    await channel.purge(limit=5)
    await channel.send(embed=creator_embed)
    await channel.send(embed=user_embed, view=ClockButtons(bot))
    await channel.send(embed=admin_embed, view=AdminClockButtons(bot))


# --- Run the Bot ---
bot.add_cog(TimeTracker(bot))
bot.run(TOKEN)
