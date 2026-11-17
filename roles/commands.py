"""Discord slash commands for role management.

These handlers are deliberately thin. They parse arguments, build an
ActionContext and call RoleManagerService — they contain no permission logic,
no sheet access and no synchronization. Every authorization decision happens
inside the service, in permissions.check().

Registration is factored out as `register_commands(tree)` so the command set can
live on its own bot or be attached to an existing CommandTree.
"""

import discord
from discord import app_commands

from roles import config as roles_config
from roles import state as roles_state
from roles import timezones
from roles.models import ActionContext, ActionOrigin, ActionResult, ActionStatus
from roles.service import service

# ── Presentation ─────────────────────────────────────────────────────────────

_COLORS = {
    ActionStatus.OK: discord.Color.green(),
    ActionStatus.DENIED: discord.Color.red(),
    ActionStatus.INVALID: discord.Color.orange(),
    ActionStatus.NOT_FOUND: discord.Color.orange(),
    ActionStatus.ERROR: discord.Color.dark_red(),
}

_TITLES = {
    ActionStatus.OK: "✅ Done",
    ActionStatus.DENIED: "⛔ Not authorized",
    ActionStatus.INVALID: "⚠️ Cannot do that",
    ActionStatus.NOT_FOUND: "❓ Not found",
    ActionStatus.ERROR: "🛑 Error",
}


def result_embed(result):
    embed = discord.Embed(
        title=_TITLES.get(result.status, "Result"),
        description=result.message[:4000],
        color=_COLORS.get(result.status, discord.Color.blurple()),
    )
    if result.queued_targets:
        embed.set_footer(text=f"Queued for: {', '.join(result.queued_targets)}")
    return embed


def context_from(interaction, origin=ActionOrigin.DISCORD_COMMAND):
    """Build an ActionContext from a Discord interaction.

    The actor is always interaction.user.id — the identity Discord itself
    verified. Nothing user-supplied can influence who the system thinks is acting.
    """
    return ActionContext(
        actor_discord_id=interaction.user.id,
        origin=origin,
        guild_id=interaction.guild_id,
        channel_id=interaction.channel_id,
    )


async def _respond(interaction, result, ephemeral=True):
    embed = result_embed(result)
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=ephemeral)


# ── Autocomplete ─────────────────────────────────────────────────────────────

async def rank_autocomplete(interaction, current):
    cfg = roles_config.current()
    needle = (current or "").casefold()
    ranks = list(cfg.normal_ranks()) + list(cfg.special_ranks())
    return [
        app_commands.Choice(name=r.display[:100], value=r.key)
        for r in ranks
        if needle in r.display.casefold() or needle in r.key.casefold()
    ][:25]


async def branch_autocomplete(interaction, current):
    cfg = roles_config.current()
    needle = (current or "").casefold()
    return [
        app_commands.Choice(name=b.display[:100], value=b.key)
        for b in cfg.branches.values()
        if needle in b.display.casefold() or needle in b.key.casefold()
    ][:25]


async def grantable_autocomplete(interaction, current):
    cfg = roles_config.current()
    needle = (current or "").casefold()
    return [
        app_commands.Choice(
            name=f"{g.display} — {cfg.grantable_guild_label(g)}"[:100], value=g.key,
        )
        for g in cfg.grantable_roles.values()
        if needle in g.display.casefold() or needle in g.key.casefold()
    ][:25]


async def timezone_autocomplete(interaction, current):
    options = timezones.suggestions(current or "")
    return [app_commands.Choice(name=o[:100], value=o) for o in options[:25]]


async def primary_line_autocomplete(interaction, current):
    """The lines the member being edited can actually choose from.

    Display only: the service re-checks every choice, so a stale list can at
    worst offer something that is then refused.
    """
    cfg = roles_config.current()
    target = getattr(interaction.namespace, "user", None)
    target_id = getattr(target, "id", None) or interaction.user.id
    record = service.find_record(target_id)

    choices = [app_commands.Choice(name="Mainline (N/A)", value="N/A")]
    if record is not None:
        rank = cfg.rank(record.rank_key)
        for key in cfg.primary_line_options(
                rank, record.branch_keys, roles_state.branch_roles_enabled(cfg)):
            choices.append(app_commands.Choice(
                name=f"{cfg.branches[key].display} line"[:100], value=key,
            ))

    needle = (current or "").casefold()
    return [c for c in choices if needle in c.name.casefold()][:25]


