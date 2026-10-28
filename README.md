# Claude Telefon

Claude ruft dich über die FRITZ!Box an und spricht mit dir. Du kannst ihn auch anrufen.
Läuft unter Windows (Python 3.12) oder auf Linux/Raspberry Pi. Codec: G.711 über pyVoIP.

```
Auslöser (HA-Automation, Skript, call.py)
   │ HTTP POST /call {"message": "..."}
   ▼
app/main.py ── SIP/RTP ──► FRITZ!Box ──► dein Handy
   │
   ├─ Sprach-Erkennung (VAD) ─► OpenAI STT (gpt-4o-transcribe)
   ├─ Claude (Streaming, Tools: HA lesen, auflegen)
   └─ OpenAI TTS (gpt-4o-mini-tts) ─► Telefon
```

## 1. FRITZ!Box

Telefonie → Telefoniegeräte → dein IP-Telefon (bzw. „Neues Gerät einrichten“ → Telefon → LAN/WLAN):

- **Benutzername / Kennwort** notieren → `sip_user` / `sip_password`
- **Ausgehende Rufnummer** zuweisen, am besten eine eigene MSN, damit du auf dem Handy siehst, dass der Bot anruft
- **Ankommende Rufnummern:** nur diese MSN, wenn du den Bot auch anrufen willst
- „Anmeldung aus dem Internet erlauben“ bleibt **aus**

## 2. Einrichten (Windows)

1. `config.example.json` → `config.json` kopieren und ausfüllen. Pflicht sind `sip_user`, `sip_password`, `anthropic_api_key`, `openai_api_key` und `default_number`.
   Die Keys kannst du statt in die Datei auch als Umgebungsvariablen setzen (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `HA_TOKEN`) – der Ordner liegt in OneDrive.
2. PowerShell **als Administrator**: `powershell -ExecutionPolicy Bypass -File firewall.ps1`
   (UDP 5062 + 30000–30100, TCP 8099, nur Profil „Privat“. Dein WLAN muss als privates Netz eingestuft sein.)
3. `start.bat` ausführen. Beim ersten Start wird das venv unter `%LOCALAPPDATA%\claude-telefon\venv` angelegt.
   Im Log muss stehen: `SIP-Status: REGISTERED`.
4. Testanruf, in zweitem Fenster:
   ```
   %LOCALAPPDATA%\claude-telefon\venv\Scripts\python app\call.py "Das ist ein Testanruf" **611
   ```
   `**611` ist das erste DECT-Telefon der FRITZ!Box (intern, kostenlos). Ohne Nummer wird `default_number` angerufen.

## 3. Home Assistant

**Lesender Zugriff** (damit Claude am Telefon Werte nachschauen kann): in HA unter Profil → Sicherheit ein
Langzeit-Token erzeugen → `ha_token`, dazu `ha_url`. Bei eigenem Zertifikat `ha_verify_ssl` auf den Pfad
zur CA-Datei setzen (oder `false`).

**Anruf aus HA auslösen:** `api_token` in config.json setzen (dann lauscht der Trigger im LAN), in HA:

```yaml
# secrets.yaml
claude_telefon_auth: "Bearer DEIN_API_TOKEN"

# configuration.yaml
rest_command:
  claude_anrufen:
    url: "http://jan8:8099/call"      # Rechnername oder IP des Laptops
    method: post
    headers:
      Authorization: !secret claude_telefon_auth
    content_type: "application/json"
    payload: '{"message": {{ message | tojson }}}'
```

```yaml
# Beispiel-Automation
automation:
  - alias: "Garage offen → Claude ruft an"
    trigger:
      - platform: state
        entity_id: cover.garageleft
        to: "open"
        for: "00:30:00"
    action:
      - service: rest_command.claude_anrufen
        data:
          message: "Das linke Garagentor steht seit 30 Minuten offen."
```

Nach jedem Anruf feuert der Agent das HA-Event `claude_telefon_anruf`
(`angenommen`, `dauer_s`, `transkript`). Damit kannst du z. B. bei `angenommen: false` eine Push-Nachricht nachschieben.

## 4. Optionen (Auszug)

| Option | Bedeutung |
|---|---|
| `inbound_allowed` | Nummern, von denen der Bot Anrufe annimmt. Leer = alle ablehnen |
| `extra_prompt` | Zusatzwissen/Regeln für Claude, z. B. wichtige Entity-IDs |
| `claude_model` | Standard `claude-haiku-5-5` (schnell, günstig); Sonnet ist klüger, aber langsamer |
| `tts_voice` | OpenAI-Stimme, z. B. `onyx`, `nova`, `alloy` |
| `vad_min_energy` | Schwelle der Spracherkennung; höher, wenn Hintergrundgeräusche als Sprache zählen |
| `end_silence_ms` | Pause, nach der deine Äußerung als beendet gilt |
| `barge_in` | Du kannst Claude ins Wort fallen |

## Grenzen

- **Antwortzeit** pro Wechsel ca. 1,5–3 s (STT nach Satzende, dann Claude, dann TTS). Kein Echtzeit-Duplex.
- **Mailbox:** Geht die Handy-Mailbox ran, erkennt Claude das am Ansagetext und spricht eine kurze Nachricht drauf.
- **Klingeln abbrechen** kann pyVoIP nicht. Nimmst du nicht ab, klingelt es, bis die Gegenseite aufgibt.
- Der Rechner muss **an, wach und im Heimnetz** sein (Energieoptionen: Ruhezustand am Netzteil aus).
  Für Dauerbetrieb ist ein Raspberry Pi o. Ä. die bessere Wahl; der Code läuft dort unverändert (`python3 app/main.py`).
- Der Bot am Telefon ist eine eigene Claude-Instanz über die API. Er kennt diesen Chat und dein Claude-Gedächtnis nicht, nur was im Prompt, im Anlass und in HA steht.
- Kosten laufen über deine API-Konten (Anthropic + OpenAI), getrennt vom Claude-Abo.
