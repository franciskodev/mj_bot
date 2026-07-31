# Agent Instructions

## Project

This is a small Discord bot built with `disnake`.

Main files:

- `main.py` - bot entry point and all current logic.
- `requirements.txt` - Python dependencies.
- `.env.example` - public environment variable example.
- `.env` - local secrets file. Never commit or print its contents.

## Bot Rules

- Keep the bot slash-command only. Do not add prefix commands.
- The bot must work only in guild `1522843375840268338`.
- The broadcast command must stay restricted to Discord administrators.
- Do not remove DM forwarding to channel `1532145285185278032` unless explicitly requested.
- Broadcast recipients are members with roles:
  - `1522843375965831271`
  - `1528511540779811046`
- The broadcast text is intentionally defined in code as `BROADCAST_TEXT`.

## Development

- Prefer small, focused changes in `main.py`.
- Keep secrets in `.env`; update `.env.example` only with placeholder values.
- Do not commit `.env`, virtual environments, `__pycache__`, or compiled Python files.
- If adding dependencies, update `requirements.txt`.
- After Python changes, run:

```powershell
python -m py_compile main.py
```

## Discord Setup Notes

The bot needs these Discord Developer Portal intents enabled:

- `SERVER MEMBERS INTENT` for role-based member lookup.
- `MESSAGE CONTENT INTENT` for reading and forwarding DM text.

The bot also needs permission to send messages in the DM forwarding channel.
