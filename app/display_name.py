"""The name a speaking bot shows in the call."""


def resolve_display_name(
    requested_name: str | None, persona_name: str, bot_name_selected_persona: bool
) -> str:
    """Pick the name MeetingBaas displays for the bot.

    ``bot_name`` is the caller's display name and wins whenever it was given.
    The persona only names the character the bot plays, so it stands in when
    no name was given, or when ``bot_name`` was merely a persona key (its own
    name reads better in the call than the key).
    """
    requested = (requested_name or "").strip()
    if requested and not bot_name_selected_persona:
        return requested
    return persona_name
