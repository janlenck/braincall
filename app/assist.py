"""Brain Assist (#50): Brain Call as the conversation agent of Home Assistant Assist.

The integration Brain Assist runs inside Home Assistant and forwards every Assist request to POST /api/converse:
the text, who asks (Home Assistant user), from where (device, area) and whether the answer is spoken ("voice", a
voice device) or read ("text", the chat in the app or on a dashboard). Here the request gets the same brain as a
phone call: Brain.turn with the tools the person may use. The answer goes back as a stream of JSON lines.

Who may do what comes from callers, like on the phone: an entry with ha_user and/or ha_device. Not listed =
refused with a fixed sentence, without asking Claude."""
import logging
import threading
import time
import uuid

import phrases
import rights

log = logging.getLogger("assist")

API_VERSION = 1            # GET /api/version; raised when the API changes in a way an older Brain Assist cannot use
IDLE_SECONDS = 15 * 60     # a conversation not used this long is forgotten
MAX_CONVERSATIONS = 200    # the longest unused ones go first
MAX_TEXT = 4000            # characters of one request
MAX_RESULT = 2000          # characters of a tool result passed on to the debug view
CHANNELS = ("voice", "text")
WHO = ("user_id", "user_name", "device_id", "device_name", "area_id", "area_name")
LANGS = ("de", "en", "uk")

REFUSAL = {
    "de": "Dieser Benutzer oder dieses Gerät ist für {name} nicht freigeschaltet.",
    "en": "This user or device is not set up for {name}.",
    "uk": "Цього користувача або пристрій не налаштовано для {name}.",
}
# only in the chat, where it can be read: what to enter where
HINT = {
    "de": " Dafür braucht es in Brain Call unter „Anrufer“ (callers) einen Eintrag mit ha_user{user} oder "
          "ha_device{device}.",
    "en": " It takes an entry under “Callers” in Brain Call with ha_user{user} or ha_device{device}.",
    "uk": " Для цього потрібен запис у Brain Call у розділі «Callers» з ha_user{user} або ha_device{device}.",
}


CLOSING = "Answer the request right away.]"  # the end of the first system note of a conversation


def _fold(value) -> str:
    return str(value or "").strip().casefold()


def _clean(value) -> str:
    """A name or free text inside a system note: the note is one pair of square brackets."""
    return str(value or "").strip().replace("[", "(").replace("]", ")")


def parse(body: dict) -> dict:
    """The request of POST /api/converse; ValueError says what is wrong with it."""
    text = str(body.get("text") or "").strip()
    if not text:
        raise ValueError("no text")
    if len(text) > MAX_TEXT:
        raise ValueError(f"text longer than {MAX_TEXT} characters")
    channel = str(body.get("channel") or "text").strip().lower()
    if channel not in CHANNELS:
        raise ValueError(f"channel must be one of {', '.join(CHANNELS)}")
    req = {"text": text, "channel": channel,
           "conversation_id": str(body.get("conversation_id") or "").strip()[:100],
           "language": _fold(body.get("language")).replace("_", "-").split("-")[0]}
    req.update({k: str(body.get(k) or "").strip()[:200] for k in WHO})
    return req


def _entry(callers: list, field: str, values: list) -> dict | None:
    """The entry whose ha_user / ha_device is one of the values; "*" = everyone else.
    The values count in their order, whatever the order of the entries: an id before a name (names are not unique:
    a user named like an admin must not get the admin's entry while an entry lists their own id), a device before
    its area. "*" only if none of them matches."""
    entries = [(_fold(c.get(field)), c) for c in callers or []]
    for value in (_fold(v) for v in values):
        if value and value != "*":
            for own, c in entries:
                if own == value:
                    return c
    return next((c for own, c in entries if own == "*"), None)


