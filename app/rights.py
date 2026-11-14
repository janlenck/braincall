"""What a person may do, in words for Claude and as a rule for two entries at once.

The tools themselves are limited in Brain.tools_for; the note tells Claude what that means in the conversation."""

# admin includes everything the other rights allow (and more: control, planned calls)
_ALL = frozenset({"read", "calendar", "shopping"})
ADMIN = frozenset({"admin"})
GUEST = frozenset({"guest"})


def note(access, name: str, owner: str, calendar_write: bool, shopping_history: bool, grants=None,
         where: str = "call", anyone: str = "The caller") -> str:
    """System note on what the person may do. name: how to speak of them ("" = anyone); owner: the owner's first
    name; grants: what the task of a planned call adds for the callee (tasks.build); where: "call" on the phone,
    "conversation" in Brain Assist."""
    who = name or anyone
    quiet = (f"Give no information at all about {owner}, the house, whether anyone is at home, or about devices"
             + (", except what is allowed above. " if access - {"guest"} else ". "))
    if "admin" in access:
        return ""
    grants = grants or {}
    granted = ""
    if grants and not access & {"read", "calendar"}:
        granted = (f"For this task {who} may hear when {owner} is free or busy from {grants['calendar_from']} to "
                   f"{grants['calendar_to']} (ha_busy_times: you only see busy times, never guess or reveal what "
                   "an appointment is). ")
    if grants.get("book_title") and "calendar" not in access and calendar_write:
        granted += (f"You may book one appointment titled “{grants['book_title']}” into a free time of these "
                    "days: repeat day and time once, and after a yes call ha_create_event with confirmed=true. ")
    if access == {"guest"}:
        return (f"You have no access to the house in this {where}{', except for the task' if granted else ''}. "
                + granted + quiet)
    parts = [granted] if granted else []
    if "read" in access:
        parts.append(f"{who} may ask about everything in Home Assistant (states, sensors, appointments "
                     "with details), but must not switch or change anything. ")
    elif "calendar" in access:
        parts.append(f"{who} may ask whether {owner} is free or busy (ha_busy_times: you only see busy times, "
                     "never guess or reveal what an appointment is). ")
    if "calendar" in access and calendar_write:
        parts.append(f"{who} may book an appointment in a free slot of the default calendar without {owner}'s "
                     "approval: repeat day and time once, and after a yes call ha_create_event with "
                     "confirmed=true. ")
    if "shopping" in access:
        parts.append(f"{who} may hear and change the shopping list (shopping_list, shopping_edit)"
                     + (" and ask what was bought when (shopping_history)" if shopping_history else "")
                     + ". ")
    return "".join(parts) + ("" if "read" in access else quiet)


def stricter(a, b) -> frozenset:
    """What both of two access sets allow (Brain Assist: the entry of the user and the entry of the device).
    admin only if both are admin; nothing in common = guest. A hallway tablet with "shopping" stays at the
    shopping list even while an admin is logged in on it."""
    if "admin" in a and "admin" in b:
        return ADMIN
    common = (_ALL if "admin" in a else frozenset(a)) & (_ALL if "admin" in b else frozenset(b))
    return frozenset(common - {"guest"}) or GUEST
