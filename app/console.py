"""Colored console output plus key controls (Windows: X/Q quits, A = test call, L = open log)."""
import logging
import os
import sys
import threading

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
MAGENTA = "\033[95m"
CYAN = "\033[96m"
GREY = "\033[90m"


def enable_ansi():
    """Explicitly enables colors (VT sequences) in the Windows console."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        for std in (-11, -12):  # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            h = k32.GetStdHandle(std)
            mode = wintypes.DWORD()
            if k32.GetConsoleMode(h, ctypes.byref(mode)):
                k32.SetConsoleMode(h, mode.value | 0x0004 | 0x0001)  # VIRTUAL_TERMINAL_PROCESSING | PROCESSED_OUTPUT
        k32.SetConsoleOutputCP(65001)
    except Exception:  # noqa: BLE001
        os.system("")
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


ASSISTANT = "Claude"  # set to assistant_name in main.py


class ColorFormatter(logging.Formatter):
    LEVEL = {logging.WARNING: YELLOW, logging.ERROR: RED, logging.CRITICAL: RED + BOLD}
    NAME = {"sip": YELLOW, "ha": BLUE, "brain": MAGENTA, "main": GREY}

    def format(self, record):
        t = self.formatTime(record, "%H:%M:%S")
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        if record.name == "httpx" or record.name.startswith("httpx"):
            return f"{GREY}{t}  {DIM}{msg}{RESET}"
        if record.levelno >= logging.WARNING:
            c = self.LEVEL.get(record.levelno, YELLOW)
            return f"{GREY}{t}{RESET}  {c}{BOLD}{record.levelname:<7}{RESET} {c}{msg}{RESET}"
        if record.name == "session" and msg.startswith(ASSISTANT + ":"):
            return (f"{GREY}{t}{RESET}  {CYAN}{BOLD}🤖 {ASSISTANT} {RESET}"
                    f"{CYAN}{msg[len(ASSISTANT) + 1:].strip()}{RESET}")
        if record.name == "session" and ": " in msg and not msg.startswith(("Error", "TTS", "STT")):
            who, rest = msg.split(": ", 1)
            return f"{GREY}{t}{RESET}  {GREEN}{BOLD}🗣  {who} {RESET}{GREEN}{rest.strip()}{RESET}"
        if msg.startswith(("Calling ", "Answering call")):
            return f"{GREY}{t}{RESET}  {MAGENTA}{BOLD}📞 {msg}{RESET}"
        if msg.startswith(("Call ended", "Not answered")):
            return f"{GREY}{t}{RESET}  {MAGENTA}📴 {msg}{RESET}"
        if "REGISTERED" in msg:
            return f"{GREY}{t}{RESET}  {GREEN}{BOLD}✔ {msg}{RESET}"
        c = self.NAME.get(record.name, "")
        return f"{GREY}{t}{RESET}  {c}{msg}{RESET}"


def banner(cfg):
    line = "─" * 62
    print(f"{CYAN}{line}{RESET}")
    print(f"{CYAN}{BOLD}  BrainCall{RESET}   {GREY}model: {cfg.claude_model}  ·  voice: {cfg.tts_voice}{RESET}")
    if os.name == "nt":
        print(f"  {BOLD}[X]{RESET} quit   {BOLD}[A]{RESET} test call to {cfg.default_number or '-'}   "
              f"{BOLD}[L]{RESET} open log file")
    print(f"{CYAN}{line}{RESET}")


def start_keys(on_quit, on_test, on_log):
    """Reads keys without blocking (Windows console only)."""
    if os.name != "nt" or not sys.stdin.isatty():
        return
    import msvcrt
    import time

    def loop():
        while True:
            if msvcrt.kbhit():
                ch = msvcrt.getwch().lower()
                if ch in ("x", "q", "\x1b"):
                    on_quit()
                    return
                if ch == "a":
                    on_test()
                if ch == "l":
                    on_log()
            time.sleep(0.1)

    threading.Thread(target=loop, daemon=True).start()
