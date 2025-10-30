import discord
from discord.ext import commands, tasks
from discord import app_commands, ui
import asyncio
import datetime
import json
from flask import Flask
import threading

# ===== CONFIG =====
TICKET_CATEGORY_NAMES = ["Mods Related", "Other Query", "Bugs", "Suggestions", "Feedback"]
TICKET_LOG_CHANNEL_ID = 1432707234755776512  # Replace with your logs channel ID
MOD_ROLE_IDS = {
    "Mods Related": 1430639523360145560,
    "Other Query": 1430639523360145560,
    "Bugs": 1430639523360145560,
    "Suggestions": 1430639523360145560,
    "Feedback": 1430639523360145560
}
TICKET_TIMEOUT = 600  # 10 minutes idle deletion
CLOSED_TIMEOUT = 300  # 5 minutes after closure
ESCALATION_TIME = 900  # 15 minutes to escalate unresolved tickets
# ==================

intents = discord.Intents.default()
intents.members = True
intents.messages = True
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ---- Data storage ----
tickets_data = {}  # {channel_id: {"user_id": , "category": , "created_at": , "closed_at": , "feedback": , "escalated": False, "handled_by": mod_id}}
user_points = {}  # {user_id: points}

# ===== FLASK SERVER / KEEP ALIVE =====
app = Flask("")

@app.route("/")
def home():
    return "RASH MODS Ultimate Bot is Online!"

def run_flask():
    app.run(host="0.0.0.0", port=8080)

threading.Thread(target=run_flask).start()

