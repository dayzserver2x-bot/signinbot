import sys
import types

# 🩹 Patch for Python 3.13 — prevents discord.py from trying to import the removed 'audioop' module
if "audioop" not in sys.modules:
    sys.modules["audioop"] = types.ModuleType("audioop")

import discord
from discord import app_commands
from discord.ext import commands

import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import re
import os
import asyncio
from aiohttp import web
from dotenv import load_dotenv
from typing import Optional, Tuple


# -------------------- Config --------------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
BUTTON_CHANNEL_ID = int(os.getenv("BUTTON_CHANNEL_ID"))
ADMIN_ROLE_IDS = [
    int(rid.strip())
    for rid in os.getenv("ADMIN_ROLE_IDS", "").split(",")
    if rid.strip()
]

CENTRAL_TZ = ZoneInfo("America/Chicago")
AUTO_DELETE_TIME = 60
ADMIN_AUTO_DELETE_TIME = 60
HOURLY_PAY = 3000


# -------------------- Utility --------------------
async def send_temp_message(
    interaction: discord.Interaction,
    content: Optional[str] = None,
    embed: Optional[discord.Embed] = None,
    ephemeral: bool = False,
    admin: bool = False,
):
    """Unified message sender that auto-deletes after AUTO_DELETE_TIME or ADMIN_AUTO_DELETE_TIME."""
    delete_time = ADMIN_AUTO_DELETE_TIME if admin else AUTO_DELETE_TIME
    if not interaction.response.is_done():
        await interaction.response.send_message(
            content=content,
            embed=embed,
            ephemeral=ephemeral,
            delete_after=delete_time,
        )
    else:
        await interaction.followup.send(
            content=content,
            embed=embed,
            ephemeral=ephemeral,
            delete_after=delete_time,
        )


def is_admin(interaction: discord.Interaction) -> bool:
    # Admin permission OR in allowed roles list
    try:
        if interaction.user.guild_permissions.administrator:
            return True
        return any(role.id in ADMIN_ROLE_IDS for role in getattr(interaction.user, "roles", []))
    except Exception:
        return False


# -------------------- Database --------------------
conn = sqlite3.connect("clockbot.db")
cursor = conn.cursor()

cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS time_tracking (
        user_id INTEGER,
        username TEXT,
        clock_in TEXT,
        clock_out TEXT
    )
"""
)
conn.commit()

cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS adjustments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        hours_delta REAL,
        reason TEXT,
        admin_id INTEGER,
        admin_name TEXT,
        created_at TEXT
    )
"""
)
conn.commit()


# -------------------- Bot Setup --------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


# -------------------- Helpers --------------------
USER_ID_RE = re.compile(r"(\d{15,20})")


