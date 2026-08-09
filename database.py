import logging
from datetime import datetime

import asyncpg
import disnake

from config import DATABASE_URL, GUILD_ID, TARGET_ROLE_IDS


class Database:
    def __init__(self) -> None:
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is missing. Add it to your .env file.")

        self.pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=10,
            command_timeout=30,
        )

        async with self.pool.acquire() as connection:
            await connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    discord_id BIGINT PRIMARY KEY,
                    username TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    discord_created_at TIMESTAMPTZ NOT NULL,
                    guild_joined_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                ALTER TABLE users ADD COLUMN IF NOT EXISTS game_nickname TEXT;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS server_name TEXT;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_status TEXT NOT NULL DEFAULT 'Активен';
                ALTER TABLE users ADD COLUMN IF NOT EXISTS bio TEXT;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS added_by BIGINT;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS last_message_at TIMESTAMPTZ;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS roles_removed_at TIMESTAMPTZ;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS inactive_reason TEXT;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS inactive_at TIMESTAMPTZ;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS last_activity_warning_at TIMESTAMPTZ;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS family_name TEXT;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS last_inactivity_notice_at TIMESTAMPTZ;
                ALTER TABLE users ADD COLUMN IF NOT EXISTS activity_check_after TIMESTAMPTZ;
                UPDATE users SET is_active=FALSE, inactive_reason=COALESCE(inactive_reason, 'Роли сняты за неактивность'),
                    inactive_at=COALESCE(inactive_at, roles_removed_at) WHERE roles_removed_at IS NOT NULL;
                ALTER TABLE users DROP COLUMN IF EXISTS kicked_at;
                ALTER TABLE users DROP COLUMN IF EXISTS promo_code;

                CREATE TABLE IF NOT EXISTS voice_sessions (
                    id BIGSERIAL PRIMARY KEY,
                    discord_id BIGINT NOT NULL REFERENCES users(discord_id) ON DELETE CASCADE,
                    channel_id BIGINT NOT NULL,
                    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    ended_at TIMESTAMPTZ,
                    duration_seconds INTEGER
                );
                CREATE INDEX IF NOT EXISTS voice_sessions_user_started_idx ON voice_sessions(discord_id, started_at DESC);
                CREATE OR REPLACE FUNCTION voice_seconds_since(
                    target_discord_id BIGINT,
                    since_at TIMESTAMPTZ
                ) RETURNS BIGINT LANGUAGE SQL STABLE AS $$
                    SELECT COALESCE(SUM(EXTRACT(EPOCH FROM(
                        UPPER(session_range)-LOWER(session_range)
                    ))), 0)::BIGINT
                    FROM UNNEST((SELECT RANGE_AGG(TSTZRANGE(
                        GREATEST(started_at, since_at),
                        LEAST(COALESCE(ended_at, NOW()), NOW()), '[)'
                    )) FROM voice_sessions
                    WHERE discord_id=target_discord_id
                      AND COALESCE(ended_at, NOW())>since_at
                      AND started_at<NOW())) AS session_range;
                $$;


                CREATE TABLE IF NOT EXISTS leave_requests (
                    id BIGSERIAL PRIMARY KEY,
                    discord_id BIGINT NOT NULL REFERENCES users(discord_id) ON DELETE CASCADE,
                    reason TEXT NOT NULL,
                    starts_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    ends_at TIMESTAMPTZ NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    reviewed_by BIGINT,
                    reviewed_at TIMESTAMPTZ,
                    log_message_id BIGINT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS meetings (
                    id BIGSERIAL PRIMARY KEY,
                    voice_channel_id BIGINT NOT NULL,
                    checked_by BIGINT NOT NULL,
                    checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS meeting_attendance (
                    meeting_id BIGINT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
                    discord_id BIGINT NOT NULL REFERENCES users(discord_id) ON DELETE CASCADE,
                    present BOOLEAN NOT NULL,
                    on_leave BOOLEAN NOT NULL,
                    PRIMARY KEY (meeting_id, discord_id)
                );

                CREATE TABLE IF NOT EXISTS activity_warnings (
                    id BIGSERIAL PRIMARY KEY,
                    discord_id BIGINT NOT NULL REFERENCES users(discord_id) ON DELETE CASCADE,
                    reason TEXT NOT NULL,
                    delivered BOOLEAN NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS activity_warnings_user_created_idx
                    ON activity_warnings(discord_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS disciplinary_warnings (
                    id BIGSERIAL PRIMARY KEY,
                    discord_id BIGINT NOT NULL REFERENCES users(discord_id) ON DELETE CASCADE,
                    issued_by BIGINT NOT NULL,
                    reason TEXT NOT NULL,
                    meeting_id BIGINT REFERENCES meetings(id) ON DELETE SET NULL,
                    revoked_by BIGINT,
                    revoked_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS disciplinary_warnings_active_user_idx
                    ON disciplinary_warnings(discord_id) WHERE revoked_at IS NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS disciplinary_warnings_meeting_user_idx
                    ON disciplinary_warnings(meeting_id, discord_id)
                    WHERE meeting_id IS NOT NULL;

                CREATE TABLE IF NOT EXISTS server_tag_checks (
                    discord_id BIGINT PRIMARY KEY REFERENCES users(discord_id) ON DELETE CASCADE,
                    checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    is_compliant BOOLEAN NOT NULL,
                    last_reminded_at TIMESTAMPTZ,
                    last_delivery_succeeded BOOLEAN
                );

                CREATE TABLE IF NOT EXISTS profile_server_reminders (
                    discord_id BIGINT PRIMARY KEY REFERENCES users(discord_id) ON DELETE CASCADE,
                    last_reminded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_delivery_succeeded BOOLEAN NOT NULL
                );

                CREATE TABLE IF NOT EXISTS application_config (
                    id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id=1),
                    panel_channel_id BIGINT NOT NULL,
                    category_id BIGINT NOT NULL,
                    reviewer_role_id BIGINT NOT NULL,
                    accepted_role_id BIGINT NOT NULL,
                    panel_message_id BIGINT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS applications (
                    id BIGSERIAL PRIMARY KEY,
                    discord_id BIGINT NOT NULL,
                    username TEXT NOT NULL,
                    game_nickname TEXT NOT NULL,
                    static_id TEXT NOT NULL,
                    ooc_age INTEGER NOT NULL,
                    goal TEXT NOT NULL,
                    screenshot_url TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    reviewer_id BIGINT,
                    ticket_channel_id BIGINT,
                    ticket_message_id BIGINT,
                    application_json_path TEXT,
                    transcript_path TEXT,
                    transcript_saved_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                ALTER TABLE applications ADD COLUMN IF NOT EXISTS application_json_path TEXT;
                ALTER TABLE applications ADD COLUMN IF NOT EXISTS transcript_path TEXT;
                ALTER TABLE applications ADD COLUMN IF NOT EXISTS transcript_saved_at TIMESTAMPTZ;
                ALTER TABLE applications ADD COLUMN IF NOT EXISTS guild_id BIGINT;
                ALTER TABLE applications ADD COLUMN IF NOT EXISTS family_name TEXT;
                ALTER TABLE applications ADD COLUMN IF NOT EXISTS game_server TEXT
                    NOT NULL DEFAULT 'Не указан';
                CREATE INDEX IF NOT EXISTS applications_user_created_idx
                    ON applications(discord_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS application_guild_config (
                    guild_id BIGINT PRIMARY KEY,
                    panel_channel_id BIGINT NOT NULL,
                    category_id BIGINT NOT NULL,
                    reviewer_role_id BIGINT NOT NULL,
                    accepted_role_id BIGINT NOT NULL,
                    panel_message_id BIGINT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                ALTER TABLE application_guild_config
                    ADD COLUMN IF NOT EXISTS interpolov_role_id BIGINT;
                ALTER TABLE application_guild_config
                    ADD COLUMN IF NOT EXISTS intolerance_role_id BIGINT;
                ALTER TABLE application_guild_config
                    ADD COLUMN IF NOT EXISTS reviewer_role_ids BIGINT[];
                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TIMESTAMPTZ NOT NULL
                );
                CREATE TABLE IF NOT EXISTS activity_check_config (
                    guild_id BIGINT PRIMARY KEY,
                    remove_roles BOOLEAN NOT NULL DEFAULT TRUE,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS family_blacklist (
                    id BIGSERIAL PRIMARY KEY,
                    discord_id BIGINT,
                    static_ids TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
                    reason TEXT NOT NULL,
                    created_by BIGINT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    removed_by BIGINT,
                    removed_at TIMESTAMPTZ,
                    channel_message_id BIGINT,
                    CHECK (discord_id IS NOT NULL OR CARDINALITY(static_ids) > 0)
                );
                CREATE INDEX IF NOT EXISTS family_blacklist_active_discord_idx
                    ON family_blacklist(discord_id) WHERE active=TRUE;
                CREATE INDEX IF NOT EXISTS family_blacklist_active_statics_idx
                    ON family_blacklist USING GIN(static_ids) WHERE active=TRUE;

                CREATE TABLE IF NOT EXISTS command_access_levels (
                    guild_id BIGINT NOT NULL,
                    command_name TEXT NOT NULL,
                    access_level SMALLINT NOT NULL CHECK(access_level BETWEEN 1 AND 6),
                    updated_by BIGINT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY(guild_id, command_name)
                );
                CREATE TABLE IF NOT EXISTS access_level_roles (
                    guild_id BIGINT NOT NULL,
                    access_level SMALLINT NOT NULL CHECK(access_level BETWEEN 1 AND 4),
                    role_id BIGINT NOT NULL,
                    added_by BIGINT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY(guild_id, access_level, role_id)
                );
                CREATE TABLE IF NOT EXISTS department_config (
                    guild_id BIGINT NOT NULL,
                    department TEXT NOT NULL CHECK(department IN ('recruiters','contractors')),
                    member_role_id BIGINT NOT NULL,
                    head_role_id BIGINT NOT NULL,
                    updated_by BIGINT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY(guild_id, department)
                );
                """
            )
            await connection.execute(
                "UPDATE applications SET guild_id=$1 WHERE guild_id IS NULL",
                GUILD_ID,
            )
            await connection.execute(
                "ALTER TABLE applications ALTER COLUMN guild_id SET NOT NULL"
            )
            await connection.execute(
                """UPDATE applications SET family_name='INTOLERANCE'
                WHERE family_name IS NULL"""
            )
            await connection.execute(
                "ALTER TABLE applications ALTER COLUMN family_name SET NOT NULL"
            )
            await connection.execute(
                """INSERT INTO application_guild_config(
                    guild_id,panel_channel_id,category_id,reviewer_role_id,
                    accepted_role_id,panel_message_id,updated_at,
                    interpolov_role_id,intolerance_role_id,reviewer_role_ids
                ) SELECT $1,panel_channel_id,category_id,
                    reviewer_role_id,accepted_role_id,panel_message_id,updated_at,
                    accepted_role_id,accepted_role_id,ARRAY[reviewer_role_id]
                FROM application_config WHERE id=1
                ON CONFLICT(guild_id) DO NOTHING""",
                GUILD_ID,
            )
            await connection.execute(
                """UPDATE application_guild_config SET
                    interpolov_role_id=COALESCE(interpolov_role_id,accepted_role_id),
                    intolerance_role_id=COALESCE(intolerance_role_id,accepted_role_id),
                    reviewer_role_ids=COALESCE(reviewer_role_ids,ARRAY[reviewer_role_id])"""
            )
            await connection.execute(
                """ALTER TABLE application_guild_config
                    ALTER COLUMN interpolov_role_id SET NOT NULL,
                    ALTER COLUMN intolerance_role_id SET NOT NULL,
                    ALTER COLUMN reviewer_role_ids SET NOT NULL"""
            )

        logging.info("Connected to PostgreSQL")

    async def close(self) -> None:
        if self.pool is None:
            return

        await self.pool.execute("""UPDATE voice_sessions SET ended_at=NOW(),
            duration_seconds=GREATEST(0,EXTRACT(EPOCH FROM(NOW()-started_at))::INTEGER)
            WHERE ended_at IS NULL""")
        await self.pool.close()
        self.pool = None
        logging.info("PostgreSQL connection pool closed")

    async def save_users(
        self,
        users: list[disnake.User | disnake.Member],
        role_ids: set[int] = TARGET_ROLE_IDS,
    ) -> None:
        if self.pool is None or not users:
            return

        family_members = [
            user for user in users
            if isinstance(user, disnake.Member)
            and (user.guild_permissions.administrator
                or any(role.id in role_ids for role in user.roles))
        ]
        values = [
            (
                user.id,
                user.name,
                user.display_name,
                user.created_at,
                user.joined_at,
            )
            for user in family_members
        ]
        if not values:
            return

        try:
            await self.pool.executemany(
                """
                INSERT INTO users (
                    discord_id,
                    username,
                    display_name,
                    discord_created_at,
                    guild_joined_at
                )
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (discord_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    display_name = EXCLUDED.display_name,
                    discord_created_at = EXCLUDED.discord_created_at,
                    guild_joined_at = COALESCE(EXCLUDED.guild_joined_at, users.guild_joined_at),
                    updated_at = NOW()
                """,
                values,
            )
        except asyncpg.PostgresError:
            logging.exception("Could not save users to PostgreSQL")

    async def get_user(self, discord_id: int) -> asyncpg.Record | None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")

        return await self.pool.fetchrow(
            "SELECT * FROM users WHERE discord_id = $1",
            discord_id,
        )

    async def set_user_family(
        self, discord_id: int, family_name: str
    ) -> asyncpg.Record | None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        if family_name not in {"INTERPOLOV", "INTOLERANCE"}:
            raise ValueError("Unknown family name")

        return await self.pool.fetchrow(
            """UPDATE users SET family_name=$2,updated_at=NOW()
            WHERE discord_id=$1 RETURNING *""",
            discord_id,
            family_name,
        )

    async def update_user_profile(
        self,
        discord_id: int,
        *,
        game_nickname: str | None = None,
        server_name: str | None = None,
        profile_status: str | None = None,
        bio: str | None = None,
        added_by: int | None = None,
        reactivate: bool = False,
    ) -> asyncpg.Record | None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")

        return await self.pool.fetchrow(
            """
            UPDATE users SET
                game_nickname = CASE
                    WHEN $2::TEXT IS NULL THEN game_nickname ELSE NULLIF($2, '-')
                END,
                server_name = CASE
                    WHEN $3::TEXT IS NULL THEN server_name ELSE NULLIF($3, '-')
                END,
                profile_status = CASE
                    WHEN $7 THEN 'Активен' ELSE COALESCE($4::TEXT, profile_status)
                END,
                bio = CASE
                    WHEN $5::TEXT IS NULL THEN bio ELSE NULLIF($5, '-')
                END,
                added_by = COALESCE($6::BIGINT, added_by),
                last_message_at = CASE WHEN $7 THEN NOW() ELSE COALESCE(last_message_at, NOW()) END,
                last_activity_warning_at = CASE WHEN $7 THEN NOW() ELSE last_activity_warning_at END,
                roles_removed_at = CASE WHEN $7 THEN NULL ELSE roles_removed_at END,
                is_active = CASE WHEN $7 THEN TRUE ELSE is_active END,
                inactive_reason = CASE WHEN $7 THEN NULL ELSE inactive_reason END,
                inactive_at = CASE WHEN $7 THEN NULL ELSE inactive_at END,
                activity_check_after = CASE
                    WHEN $7 AND is_active=FALSE THEN NOW()+INTERVAL '7 days'
                    ELSE activity_check_after
                END,
                updated_at = NOW()
            WHERE discord_id = $1
            RETURNING *
            """,
            discord_id,
            game_nickname,
            server_name,
            profile_status,
            bio,
            added_by,
            reactivate,
        )

    async def deactivate_user(
        self, discord_id: int, reason: str, *, roles_removed: bool = False
    ) -> bool:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        was_active = await self.pool.fetchval(
            "SELECT is_active FROM users WHERE discord_id=$1", discord_id
        )
        if was_active is None:
            return False
        await self.pool.execute(
            """UPDATE users SET is_active=FALSE,profile_status='Неактивен',
            inactive_reason=$2,inactive_at=NOW(),updated_at=NOW(),
            roles_removed_at=CASE WHEN $3 THEN NOW() ELSE roles_removed_at END
            WHERE discord_id=$1""",
            discord_id, reason, roles_removed,
        )
        return bool(was_active)

    async def record_message_activity(self, discord_id: int) -> None:
        if self.pool is not None:
            await self.pool.execute("UPDATE users SET last_message_at=NOW(),updated_at=NOW() WHERE discord_id=$1 AND is_active=TRUE", discord_id)

    async def start_voice_session(self, discord_id: int, channel_id: int) -> int | None:
        if self.pool is None:
            return None
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute("SELECT pg_advisory_xact_lock($1)", discord_id)
                session_id = await connection.fetchval(
                    """SELECT id FROM voice_sessions
                    WHERE discord_id=$1 AND ended_at IS NULL
                    ORDER BY started_at DESC LIMIT 1""", discord_id,
                )
                if session_id is not None:
                    return session_id
                return await connection.fetchval(
                    """INSERT INTO voice_sessions(discord_id,channel_id)
                    SELECT $1,$2 WHERE EXISTS(SELECT 1 FROM users
                    WHERE discord_id=$1 AND is_active=TRUE) RETURNING id""",
                    discord_id, channel_id,
                )
    async def end_voice_session(self, session_id: int) -> None:
        if self.pool is not None:
            await self.pool.execute(
                """UPDATE voice_sessions SET ended_at=NOW(),
                duration_seconds=GREATEST(0,EXTRACT(EPOCH FROM(NOW()-started_at))::INTEGER)
                WHERE id=$1 AND ended_at IS NULL""", session_id,
            )

    async def get_weekly_voice_seconds(self, discord_id: int) -> int:
        if self.pool is None:
            return 0
        value = await self.pool.fetchval(
            "SELECT voice_seconds_since($1, NOW()-INTERVAL '7 days')", discord_id,
        )
        return int(value)

    async def get_total_voice_seconds(self, discord_id: int) -> int:
        if self.pool is None:
            return 0
        value = await self.pool.fetchval(
            "SELECT voice_seconds_since($1, '-infinity'::TIMESTAMPTZ)", discord_id,
        )
        return int(value)

    async def create_leave_request(self, discord_id: int, reason: str, days: int) -> asyncpg.Record:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        return await self.pool.fetchrow(
            """INSERT INTO leave_requests(discord_id,reason,ends_at)
            VALUES($1,$2,NOW()+($3*INTERVAL '1 day')) RETURNING *""", discord_id, reason, days,
        )

    async def set_leave_log_message(self, request_id: int, message_id: int) -> None:
        if self.pool is not None:
            await self.pool.execute("UPDATE leave_requests SET log_message_id=$2 WHERE id=$1", request_id, message_id)

    async def review_leave(self, request_id: int, status: str, reviewer_id: int) -> asyncpg.Record | None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                request = await connection.fetchrow(
                    """UPDATE leave_requests SET status=$2,reviewed_by=$3,reviewed_at=NOW()
                    WHERE id=$1 AND status='pending' RETURNING *""", request_id, status, reviewer_id,
                )
                if request and status == "approved":
                    await connection.execute("UPDATE users SET profile_status='В отпуске',updated_at=NOW() WHERE discord_id=$1", request["discord_id"])
                return request

    async def get_inactive_users(
        self, *, notification_only: bool = False
    ) -> list[asyncpg.Record]:
        if self.pool is None:
            return []
        await self.pool.execute("""UPDATE users u SET profile_status='Активен',updated_at=NOW()
        WHERE profile_status='В отпуске' AND NOT EXISTS(SELECT 1 FROM leave_requests l
        WHERE l.discord_id=u.discord_id AND l.status='approved' AND l.ends_at>NOW())""")
        return await self.pool.fetch(
            """SELECT u.*, voice_seconds_since(
                u.discord_id, NOW()-INTERVAL '7 days'
            )::INTEGER AS weekly_voice_seconds
            FROM users u WHERE COALESCE(u.guild_joined_at,u.created_at)<=NOW()-INTERVAL '7 days'
            AND u.is_active=TRUE AND NOT EXISTS(SELECT 1 FROM leave_requests l
            WHERE l.discord_id=u.discord_id AND l.status='approved' AND l.ends_at>NOW())
            AND (u.activity_check_after IS NULL OR u.activity_check_after<=NOW())
            AND (NOT $1::BOOLEAN OR u.last_inactivity_notice_at IS NULL
                OR u.last_inactivity_notice_at<NOW()-INTERVAL '24 hours')
            AND (COALESCE(u.last_message_at,u.guild_joined_at,u.created_at)
                <NOW()-INTERVAL '7 days' OR voice_seconds_since(
                    u.discord_id, NOW()-INTERVAL '7 days'
                )<3600)""",
            notification_only,
        )

    async def get_activity_remove_roles(self, guild_id: int) -> bool:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        return bool(await self.pool.fetchval(
            """INSERT INTO activity_check_config(guild_id) VALUES($1)
            ON CONFLICT(guild_id) DO UPDATE SET guild_id=EXCLUDED.guild_id
            RETURNING remove_roles""",
            guild_id,
        ))

    async def set_activity_remove_roles(self, guild_id: int, enabled: bool) -> None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        await self.pool.execute(
            """INSERT INTO activity_check_config(guild_id,remove_roles)
            VALUES($1,$2) ON CONFLICT(guild_id) DO UPDATE SET
            remove_roles=EXCLUDED.remove_roles,updated_at=NOW()""",
            guild_id,
            enabled,
        )

    async def record_inactivity_notice(self, discord_id: int) -> None:
        if self.pool is not None:
            await self.pool.execute(
                """UPDATE users SET last_inactivity_notice_at=NOW(),updated_at=NOW()
                WHERE discord_id=$1""",
                discord_id,
            )

    async def has_pending_leave(self, discord_id: int) -> bool:
        if self.pool is None:
            return False
        return bool(await self.pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM leave_requests WHERE discord_id=$1 AND status='pending')",
            discord_id,
        ))

    async def get_active_leave_user_ids(self) -> set[int]:
        if self.pool is None:
            return set()
        rows = await self.pool.fetch(
            """SELECT DISTINCT discord_id FROM leave_requests
            WHERE status='approved' AND ends_at>NOW()"""
        )
        return {row["discord_id"] for row in rows}

    async def get_activity_enforcement_started_at(self, guild_id: int) -> datetime:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        return await self.pool.fetchval(
            """INSERT INTO bot_settings(key,value) VALUES($1,NOW())
            ON CONFLICT(key) DO UPDATE SET value=bot_settings.value RETURNING value""",
            f"activity_enforcement_started_at:{guild_id}",
        )

    async def record_meeting_attendance(
        self,
        voice_channel_id: int,
        checked_by: int,
        present_ids: list[int],
        family_ids: list[int],
    ) -> tuple[int, list[asyncpg.Record]]:
        if self.pool is None:
            raise RuntimeError("Database is not connected")

        async with self.pool.acquire() as connection:
            async with connection.transaction():
                meeting_id = await connection.fetchval(
                    """INSERT INTO meetings(voice_channel_id,checked_by)
                    VALUES($1,$2) RETURNING id""",
                    voice_channel_id,
                    checked_by,
                )
                rows = await connection.fetch(
                    """SELECT u.discord_id,u.username,u.display_name,u.game_nickname,u.server_name,
                    u.discord_id=ANY($1::BIGINT[]) AS present,
                    EXISTS(SELECT 1 FROM leave_requests l WHERE l.discord_id=u.discord_id
                        AND l.status='approved' AND l.ends_at>NOW()) AS on_leave,
                    (SELECT MAX(l.ends_at) FROM leave_requests l WHERE l.discord_id=u.discord_id
                        AND l.status='approved' AND l.ends_at>NOW()) AS leave_ends_at
                    FROM users u WHERE u.is_active=TRUE
                    AND u.discord_id=ANY($2::BIGINT[]) ORDER BY u.display_name""",
                    present_ids,
                    family_ids,
                )
                await connection.executemany(
                    """INSERT INTO meeting_attendance(meeting_id,discord_id,present,on_leave)
                    VALUES($1,$2,$3,$4)""",
                    [(meeting_id, row["discord_id"], row["present"], row["on_leave"]) for row in rows],
                )
                return meeting_id, list(rows)

    async def issue_disciplinary_warning(
        self,
        discord_id: int,
        issued_by: int,
        reason: str,
        meeting_id: int | None = None,
    ) -> tuple[asyncpg.Record | None, int, str | None]:
        """Issue one warning and return (row, active count, refusal reason)."""
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute("SELECT pg_advisory_xact_lock($1)", discord_id)
                if meeting_id is not None and await connection.fetchval(
                    """SELECT EXISTS(SELECT 1 FROM disciplinary_warnings
                    WHERE discord_id=$1 AND meeting_id=$2)""",
                    discord_id,
                    meeting_id,
                ):
                    count = await connection.fetchval(
                        """SELECT COUNT(*) FROM disciplinary_warnings
                        WHERE discord_id=$1 AND revoked_at IS NULL""",
                        discord_id,
                    )
                    return None, int(count), "already_issued"
                count = int(await connection.fetchval(
                    """SELECT COUNT(*) FROM disciplinary_warnings
                    WHERE discord_id=$1 AND revoked_at IS NULL""",
                    discord_id,
                ))
                if count >= 3:
                    return None, count, "limit_reached"
                row = await connection.fetchrow(
                    """INSERT INTO disciplinary_warnings(
                        discord_id,issued_by,reason,meeting_id
                    ) VALUES($1,$2,$3,$4) RETURNING *""",
                    discord_id,
                    issued_by,
                    reason,
                    meeting_id,
                )
                return row, count + 1, None

    async def get_disciplinary_warnings(self, discord_id: int) -> list[asyncpg.Record]:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        return list(await self.pool.fetch(
            """SELECT * FROM disciplinary_warnings WHERE discord_id=$1
            ORDER BY (revoked_at IS NULL) DESC, created_at DESC""",
            discord_id,
        ))

    async def get_active_disciplinary_warning_count(self, discord_id: int) -> int:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        count = await self.pool.fetchval(
            """SELECT COUNT(*) FROM disciplinary_warnings
            WHERE discord_id=$1 AND revoked_at IS NULL""",
            discord_id,
        )
        return int(count)

    async def get_due_server_tag_check_user_ids(
        self, discord_ids: list[int]
    ) -> list[int]:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        if not discord_ids:
            return []
        rows = await self.pool.fetch(
            """SELECT candidate.discord_id
            FROM UNNEST($1::BIGINT[]) AS candidate(discord_id)
            LEFT JOIN server_tag_checks check_result
                ON check_result.discord_id=candidate.discord_id
            WHERE check_result.checked_at IS NULL
               OR check_result.checked_at<=NOW()-INTERVAL '3 days'
            ORDER BY candidate.discord_id""",
            discord_ids,
        )
        return [row["discord_id"] for row in rows]

    async def record_server_tag_check(
        self,
        discord_id: int,
        *,
        is_compliant: bool,
        reminded: bool,
        delivered: bool | None,
    ) -> None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        await self.pool.execute(
            """INSERT INTO server_tag_checks(
                discord_id,checked_at,is_compliant,last_reminded_at,
                last_delivery_succeeded
            ) VALUES($1,NOW(),$2,CASE WHEN $3 THEN NOW() END,$4)
            ON CONFLICT(discord_id) DO UPDATE SET
                checked_at=NOW(),
                is_compliant=EXCLUDED.is_compliant,
                last_reminded_at=CASE WHEN $3 THEN NOW()
                    ELSE server_tag_checks.last_reminded_at END,
                last_delivery_succeeded=CASE WHEN $3 THEN $4
                    ELSE server_tag_checks.last_delivery_succeeded END""",
            discord_id,
            is_compliant,
            reminded,
            delivered,
        )

    async def is_server_tag_reminder_due(self, discord_id: int) -> bool:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        return bool(await self.pool.fetchval(
            """SELECT COALESCE((SELECT last_reminded_at IS NULL
                OR last_reminded_at<=NOW()-INTERVAL '3 days'
                FROM server_tag_checks WHERE discord_id=$1), TRUE)""",
            discord_id,
        ))

    async def is_profile_server_reminder_due(self, discord_id: int) -> bool:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        return bool(await self.pool.fetchval(
            """SELECT COALESCE((SELECT last_reminded_at<=NOW()-INTERVAL '12 hours'
                FROM profile_server_reminders WHERE discord_id=$1), TRUE)""",
            discord_id,
        ))

    async def record_profile_server_reminder(
        self, discord_id: int, delivered: bool
    ) -> None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        await self.pool.execute(
            """INSERT INTO profile_server_reminders(
                discord_id,last_reminded_at,last_delivery_succeeded
            ) VALUES($1,NOW(),$2)
            ON CONFLICT(discord_id) DO UPDATE SET
                last_reminded_at=NOW(),
                last_delivery_succeeded=EXCLUDED.last_delivery_succeeded""",
            discord_id,
            delivered,
        )

    async def revoke_disciplinary_warning(
        self, warning_id: int, discord_id: int, revoked_by: int
    ) -> tuple[asyncpg.Record | None, int]:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """UPDATE disciplinary_warnings SET revoked_by=$3,revoked_at=NOW()
                    WHERE id=$1 AND discord_id=$2 AND revoked_at IS NULL RETURNING *""",
                    warning_id,
                    discord_id,
                    revoked_by,
                )
                count = await connection.fetchval(
                    """SELECT COUNT(*) FROM disciplinary_warnings
                    WHERE discord_id=$1 AND revoked_at IS NULL""",
                    discord_id,
                )
                return row, int(count)

    async def get_meeting_absentees(self, meeting_id: int) -> list[int]:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        rows = await self.pool.fetch(
            """SELECT discord_id FROM meeting_attendance
            WHERE meeting_id=$1 AND present=FALSE AND on_leave=FALSE
            ORDER BY discord_id""",
            meeting_id,
        )
        return [row["discord_id"] for row in rows]

    async def get_daily_warning_started_at(self, guild_id: int) -> datetime:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        return await self.pool.fetchval(
            """INSERT INTO bot_settings(key,value) VALUES($1,NOW())
            ON CONFLICT(key) DO UPDATE SET value=bot_settings.value RETURNING value""",
            f"daily_warning_started_at:{guild_id}",
        )

    async def get_daily_warning_users(self) -> list[asyncpg.Record]:
        if self.pool is None:
            return []
        return await self.pool.fetch(
            """SELECT u.*, voice_seconds_since(
                u.discord_id, NOW()-INTERVAL '24 hours'
            )::INTEGER AS daily_voice_seconds
            FROM users u WHERE u.is_active=TRUE
            AND NOT EXISTS(SELECT 1 FROM leave_requests l WHERE l.discord_id=u.discord_id
                AND l.status='approved' AND l.ends_at>NOW())
            AND COALESCE(u.guild_joined_at,u.created_at)<=NOW()-INTERVAL '24 hours'
            AND (u.last_activity_warning_at IS NULL
                OR u.last_activity_warning_at<=NOW()-INTERVAL '24 hours')
            AND (COALESCE(u.last_message_at,u.guild_joined_at,u.created_at)
                <NOW()-INTERVAL '24 hours' OR voice_seconds_since(
                    u.discord_id, NOW()-INTERVAL '24 hours'
                )<1800)"""
        )

    async def record_activity_warning(
        self,
        discord_id: int,
        reason: str,
        delivered: bool,
    ) -> None:
        if self.pool is None:
            return
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """INSERT INTO activity_warnings(discord_id,reason,delivered)
                    VALUES($1,$2,$3)""",
                    discord_id, reason, delivered,
                )
                await connection.execute(
                    "UPDATE users SET last_activity_warning_at=NOW() WHERE discord_id=$1",
                    discord_id,
                )

    async def save_application_config(
        self,
        guild_id: int,
        panel_channel_id: int,
        category_id: int,
        reviewer_role_ids: list[int],
        interpolov_role_id: int,
        intolerance_role_id: int,
    ) -> None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        await self.pool.execute(
            """INSERT INTO application_guild_config(
                guild_id,panel_channel_id,category_id,reviewer_role_id,reviewer_role_ids,
                accepted_role_id,interpolov_role_id,intolerance_role_id
            ) VALUES($1,$2,$3,($4::BIGINT[])[1],$4,$6,$5,$6) ON CONFLICT(guild_id) DO UPDATE SET
                panel_channel_id=EXCLUDED.panel_channel_id,
                category_id=EXCLUDED.category_id,
                reviewer_role_id=EXCLUDED.reviewer_role_id,
                reviewer_role_ids=EXCLUDED.reviewer_role_ids,
                accepted_role_id=EXCLUDED.accepted_role_id,
                interpolov_role_id=EXCLUDED.interpolov_role_id,
                intolerance_role_id=EXCLUDED.intolerance_role_id,
                updated_at=NOW()""",
            guild_id, panel_channel_id, category_id, reviewer_role_ids,
            interpolov_role_id, intolerance_role_id,
        )

    async def set_application_panel_message(self, guild_id: int, message_id: int) -> None:
        if self.pool is not None:
            await self.pool.execute(
                "UPDATE application_guild_config SET panel_message_id=$2,updated_at=NOW() WHERE guild_id=$1",
                guild_id, message_id,
            )

    async def get_application_config(self, guild_id: int) -> asyncpg.Record | None:
        if self.pool is None:
            return None
        return await self.pool.fetchrow(
            "SELECT * FROM application_guild_config WHERE guild_id=$1", guild_id
        )

    async def has_open_application(self, discord_id: int, guild_id: int) -> bool:
        if self.pool is None:
            return False
        return bool(await self.pool.fetchval(
            """SELECT EXISTS(SELECT 1 FROM applications WHERE discord_id=$1
            AND guild_id=$2 AND status IN('pending','review','interview'))""",
            discord_id, guild_id,
        ))

    async def create_application(
        self,
        guild_id: int,
        family_name: str,
        game_server: str,
        discord_id: int,
        username: str,
        game_nickname: str,
        static_id: str,
        ooc_age: int,
        goal: str,
        screenshot_url: str,
    ) -> asyncpg.Record:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        return await self.pool.fetchrow(
            """INSERT INTO applications(
                guild_id,family_name,game_server,discord_id,username,game_nickname,static_id,ooc_age,goal,screenshot_url
            ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING *""",
            guild_id, family_name, game_server, discord_id, username,
            game_nickname, static_id, ooc_age, goal, screenshot_url,
        )

    async def set_application_ticket(
        self, application_id: int, channel_id: int, message_id: int
    ) -> None:
        if self.pool is not None:
            await self.pool.execute(
                """UPDATE applications SET ticket_channel_id=$2,ticket_message_id=$3,
                updated_at=NOW() WHERE id=$1""",
                application_id, channel_id, message_id,
            )

    async def get_previous_applications(self, discord_id: int, exclude_id: int) -> list[asyncpg.Record]:
        if self.pool is None:
            return []
        return await self.pool.fetch(
            """SELECT * FROM applications WHERE discord_id=$1 AND id<>$2
            ORDER BY created_at DESC LIMIT 5""",
            discord_id, exclude_id,
        )

    async def review_application(
        self, application_id: int, status: str, reviewer_id: int
    ) -> asyncpg.Record | None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        return await self.pool.fetchrow(
            """UPDATE applications SET status=$2,reviewer_id=$3,updated_at=NOW()
            WHERE id=$1 AND status NOT IN('accepted','declined') RETURNING *""",
            application_id, status, reviewer_id,
        )

    async def get_application(self, application_id: int) -> asyncpg.Record | None:
        if self.pool is None:
            return None
        return await self.pool.fetchrow("SELECT * FROM applications WHERE id=$1", application_id)

    async def set_application_json_path(self, application_id: int, path: str) -> None:
        if self.pool is not None:
            await self.pool.execute(
                "UPDATE applications SET application_json_path=$2,updated_at=NOW() WHERE id=$1",
                application_id, path,
            )

    async def set_application_transcript(self, application_id: int, path: str) -> None:
        if self.pool is not None:
            await self.pool.execute(
                """UPDATE applications SET transcript_path=$2,transcript_saved_at=NOW(),
                updated_at=NOW() WHERE id=$1""",
                application_id, path,
            )

    async def get_active_users(self) -> list[asyncpg.Record]:
        if self.pool is None:
            return []
        return await self.pool.fetch("SELECT * FROM users WHERE is_active=TRUE")

    async def add_blacklist_entry(
        self,
        discord_id: int | None,
        static_ids: list[str],
        reason: str,
        created_by: int,
    ) -> asyncpg.Record:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        return await self.pool.fetchrow(
            """INSERT INTO family_blacklist(discord_id,static_ids,reason,created_by)
            VALUES($1,$2,$3,$4) RETURNING *""",
            discord_id, static_ids, reason, created_by,
        )

    async def set_blacklist_message(self, entry_id: int, message_id: int) -> None:
        if self.pool is not None:
            await self.pool.execute(
                "UPDATE family_blacklist SET channel_message_id=$2 WHERE id=$1",
                entry_id, message_id,
            )

    async def get_blacklist_entry(self, entry_id: int) -> asyncpg.Record | None:
        if self.pool is None:
            return None
        return await self.pool.fetchrow(
            "SELECT * FROM family_blacklist WHERE id=$1", entry_id
        )

    async def find_blacklist_entries(
        self, discord_id: int, static_ids: list[str]
    ) -> list[asyncpg.Record]:
        if self.pool is None:
            return []
        return await self.pool.fetch(
            """SELECT * FROM family_blacklist WHERE active=TRUE AND
            (discord_id=$1 OR static_ids && $2::TEXT[])
            ORDER BY created_at DESC""",
            discord_id, static_ids,
        )

    async def remove_blacklist_entry(
        self, entry_id: int, removed_by: int
    ) -> asyncpg.Record | None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        return await self.pool.fetchrow(
            """UPDATE family_blacklist SET active=FALSE,removed_by=$2,removed_at=NOW()
            WHERE id=$1 AND active=TRUE RETURNING *""",
            entry_id, removed_by,
        )

    async def get_command_access_level(
        self, guild_id: int, command_name: str
    ) -> int | None:
        if self.pool is None:
            return None
        return await self.pool.fetchval(
            """SELECT access_level FROM command_access_levels
            WHERE guild_id=$1 AND command_name=$2""",
            guild_id, command_name,
        )

    async def set_command_access_level(
        self, guild_id: int, command_name: str, level: int, updated_by: int
    ) -> None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        await self.pool.execute(
            """INSERT INTO command_access_levels(
                guild_id,command_name,access_level,updated_by
            ) VALUES($1,$2,$3,$4) ON CONFLICT(guild_id,command_name) DO UPDATE SET
                access_level=EXCLUDED.access_level,updated_by=EXCLUDED.updated_by,
                updated_at=NOW()""",
            guild_id, command_name, level, updated_by,
        )

    async def add_access_role(
        self, guild_id: int, level: int, role_id: int, added_by: int
    ) -> None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        await self.pool.execute(
            """INSERT INTO access_level_roles(guild_id,access_level,role_id,added_by)
            VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING""",
            guild_id, level, role_id, added_by,
        )

    async def remove_access_role(
        self, guild_id: int, level: int, role_id: int
    ) -> bool:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        result = await self.pool.execute(
            """DELETE FROM access_level_roles
            WHERE guild_id=$1 AND access_level=$2 AND role_id=$3""",
            guild_id, level, role_id,
        )
        return result != "DELETE 0"

    async def get_access_roles(self, guild_id: int) -> list[asyncpg.Record]:
        if self.pool is None:
            return []
        return await self.pool.fetch(
            """SELECT * FROM access_level_roles WHERE guild_id=$1
            ORDER BY access_level,role_id""",
            guild_id,
        )

    async def get_highest_role_access_level(
        self, guild_id: int, role_ids: set[int]
    ) -> int:
        if self.pool is None or not role_ids:
            return 0
        return int(await self.pool.fetchval(
            """SELECT COALESCE(MAX(access_level),0) FROM access_level_roles
            WHERE guild_id=$1 AND role_id=ANY($2::BIGINT[])""",
            guild_id, list(role_ids),
        ))

    async def set_department_config(
        self,
        guild_id: int,
        department: str,
        member_role_id: int,
        head_role_id: int,
        updated_by: int,
    ) -> None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        await self.pool.execute(
            """INSERT INTO department_config(
                guild_id,department,member_role_id,head_role_id,updated_by
            ) VALUES($1,$2,$3,$4,$5) ON CONFLICT(guild_id,department) DO UPDATE SET
                member_role_id=EXCLUDED.member_role_id,
                head_role_id=EXCLUDED.head_role_id,
                updated_by=EXCLUDED.updated_by,updated_at=NOW()""",
            guild_id, department, member_role_id, head_role_id, updated_by,
        )

    async def get_department_config(
        self, guild_id: int, department: str
    ) -> asyncpg.Record | None:
        if self.pool is None:
            return None
        return await self.pool.fetchrow(
            """SELECT * FROM department_config
            WHERE guild_id=$1 AND department=$2""",
            guild_id, department,
        )

    async def get_department_configs(self, guild_id: int) -> list[asyncpg.Record]:
        if self.pool is None:
            return []
        return await self.pool.fetch(
            "SELECT * FROM department_config WHERE guild_id=$1 ORDER BY department",
            guild_id,
        )