def identify(callers: list, req: dict) -> dict | None:
    """Who a request comes from and what they may do; None = not listed.
    The user is found by id or name (ha_user), the device by its id or name, then by the id or name of its area
    (ha_device); "*" stands for every other user or device. With an entry for the user and one for the device the
    stricter access wins: the hallway tablet stays restricted even while an admin is logged in on it."""
    user = _entry(callers, "ha_user", [req.get("user_id"), req.get("user_name")])
    device = None
    if req.get("device_id") or req.get("device_name"):
        device = _entry(callers, "ha_device", [req.get("device_id"), req.get("device_name"),
                                               req.get("area_id"), req.get("area_name")])
    entries = [e for e in (user, device) if e is not None]
    if not entries:
        return None
    access = rights.stricter(user["access"], device["access"]) if user and device else entries[0]["access"]
    webs = [e.get("web") for e in entries]
    # Whose memory this is (memory.CallMemory): the person's, shared with their phone number. A user who has no
    # entry of their own ("*", or only the device is listed) is remembered by their own id: two people logged in
    # on the same tablet never read each other's notes. Only without any user it is the memory of the device,
    # for whoever speaks there.
    asking = req.get("user_id") or req.get("user_name")
    if user is not None and _fold(user["ha_user"]) != "*":
        whom = {"ha_user": user["ha_user"], "number": user["number"] if user["number"] != "*" else ""}
        if user.get("id"):
            whom["contact"] = user["id"]
        if len(user.get("numbers") or []) > 1:  # notes from all their phones
            whom["numbers"] = [n["number"] for n in user["numbers"]]
    elif asking:
        whom = {"ha_user": asking, "number": ""}
    elif device is not None:  # nobody is logged in there (a voice device), whatever entry gave the access
        own = _fold(device["ha_device"]) != "*"
        whom = {"ha_device": device["ha_device"] if own else req.get("device_id") or req.get("device_name")}
    else:
        whom = {}  # a request without user and device: nothing to remember it by
    return {
        "who": whom,
        # summary of a conversation to the notify services: off unless an entry asks for it
        "notify": any(e.get("notify") is True for e in entries),
        # the entries plus who it really is: "*" stands for many users and devices
        "key": "|".join([_fold(user["ha_user"]) if user else "", _fold(device["ha_device"]) if device else "",
                         _fold(req.get("user_id") or req.get("user_name")),
                         _fold(req.get("device_id") or req.get("device_name"))]),
        # a device entry alone says nothing about who is speaking there
        "name": (user["name"] or req.get("user_name") or "") if user and _fold(user["ha_user"]) != "*" else "",
        "access": access,
        "instructions": " ".join(e["instructions"] for e in entries if e.get("instructions")),
        "language": next((e["language"] for e in entries if e.get("language")), ""),
        "web": False if False in webs else (True if True in webs else None),
    }


def language(person: dict | None, req: dict, cfg) -> str:
    """The entry's language, otherwise the language of the Assist pipeline, otherwise the global setting."""
    if person and person["language"]:
        return person["language"]
    if req["language"] in LANGS:
        return req["language"]
    return cfg.language or "de"


def label(person: dict | None, req: dict) -> str:
    """For the log: "Jan via Küche Satellite"."""
    who = (person or {}).get("name") or req["user_name"] or "someone"
    where = req["device_name"] or req["area_name"]
    return f"{who} via {where}" if where else who


def intro(person: dict, req: dict, cfg, owner: str, shopping_history: bool) -> str:
    """The first system note of a conversation: who asks, from where, and what they may do."""
    name = _clean(phrases.first_name(person["name"]))
    verb = "speaking" if req["channel"] == "voice" else "writing"
    area = _clean(req["area_name"])
    who = f"{name} is {verb} to you" if name else f"Someone is {verb} to you; you do not know who"
    where = (f" through the device “{_clean(req['device_name'])}”" if req["device_name"] else "") + (
        f" in the area “{area}”" if area else "")
    note = f"[System: {who}{where}. "
    if area:
        note += f"“Here” and requests that name no room mean the area “{area}”. "
    note += rights.note(person["access"], name, owner, bool(cfg.calendar_write), shopping_history,
                        where="conversation", anyone="The person")
    if person["instructions"]:
        note += f"Instructions from {owner} for this person: {_clean(person['instructions'])} "
    return note + CLOSING


def refusal(req: dict, lang: str, assistant: str) -> str:
    """The fixed answer for a user or device that is not listed (no request to Claude, no cost)."""
    lang = lang if lang in REFUSAL else "de"
    text = REFUSAL[lang].format(name=assistant)
    if req["channel"] == "text":
        text += HINT[lang].format(user=f" „{req['user_name']}“" if req["user_name"] else "",
                                  device=f" „{req['device_name'] or req['area_name']}“"
                                  if req["device_name"] or req["area_name"] else "")
    return text


def asks(text: str) -> bool:
    """Whether an answer ends with a question: the voice device then keeps listening (continue_conversation)."""
    return text.rstrip(" \n\t*_\"'”“»)").endswith(("?", "？", ";"))


