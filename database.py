import logging
from datetime import datetime

import asyncpg
import disnake

from config import DATABASE_URL, TARGET_ROLE_IDS


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
                CREATE INDEX IF NOT EXISTS applications_user_created_idx
                    ON applications(discord_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TIMESTAMPTZ NOT NULL
                );
                """
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

    async def save_users(self, users: list[disnake.User | disnake.Member]) -> None:
        if self.pool is None or not users:
            return

        family_members = [
            user for user in users
            if isinstance(user, disnake.Member)
            and any(role.id in TARGET_ROLE_IDS for role in user.roles)
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
                profile_status = COALESCE($4::TEXT, profile_status),
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
        return await self.pool.fetchval(
            """INSERT INTO voice_sessions(discord_id,channel_id)
            SELECT $1,$2 WHERE EXISTS(SELECT 1 FROM users WHERE discord_id=$1 AND is_active=TRUE) RETURNING id""",
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
            """SELECT COALESCE(SUM(EXTRACT(EPOCH FROM(LEAST(COALESCE(ended_at,NOW()),NOW())
            -GREATEST(started_at,NOW()-INTERVAL '7 days')))),0) FROM voice_sessions
            WHERE discord_id=$1 AND COALESCE(ended_at,NOW())>NOW()-INTERVAL '7 days'""", discord_id,
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

    async def get_inactive_users(self) -> list[asyncpg.Record]:
        if self.pool is None:
            return []
        await self.pool.execute("""UPDATE users u SET profile_status='Активен',updated_at=NOW()
        WHERE profile_status='В отпуске' AND NOT EXISTS(SELECT 1 FROM leave_requests l
        WHERE l.discord_id=u.discord_id AND l.status='approved' AND l.ends_at>NOW())""")
        return await self.pool.fetch(
            """SELECT u.*,COALESCE((SELECT SUM(EXTRACT(EPOCH FROM(LEAST(COALESCE(v.ended_at,NOW()),NOW())
            -GREATEST(v.started_at,NOW()-INTERVAL '7 days')))) FROM voice_sessions v WHERE v.discord_id=u.discord_id
            AND COALESCE(v.ended_at,NOW())>NOW()-INTERVAL '7 days'),0)::INTEGER AS weekly_voice_seconds
            FROM users u WHERE COALESCE(u.guild_joined_at,u.created_at)<=NOW()-INTERVAL '7 days'
            AND u.is_active=TRUE AND NOT EXISTS(SELECT 1 FROM leave_requests l WHERE l.discord_id=u.discord_id
            AND l.status='approved' AND l.ends_at>NOW()) AND(
            COALESCE(u.last_message_at,u.guild_joined_at,u.created_at)<NOW()-INTERVAL '7 days' OR
            COALESCE((SELECT SUM(EXTRACT(EPOCH FROM(LEAST(COALESCE(v.ended_at,NOW()),NOW())
            -GREATEST(v.started_at,NOW()-INTERVAL '7 days')))) FROM voice_sessions v WHERE v.discord_id=u.discord_id
            AND COALESCE(v.ended_at,NOW())>NOW()-INTERVAL '7 days'),0)<3600)"""
        )

    async def has_pending_leave(self, discord_id: int) -> bool:
        if self.pool is None:
            return False
        return bool(await self.pool.fetchval(
            "SELECT EXISTS(SELECT 1 FROM leave_requests WHERE discord_id=$1 AND status='pending')",
            discord_id,
        ))

    async def get_activity_enforcement_started_at(self) -> datetime:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        return await self.pool.fetchval(
            """INSERT INTO bot_settings(key,value) VALUES('activity_enforcement_started_at',NOW())
            ON CONFLICT(key) DO UPDATE SET value=bot_settings.value RETURNING value"""
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

    async def get_daily_warning_started_at(self) -> datetime:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        return await self.pool.fetchval(
            """INSERT INTO bot_settings(key,value) VALUES('daily_warning_started_at',NOW())
            ON CONFLICT(key) DO UPDATE SET value=bot_settings.value RETURNING value"""
        )

    async def get_daily_warning_users(self) -> list[asyncpg.Record]:
        if self.pool is None:
            return []
        return await self.pool.fetch(
            """SELECT u.*,COALESCE((SELECT SUM(EXTRACT(EPOCH FROM(
            LEAST(COALESCE(v.ended_at,NOW()),NOW())
            -GREATEST(v.started_at,NOW()-INTERVAL '24 hours'))))
            FROM voice_sessions v WHERE v.discord_id=u.discord_id
            AND COALESCE(v.ended_at,NOW())>NOW()-INTERVAL '24 hours'),0)::INTEGER
            AS daily_voice_seconds FROM users u WHERE u.is_active=TRUE
            AND NOT EXISTS(SELECT 1 FROM leave_requests l WHERE l.discord_id=u.discord_id
                AND l.status='approved' AND l.ends_at>NOW())
            AND COALESCE(u.guild_joined_at,u.created_at)<=NOW()-INTERVAL '24 hours'
            AND (u.last_activity_warning_at IS NULL
                OR u.last_activity_warning_at<=NOW()-INTERVAL '24 hours')
            AND (COALESCE(u.last_message_at,u.guild_joined_at,u.created_at)
                <NOW()-INTERVAL '24 hours' OR COALESCE((SELECT SUM(EXTRACT(EPOCH FROM(
                    LEAST(COALESCE(v.ended_at,NOW()),NOW())
                    -GREATEST(v.started_at,NOW()-INTERVAL '24 hours'))))
                    FROM voice_sessions v WHERE v.discord_id=u.discord_id
                    AND COALESCE(v.ended_at,NOW())>NOW()-INTERVAL '24 hours'),0)<1800)"""
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
        panel_channel_id: int,
        category_id: int,
        reviewer_role_id: int,
        accepted_role_id: int,
    ) -> None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        await self.pool.execute(
            """INSERT INTO application_config(
                id,panel_channel_id,category_id,reviewer_role_id,accepted_role_id
            ) VALUES(1,$1,$2,$3,$4) ON CONFLICT(id) DO UPDATE SET
                panel_channel_id=EXCLUDED.panel_channel_id,
                category_id=EXCLUDED.category_id,
                reviewer_role_id=EXCLUDED.reviewer_role_id,
                accepted_role_id=EXCLUDED.accepted_role_id,
                updated_at=NOW()""",
            panel_channel_id, category_id, reviewer_role_id, accepted_role_id,
        )

    async def set_application_panel_message(self, message_id: int) -> None:
        if self.pool is not None:
            await self.pool.execute(
                "UPDATE application_config SET panel_message_id=$1,updated_at=NOW() WHERE id=1",
                message_id,
            )

    async def get_application_config(self) -> asyncpg.Record | None:
        if self.pool is None:
            return None
        return await self.pool.fetchrow("SELECT * FROM application_config WHERE id=1")

    async def has_open_application(self, discord_id: int) -> bool:
        if self.pool is None:
            return False
        return bool(await self.pool.fetchval(
            """SELECT EXISTS(SELECT 1 FROM applications WHERE discord_id=$1
            AND status IN('pending','review','interview'))""",
            discord_id,
        ))

    async def create_application(
        self,
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
                discord_id,username,game_nickname,static_id,ooc_age,goal,screenshot_url
            ) VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING *""",
            discord_id, username, game_nickname, static_id, ooc_age, goal, screenshot_url,
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