# ----- Ticket Dropdown -----
class TicketCategoryDropdown(ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=cat) for cat in TICKET_CATEGORY_NAMES]
        super().__init__(placeholder="Choose a category...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        category_name = self.values[0]
        guild = interaction.guild
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }

        mod_role_id = MOD_ROLE_IDS.get(category_name)
        if mod_role_id:
            mod_role = guild.get_role(mod_role_id)
            overwrites[mod_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        channel = await guild.create_text_channel(
            name=f"ticket-{interaction.user.name}-{int(datetime.datetime.utcnow().timestamp())}",
            overwrites=overwrites,
            topic=f"Ticket for {interaction.user} | Category: {category_name}"
        )
        tickets_data[channel.id] = {
            "user_id": interaction.user.id,
            "category": category_name,
            "created_at": datetime.datetime.utcnow(),
            "closed_at": None,
            "feedback": None,
            "escalated": False,
            "handled_by": None
        }

        await interaction.response.send_message(f"Ticket created: {channel.mention}", ephemeral=True)
        await channel.send(f"Hello {interaction.user.mention}, describe your issue. Mods will assist you soon.")
        
        bot.loop.create_task(ticket_idle_checker(channel))
        bot.loop.create_task(ticket_escalation_checker(channel))

async def ticket_idle_checker(channel):
    await asyncio.sleep(TICKET_TIMEOUT)
    if channel.id in tickets_data and tickets_data[channel.id]["closed_at"] is None:
        await close_ticket(channel, reason="Ticket idle timeout")

async def ticket_escalation_checker(channel):
    await asyncio.sleep(ESCALATION_TIME)
    ticket = tickets_data.get(channel.id)
    if ticket and not ticket["escalated"] and ticket["closed_at"] is None:
        mod_role_id = MOD_ROLE_IDS.get(ticket["category"])
        mod_role = channel.guild.get_role(mod_role_id)
        await channel.send(f"{mod_role.mention} This ticket has been idle for {ESCALATION_TIME//60} minutes. Please assist!")
        ticket["escalated"] = True

# ----- Ticket Feedback -----
class FeedbackDropdown(ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="👍 Satisfied", value="satisfied"),
            discord.SelectOption(label="👎 Unsatisfied", value="unsatisfied")
        ]
        super().__init__(placeholder="How was your support?", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        response = self.values[0]
        ticket = tickets_data.get(interaction.channel.id)
        if ticket:
            ticket["feedback"] = response
            ticket["closed_at"] = datetime.datetime.utcnow()
            # Assume the last message author (mod) handled it
            ticket["handled_by"] = interaction.user.id if interaction.user.id != ticket["user_id"] else None
            user_points[ticket["user_id"]] = user_points.get(ticket["user_id"], 0) + (5 if response=="satisfied" else 0)
        await interaction.response.send_message(f"Thanks for your feedback: {response}", ephemeral=True)
        await generate_transcript(interaction.channel)
        await asyncio.sleep(CLOSED_TIMEOUT)
        await interaction.channel.delete(reason="Ticket closed with feedback")

class FeedbackView(ui.View):
    def __init__(self):
        super().__init__()
        self.add_item(FeedbackDropdown())

async def generate_transcript(channel):
    messages = await channel.history(limit=None, oldest_first=True).flatten()
    transcript = "\n".join([f"{m.author}: {m.content}" for m in messages])
    log_channel = channel.guild.get_channel(TICKET_LOG_CHANNEL_ID)
    await log_channel.send(f"Transcript for {channel.name}:\n```{transcript}```")

async def close_ticket(channel, reason="Closed"):
    if channel.id in tickets_data:
        ticket = tickets_data[channel.id]
        ticket["closed_at"] = datetime.datetime.utcnow()
        await generate_transcript(channel)
        await channel.send(f"Ticket closed: {reason}", view=FeedbackView())

# ----- Ticket Button -----
class CreateTicketButton(ui.View):
    def __init__(self):
        super().__init__()
        self.add_item(ui.Button(label="Create Ticket", style=discord.ButtonStyle.green, custom_id="create_ticket"))

    @ui.button(label="Create Ticket", style=discord.ButtonStyle.green, custom_id="create_ticket")
    async def create_ticket(self, button: ui.Button, interaction: discord.Interaction):
        await interaction.response.send_message("Select ticket category:", view=TicketCategoryView(), ephemeral=True)

class TicketCategoryView(ui.View):
    def __init__(self):
        super().__init__()
        self.add_item(TicketCategoryDropdown())

# ----- Polls -----
class PollDropdown(ui.Select):
    def __init__(self, options_list):
        options = [discord.SelectOption(label=opt) for opt in options_list]
        super().__init__(placeholder="Vote now!", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"You voted for: {self.values[0]}", ephemeral=True)

# ----- Commands -----
@bot.event
async def on_ready():
    print(f"Bot connected as {bot.user}")
    try:
        await bot.tree.sync()
        print("Commands synced.")
    except Exception as e:
        print(e)

@bot.tree.command(name="faq", description="Get answers to frequently asked questions")
async def faq(interaction: discord.Interaction):
    embed = discord.Embed(title="FAQ", description="Common questions:", color=0x00ff00)
    embed.add_field(name="Q1", value="How to use mods?", inline=False)
    embed.add_field(name="Q2", value="Where to report bugs?", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="assign_role", description="Assign a role to a user")
@app_commands.describe(user="User", role="Role to assign")
async def assign_role(interaction: discord.Interaction, user: discord.Member, role: discord.Role):
    await user.add_roles(role)
    await interaction.response.send_message(f"Role {role.name} assigned to {user.name}", ephemeral=True)

@bot.tree.command(name="remove_role", description="Remove a role from a user")
@app_commands.describe(user="User", role="Role to remove")
async def remove_role(interaction: discord.Interaction, user: discord.Member, role: discord.Role):
    await user.remove_roles(role)
    await interaction.response.send_message(f"Role {role.name} removed from {user.name}", ephemeral=True)

@bot.tree.command(name="poll", description="Create a mini poll")
@app_commands.describe(options="Comma-separated options")
async def poll(interaction: discord.Interaction, options: str):
    options_list = [opt.strip() for opt in options.split(",")][:25]
    view = ui.View()
    view.add_item(PollDropdown(options_list))
    await interaction.response.send_message("Vote below:", view=view)

@bot.tree.command(name="points", description="Check your points")
async def points(interaction: discord.Interaction):
    pts = user_points.get(interaction.user.id, 0)
    await interaction.response.send_message(f"You have {pts} points.", ephemeral=True)

@bot.tree.command(name="dashboard", description="View ticket and user analytics")
async def dashboard(interaction: discord.Interaction):
    total_open = sum(1 for t in tickets_data.values() if t["closed_at"] is None)
    total_closed = sum(1 for t in tickets_data.values() if t["closed_at"] is not None)
    
    # Closed tickets per mod
    closed_by_mod = {}
    for t in tickets_data.values():
        mod_id = t["handled_by"]
        if mod_id:
            closed_by_mod[mod_id] = closed_by_mod.get(mod_id, 0) + 1
    
    # Average response time
    total_time = 0
    count_time = 0
    for t in tickets_data.values():
        if t["closed_at"]:
            created = t["created_at"]
            closed = t["closed_at"]
            total_time += (closed - created).total_seconds()
            count_time += 1
    avg_response = (total_time / count_time)/60 if count_time > 0 else 0

    # Feedback stats
    feedback_counts = {"satisfied": 0, "unsatisfied": 0}
    for t in tickets_data.values():
        fb = t["feedback"]
        if fb in feedback_counts:
            feedback_counts[fb] += 1
    
    # Points leaderboard
    leaderboard = sorted(user_points.items(), key=lambda x: x[1], reverse=True)[:10]
    lb_text = "\n".join([f"<@{uid}>: {pts} pts" for uid, pts in leaderboard]) or "No points yet"

    embed = discord.Embed(title="RASH MODS Analytics Dashboard", color=0x00ff00)
    embed.add_field(name="Open Tickets", value=str(total_open))
    embed.add_field(name="Closed Tickets", value=str(total_closed))
    embed.add_field(name="Average Response Time", value=f"{avg_response:.2f} minutes")
    embed.add_field(name="Feedback Stats", value=f"👍 {feedback_counts['satisfied']} | 👎 {feedback_counts['unsatisfied']}")
    embed.add_field(name="Closed Tickets per Mod", value="\n".join([f"<@{mid}>: {cnt}" for mid, cnt in closed_by_mod.items()]) or "None")
    embed.add_field(name="Points Leaderboard", value=lb_text, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ----- Welcome -----
@bot.event
async def on_member_join(member):
    try:
        embed = discord.Embed(title=f"Welcome {member.name}!", color=0x00ff00)
        embed.add_field(name="Rules & Guidelines", value="1. Be respectful\n2. Follow channel rules\n3. Enjoy RASH MODS!")
        await member.send(embed=embed)
    except:
        pass

# ----- Ticket Creation Command -----
@bot.command()
async def ticket(ctx):
    view = CreateTicketButton()
    await ctx.send("Click the button below to create a ticket:", view=view)

# ----- Run Bot -----
bot.run("MTQzMjQ1MDc0NDQwNTE5Njg3Mw.GKLGKp.5mNt7l2krR5pJvXND-Tnzio_4HyL2VTiKCQJwk")