class Conversation:
    def __init__(self, cid: str, key: str):
        self.id = cid
        self.key = key            # who it belongs to (identify): another person never continues it
        self.messages: list = []
        self.counters: dict = {}  # pending confirmations, bookings (Brain.turn)
        self.actions: list = []   # changing tool calls and web lookups, for the summary
        self.transcript: list = []  # "Jan: …" / "Claudine: …", for the summary once the conversation is over
        self.person: dict | None = None  # identify() of the last request
        self.channel = "text"
        self.language = "de"
        self.label = ""           # for the log and the summary: "Jan via Küche Satellite"
        self.device = ""          # name of the device or its area
        self.ended = 0.0          # time.time() of the end of the last request
        self.lock = threading.Lock()  # one turn at a time
        self.used = 0.0
        self.pins = 0             # requests that hold it (Conversations.get … release): never forgotten meanwhile


class Conversations:
    """The running Assist conversations by conversation_id, forgotten after IDLE_SECONDS without a request.
    get() hands a conversation out pinned; release() after the turn. A pinned one is never dropped: otherwise a
    second request with the same id could get a new object while the first still runs, with its own history and
    confirmations.
    on_close(conversation) is called for every conversation that is over: not used for the idle time, replaced
    by a new one of the same user and device, or dropped for the limit. That is when it is summarized for the
    memory (Assist has no hang-up). It runs outside the lock and must return quickly."""

    def __init__(self, idle: float = IDLE_SECONDS, limit: int = MAX_CONVERSATIONS, clock=time.monotonic,
                 on_close=None):
        self.idle = idle
        self.limit = limit
        self.clock = clock
        self.on_close = on_close
        self.lock = threading.Lock()
        self.items: dict[str, Conversation] = {}

    def _expired(self, now: float) -> list:
        """Takes out what was not used for the idle time (caller holds the lock)."""
        old = [c for c in self.items.values() if now - c.used > self.idle and not c.pins]
        for c in old:
            del self.items[c.id]
        return old

    def _closed(self, closed: list):
        if self.on_close is not None:
            for conv in closed:
                try:
                    self.on_close(conv)
                except Exception:  # noqa: BLE001  the summary must never break a request
                    log.exception("Closing an Assist conversation failed")

    def sweep(self):
        """Closes the conversations whose idle time is over. Called regularly, so the last conversation of the
        day is summarized too, not only when the next request comes."""
        with self.lock:
            closed = self._expired(self.clock())
        self._closed(closed)

    def get(self, cid: str, key: str) -> Conversation:
        """The conversation with this id, or a new one: without an id, after the idle time, or if the id belongs
        to someone else (what an admin was told is never continued by another user or device).
        It comes pinned: call release() when the request is done."""
        now = self.clock()
        with self.lock:
            closed = self._expired(now)
            conv = self.items.get(cid) if cid else None
            if conv is None or conv.key != key:
                # a new conversation of the same user on the same device: the earlier one is over
                # (Home Assistant gives a new id after a few minutes, or for a new chat)
                earlier = [c for c in self.items.values() if c.key == key and not c.pins]
                for c in earlier:
                    del self.items[c.id]
                closed += earlier
                conv = Conversation(cid if cid and conv is None else uuid.uuid4().hex, key)
                self.items[conv.id] = conv
            conv.used = now
            conv.pins += 1
            if len(self.items) > self.limit:
                idle = sorted((c for c in self.items.values() if not c.pins), key=lambda c: c.used)
                for c in idle[:len(self.items) - self.limit]:
                    del self.items[c.id]
                    closed.append(c)
        self._closed(closed)
        return conv

    def release(self, conv: Conversation):
        """The request that got conv from get() is done: the idle time counts from now."""
        with self.lock:
            conv.pins = max(0, conv.pins - 1)
            conv.used = self.clock()
            conv.ended = time.time()

    def __len__(self) -> int:
        return len(self.items)


def converse(brain, cfg, store: Conversations, req: dict, emit, aborted, owner: str = "", recall=None) -> None:
    """One Assist request (parse) answered as events for the stream of POST /api/converse:
      {"type": "start", "conversation_id", "access"}
      {"type": "text", "text"}                          pieces of the answer, to be joined as they are
      {"type": "tool_call", "id", "name", "input"}      a tool Claude uses ("server": true = web search / fetch)
      {"type": "tool_result", "id", "name", "result"}
      {"type": "end", "conversation_id", "continue_conversation"}
      {"type": "error", "message"}                      instead of "end"
    aborted() is True once the client is gone. recall(person) -> the system note on what is remembered of the
    person (memory.note), for the start of a conversation."""
    person = identify(cfg.callers, req)
    lang = language(person, req, cfg)
    who = label(person, req)
    channel = req["channel"]
    assistant = cfg.assistant_name or "Claude"
    if person is None:
        log.warning("Assist (%s) refused: user %r (id %s), device %r (id %s), area %r (id %s) match no entry in "
                    "callers (ha_user / ha_device)", channel, req["user_name"], req["user_id"] or "-",
                    req["device_name"], req["device_id"] or "-", req["area_name"], req["area_id"] or "-")
        cid = req["conversation_id"] or uuid.uuid4().hex
        emit({"type": "start", "conversation_id": cid, "access": ""})
        emit({"type": "text", "text": refusal(req, lang, assistant)})
        emit({"type": "end", "conversation_id": cid, "continue_conversation": False})
        return
    conv = store.get(req["conversation_id"], person["key"])
    try:
        _turn(brain, cfg, conv, person, req, lang, who, emit, aborted, owner, recall)
    finally:
        store.release(conv)


