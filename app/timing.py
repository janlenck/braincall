"""Timing log per turn, relative to the end of the caller's speech, and a summary per call."""
import statistics
import threading
import time

# step -> label in the log; "audio" = first sound after the caller's speech, filler or answer
STEPS = {
    "detected": "end detected",
    "stt": "STT",
    "token": "first token",
    "sentence": "first sentence",
    "tts": "TTS",
    "filler": "filler",
    "playback": "playback",
    "audio": "first audio",
}


class TurnTimer:
    def __init__(self, t0: float, number: int):
        self.t0 = t0  # end of the caller's speech (time.monotonic)
        self.number = number
        self.marks: dict[str, float] = {}
        self.tools: list[tuple[str, float]] = []
        self.filler = ""
        self.lock = threading.Lock()

    def mark(self, step: str, t: float | None = None):
        """Only the first time of each step counts."""
        t = time.monotonic() if t is None else t
        with self.lock:
            self.marks.setdefault(step, t - self.t0)
            if step in ("filler", "playback"):
                self.marks.setdefault("audio", t - self.t0)

    def tool(self, name: str, seconds: float):
        with self.lock:
            self.tools.append((name, seconds))

    def line(self) -> str:
        parts = []
        for step, label in STEPS.items():
            if step in self.marks:
                extra = f" ({self.filler})" if step == "filler" and self.filler else ""
                parts.append(f"{label} {self.marks[step] * 1000:+.0f}{extra}")
        tools = ", ".join(f"{n} {s * 1000:.0f} ms" for n, s in self.tools)
        return (f"Timing turn {self.number} (ms after end of speech): " + ", ".join(parts)
                + (f"; tools: {tools}" if tools else ""))


class CallTiming:
    def __init__(self):
        self.turns: list[TurnTimer] = []

    def start(self, t0: float) -> TurnTimer:
        timer = TurnTimer(t0, len(self.turns) + 1)
        self.turns.append(timer)
        return timer

    def summary(self) -> str:
        """Median and maximum per step over the turns of the call, for comparing settings."""
        if not self.turns:
            return ""
        parts = []
        for step, label in STEPS.items():
            values = [t.marks[step] for t in self.turns if step in t.marks]
            if values:
                parts.append(f"{label} {statistics.median(values) * 1000:.0f}/{max(values) * 1000:.0f}"
                             + (f" ({len(values)}x)" if len(values) != len(self.turns) else ""))
        tools = [s for t in self.turns for _, s in t.tools]
        if tools:
            parts.append(f"tools {statistics.median(tools) * 1000:.0f}/{max(tools) * 1000:.0f} ({len(tools)}x)")
        return f"Timing of {len(self.turns)} turns, median/max ms after end of speech: " + ", ".join(parts)
