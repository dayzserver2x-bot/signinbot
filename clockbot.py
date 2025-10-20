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
HOURLY_PAY = 2500

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
            await send_temp_message(interaction, content="❌ You don’t have permission to use this.", admin=True)
            return
        await self.cog.clockstatus_func(interaction)

    @discord.ui.button(label="🧾 All Hours", style=discord.ButtonStyle.success, custom_id="persistent_admin_allhours_btn")
    async def all_hours_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await send_temp_message(interaction, content="❌ You don’t have permission to use this.", admin=True)
            return
        await self.cog.allhours_func(interaction, export=False)

    @discord.ui.button(label="📅 7-Day Report", style=discord.ButtonStyle.secondary, custom_id="persistent_admin_weekly_btn")
    async def weekly_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction):
            await send_temp_message(interaction, content="❌ You don’t have permission to use this.", admin=True)
            return
        await self.cog.weeklyreport_func(interaction)

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
        cursor.execute("SELECT clock_in, clock_out FROM time_tracking WHERE user_id = ? AND clock_out IS NOT NULL", (user_id,))
        records = cursor.fetchall()

        if not records:
            await send_temp_message(interaction, content="❌ You don't have any completed work sessions yet.")
            return

        total_hours = 0
        for clock_in, clock_out in records:
            try:
                start = datetime.fromisoformat(clock_in).astimezone(CENTRAL_TZ)
                end = datetime.fromisoformat(clock_out).astimezone(CENTRAL_TZ)
                total_hours += (end - start).total_seconds() / 3600
            except Exception:
                continue

        total_pay = total_hours * HOURLY_PAY
        embed = discord.Embed(
            title=f"🕒 Work Summary for {username}",
            color=discord.Color.teal(),
            description=f"**Total Hours Worked:** {total_hours:.2f}h\n**Total Sessions:** {len(records)}\n**💰 Estimated Pay:** ${total_pay:,.2f}"
        )
        embed.set_footer(text=f"Hourly Rate: ${HOURLY_PAY}/hr • Times shown in CT")
        await send_temp_message(interaction, embed=embed)

    # --- Admin functions ---
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
        cursor.execute("SELECT username, clock_in, clock_out FROM time_tracking WHERE clock_out IS NOT NULL")
        rows = cursor.fetchall()
        if not rows:
            await send_temp_message(interaction, content="❌ No completed work sessions found.", admin=True)
            return
        totals = {}
        for username, clock_in, clock_out in rows:
            try:
                start = datetime.fromisoformat(clock_in).astimezone(CENTRAL_TZ)
                end = datetime.fromisoformat(clock_out).astimezone(CENTRAL_TZ)
                hours = (end - start).total_seconds() / 3600
                totals[username] = totals.get(username, 0) + hours
            except Exception:
                continue
        sorted_totals = sorted(totals.items(), key=lambda x: x[1], reverse=True)
        desc = "\n".join([f"**{u}** — {h:.2f}h" for u, h in sorted_totals])
        embed = discord.Embed(title="🕒 Total Hours Worked (All Users)", description=desc[:4000], color=discord.Color.orange())
        embed.set_footer(text="All times in CT")
        await send_temp_message(interaction, embed=embed, admin=True)

    async def weeklyreport_func(self, interaction: discord.Interaction):
        now = datetime.now(CENTRAL_TZ)
        start = now - timedelta(days=7)
        cursor.execute("SELECT username, clock_in, clock_out FROM time_tracking WHERE clock_out IS NOT NULL")
        rows = cursor.fetchall()
        totals = {}
        for username, clock_in, clock_out in rows:
            try:
                ci = datetime.fromisoformat(clock_in).astimezone(CENTRAL_TZ)
                co = datetime.fromisoformat(clock_out).astimezone(CENTRAL_TZ)
                if co >= start:
                    hours = (co - ci).total_seconds() / 3600
                    totals[username] = totals.get(username, 0) + hours
            except Exception:
                continue
        if not totals:
            await send_temp_message(interaction, content="❌ No work sessions in the past 7 days.", admin=True)
            return
        desc_lines = []
        total_pay = 0
        for user, h in sorted(totals.items(), key=lambda x: x[1], reverse=True):
            pay = h * HOURLY_PAY
            total_pay += pay
            desc_lines.append(f"**{user}** — {h:.2f}h • 💰 ${pay:,.2f}")
        embed = discord.Embed(title="📅 7-Day Work Summary (Admin)", description="\n".join(desc_lines), color=discord.Color.gold())
        embed.add_field(name="🏦 Total Payroll", value=f"${total_pay:,.2f}", inline=False)
        embed.set_footer(text=f"Hourly Rate: ${HOURLY_PAY}/hr • Period: {start.strftime('%b %d')} → {now.strftime('%b %d')} CT")
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
        conn.commit()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="✅ Database Cleared",
                description=f"All time-tracking records have been deleted.\n👤 **Action by:** {interaction.user.mention}",
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
    await bot.add_cog(TimeTracker(bot), guild=discord.Object(id=GUILD_ID))
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
                "📦 **Version:** 1.5.0\n"
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