def parse_user_id(text: str) -> Optional[int]:
    """Extract a Discord user ID from a mention like <@123> or a raw ID string."""
    if not text:
        return None
    m = USER_ID_RE.search(text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


async def resolve_user(interaction: discord.Interaction, user_text: str) -> Optional[discord.abc.User]:
    """Resolve a user from mention/ID using guild cache, falling back to fetch_user."""
    uid = parse_user_id(user_text)
    if not uid:
        return None

    if interaction.guild:
        member = interaction.guild.get_member(uid)
        if member:
            return member

    try:
        return await bot.fetch_user(uid)
    except Exception:
        return None


# -------------------- Modals --------------------
class ClockInCountModal(discord.ui.Modal, title="🔢 Clock-In Count"):
    user = discord.ui.TextInput(
        label="User (mention or ID)",
        placeholder="e.g. @Somebody or 123456789012345678",
        required=True,
        max_length=64,
    )
    days = discord.ui.TextInput(
        label="Days (optional)",
        placeholder="e.g. 30",
        required=False,
        max_length=8,
    )

    def __init__(self, cog: "TimeTracker"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.clockincount_from_modal(
            interaction,
            str(self.user.value),
            str(self.days.value),
        )


class AdjustHoursModal(discord.ui.Modal, title="➕➖ Adjust Hours"):
    user = discord.ui.TextInput(
        label="User (mention or ID)",
        placeholder="e.g. @Somebody or 123456789012345678",
        required=True,
        max_length=64,
    )
    hours = discord.ui.TextInput(
        label="Hours to add/subtract",
        placeholder="e.g. 2.5  or  -1.0",
        required=True,
        max_length=16,
    )
    reason = discord.ui.TextInput(
        label="Reason (optional)",
        placeholder="Optional notes (payroll correction, missed clock-out, etc.)",
        required=False,
        max_length=256,
        style=discord.TextStyle.long,
    )

    def __init__(self, cog: "TimeTracker"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.adjusthours_from_modal(
            interaction,
            str(self.user.value),
            str(self.hours.value),
            str(self.reason.value),
        )


# -------------------- Views --------------------
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


class AdminClockButtons(discord.ui.View):
    def __init__(self, cog: "TimeTracker"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="👥 Clock Status", style=discord.ButtonStyle.primary, custom_id="persistent_admin_status_btn", row=0)
    async def clock_status_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await send_temp_message(interaction, content="❌ You don’t have permission to use this.", admin=True)
            return
        await self.cog.clockstatus_func(interaction)

    @discord.ui.button(label="🧾 All Hours", style=discord.ButtonStyle.success, custom_id="persistent_admin_allhours_btn", row=0)
    async def all_hours_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await send_temp_message(interaction, content="❌ You don’t have permission to use this.", admin=True)
            return
        await self.cog.allhours_func(interaction)

    @discord.ui.button(label="📅 30-Day Report", style=discord.ButtonStyle.secondary, custom_id="persistent_admin_30day_btn", row=0)
    async def report_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await send_temp_message(interaction, content="❌ You don’t have permission to use this.", admin=True)
            return
        await self.cog.weeklyreport_func(interaction)

    @discord.ui.button(label="🧹 Purge Data", style=discord.ButtonStyle.danger, custom_id="persistent_admin_purge_btn", row=0)
    async def purge_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await send_temp_message(interaction, content="❌ You don’t have permission to use this.", admin=True)
            return
        await self.cog.purge_func(interaction)

    @discord.ui.button(label="🔢 Clock-In Count", style=discord.ButtonStyle.secondary, custom_id="persistent_admin_count_btn", row=1)
    async def clock_in_count_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await send_temp_message(interaction, content="❌ You don’t have permission to use this.", admin=True)
            return
        await interaction.response.send_modal(ClockInCountModal(self.cog))

    @discord.ui.button(label="➕➖ Adjust Hours", style=discord.ButtonStyle.secondary, custom_id="persistent_admin_adjust_btn", row=1)
    async def adjust_hours_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await send_temp_message(interaction, content="❌ You don’t have permission to use this.", admin=True)
            return
        await interaction.response.send_modal(AdjustHoursModal(self.cog))


# -------------------- Purge Confirm View --------------------
class PurgeConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="🚮 Purge All", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        cursor.execute("DELETE FROM time_tracking")
        cursor.execute("DELETE FROM adjustments")
        conn.commit()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="✅ Database Cleared",
                description=f"All time-tracking records and adjustments have been deleted.\n👤 **Action by:** {interaction.user.mention}",
                color=discord.Color.green(),
            ),
            view=None,
        )

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="❌ Purge Cancelled",
                description="No data was deleted.",
                color=discord.Color.greyple(),
            ),
            view=None,
        )