def requester(person: dict, req: dict) -> dict:
    """Who plans a call in this conversation (plan_call): the person, with their number for a call back if their
    entry has one; a voice device without a user plans as the device."""
    who = person["who"]
    return {"number": who.get("number", ""), "ha_user": who.get("ha_user", ""), "channel": req["channel"],
            "name": person["name"] or req.get("user_name") or req.get("device_name") or req.get("area_name") or ""}


def subject(person: dict) -> str:
    """Whom the notes of a memory are about, for memory.note."""
    return "the people who used this device" if "ha_device" in person["who"] else "this person"


def _turn(brain, cfg, conv: Conversation, person: dict, req: dict, lang: str, who: str, emit, aborted, owner: str,
          recall=None):
    """The turn of converse() for a listed person, in their conversation."""
    channel = req["channel"]
    assistant = cfg.assistant_name or "Claude"
    access = ",".join(sorted(person["access"]))
    said: list[str] = []
    announced: set = set()  # ids of the tool calls the client was told about

    def on_text(piece: str):
        # sentences cleaned for speech come without the space between them; text pieces come as written
        piece = (" " + piece) if channel == "voice" and said else piece
        said.append(piece)
        emit({"type": "text", "text": piece})

    def tool_call(block):
        if block.id not in announced:
            announced.add(block.id)
            emit({"type": "tool_call", "id": block.id, "name": block.name, "input": block.input or {}})

    def before_tool(block) -> bool:
        """Before each tool: once the client is gone (emit noticed it), no further tool runs."""
        tool_call(block)
        return not aborted()

    def on_tool_result(block, out):
        tool_call(block)  # a tool held back by the web safety check never passed before_tool
        emit({"type": "tool_result", "id": block.id, "name": block.name, "result": str(out)[:MAX_RESULT]})

    with conv.lock:
        emit({"type": "start", "conversation_id": conv.id, "access": access})
        log.info("Assist (%s, access %s) %s: %s", channel, access, who, req["text"])
        conv.person, conv.channel, conv.language, conv.label = person, channel, lang, who
        conv.device = req["device_name"] or req["area_name"]
        if not conv.messages:
            note = intro(person, req, cfg, owner or "the owner", bool(getattr(brain, "shopping", None)))
            if recall is not None:
                try:
                    remembered = _clean(recall(person))
                except Exception:  # noqa: BLE001  the memory must never stop a conversation
                    log.exception("Reading the memory failed")
                    remembered = ""
                if remembered:  # inside the system note, before its closing sentence
                    note = note.removesuffix(CLOSING) + remembered.rstrip() + " " + CLOSING
            conv.messages.append({"role": "user", "content": note})
        conv.transcript.append(f"{who}: {req['text']}")
        last = conv.messages[-1]
        if last["role"] == "user" and isinstance(last["content"], str):
            conv.messages[-1] = {"role": "user", "content": last["content"] + "\n" + req["text"]}
        else:
            conv.messages.append({"role": "user", "content": req["text"]})
        try:
            brain.turn(conv.messages, on_text, aborted, access=person["access"], language=lang,
                       actions=conv.actions, counters=conv.counters, before_tool=before_tool,
                       web=person["web"], who=f"{who} (assist {channel})", call=requester(person, req),
                       on_server_tool=lambda name: emit({"type": "tool_call", "name": name, "server": True}),
                       channel=channel, on_tool_result=on_tool_result)
        except Exception as e:  # noqa: BLE001  the client gets the reason instead of a stream that just ends
            log.exception("Assist request failed")
            emit({"type": "error", "message": f"{type(e).__name__}: {e}"[:300]})
            return
        answer = "".join(said).strip()
        if answer:
            conv.transcript.append(f"{assistant}: {answer}")
        log.info("%s: %s", assistant, answer)
        emit({"type": "end", "conversation_id": conv.id, "continue_conversation": asks(answer)})