# ── Command registration ─────────────────────────────────────────────────────

def register_commands(tree):
    """Attach every role-management command to a CommandTree."""

    set_group = app_commands.Group(
        name="set", description="Modify a PERSONNEL record"
    )
    roles_group = app_commands.Group(
        name="aria", description="Role management administration"
    )
    grant_group = app_commands.Group(
        name="grant", description="Give or revoke grantable roles (role modifiers)"
    )

    # ── /register ──

    @tree.command(
        name="register",
        description="Register yourself in PERSONNEL (verifies your Roblox account)",
    )
    async def register_command(interaction: discord.Interaction):
        # Always ephemeral: the response can carry a verification link that must
        # reach nobody but this user.
        await interaction.response.defer(ephemeral=True)
        result = await service.register(
            context_from(interaction),
            # The account username (@handle), not display_name — that would give
            # the per-server nickname, which differs between servers and changes
            # whenever someone edits it.
            discord_username=interaction.user.name,
        )

        auth_url = result.detail.get("auth_url")
        if result.ok and auth_url:
            embed = discord.Embed(
                title="🔗 Link your Roblox account",
                description=(
                    f"{result.message}\n\n"
                    f"**[Authorize with Roblox]({auth_url})**"
                ),
                color=discord.Color.blurple(),
            )
            embed.set_footer(text="This link is yours alone. Do not share it.")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        await _respond(interaction, result)

    # ── /set timezone ──

    @set_group.command(name="timezone", description="Set your timezone")
    @app_commands.describe(
        timezone="Your UTC offset, such as UTC+1, UTC-5 or UTC+5:30",
        user="Whose timezone to set (requires authorization; defaults to yourself)",
    )
    @app_commands.autocomplete(timezone=timezone_autocomplete)
    async def set_timezone_command(
        interaction: discord.Interaction,
        timezone: str,
        user: discord.Member = None,
    ):
        await interaction.response.defer(ephemeral=True)
        result = await service.set_timezone(
            context_from(interaction),
            raw_timezone=timezone,
            target_discord_id=user.id if user else None,
        )
        await _respond(interaction, result)

    # ── /set rank ──

    @set_group.command(name="rank", description="Set a user's rank")
    @app_commands.describe(user="The user to modify", rank="The rank to assign")
    @app_commands.autocomplete(rank=rank_autocomplete)
    async def set_rank_command(
        interaction: discord.Interaction,
        user: discord.Member,
        rank: str,
    ):
        await interaction.response.defer(ephemeral=True)
        result = await service.set_rank(
            context_from(interaction), rank_input=rank, target_discord_id=user.id,
        )
        await _respond(interaction, result)

    # ── /set branch ──

    @set_group.command(
        name="branch",
        description="Set a user's branches (replaces all of their current branches)",
    )
    @app_commands.describe(
        user="The user to modify",
        branch="A branch to assign, or N/A to remove them from every branch",
        branch_2="Another branch they belong to (optional)",
        branch_3="Another branch they belong to (optional)",
        branch_4="Another branch they belong to (optional)",
        branch_5="Another branch they belong to (optional)",
    )
    @app_commands.autocomplete(
        branch=branch_autocomplete, branch_2=branch_autocomplete,
        branch_3=branch_autocomplete, branch_4=branch_autocomplete,
        branch_5=branch_autocomplete,
    )
    async def set_branch_command(
        interaction: discord.Interaction,
        user: discord.Member,
        branch: str,
        branch_2: str = None,
        branch_3: str = None,
        branch_4: str = None,
        branch_5: str = None,
    ):
        await interaction.response.defer(ephemeral=True)
        branches = [b for b in (branch, branch_2, branch_3, branch_4, branch_5) if b]
        result = await service.set_branches(
            context_from(interaction), branch_inputs=branches, target_discord_id=user.id,
        )
        await _respond(interaction, result)

    # ── /set primary ──

    @set_group.command(
        name="primary",
        description="Choose whether your Roblox role is your mainline or a branch-line role",
    )
    @app_commands.describe(
        line="Mainline (N/A), or one of your branches that has a role at your rank",
        user="Whose primary role to set (requires authorization; defaults to yourself)",
    )
    @app_commands.autocomplete(line=primary_line_autocomplete)
    async def set_primary_command(
        interaction: discord.Interaction,
        line: str,
        user: discord.Member = None,
    ):
        await interaction.response.defer(ephemeral=True)
        result = await service.set_primary_role(
            context_from(interaction),
            line_input=line,
            target_discord_id=user.id if user else None,
        )
        await _respond(interaction, result)

    # ── /set status ──

    @set_group.command(name="status", description="Set an activity status")
    @app_commands.describe(
        status="ACTIVE, SEMI-ACTIVE or IN-ACTIVE",
        user="Whose status to set (requires authorization; defaults to yourself)",
    )
    @app_commands.choices(status=[
        app_commands.Choice(name="ACTIVE", value="ACTIVE"),
        app_commands.Choice(name="SEMI-ACTIVE", value="SEMI-ACTIVE"),
        app_commands.Choice(name="IN-ACTIVE", value="IN-ACTIVE"),
    ])
    async def set_status_command(
        interaction: discord.Interaction,
        status: app_commands.Choice[str],
        user: discord.Member = None,
    ):
        await interaction.response.defer(ephemeral=True)
        result = await service.set_status(
            context_from(interaction),
            status_input=status.value,
            target_discord_id=user.id if user else None,
        )
        await _respond(interaction, result)

    # ── /set loa ──

    @set_group.command(name="loa", description="Record a leave of absence")
    @app_commands.describe(
        value="LOA value in the form #/#/# (field meanings pending definition), or NONE to clear",
        user="Whose LOA to set (requires authorization; defaults to yourself)",
    )
    async def set_loa_command(
        interaction: discord.Interaction,
        value: str,
        user: discord.Member = None,
    ):
        await interaction.response.defer(ephemeral=True)
        result = await service.set_loa(
            context_from(interaction),
            raw_value=value,
            target_discord_id=user.id if user else None,
        )
        await _respond(interaction, result)

    # ── /grant add ──

    @grant_group.command(
        name="add",
        description="Give a user a grantable role on top of their rank",
    )
    @app_commands.describe(user="The user to modify", role="The grantable role to give")
    @app_commands.autocomplete(role=grantable_autocomplete)
    async def grant_add_command(
        interaction: discord.Interaction,
        user: discord.Member,
        role: str,
    ):
        await interaction.response.defer(ephemeral=True)
        result = await service.grant_role(
            context_from(interaction), grantable_input=role, target_discord_id=user.id,
        )
        await _respond(interaction, result)

    # ── /grant remove ──

    @grant_group.command(
        name="remove",
        description="Revoke a grantable role from a user",
    )
    @app_commands.describe(user="The user to modify", role="The grantable role to revoke")
    @app_commands.autocomplete(role=grantable_autocomplete)
    async def grant_remove_command(
        interaction: discord.Interaction,
        user: discord.Member,
        role: str,
    ):
        await interaction.response.defer(ephemeral=True)
        result = await service.revoke_role(
            context_from(interaction), grantable_input=role, target_discord_id=user.id,
        )
        await _respond(interaction, result)

    # ── /roles whois ──

    @roles_group.command(name="whois", description="Show a PERSONNEL record")
    @app_commands.describe(user="Whose record to show (defaults to yourself)")
    async def whois_command(
        interaction: discord.Interaction,
        user: discord.Member = None,
    ):
        await interaction.response.defer(ephemeral=True)
        result = await service.get_record(
            context_from(interaction),
            target_discord_id=user.id if user else None,
        )
        await _respond(interaction, result)

    # ── /roles sync ──

    @roles_group.command(name="sync", description="Force re-synchronization to Discord and Roblox")
    @app_commands.describe(
        user="A specific user, or omit to re-sync everyone",
        clean="With a user: remove ALL their Discord roles, then give back only the ones they should have",
    )
    async def sync_command(
        interaction: discord.Interaction,
        user: discord.Member = None,
        clean: bool = False,
    ):
        await interaction.response.defer(ephemeral=True)
        result = await service.force_sync(
            context_from(interaction),
            target_discord_id=user.id if user else None,
            clean=clean,
        )
        await _respond(interaction, result)

    # ── /roles restructure ──

    @roles_group.command(
        name="restructure",
        description="Re-sort the PERSONNEL sheet and remove unused rows",
    )
    @app_commands.describe(
        purge_incomplete="DESTRUCTIVE: also delete hand-entered rows that have no "
                         "Discord ID. Use once to prepare a pre-existing sheet.",
    )
    async def restructure_command(
        interaction: discord.Interaction,
        purge_incomplete: bool = False,
    ):
        await interaction.response.defer(ephemeral=True)
        result = await service.restructure(
            context_from(interaction), purge_incomplete=purge_incomplete
        )
        await _respond(interaction, result)

    # ── /aria branch-roles ──

    @roles_group.command(
        name="branch-roles",
        description="Enable or disable branch-line Roblox roles for everyone",
    )
    @app_commands.describe(
        setting="Disable moves everyone back to their mainline Roblox role and "
                "stops anyone from switching",
    )
    @app_commands.choices(setting=[
        app_commands.Choice(name="enable", value="enable"),
        app_commands.Choice(name="disable", value="disable"),
    ])
    async def branch_roles_command(
        interaction: discord.Interaction,
        setting: app_commands.Choice[str],
    ):
        await interaction.response.defer(ephemeral=True)
        result = await service.set_branch_roles_enabled(
            context_from(interaction), enabled=setting.value == "enable",
        )
        await _respond(interaction, result)

    # ── /roles status ──

    @roles_group.command(
        name="status",
        description="Integrity, synchronization and configuration report",
    )
    async def status_command(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        result = await service.inspect(context_from(interaction))
        await _respond(interaction, result)

    # ── /roles reload ──

    @roles_group.command(
        name="reload",
        description="Reload roles_config.json without restarting the bot",
    )
    async def reload_command(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        # Gated by the same permission as other administrative actions.
        result = await service.inspect(context_from(interaction))
        if not result.ok:
            await _respond(interaction, result)
            return

        cfg = roles_config.reload()
        missing = cfg.missing_configuration()
        summary = (
            f"Configuration reloaded: **{len(cfg.ranks)}** rank(s), "
            f"**{len(cfg.branches)}** branch(es), "
            f"**{len(cfg.grantable_roles)}** grantable role(s), "
            f"**{sum(len(ids) for ids in cfg.general_roles.values())}** general role(s)."
        )
        if missing:
            summary += "\n\n**Still missing:**\n" + "\n".join(f"• {m}" for m in missing)

        await interaction.followup.send(
            embed=discord.Embed(
                title="✅ Configuration reloaded",
                description=summary[:4000],
                color=discord.Color.green(),
            ),
            ephemeral=True,
        )

    @tree.error
    async def on_command_error(interaction: discord.Interaction, error):
        # Every command defers first, so an exception escaping the service (a
        # Sheets call that timed out, say) would otherwise leave the member
        # watching "thinking..." until Discord gives up on it.
        original = getattr(error, "original", error)
        print(f"[Roles] /{interaction.command.qualified_name if interaction.command else '?'} "
              f"failed: {type(original).__name__}: {original}")
        try:
            await _respond(interaction, ActionResult.error(
                f"The operation failed: {type(original).__name__}: {original}. "
                f"Check the record with /aria whois before trying again."
            ))
        except discord.HTTPException:
            pass

    tree.add_command(set_group)
    tree.add_command(roles_group)
    tree.add_command(grant_group)

    print("[Roles] Slash commands registered: /register, /set …, /grant …, /aria …")
    return tree