# -------------------- TimeTracker Cog --------------------
class TimeTracker(commands.Cog):
    def __init__(self, bot_: commands.Bot):
        self.bot = bot_

    # ---- User Commands ----
    @app_commands.command(name="clockin", description="Clock in to start tracking time.")
    async def clockin(self, interaction: discord.Interaction):
        await self.clockin_func(interaction)

    async def clockin_func(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        username = str(interaction.user)

        cursor.execute(
            "SELECT 1 FROM time_tracking WHERE user_id = ? AND clock_out IS NULL",
            (user_id,),
        )
        if cursor.fetchone():
            await send_temp_message(interaction, content="❌ You're already clocked in!")
            return

        clock_in_time = datetime.now(CENTRAL_TZ).isoformat()
        cursor.execute(
            "INSERT INTO time_tracking (user_id, username, clock_in, clock_out) VALUES (?, ?, ?, NULL)",
            (user_id, username, clock_in_time),
        )
        conn.commit()

        await send_temp_message(
            interaction,
            content=f"✅ Clocked in at {datetime.now(CENTRAL_TZ).strftime('%I:%M %p %Z')}.",
        )

    @app_commands.command(name="clockout", description="Clock out and stop tracking time.")
    async def clockout(self, interaction: discord.Interaction):
        await self.clockout_func(interaction)

    async def clockout_func(self, interaction: discord.Interaction):
        user_id = interaction.user.id

        cursor.execute(
            "SELECT clock_in FROM time_tracking WHERE user_id = ? AND clock_out IS NULL",
            (user_id,),
        )
        row = cursor.fetchone()
        if not row:
            await send_temp_message(interaction, content="❌ You're not clocked in.")
            return

        clock_in_time = datetime.fromisoformat(row[0]).astimezone(CENTRAL_TZ)
        clock_out_time = datetime.now(CENTRAL_TZ)

        cursor.execute(
            "UPDATE time_tracking SET clock_out = ? WHERE user_id = ? AND clock_out IS NULL",
            (clock_out_time.isoformat(), user_id),
        )
        conn.commit()

        hours = (clock_out_time - clock_in_time).total_seconds() / 3600.0
        await send_temp_message(
            interaction,
            content=f"🕒 Clocked out at {clock_out_time.strftime('%I:%M %p %Z')}. You worked for {hours:.2f} hours.",
        )

    @app_commands.command(name="status", description="Check your current clock-in status.")
    async def status_slash(self, interaction: discord.Interaction):
        await self.status_func(interaction)

    async def status_func(self, interaction: discord.Interaction):
        user_id = interaction.user.id

        cursor.execute(
            "SELECT clock_in FROM time_tracking WHERE user_id = ? AND clock_out IS NULL",
            (user_id,),
        )
        row = cursor.fetchone()

        if row:
            t = datetime.fromisoformat(row[0]).astimezone(CENTRAL_TZ)
            await send_temp_message(
                interaction,
                content=f"✅ You are clocked in since {t.strftime('%I:%M %p %Z')}.",
            )
            return

        cursor.execute(
            "SELECT clock_out FROM time_tracking WHERE user_id = ? AND clock_out IS NOT NULL ORDER BY clock_out DESC LIMIT 1",
            (user_id,),
        )
        last = cursor.fetchone()
        if last:
            last_out = datetime.fromisoformat(last[0]).astimezone(CENTRAL_TZ)
            await send_temp_message(
                interaction,
                content=f"❌ You are not clocked in. Last clock-out was at {last_out.strftime('%I:%M %p %Z')}.",
            )
        else:
            await send_temp_message(interaction, content="❌ You have no work sessions recorded yet.")

    @app_commands.command(name="myhours", description="Check your total recorded work hours.")
    async def myhours(self, interaction: discord.Interaction):
        await self.myhours_func(interaction)

    async def myhours_func(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        username = str(interaction.user)

        session_hours, adj_total, net_hours, completed_sessions = self._calc_user_hours(user_id)

        if completed_sessions == 0 and abs(adj_total) < 1e-9:
            await send_temp_message(interaction, content="❌ You don't have any recorded time yet.")
            return

        total_pay = net_hours * HOURLY_PAY

        embed = discord.Embed(
            title=f"🕒 Work Summary for {username}",
            color=discord.Color.teal(),
            description=(
                f"**Session Hours:** {session_hours:.2f}h\n"
                f"**Adjustments:** {adj_total:+.2f}h\n"
                f"**Net Hours:** {net_hours:.2f}h\n"
                f"**Completed Sessions:** {completed_sessions}\n"
                f"**💰 Estimated Pay:** ${total_pay:,.2f}"
            ),
        )
        embed.set_footer(text=f"Hourly Rate: ${HOURLY_PAY}/hr • Times shown in CT")
        await send_temp_message(interaction, embed=embed)

    # ---- Admin Slash Commands ----
    @app_commands.command(name="clockincount", description="(Admin) Count how many times a user has clocked in.")
    @app_commands.check(is_admin)
    async def clockincount(self, interaction: discord.Interaction, member: discord.Member, days: Optional[int] = None):
        await self._clockincount(interaction, member.id, str(member), days)

    @app_commands.command(name="adjusthours", description="(Admin) Add/subtract hours for a user (manual correction).")
    @app_commands.check(is_admin)
    async def adjusthours(self, interaction: discord.Interaction, member: discord.Member, hours: float, reason: Optional[str] = None):
        await self._adjust_hours(interaction, member.id, str(member), hours, reason or "")

    @app_commands.command(name="adjustlog", description="(Admin) Show recent hour adjustments.")
    @app_commands.check(is_admin)
    async def adjustlog(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        await self._adjust_log(interaction, member.id if member else None)

    @app_commands.command(name="undoadjust", description="(Admin) Undo an hour adjustment by ID.")
    @app_commands.check(is_admin)
    async def undoadjust(self, interaction: discord.Interaction, adjustment_id: int):
        cursor.execute("SELECT user_id, hours_delta, reason FROM adjustments WHERE id = ?", (adjustment_id,))
        row = cursor.fetchone()
        if not row:
            await send_temp_message(interaction, content="❌ No adjustment found with that ID.", admin=True)
            return
        cursor.execute("DELETE FROM adjustments WHERE id = ?", (adjustment_id,))
        conn.commit()
        await send_temp_message(interaction, content=f"✅ Adjustment {adjustment_id} deleted/undone.", admin=True)

    # ---- Admin Button Actions ----
    async def clockstatus_func(self, interaction: discord.Interaction):
        cursor.execute("SELECT username, clock_in FROM time_tracking WHERE clock_out IS NULL")
        rows = cursor.fetchall()
        if not rows:
            await send_temp_message(interaction, content="✅ No one is currently clocked in.", admin=True)
            return

        embed = discord.Embed(title="👥 Currently Clocked In", color=discord.Color.green())
        for username, clock_in in rows:
            t = datetime.fromisoformat(clock_in).astimezone(CENTRAL_TZ)
            embed.add_field(name=username, value=f"Since {t.strftime('%I:%M %p %Z')}", inline=False)
        await send_temp_message(interaction, embed=embed, admin=True)

    async def allhours_func(self, interaction: discord.Interaction):
        totals: dict[int, dict[str, float | str]] = {}

        # completed sessions
        cursor.execute("SELECT user_id, username, clock_in, clock_out FROM time_tracking WHERE clock_out IS NOT NULL")
        for user_id, username, clock_in, clock_out in cursor.fetchall():
            try:
                start = datetime.fromisoformat(clock_in).astimezone(CENTRAL_TZ)
                end = datetime.fromisoformat(clock_out).astimezone(CENTRAL_TZ)
                hours = (end - start).total_seconds() / 3600.0
            except Exception:
                continue

            entry = totals.setdefault(int(user_id), {"username": username, "hours": 0.0})
            entry["username"] = username
            entry["hours"] = float(entry["hours"]) + hours

        # adjustments
        cursor.execute("SELECT user_id, username, hours_delta FROM adjustments")
        for user_id, username, delta in cursor.fetchall():
            try:
                delta_f = float(delta)
            except Exception:
                continue
            entry = totals.setdefault(int(user_id), {"username": username, "hours": 0.0})
            entry["username"] = username
            entry["hours"] = float(entry["hours"]) + delta_f

        if not totals:
            await send_temp_message(interaction, content="❌ No hours found yet.", admin=True)
            return

        sorted_items = sorted(totals.values(), key=lambda x: float(x["hours"]), reverse=True)
        desc = "\n".join([f"**{e['username']}** — {float(e['hours']):.2f}h" for e in sorted_items])

        embed = discord.Embed(
            title="🧾 Total Hours Worked (All Users)",
            description=(desc[:4000] + ("…" if len(desc) > 4000 else "")),
            color=discord.Color.orange(),
        )
        embed.set_footer(text="Includes manual adjustments • All times in CT")
        await send_temp_message(interaction, embed=embed, admin=True)

    async def weeklyreport_func(self, interaction: discord.Interaction):
        now = datetime.now(CENTRAL_TZ)
        start_window = now - timedelta(days=30)

        totals: dict[int, dict[str, float | str]] = {}

        # sessions that ended in window
        cursor.execute("SELECT user_id, username, clock_in, clock_out FROM time_tracking WHERE clock_out IS NOT NULL")
        for user_id, username, clock_in, clock_out in cursor.fetchall():
            try:
                ci = datetime.fromisoformat(clock_in).astimezone(CENTRAL_TZ)
                co = datetime.fromisoformat(clock_out).astimezone(CENTRAL_TZ)
            except Exception:
                continue
            if co < start_window:
                continue

            hours = (co - ci).total_seconds() / 3600.0
            entry = totals.setdefault(int(user_id), {"username": username, "hours": 0.0})
            entry["username"] = username
            entry["hours"] = float(entry["hours"]) + hours

        # adjustments created in window
        cursor.execute("SELECT user_id, username, hours_delta, created_at FROM adjustments")
        for user_id, username, delta, created_at in cursor.fetchall():
            try:
                ts = datetime.fromisoformat(created_at).astimezone(CENTRAL_TZ)
                if ts < start_window:
                    continue
                delta_f = float(delta)
            except Exception:
                continue

            entry = totals.setdefault(int(user_id), {"username": username, "hours": 0.0})
            entry["username"] = username
            entry["hours"] = float(entry["hours"]) + delta_f

        if not totals:
            await send_temp_message(interaction, content="❌ No work activity in the past 30 days.", admin=True)
            return

        desc_lines = []
        total_pay = 0.0

        for e in sorted(totals.values(), key=lambda x: float(x["hours"]), reverse=True):
            h = float(e["hours"])
            pay = h * HOURLY_PAY
            total_pay += pay
            desc_lines.append(f"**{e['username']}** — {h:.2f}h • 💰 ${pay:,.2f}")

        embed = discord.Embed(
            title="📅 30-Day Work Summary (Admin)",
            description="\n".join(desc_lines)[:4000],
            color=discord.Color.gold(),
        )
        embed.add_field(name="🏦 Total Payroll", value=f"${total_pay:,.2f}", inline=False)
        embed.set_footer(
            text=f"Hourly Rate: ${HOURLY_PAY}/hr • Includes adjustments • Period: {start_window.strftime('%b %d')} → {now.strftime('%b %d')} CT"
        )
        await send_temp_message(interaction, embed=embed, admin=True)

    async def purge_func(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🧹 Purge Time Data",
            description="This action **cannot be undone.**\n\nChoose:\n• 🚮 **Purge All** — delete all data\n• ❌ **Cancel** — abort",
            color=discord.Color.red(),
        )
        embed.set_footer(text=f"Requested by {interaction.user} • {datetime.now(CENTRAL_TZ).strftime('%I:%M %p %Z')}")
        await interaction.response.send_message(embed=embed, view=PurgeConfirmView(), ephemeral=False)

    # ---- Modal handlers ----
    async def clockincount_from_modal(self, interaction: discord.Interaction, user_text: str, days_text: str):
        if not is_admin(interaction):
            await send_temp_message(interaction, content="❌ You don’t have permission to use this.", admin=True)
            return

        user = await resolve_user(interaction, user_text)
        if not user:
            await send_temp_message(interaction, content="❌ Could not parse/find that user.", admin=True)
            return

        days: Optional[int] = None
        if days_text.strip():
            try:
                days = int(days_text.strip())
            except Exception:
                await send_temp_message(interaction, content="❌ Days must be a number.", admin=True)
                return

        await self._clockincount(interaction, user.id, str(user), days)

    async def adjusthours_from_modal(self, interaction: discord.Interaction, user_text: str, hours_text: str, reason: str):
        if not is_admin(interaction):
            await send_temp_message(interaction, content="❌ You don’t have permission to use this.", admin=True)
            return

        user = await resolve_user(interaction, user_text)
        if not user:
            await send_temp_message(interaction, content="❌ Could not parse/find that user.", admin=True)
            return

        try:
            delta = float(hours_text.strip())
        except Exception:
            await send_temp_message(interaction, content="❌ Hours must be a number (e.g. 2.5 or -1).", admin=True)
            return

        await self._adjust_hours(interaction, user.id, str(user), delta, reason)

    # ---- Internal helpers ----
    async def _clockincount(self, interaction: discord.Interaction, user_id: int, username: str, days: Optional[int]):
        if days is not None and days > 0:
            start = datetime.now(CENTRAL_TZ) - timedelta(days=days)
            cursor.execute(
                "SELECT COUNT(*) FROM time_tracking WHERE user_id = ? AND clock_in >= ?",
                (user_id, start.isoformat()),
            )
            total = cursor.fetchone()[0]
            cursor.execute(
                "SELECT COUNT(*) FROM time_tracking WHERE user_id = ? AND clock_out IS NULL AND clock_in >= ?",
                (user_id, start.isoformat()),
            )
            open_count = cursor.fetchone()[0]
            label = f"last {days} days"
        else:
            cursor.execute("SELECT COUNT(*) FROM time_tracking WHERE user_id = ?", (user_id,))
            total = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM time_tracking WHERE user_id = ? AND clock_out IS NULL", (user_id,))
            open_count = cursor.fetchone()[0]
            label = "all time"

        embed = discord.Embed(
            title="🔢 Clock-In Count",
            description=(
                f"**User:** {username}\n"
                f"**Period:** {label}\n"
                f"**Clock-ins:** {total}\n"
                f"**Open (currently clocked-in):** {open_count}"
            ),
            color=discord.Color.blurple(),
        )
        await send_temp_message(interaction, embed=embed, admin=True)

    async def _adjust_hours(self, interaction: discord.Interaction, user_id: int, username: str, delta: float, reason: str):
        now = datetime.now(CENTRAL_TZ).isoformat()
        cursor.execute(
            """
            INSERT INTO adjustments (user_id, username, hours_delta, reason, admin_id, admin_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, username, float(delta), reason, interaction.user.id, str(interaction.user), now),
        )
        conn.commit()

        embed = discord.Embed(
            title="✅ Hours Adjusted",
            description=(
                f"**User:** {username}\n"
                f"**Delta:** {delta:+.2f}h\n"
                f"**Reason:** {reason or '(none)'}\n"
                f"**Admin:** {interaction.user}"
            ),
            color=discord.Color.green(),
        )
        await send_temp_message(interaction, embed=embed, admin=True)

    async def _adjust_log(self, interaction: discord.Interaction, user_id: Optional[int]):
        if user_id is None:
            cursor.execute(
                "SELECT id, username, hours_delta, reason, admin_name, created_at FROM adjustments ORDER BY id DESC LIMIT 10"
            )
        else:
            cursor.execute(
                "SELECT id, username, hours_delta, reason, admin_name, created_at FROM adjustments WHERE user_id = ? ORDER BY id DESC LIMIT 10",
                (user_id,),
            )

        rows = cursor.fetchall()
        if not rows:
            await send_temp_message(interaction, content="❌ No adjustments found.", admin=True)
            return

        lines = []
        for adj_id, uname, delta, reason, admin_name, created_at in rows:
            try:
                ts = datetime.fromisoformat(created_at).astimezone(CENTRAL_TZ).strftime("%b %d %I:%M %p")
            except Exception:
                ts = created_at
            reason_short = (reason or "").strip()
            if len(reason_short) > 60:
                reason_short = reason_short[:60] + "…"
            lines.append(
                f"**#{adj_id}** {uname} {float(delta):+.2f}h • {ts} • by {admin_name}"
                + (f" • _{reason_short}_" if reason_short else "")
            )

        embed = discord.Embed(
            title="🧾 Adjustment Log (last 10)",
            description="\n".join(lines)[:4000],
            color=discord.Color.orange(),
        )
        await send_temp_message(interaction, embed=embed, admin=True)

    def _calc_user_hours(self, user_id: int) -> Tuple[float, float, float, int]:
        # completed sessions
        cursor.execute(
            "SELECT clock_in, clock_out FROM time_tracking WHERE user_id = ? AND clock_out IS NOT NULL",
            (user_id,),
        )
        records = cursor.fetchall()

        session_hours = 0.0
        for clock_in, clock_out in records:
            try:
                start = datetime.fromisoformat(clock_in).astimezone(CENTRAL_TZ)
                end = datetime.fromisoformat(clock_out).astimezone(CENTRAL_TZ)
                session_hours += (end - start).total_seconds() / 3600.0
            except Exception:
                continue

        # adjustments
        cursor.execute("SELECT hours_delta FROM adjustments WHERE user_id = ?", (user_id,))
        adj_total = 0.0
        for (delta,) in cursor.fetchall():
            try:
                adj_total += float(delta)
            except Exception:
                continue

        net_hours = session_hours + adj_total
        return session_hours, adj_total, net_hours, len(records)


# -------------------- Sync Command (text) --------------------
@bot.command()
@commands.has_permissions(administrator=True)
async def sync(ctx):
    await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    await ctx.send("✅ Slash commands synced.")


# -------------------- Startup guards --------------------
_SETUP_DONE = False
_VIEWS_ADDED = False
_STATUS_TASK_STARTED = False
_PANELS_POSTED = False


async def setup_once():
    global _SETUP_DONE
    if _SETUP_DONE:
        return
    await bot.add_cog(TimeTracker(bot))
    synced = await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    print(f"✅ Cog loaded • ✅ Synced {len(synced)} commands")
    _SETUP_DONE = True


# --- 🌀 Funny rotating presence/status ---
async def rotate_statuses():
    await bot.wait_until_ready()
    statuses = [
        "😴 Calculating how many naps equal a shift...",
        "🧠 Thinking about time... philosophically ⏳",
        "🕐 Time is money, but I accept memes 💸",
        "👀 Watching people forget to clock out...",
        "💻 Pretending to work since 2025",
        "⏰ Running on coffee and bad decisions ☕",
        "🦥 Taking a productivity nap...",
        "🎭 Acting busy for the admin",
        "📊 Making up numbers that look impressive",
        "🧾 Auditing everyone's snack breaks 🍪",
        "💀 Help, I'm trapped in a database",
        "🦾 More reliable than your memory",
        "🌈 Calculating pay in friendship coins 💖",
        "🐢 Slow and steady clocks the hours",
        "🪩 Vibing in the time dimension",
    ]
    while not bot.is_closed():
        for status in statuses:
            await bot.change_presence(activity=discord.Game(name=status))
            await asyncio.sleep(60)


@bot.event
async def on_ready():
    global _VIEWS_ADDED, _STATUS_TASK_STARTED, _PANELS_POSTED

    await setup_once()

    if not _STATUS_TASK_STARTED:
        bot.loop.create_task(rotate_statuses())
        _STATUS_TASK_STARTED = True

    cog = bot.get_cog("TimeTracker")

    if cog and not _VIEWS_ADDED:
        bot.add_view(ClockButtons(cog))
        bot.add_view(AdminClockButtons(cog))
        _VIEWS_ADDED = True

    print(f"🤖 Logged in as {bot.user}")
    print("🕒 All times shown in Central Time (CT — auto-adjusts for CDT/CST)")

    channel = bot.get_channel(BUTTON_CHANNEL_ID)
    if channel and cog and not _PANELS_POSTED:
        creator_embed = discord.Embed(
            title="👨‍💻 TimeTracker Bot",
            description=(
                "Something I created to help track Yall!!! 😘😘😘.\n\n"
                "**Created by:** <@691108551258800128>\n"
                "📦 **Version:** 1.6.2\n"
                "🕓 **Timezone:** Central Time (auto-adjusts for CDT/CST)\n"
                "💾 **Database:** SQLite (`clockbot.db`)"
            ),
            color=discord.Color.blurple(),
        )
        creator_embed.set_footer(text="© 2025 TimeTracker Bot • Developed with ❤️ using discord.py")

        await channel.send(embed=creator_embed)
        await channel.send("👋 **Time Tracking Panel**", view=ClockButtons(cog))
        await channel.send("🛠️ **Admin Control Panel**", view=AdminClockButtons(cog))
        _PANELS_POSTED = True


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        await send_temp_message(interaction, content="❌ You don’t have permission to run this command.", admin=True)
    else:
        await send_temp_message(interaction, content=f"❌ Error: {error}", admin=True)


# -------------------- Dummy Web Server for Render --------------------
async def handle(request):
    return web.Response(text="Bot is running!")

app = web.Application()
app.router.add_get("/", handle)


# -------------------- Run Bot + Keep-Alive --------------------
async def main():
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
