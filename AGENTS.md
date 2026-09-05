# Project Standards

## Architecture

- This is an asynchronous Python Discord bot built with `disnake`.
- Keep commands slash-only; do not add prefix commands.
- Keep `main.py` as the entry point, `bot.py` for bot setup, feature logic in `cogs/`,
  shared access rules in `access_control.py`, configuration in `config.py`, and
  persistence in `database.py`.
- Prefer small, focused changes and preserve existing behavior unless the request
  explicitly changes it.

## Access and Discord Scope

- Main functionality is limited to guild `1278169762140258367`.
- Guild `1522843375840268338` is authorized only for the administrator-only DM
  broadcast command.
- Enforce command access through the existing access-level system.
- Security and role-moderation logic must respect Discord role hierarchy and the
  level 6 developer exemption.
- Protection against channel deletion, role deletion, and role removal must ignore
  actions performed by this bot itself.
- Do not remove DM forwarding or the shared `INTERPOLOV & INTOLERANCE` recruitment
  system unless explicitly requested.

## Security and Configuration

- Never print, expose, or commit `.env` values or Discord/database credentials.
- Keep public placeholders in `.env.example` and runtime constants in `config.py`.
- Do not commit virtual environments, `__pycache__`, or compiled Python files.
- Preserve required Discord intents and permissions for member lookup, DM content,
  audit-log inspection, message delivery, and role management.

## Development

- Follow the existing asynchronous style and handle Discord/API failures without
  crashing unrelated bot features.
- Update `requirements.txt` whenever a Python dependency changes.
- After Python changes, compile every changed Python file; at minimum run:

```bash
python -m py_compile main.py
```
