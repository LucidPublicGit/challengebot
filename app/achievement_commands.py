"""Admin-only achievement catalog commands; mutations use the shared validated service."""

import asyncio

import discord
from discord import app_commands

from app.services import achievements as ach


@app_commands.guild_only()
class AchievementAdmin(app_commands.Group):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        from app.bot import is_bot_admin

        if interaction.guild and is_bot_admin(interaction):
            return True
        await interaction.response.send_message("Admin only.", ephemeral=True)
        return False


achievement_admin = AchievementAdmin(
    name="achievement_admin", description="Admin: manage achievement definitions"
)


async def _save(interaction, data):
    await interaction.response.defer(ephemeral=True)
    try:
        aid = await asyncio.to_thread(ach.save_definition, data)
    except ValueError as exc:
        await interaction.followup.send(
            str(exc)[:1900], ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )
        return
    await interaction.followup.send(
        f"Saved achievement `{aid}`.", ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@achievement_admin.command(name="add", description="Add an achievement with a permanent generated ID")
@app_commands.describe(
    rule_json='Unlock rule, e.g. {"kind":"clears","count":5}',
    image_file="Managed PNG filename, e.g. achievement-001.png; upload new art in the web UI",
)
async def add(
    interaction: discord.Interaction, title: str, description: str, rule_json: str,
    image_file: str, position: app_commands.Range[int, 0, 100000] = 0,
    published: bool = True,
):
    await _save(interaction, dict(
        title=title, description=description, rule_json=rule_json,
        image_file=image_file, position=position, enabled=int(published),
    ))


@achievement_admin.command(name="edit", description="Modify a definition; omitted fields stay unchanged")
async def edit(
    interaction: discord.Interaction, achievement_id: str, title: str | None = None,
    description: str | None = None, rule_json: str | None = None,
    image_file: str | None = None, position: app_commands.Range[int, 0, 100000] | None = None,
    published: bool | None = None,
):
    definitions = await asyncio.to_thread(ach.list_definitions, include_removed=True)
    item = next((d for d in definitions if d["id"] == achievement_id), None)
    if item is None:
        await interaction.response.send_message("Achievement not found.", ephemeral=True)
        return
    for key, value in dict(title=title, description=description, rule_json=rule_json,
                           image_file=image_file, position=position, enabled=published).items():
        if value is not None:
            item[key] = int(value) if key == "enabled" else value
    await _save(interaction, item)


@achievement_admin.command(name="remove", description="Hide an achievement, preserving its ID and receipts")
async def remove(interaction: discord.Interaction, achievement_id: str, confirm: bool):
    if not confirm:
        await interaction.response.send_message("Set confirm:True to remove.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        await asyncio.to_thread(ach.remove_definition, achievement_id)
    except ValueError:
        await interaction.followup.send("Achievement not found.", ephemeral=True)
        return
    await interaction.followup.send("Achievement removed. Unlock history kept.", ephemeral=True)
