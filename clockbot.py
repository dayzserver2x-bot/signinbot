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
import asyncio
import re
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
BUTTON_CHANNEL_ID = int(os.getenv("BUTTON_CHANNEL_ID"))
ADMIN_ROLE_IDS = [int(rid.strip()) for rid in os.getenv("ADMIN_ROLE_IDS", "").split(",") if rid.strip()]

CENTRAL_TZ = ZoneInfo("America/Chicago")
AUTO_DELETE_TIME = 60
ADMIN_AUTO_DELETE_TIME = 60
HOURLY_PAY = 3000

# --- Utility: Auto-deleting Responses ---
async def send_temp_message(interaction: discord.Interaction, content=None, embed=None, ephemeral=False, admin=False):
    """Unified message sender that auto-deletes after AUTO_DELETE_TIME or ADMIN_AUTO_DELETE_TIME."""
    delete_time = ADMIN_AUTO_DELETE_TIME if admin else AUTO_DELETE_TIME
    if not interaction.response.is_done():
        await interaction.response.send_message(content=content, embed=embed, ephemeral=ephemeral, delete_after=delete_time)
    else:
        await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral, delete_after=delete_time)


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

# --- Adjustments Table (Admin Hour Corrections) ---
cursor.execute("""
CREATE TABLE IF NOT EXISTS adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    hours_delta REAL NOT NULL,
    reason TEXT,
    admin_id INTEGER NOT NULL,
    admin_name TEXT NOT NULL,
    created_at TEXT NOT NULL
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

# --- Helpers for Admin Tools ---
USER_MENTION_RE = re.compile(r"<@!?(\d+)>")

def parse_user_id(value: str):
    """Parse a Discord mention like <@123> / <@!123> or a raw numeric ID."""
    if not value:
        return None
    value = value.strip()
    m = USER_MENTION_RE.fullmatch(value)
    if m:
        return int(m.group(1))
    if value.isdigit():
        return int(value)
    return None


class AdjustHoursModal(discord.ui.Modal, title="➕➖ Adjust Hours"):
    def __init__(self, cog: "TimeTracker"):
        super().__init__(timeout=None)
        self.cog = cog

        self.user_field = discord.ui.TextInput(
            label="User (mention or ID)",
            placeholder="e.g. @SomeUser or 123456789012345678",
            required=True,
            max_length=64,
        )
        self.hours_field = discord.ui.TextInput(
            label="Hours to add/subtract",
            placeholder="e.g. 1.5 to add, -2 to subtract",
            required=True,
            max_length=32,
        )
        self.reason_field = discord.ui.TextInput(
            label="Reason (optional)",
            placeholder="e.g. missed clock-out, manual correction...",
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=256,
        )

        self.add_item(self.user_field)
        self.add_item(self.hours_field)
        self.add_item(self.reason_field)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.adjusthours_from_modal(
            interaction,
            self.user_field.value,
            self.hours_field.value,
            self.reason_field.value,
        )


class ClockInCountModal(discord.ui.Modal, title="🔢 Clock-In Count"):
    def __init__(self, cog: "TimeTracker"):
        super().__init__(timeout=None)
        self.cog = cog

        self.user_field = discord.ui.TextInput(
            label="User (mention or ID)",
            placeholder="e.g. @SomeUser or 123456789012345678",
            required=True,
            max_length=64,
        )
        self.days_field = discord.ui.TextInput(
            label="Days (optional)",
            placeholder="e.g. 30",
            required=False,
            max_length=8,
        )

        self.add_item(self.user_field)
        self.add_item(self.days_field)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.clockincount_from_modal(
            interaction,
            self.user_field.value,
            self.days_field.value,
        )



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
            await send_temp_message(interaction, content="❌ You don’t have permission to use this.", admin=True)
            return
        await self.cog.clockstatus_func(interaction)

    @discord.ui.button(label="🧾 All Hours", style=discord.ButtonStyle.success, custom_id="persistent_admin_allhours_btn")
    async def all_hours_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await send_temp_message(interaction, content="❌ You don’t have permission to use this.", admin=True)
            return
        await self.cog.allhours_func(interaction, export=False)

    @discord.ui.button(label="📅 30-Day Report", style=discord.ButtonStyle.secondary, custom_id="persistent_admin_weekly_btn")
    async def weekly_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await send_temp_message(interaction, content="❌ You don’t have permission to use this.", admin=True)
            return
        await self.cog.weeklyreport_func(interaction)



@discord.ui.button(label="🔢 Clock-In Count", style=discord.ButtonStyle.secondary, custom_id="persistent_admin_clockins_btn", row=1)
async def clockins_count_button(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not is_admin(interaction):
        await send_temp_message(interaction, content="❌ You don’t have permission to use this.", admin=True)
        return
    await interaction.response.send_modal(ClockInCountModal(self.cog))

@discord.ui.button(label="➕➖ Adjust Hours", style=discord.ButtonStyle.primary, custom_id="persistent_admin_adjusthours_btn", row=1)
async def adjust_hours_button(self, interaction: discord.Interaction, button: discord.ui.Button):
    if not is_admin(interaction):
        await send_temp_message(interaction, content="❌ You don’t have permission to use this.", admin=True)
        return
    await interaction.response.send_modal(AdjustHoursModal(self.cog))
    @discord.ui.button(label="🧹 Purge Data", style=discord.ButtonStyle.danger, custom_id="persistent_admin_purge_btn")
    async def purge_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await send_temp_message(interaction, content="❌ You don’t have permission to use this.", admin=True)
            return
        await self.cog.purge_func(interaction)


# --- TimeTracker Cog ---
class TimeTracker(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="clockin", description="Clock in to start tracking time.")
    async def clockin(self, interaction: discord.Interaction):
        await self.clockin_func(interaction)

    async def clockin_func(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        username = str(interaction.user)

        cursor.execute("SELECT * FROM time_tracking WHERE user_id = ? AND clock_out IS NULL", (user_id,))
        if cursor.fetchone():
            await send_temp_message(interaction, content="❌ You're already clocked in!")
            return

        clock_in_time = datetime.now(CENTRAL_TZ).isoformat()
        cursor.execute("INSERT INTO time_tracking (user_id, username, clock_in, clock_out) VALUES (?, ?, ?, NULL)",
                       (user_id, username, clock_in_time))
        conn.commit()

        await send_temp_message(
            interaction,
            content=f"✅ Clocked in at {datetime.now(CENTRAL_TZ).strftime('%I:%M %p %Z')}."
        )

    @app_commands.command(name="clockout", description="Clock out and stop tracking time.")
    async def clockout(self, interaction: discord.Interaction):
        await self.clockout_func(interaction)

    async def clockout_func(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        cursor.execute("SELECT clock_in FROM time_tracking WHERE user_id = ? AND clock_out IS NULL", (user_id,))
        row = cursor.fetchone()

        if not row:
            await send_temp_message(interaction, content="❌ You're not clocked in.")
            return

        clock_in_time = datetime.fromisoformat(row[0])
        clock_out_time = datetime.now(CENTRAL_TZ)
        cursor.execute("UPDATE time_tracking SET clock_out = ? WHERE user_id = ? AND clock_out IS NULL",
                       (clock_out_time.isoformat(), user_id))
        conn.commit()

        total_time = clock_out_time - clock_in_time
        hours = total_time.total_seconds() / 3600
        await send_temp_message(
            interaction,
            content=f"🕒 Clocked out at {clock_out_time.strftime('%I:%M %p %Z')}. You worked for {hours:.2f} hours."
        )

    @app_commands.command(name="status", description="Check your current clock-in status.")
    async def status_slash(self, interaction: discord.Interaction):
        await self.status_func(interaction)

    async def status_func(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        cursor.execute("SELECT clock_in FROM time_tracking WHERE user_id = ? AND clock_out IS NULL", (user_id,))
        row = cursor.fetchone()

        if row:
            clock_in_time = datetime.fromisoformat(row[0]).astimezone(CENTRAL_TZ)
            await send_temp_message(interaction, content=f"✅ You are clocked in since {clock_in_time.strftime('%I:%M %p %Z')}.")
        else:
            cursor.execute("SELECT clock_out FROM time_tracking WHERE user_id = ? ORDER BY clock_out DESC LIMIT 1", (user_id,))
            last = cursor.fetchone()
            if last:
                last_out = datetime.fromisoformat(last[0]).astimezone(CENTRAL_TZ)
                await send_temp_message(interaction, content=f"❌ You are not clocked in. Last clock-out was at {last_out.strftime('%I:%M %p %Z')}.")
            else:
                await send_temp_message(interaction, content="❌ You have no work sessions recorded yet.")

    @app_commands.command(name="myhours", description="Check your total recorded work hours.")
    async def myhours(self, interaction: discord.Interaction):
        await self.myhours_func(interaction)

async def myhours_func(self, interaction: discord.Interaction):
    user_id = interaction.user.id
    username = str(interaction.user)

    # Completed sessions
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
            session_hours += (end - start).total_seconds() / 3600
        except Exception:
            continue

    # Admin adjustments (manual hour corrections)
    cursor.execute("SELECT hours_delta FROM adjustments WHERE user_id = ?", (user_id,))
    adj_rows = cursor.fetchall()
    adj_total = 0.0
    for (delta,) in adj_rows:
        try:
            adj_total += float(delta)
        except Exception:
            continue

    if (len(records) == 0) and (abs(adj_total) < 1e-9):
        await send_temp_message(interaction, content="❌ You don't have any recorded time yet.")
        return

    net_hours = session_hours + adj_total
    total_pay = net_hours * HOURLY_PAY

    embed = discord.Embed(
        title=f"🕒 Work Summary for {username}",
        color=discord.Color.teal(),
        description=(
            f"**Session Hours:** {session_hours:.2f}h\n"
            f"**Adjustments:** {adj_total:+.2f}h\n"
            f"**Net Hours:** {net_hours:.2f}h\n"
            f"**Completed Sessions:** {len(records)}\n"
            f"**💰 Estimated Pay:** ${total_pay:,.2f}"
        ),
    )
    embed.set_footer(text=f"Hourly Rate: ${HOURLY_PAY}/hr • Times shown in CT")
    await send_temp_message(interaction, embed=embed)

    # --- Admin functions ---


# --- Admin slash commands ---
@app_commands.command(name="clockincount", description="(Admin) Count how many times a user has clocked in.")
@app_commands.check(is_admin)
@app_commands.describe(member="User to check (defaults to you)", days="Only include clock-ins from the past N days (optional)")
async def clockincount(self, interaction: discord.Interaction, member: discord.Member = None, days: int = None):
    await self.clockincount_func(interaction, member=member, days=days)

@app_commands.command(name="adjusthours", description="(Admin) Add/subtract hours for a user (manual correction).")
@app_commands.check(is_admin)
@app_commands.describe(member="User to adjust", hours="Hours to add (positive) or subtract (negative)", reason="Optional reason/notes")
async def adjusthours(self, interaction: discord.Interaction, member: discord.Member, hours: float, reason: str = None):
    await self.adjusthours_func(interaction, member=member, hours=hours, reason=reason)

@app_commands.command(name="adjustlog", description="(Admin) Show the last 10 hour adjustments for a user.")
@app_commands.check(is_admin)
@app_commands.describe(member="User to check (defaults to you)")
async def adjustlog(self, interaction: discord.Interaction, member: discord.Member = None):
    await self.adjustlog_func(interaction, member=member)

@app_commands.command(name="undoadjust", description="(Admin) Undo an adjustment by its ID (from /adjustlog).")
@app_commands.check(is_admin)
@app_commands.describe(adjustment_id="Adjustment ID to delete")
async def undoadjust(self, interaction: discord.Interaction, adjustment_id: int):
    await self.undoadjust_func(interaction, adjustment_id=adjustment_id)

# --- Admin feature helpers (used by buttons & slash commands) ---
async def _resolve_member_from_text(self, interaction: discord.Interaction, user_text: str):
    if not interaction.guild:
        return None
    user_id = parse_user_id(user_text)
    if not user_id:
        return None
    member = interaction.guild.get_member(user_id)
    if member:
        return member
    try:
        return await interaction.guild.fetch_member(user_id)
    except Exception:
        return None

async def clockincount_from_modal(self, interaction: discord.Interaction, user_text: str, days_text: str):
    if not is_admin(interaction):
        await send_temp_message(interaction, content="❌ You don’t have permission to use this.", admin=True)
        return

    member = await self._resolve_member_from_text(interaction, user_text)
    if not member:
        await send_temp_message(interaction, content="❌ I couldn’t find that user. Use a mention or user ID.", admin=True)
        return

    days = None
    if days_text and days_text.strip():
        try:
            days = int(days_text.strip())
        except Exception:
            await send_temp_message(interaction, content="❌ Days must be a number (e.g. 30).", admin=True)
            return

    await self.clockincount_func(interaction, member=member, days=days)

async def adjusthours_from_modal(self, interaction: discord.Interaction, user_text: str, hours_text: str, reason: str):
    if not is_admin(interaction):
        await send_temp_message(interaction, content="❌ You don’t have permission to use this.", admin=True)
        return

    member = await self._resolve_member_from_text(interaction, user_text)
    if not member:
        await send_temp_message(interaction, content="❌ I couldn’t find that user. Use a mention or user ID.", admin=True)
        return

    try:
        hours = float(hours_text.strip())
    except Exception:
        await send_temp_message(interaction, content="❌ Hours must be a number (e.g. 1.5 or -2).", admin=True)
        return

    await self.adjusthours_func(interaction, member=member, hours=hours, reason=reason)

async def _get_session_hours(self, user_id: int, since: datetime = None):
    cursor.execute("SELECT clock_in, clock_out FROM time_tracking WHERE user_id = ?", (user_id,))
    rows = cursor.fetchall()
    total = 0.0
    total_count = 0
    open_count = 0

    for clock_in, clock_out in rows:
        total_count += 1
        try:
            ci = datetime.fromisoformat(clock_in).astimezone(CENTRAL_TZ)
        except Exception:
            continue

        if since and ci < since:
            # Still counts for "all-time", but skip for since-window calculations.
            pass

        if not clock_out:
            open_count += 1
            continue

        try:
            co = datetime.fromisoformat(clock_out).astimezone(CENTRAL_TZ)
        except Exception:
            continue

        if since and co < since:
            continue

        total += (co - ci).total_seconds() / 3600

    return total, total_count, open_count

async def _get_adjustment_hours(self, user_id: int, since: datetime = None):
    cursor.execute("SELECT hours_delta, created_at FROM adjustments WHERE user_id = ?", (user_id,))
    rows = cursor.fetchall()
    total = 0.0
    for delta, created_at in rows:
        try:
            if since:
                ts = datetime.fromisoformat(created_at).astimezone(CENTRAL_TZ)
                if ts < since:
                    continue
            total += float(delta)
        except Exception:
            continue
    return total

async def clockincount_func(self, interaction: discord.Interaction, member: discord.Member = None, days: int = None):
    member = member or interaction.user
    now = datetime.now(CENTRAL_TZ)
    window_start = None
    if days and days > 0:
        window_start = now - timedelta(days=days)

    # Counts and totals
    session_hours_all, total_clockins, open_clockins = await self._get_session_hours(member.id, since=None)
    adj_all = await self._get_adjustment_hours(member.id, since=None)
    net_hours_all = session_hours_all + adj_all

    recent_clockins = None
    recent_net_hours = None
    if window_start:
        # Count clock-ins by clock_in timestamp
        cursor.execute("SELECT clock_in FROM time_tracking WHERE user_id = ?", (member.id,))
        cis = cursor.fetchall()
        recent_clockins = 0
        for (ci_str,) in cis:
            try:
                ci = datetime.fromisoformat(ci_str).astimezone(CENTRAL_TZ)
            except Exception:
                continue
            if ci >= window_start:
                recent_clockins += 1

        session_hours_recent, _, _ = await self._get_session_hours(member.id, since=window_start)
        adj_recent = await self._get_adjustment_hours(member.id, since=window_start)
        recent_net_hours = session_hours_recent + adj_recent

    embed = discord.Embed(
        title="🔢 Clock-In Count",
        color=discord.Color.blurple(),
        description=f"**User:** {member.mention} (`{member.id}`)",
    )
    embed.add_field(name="Total Clock-Ins (all time)", value=str(total_clockins), inline=True)
    embed.add_field(name="Open Clock-Ins", value=str(open_clockins), inline=True)
    embed.add_field(name="Net Hours (all time)", value=f"{net_hours_all:.2f}h", inline=True)

    if window_start:
        embed.add_field(name=f"Clock-Ins (last {days}d)", value=str(recent_clockins), inline=True)
        embed.add_field(name=f"Net Hours (last {days}d)", value=f"{recent_net_hours:.2f}h", inline=True)
        embed.set_footer(text=f"Window: {window_start.strftime('%b %d')} → {now.strftime('%b %d')} CT • Includes adjustments")
    else:
        embed.set_footer(text="Includes adjustments • All times in CT")

    await send_temp_message(interaction, embed=embed, admin=True)

async def adjusthours_func(self, interaction: discord.Interaction, member: discord.Member, hours: float, reason: str = None):
    if abs(hours) < 1e-9:
        await send_temp_message(interaction, content="❌ Hours can’t be 0.", admin=True)
        return

    now = datetime.now(CENTRAL_TZ).isoformat()
    cursor.execute(
        "INSERT INTO adjustments (user_id, username, hours_delta, reason, admin_id, admin_name, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (member.id, str(member), float(hours), (reason or "").strip()[:256], interaction.user.id, str(interaction.user), now),
    )
    conn.commit()

    session_hours_all, _, _ = await self._get_session_hours(member.id, since=None)
    adj_all = await self._get_adjustment_hours(member.id, since=None)
    net_hours_all = session_hours_all + adj_all
    pay = net_hours_all * HOURLY_PAY

    embed = discord.Embed(
        title="✅ Hours Adjusted",
        color=discord.Color.green(),
        description=(
            f"**User:** {member.mention}\n"
            f"**Adjustment:** {hours:+.2f}h\n"
            f"**New Net Total:** {net_hours_all:.2f}h\n"
            f"**Estimated Pay (net):** ${pay:,.2f}"
        ),
    )
    if reason and reason.strip():
        embed.add_field(name="Reason", value=reason.strip()[:1024], inline=False)
    embed.set_footer(text="Adjustment saved • Includes adjustments in totals")
    await send_temp_message(interaction, embed=embed, admin=True)

async def adjustlog_func(self, interaction: discord.Interaction, member: discord.Member = None):
    member = member or interaction.user
    cursor.execute(
        "SELECT id, hours_delta, reason, admin_name, created_at FROM adjustments WHERE user_id = ? ORDER BY id DESC LIMIT 10",
        (member.id,),
    )
    rows = cursor.fetchall()

    if not rows:
        await send_temp_message(interaction, content=f"❌ No adjustments found for {member.mention}.", admin=True)
        return

    lines = []
    for adj_id, delta, reason, admin_name, created_at in rows:
        try:
            ts = datetime.fromisoformat(created_at).astimezone(CENTRAL_TZ).strftime('%b %d, %Y %I:%M %p %Z')
        except Exception:
            ts = str(created_at)
        reason_txt = (reason or "").strip()
        if len(reason_txt) > 60:
            reason_txt = reason_txt[:57] + "…"
        lines.append(f"**#{adj_id}** {float(delta):+,.2f}h — {ts} — by **{admin_name}**" + (f" — _{reason_txt}_" if reason_txt else ""))

    embed = discord.Embed(
        title=f"🧾 Adjustment Log — {str(member)}",
        description="\n".join(lines)[:4000],
        color=discord.Color.dark_teal(),
    )
    embed.set_footer(text="Use /undoadjust <id> to remove an entry")
    await send_temp_message(interaction, embed=embed, admin=True)

async def undoadjust_func(self, interaction: discord.Interaction, adjustment_id: int):
    cursor.execute("SELECT user_id, hours_delta FROM adjustments WHERE id = ?", (adjustment_id,))
    row = cursor.fetchone()
    if not row:
        await send_temp_message(interaction, content="❌ Adjustment not found.", admin=True)
        return

    cursor.execute("DELETE FROM adjustments WHERE id = ?", (adjustment_id,))
    conn.commit()

    await send_temp_message(interaction, content=f"✅ Removed adjustment #{adjustment_id}.", admin=True)
    async def clockstatus_func(self, interaction: discord.Interaction):
        cursor.execute("SELECT username, clock_in FROM time_tracking WHERE clock_out IS NULL")
        rows = cursor.fetchall()
        if not rows:
            await send_temp_message(interaction, content="✅ No one is currently clocked in.", admin=True)
            return
        embed = discord.Embed(title="Currently Clocked In", color=discord.Color.green())
        for username, clock_in in rows:
            t = datetime.fromisoformat(clock_in).astimezone(CENTRAL_TZ)
            embed.add_field(name=username, value=f"Since {t.strftime('%I:%M %p %Z')}", inline=False)
        await send_temp_message(interaction, embed=embed, admin=True)

async def allhours_func(self, interaction: discord.Interaction, export: bool = False):
    """Admin: show total hours for all users (includes manual adjustments)."""
    totals = {}  # user_id -> {"username": str, "hours": float}

    # Completed sessions
    cursor.execute("SELECT user_id, username, clock_in, clock_out FROM time_tracking WHERE clock_out IS NOT NULL")
    rows = cursor.fetchall()
    for user_id, username, clock_in, clock_out in rows:
        try:
            start = datetime.fromisoformat(clock_in).astimezone(CENTRAL_TZ)
            end = datetime.fromisoformat(clock_out).astimezone(CENTRAL_TZ)
            hours = (end - start).total_seconds() / 3600
        except Exception:
            continue
        entry = totals.setdefault(int(user_id), {"username": username, "hours": 0.0})
        entry["username"] = username
        entry["hours"] += hours

    # Manual adjustments
    cursor.execute("SELECT user_id, username, hours_delta FROM adjustments")
    adj_rows = cursor.fetchall()
    for user_id, username, delta in adj_rows:
        try:
            delta_f = float(delta)
        except Exception:
            continue
        entry = totals.setdefault(int(user_id), {"username": username, "hours": 0.0})
        entry["username"] = username
        entry["hours"] += delta_f

    if not totals:
        await send_temp_message(interaction, content="❌ No hours found yet.", admin=True)
        return

    sorted_totals = sorted(totals.values(), key=lambda x: x["hours"], reverse=True)
    desc = "\n".join([f"**{e['username']}** — {e['hours']:.2f}h" for e in sorted_totals])
    embed = discord.Embed(
        title="🕒 Total Hours Worked (All Users)",
        description=(desc[:4000] + ("…" if len(desc) > 4000 else "")),
        color=discord.Color.orange(),
    )
    embed.set_footer(text="Includes manual adjustments • All times in CT")
    await send_temp_message(interaction, embed=embed, admin=True)

async def weeklyreport_func(self, interaction: discord.Interaction):
    """Admin: 30-day summary (includes manual adjustments created in the window)."""
    now = datetime.now(CENTRAL_TZ)
    start_window = now - timedelta(days=30)

    totals = {}  # user_id -> {"username": str, "hours": float}

    # Sessions that ended in the window
    cursor.execute("SELECT user_id, username, clock_in, clock_out FROM time_tracking WHERE clock_out IS NOT NULL")
    rows = cursor.fetchall()
    for user_id, username, clock_in, clock_out in rows:
        try:
            ci = datetime.fromisoformat(clock_in).astimezone(CENTRAL_TZ)
            co = datetime.fromisoformat(clock_out).astimezone(CENTRAL_TZ)
        except Exception:
            continue
        if co < start_window:
            continue
        hours = (co - ci).total_seconds() / 3600
        entry = totals.setdefault(int(user_id), {"username": username, "hours": 0.0})
        entry["username"] = username
        entry["hours"] += hours

    # Adjustments created in the window
    cursor.execute("SELECT user_id, username, hours_delta, created_at FROM adjustments")
    adj_rows = cursor.fetchall()
    for user_id, username, delta, created_at in adj_rows:
        try:
            ts = datetime.fromisoformat(created_at).astimezone(CENTRAL_TZ)
        except Exception:
            continue
        if ts < start_window:
            continue
        try:
            delta_f = float(delta)
        except Exception:
            continue
        entry = totals.setdefault(int(user_id), {"username": username, "hours": 0.0})
        entry["username"] = username
        entry["hours"] += delta_f

    if not totals:
        await send_temp_message(interaction, content="❌ No work activity in the past 30 days.", admin=True)
        return

    desc_lines = []
    total_pay = 0.0
    for e in sorted(totals.values(), key=lambda x: x["hours"], reverse=True):
        h = e["hours"]
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
            color=discord.Color.red()
        )
        embed.set_footer(text=f"Requested by {interaction.user} • {datetime.now(CENTRAL_TZ).strftime('%I:%M %p %Z')}")
        await interaction.response.send_message(embed=embed, view=PurgeConfirmView(), ephemeral=False)


# --- Purge Confirmation View ---
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
                color=discord.Color.green()
            ),
            view=None
        )
        await interaction.message.delete(delay=ADMIN_AUTO_DELETE_TIME)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=discord.Embed(title="❌ Purge Cancelled", description="No data was deleted.", color=discord.Color.greyple()),
            view=None
        )
        await interaction.message.delete(delay=ADMIN_AUTO_DELETE_TIME)


# --- Sync Command ---
@bot.command()
@commands.has_permissions(administrator=True)
async def sync(ctx):
    await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    await ctx.send("✅ Slash commands synced.")


# --- Startup ---
async def setup():
    await bot.add_cog(TimeTracker(bot))
    print("✅ Cog loaded")
    synced = await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    print(f"✅ Synced {len(synced)} commands")


@bot.event
async def on_ready():
    await setup()
    bot.loop.create_task(rotate_statuses())  # 🌀 start rotating funny + live statuses
    cog = bot.get_cog("TimeTracker")
    bot.add_view(ClockButtons(cog))
    bot.add_view(AdminClockButtons(cog))
    print(f"🤖 Logged in as {bot.user}")
    print("🕒 All times shown in Central Time (CT — auto-adjusts for CDT/CST)")

    channel = bot.get_channel(BUTTON_CHANNEL_ID)
    if channel:
        creator_embed = discord.Embed(
            title="👨‍💻 TimeTracker Bot",
            description=(
                "Something I created to help track Yall!!! 😘😘😘.\n\n"
                "**Created by:** <@691108551258800128>\n"
                "📦 **Version:** 1.6.0\n"
                "🕓 **Timezone:** Central Time (auto-adjusts for CDT/CST)\n"
                "💾 **Database:** SQLite (`clockbot.db`)"
            ),
            color=discord.Color.blurple()
        )
        creator_embed.set_footer(text="© 2025 TimeTracker Bot • Developed with ❤️ using discord.py")

        await channel.send(embed=creator_embed)
        await channel.send("👋 **Time Tracking Panel**", view=ClockButtons(cog))
        await channel.send("🛠️ **Admin Control Panel**", view=AdminClockButtons(cog))


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        await send_temp_message(interaction, content="❌ You don’t have permission to run this command.")


# --- Dummy Web Server for Render ---
async def handle(request):
    return web.Response(text="Bot is running!")

app = web.Application()
app.router.add_get("/", handle)


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
        "🪩 Vibing in the time dimension"
    ]

    while not bot.is_closed():
        for status in statuses:
            await bot.change_presence(activity=discord.Game(name=status))
            await asyncio.sleep(60)  # change every 60 seconds


# --- Run Bot + Keep-Alive ---
async def main():
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
