import asyncio
from contextlib import asynccontextmanager
import json
import logging
from datetime import datetime, timedelta, timezone
from time import monotonic

import asyncpg
import disnake

from cogs.mafia_notifications import personal_events
from cogs.family_ranks import family_rank_from_roles, recruitment_positions_from_roles

from config import (
    APPLICATION_COOLDOWN_DAYS,
    DATABASE_URL,
    FAMILY_MEMBER_ROLE_ID,
    FAMILY_NAME,
    FAMILY_RANK_ROLE_IDS,
    GUILD_ID,
    MAFIA_CLEANUP_DELAY_SECONDS,
    SERVER_ROLE_IDS,
    TARGET_ROLE_IDS,
)


VOICE_SESSION_LOCK_ID = 0x564F494345
VOICE_INSTANCE_LOCK_ID = 0x564F494346
INSTANCE_LOCK_HEARTBEAT_SECONDS = 5


class BotInstanceLockLost(RuntimeError):
    pass


class Database:
    def __init__(self) -> None:
        self.pool: asyncpg.Pool | None = None
        self._instance_lock_connection: asyncpg.Connection | None = None
        self._instance_lock_check = asyncio.Lock()
        self._last_instance_lock_check_monotonic: float | None = None

    async def connect(self) -> None:
        try:
            await self._connect()
        except BaseException:
            try:
                await self._cleanup_failed_connect()
            except Exception:
                logging.exception("Could not clean up a failed database startup")
            raise

    async def _cleanup_failed_connect(self) -> None:
        pool = self.pool
        self.pool = None
        self._last_instance_lock_check_monotonic = None
        try:
            if pool is not None:
                await pool.close()
        finally:
            lock_connection = self._instance_lock_connection
            self._instance_lock_connection = None
            if lock_connection is not None and not lock_connection.is_closed():
                await lock_connection.close()

    async def _connect(self) -> None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is missing. Add it to your .env file.")

        lock_connection = await asyncpg.connect(DATABASE_URL, command_timeout=30)
        self._instance_lock_connection = lock_connection
        try:
            lock_acquired = await lock_connection.fetchval(
                "SELECT pg_try_advisory_lock($1)", VOICE_INSTANCE_LOCK_ID
            )
            if not lock_acquired:
                raise RuntimeError(
                    "Another bot instance is already using this database"
                )
            self._last_instance_lock_check_monotonic = monotonic()
            self.pool = await asyncpg.create_pool(
                DATABASE_URL,
                min_size=1,
                max_size=10,
                command_timeout=30,
            )
        except BaseException:
            if not lock_connection.is_closed():
                await lock_connection.close()
            self._instance_lock_connection = None
            self._last_instance_lock_check_monotonic = None
            raise

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
                ALTER TABLE users ADD COLUMN IF NOT EXISTS family_rank TEXT NOT NULL
                    DEFAULT 'young';
                ALTER TABLE users
                    DROP CONSTRAINT IF EXISTS users_family_rank_check,
                    ADD CONSTRAINT users_family_rank_check CHECK (family_rank IN (
                        'young', 'main', 'old', 'high famq', 'deputy leader',
                        'tech', 'leader', 'curator', 'owner'
                    ));
                ALTER TABLE users ADD COLUMN IF NOT EXISTS recruitment_positions
                    TEXT[] NOT NULL DEFAULT '{}';
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
                    last_observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    ended_at TIMESTAMPTZ,
                    duration_seconds INTEGER
                );
                ALTER TABLE voice_sessions
                    ADD COLUMN IF NOT EXISTS last_observed_at TIMESTAMPTZ;
                UPDATE voice_sessions SET last_observed_at=GREATEST(
                    started_at, COALESCE(ended_at, started_at)
                ) WHERE last_observed_at IS NULL;
                UPDATE voice_sessions SET last_observed_at=started_at
                WHERE last_observed_at<started_at;
                ALTER TABLE voice_sessions
                    ALTER COLUMN last_observed_at SET DEFAULT NOW(),
                    ALTER COLUMN last_observed_at SET NOT NULL;
                UPDATE voice_sessions SET
                    ended_at=started_at,
                    duration_seconds=0
                WHERE ended_at<started_at;
                UPDATE voice_sessions SET duration_seconds=0
                WHERE duration_seconds<0;
                DO $migration$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname='voice_sessions_valid_end'
                          AND conrelid='voice_sessions'::REGCLASS
                    ) THEN
                        ALTER TABLE voice_sessions ADD CONSTRAINT
                            voice_sessions_valid_end CHECK (
                                ended_at IS NULL OR ended_at>=started_at
                            );
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname='voice_sessions_valid_observation'
                          AND conrelid='voice_sessions'::REGCLASS
                    ) THEN
                        ALTER TABLE voice_sessions ADD CONSTRAINT
                            voice_sessions_valid_observation CHECK (
                                last_observed_at>=started_at
                            );
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1 FROM pg_constraint
                        WHERE conname='voice_sessions_valid_duration'
                          AND conrelid='voice_sessions'::REGCLASS
                    ) THEN
                        ALTER TABLE voice_sessions ADD CONSTRAINT
                            voice_sessions_valid_duration CHECK (
                                duration_seconds IS NULL OR duration_seconds>=0
                            );
                    END IF;
                END;
                $migration$;
                WITH duplicate_open_sessions AS (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY discord_id ORDER BY started_at DESC, id DESC
                    ) AS position
                    FROM voice_sessions WHERE ended_at IS NULL
                )
                UPDATE voice_sessions AS session SET
                    ended_at=GREATEST(
                        session.started_at,
                        LEAST(
                            session.last_observed_at+INTERVAL '3 minutes', NOW()
                        )
                    ),
                    duration_seconds=GREATEST(0, EXTRACT(EPOCH FROM(
                        GREATEST(
                            session.started_at,
                            LEAST(
                                session.last_observed_at+INTERVAL '3 minutes', NOW()
                            )
                        )-session.started_at
                    ))::INTEGER)
                FROM duplicate_open_sessions AS duplicate
                WHERE session.id=duplicate.id AND duplicate.position>1;
                CREATE INDEX IF NOT EXISTS voice_sessions_user_started_idx ON voice_sessions(discord_id, started_at DESC);
                CREATE UNIQUE INDEX IF NOT EXISTS voice_sessions_one_open_user_idx
                    ON voice_sessions(discord_id) WHERE ended_at IS NULL;
                CREATE OR REPLACE FUNCTION voice_seconds_since(
                    target_discord_id BIGINT,
                    since_at TIMESTAMPTZ
                ) RETURNS BIGINT LANGUAGE SQL STABLE AS $$
                    SELECT COALESCE(SUM(EXTRACT(EPOCH FROM(
                        UPPER(session_range)-LOWER(session_range)
                    ))), 0)::BIGINT
                    FROM UNNEST((SELECT RANGE_AGG(TSTZRANGE(
                        GREATEST(started_at, since_at),
                        LEAST(COALESCE(
                            ended_at,
                            last_observed_at+INTERVAL '3 minutes'
                        ), NOW()), '[)'
                    )) FROM voice_sessions
                    WHERE discord_id=target_discord_id
                      AND LEAST(COALESCE(
                          ended_at,
                          last_observed_at+INTERVAL '3 minutes'
                      ), NOW())>GREATEST(started_at, since_at)
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

                CREATE TABLE IF NOT EXISTS promotion_requests (
                    id BIGSERIAL PRIMARY KEY,
                    guild_id BIGINT NOT NULL,
                    discord_id BIGINT NOT NULL REFERENCES users(discord_id) ON DELETE CASCADE,
                    from_rank TEXT NOT NULL,
                    target_rank TEXT NOT NULL,
                    server_name TEXT,
                    achievements TEXT NOT NULL,
                    evidence TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','approved','declined','cancelled')),
                    log_message_id BIGINT,
                    role_change_started BOOLEAN NOT NULL DEFAULT FALSE,
                    reviewed_by BIGINT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    reviewed_at TIMESTAMPTZ
                );
                CREATE UNIQUE INDEX IF NOT EXISTS promotion_requests_one_pending_user_idx
                    ON promotion_requests(guild_id,discord_id) WHERE status='pending';

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
                ALTER TABLE applications ADD COLUMN IF NOT EXISTS application_type TEXT
                    NOT NULL DEFAULT 'family' CHECK(application_type IN ('family','recruiter'));
                ALTER TABLE applications ADD COLUMN IF NOT EXISTS transcript_path TEXT;
                ALTER TABLE applications ADD COLUMN IF NOT EXISTS transcript_saved_at TIMESTAMPTZ;
                ALTER TABLE applications ADD COLUMN IF NOT EXISTS transcript_log_message_id BIGINT;
                ALTER TABLE applications ADD COLUMN IF NOT EXISTS cooldown_reset
                    BOOLEAN NOT NULL DEFAULT FALSE;
                ALTER TABLE applications ADD COLUMN IF NOT EXISTS guild_id BIGINT;
                ALTER TABLE applications ADD COLUMN IF NOT EXISTS family_name TEXT;
                ALTER TABLE applications ADD COLUMN IF NOT EXISTS game_server TEXT
                    NOT NULL DEFAULT 'Не указан';
                CREATE INDEX IF NOT EXISTS applications_user_created_idx
                    ON applications(discord_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS application_blocks (
                    guild_id BIGINT NOT NULL,
                    discord_id BIGINT NOT NULL,
                    reason TEXT NOT NULL,
                    blocked_by BIGINT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (guild_id, discord_id)
                );

                CREATE TABLE IF NOT EXISTS application_guild_config (
                    guild_id BIGINT PRIMARY KEY,
                    panel_channel_id BIGINT NOT NULL,
                    category_id BIGINT NOT NULL,
                    reviewer_role_id BIGINT NOT NULL,
                    accepted_role_id BIGINT NOT NULL,
                    family_role_id BIGINT NOT NULL,
                    panel_message_id BIGINT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                ALTER TABLE application_guild_config
                    ADD COLUMN IF NOT EXISTS family_role_id BIGINT;
                ALTER TABLE application_guild_config
                    ADD COLUMN IF NOT EXISTS reviewer_role_ids BIGINT[];
                ALTER TABLE application_guild_config
                    DROP COLUMN IF EXISTS interpolov_role_id,
                    DROP COLUMN IF EXISTS intolerance_role_id;
                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TIMESTAMPTZ NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dm_notification_settings (
                    id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id=1),
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    updated_by BIGINT,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                INSERT INTO dm_notification_settings(id, enabled)
                VALUES(1, TRUE) ON CONFLICT(id) DO NOTHING;
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
                CREATE INDEX IF NOT EXISTS family_blacklist_active_message_idx
                    ON family_blacklist(channel_message_id) WHERE active=TRUE;

                CREATE TABLE IF NOT EXISTS mafia_games (
                    id BIGSERIAL PRIMARY KEY,
                    guild_id BIGINT NOT NULL,
                    channel_id BIGINT NOT NULL,
                    message_id BIGINT,
                    state JSONB NOT NULL,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    revision INTEGER NOT NULL DEFAULT 1,
                    message_revision INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE UNIQUE INDEX IF NOT EXISTS mafia_one_active_guild_idx
                    ON mafia_games(guild_id) WHERE active=TRUE;
                CREATE TABLE IF NOT EXISTS mafia_results (
                    game_id BIGINT NOT NULL REFERENCES mafia_games(id),
                    guild_id BIGINT NOT NULL,
                    discord_id BIGINT NOT NULL,
                    role TEXT NOT NULL,
                    won BOOLEAN NOT NULL,
                    survived BOOLEAN NOT NULL,
                    points INTEGER NOT NULL,
                    finished_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY(game_id,discord_id)
                );
                CREATE INDEX IF NOT EXISTS mafia_results_guild_user_idx
                    ON mafia_results(guild_id,discord_id);
                CREATE TABLE IF NOT EXISTS mafia_role_holds (
                    game_id BIGINT NOT NULL REFERENCES mafia_games(id),
                    guild_id BIGINT NOT NULL,
                    discord_id BIGINT NOT NULL,
                    role_ids BIGINT[] NOT NULL,
                    access_level SMALLINT NOT NULL CHECK(access_level BETWEEN 0 AND 6),
                    restored_at TIMESTAMPTZ,
                    PRIMARY KEY(game_id,discord_id)
                );
                ALTER TABLE mafia_role_holds
                    DROP CONSTRAINT IF EXISTS mafia_role_holds_access_level_check;
                ALTER TABLE mafia_role_holds
                    ADD CONSTRAINT mafia_role_holds_access_level_check
                    CHECK(access_level BETWEEN 0 AND 6);
                CREATE TABLE IF NOT EXISTS mafia_events (
                    id BIGSERIAL PRIMARY KEY,
                    game_id BIGINT NOT NULL REFERENCES mafia_games(id),
                    payload JSONB NOT NULL,
                    message_id BIGINT
                );
                CREATE TABLE IF NOT EXISTS mafia_notifications (
                    id BIGSERIAL PRIMARY KEY,
                    game_id BIGINT NOT NULL REFERENCES mafia_games(id),
                    discord_id BIGINT NOT NULL,
                    payload JSONB NOT NULL,
                    message_id BIGINT,
                    processed_at TIMESTAMPTZ,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS mafia_notifications_pending_idx
                    ON mafia_notifications(next_attempt_at, id) WHERE processed_at IS NULL;

                CREATE TABLE IF NOT EXISTS giveaways (
                    id BIGSERIAL PRIMARY KEY,
                    guild_id BIGINT NOT NULL,
                    channel_id BIGINT NOT NULL,
                    message_id BIGINT UNIQUE,
                    prize TEXT NOT NULL,
                    created_by BIGINT NOT NULL,
                    earliest_end_at TIMESTAMPTZ NOT NULL,
                    latest_end_at TIMESTAMPTZ NOT NULL,
                    draw_at TIMESTAMPTZ NOT NULL,
                    winner_count SMALLINT NOT NULL CHECK(winner_count BETWEEN 1 AND 10),
                    required_role_id BIGINT,
                    claim_minutes INTEGER NOT NULL DEFAULT 0
                        CHECK(claim_minutes BETWEEN 0 AND 1440),
                    claim_deadline TIMESTAMPTZ,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN('active','drawing','claiming','finished','cancelled')),
                    cancelled_by BIGINT,
                    finished_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CHECK(earliest_end_at<=draw_at AND draw_at<=latest_end_at),
                    CHECK(earliest_end_at<latest_end_at)
                );
                CREATE INDEX IF NOT EXISTS giveaways_due_idx
                    ON giveaways(status,draw_at,claim_deadline)
                    WHERE status IN('active','drawing','claiming');
                CREATE TABLE IF NOT EXISTS giveaway_entries (
                    giveaway_id BIGINT NOT NULL REFERENCES giveaways(id) ON DELETE CASCADE,
                    discord_id BIGINT NOT NULL,
                    joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY(giveaway_id,discord_id)
                );
                CREATE INDEX IF NOT EXISTS giveaway_entries_joined_idx
                    ON giveaway_entries(giveaway_id,joined_at);
                CREATE TABLE IF NOT EXISTS giveaway_winners (
                    id BIGSERIAL PRIMARY KEY,
                    giveaway_id BIGINT NOT NULL REFERENCES giveaways(id) ON DELETE CASCADE,
                    discord_id BIGINT NOT NULL,
                    selected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    confirmed_at TIMESTAMPTZ,
                    invalidated_at TIMESTAMPTZ,
                    invalidation_reason TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS giveaway_winners_current_idx
                    ON giveaway_winners(giveaway_id,discord_id)
                    WHERE invalidated_at IS NULL;
                CREATE INDEX IF NOT EXISTS giveaway_winners_history_idx
                    ON giveaway_winners(giveaway_id,selected_at);

                CREATE TABLE IF NOT EXISTS protected_log_messages (
                    message_id BIGINT PRIMARY KEY,
                    guild_id BIGINT NOT NULL,
                    channel_id BIGINT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS nickname_warnings (
                    guild_id BIGINT NOT NULL,
                    member_id BIGINT NOT NULL,
                    nickname TEXT NOT NULL,
                    deadline TIMESTAMPTZ NOT NULL,
                    penalized_at TIMESTAMPTZ,
                    PRIMARY KEY(guild_id,member_id)
                );
                CREATE INDEX IF NOT EXISTS nickname_warnings_due_idx
                    ON nickname_warnings(guild_id,deadline) WHERE penalized_at IS NULL;

                CREATE TABLE IF NOT EXISTS discord_link_violations (
                    guild_id BIGINT NOT NULL,
                    member_id BIGINT NOT NULL,
                    message_id BIGINT NOT NULL,
                    occurred_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY(guild_id,message_id)
                );
                CREATE INDEX IF NOT EXISTS discord_link_violations_member_time_idx
                    ON discord_link_violations(guild_id,member_id,occurred_at);

                CREATE TABLE IF NOT EXISTS security_role_removals (
                    guild_id BIGINT NOT NULL,
                    member_id BIGINT NOT NULL,
                    role_id BIGINT NOT NULL,
                    removed_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY(guild_id,member_id,role_id,removed_at)
                );
                CREATE INDEX IF NOT EXISTS security_role_removals_member_time_idx
                    ON security_role_removals(guild_id,member_id,removed_at);

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
                CREATE TABLE IF NOT EXISTS game_servers (
                    id BIGSERIAL PRIMARY KEY,
                    guild_id BIGINT NOT NULL,
                    name TEXT NOT NULL,
                    role_id BIGINT,
                    added_by BIGINT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(guild_id, name)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS game_servers_guild_role_idx
                    ON game_servers(guild_id, role_id) WHERE role_id IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS game_servers_guild_name_ci_idx
                    ON game_servers(guild_id, LOWER(name));
                CREATE TABLE IF NOT EXISTS application_staff_roles (
                    guild_id BIGINT NOT NULL,
                    server_id BIGINT NOT NULL REFERENCES game_servers(id) ON DELETE CASCADE,
                    role_id BIGINT NOT NULL,
                    access_level SMALLINT NOT NULL CHECK(access_level IN (2,4)),
                    PRIMARY KEY(guild_id,role_id),
                    UNIQUE(server_id,access_level)
                );
                CREATE TABLE IF NOT EXISTS recruiter_application_config (
                    guild_id BIGINT PRIMARY KEY,
                    panel_channel_id BIGINT NOT NULL,
                    category_id BIGINT NOT NULL,
                    panel_message_id BIGINT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS families (
                    id BIGSERIAL PRIMARY KEY,
                    guild_id BIGINT NOT NULL,
                    name TEXT NOT NULL,
                    role_id BIGINT,
                    added_by BIGINT NOT NULL,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(guild_id, name)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS families_guild_role_idx
                    ON families(guild_id, role_id) WHERE role_id IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS families_guild_name_ci_idx
                    ON families(guild_id, LOWER(name));
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
                "UPDATE applications SET family_name=$1 WHERE family_name IS DISTINCT FROM $1",
                FAMILY_NAME,
            )
            await connection.execute(
                "ALTER TABLE applications ALTER COLUMN family_name SET NOT NULL"
            )
            await connection.execute(
                """UPDATE users SET family_name=$1,updated_at=NOW()
                WHERE family_name IS NOT NULL AND family_name IS DISTINCT FROM $1""",
                FAMILY_NAME,
            )
            await connection.execute(
                """INSERT INTO application_guild_config(
                    guild_id,panel_channel_id,category_id,reviewer_role_id,
                    accepted_role_id,panel_message_id,updated_at,
                    family_role_id,reviewer_role_ids
                ) SELECT $1,panel_channel_id,category_id,
                    reviewer_role_id,accepted_role_id,panel_message_id,updated_at,
                    accepted_role_id,ARRAY[reviewer_role_id]
                FROM application_config WHERE id=1
                ON CONFLICT(guild_id) DO NOTHING""",
                GUILD_ID,
            )
            await connection.execute(
                """UPDATE application_guild_config SET
                    family_role_id=COALESCE(family_role_id,accepted_role_id),
                    reviewer_role_ids=COALESCE(reviewer_role_ids,ARRAY[reviewer_role_id])"""
            )
            await connection.execute(
                """ALTER TABLE application_guild_config
                    ALTER COLUMN family_role_id SET NOT NULL,
                    ALTER COLUMN reviewer_role_ids SET NOT NULL"""
            )
            has_configured_servers = await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM game_servers WHERE guild_id=$1)", GUILD_ID
            )
            if not has_configured_servers:
                for server_name, role_id in SERVER_ROLE_IDS.items():
                    await connection.execute(
                        """INSERT INTO game_servers(guild_id,name,role_id,added_by)
                        VALUES($1,$2,$3,$4)""",
                        GUILD_ID, server_name, role_id, 811292492481298432,
                    )
            await connection.execute(
                """UPDATE application_guild_config SET
                    accepted_role_id=$2,
                    family_role_id=$2,
                    updated_at=NOW()
                WHERE guild_id=$1""",
                GUILD_ID,
                FAMILY_MEMBER_ROLE_ID,
            )
            await connection.execute(
                """UPDATE families SET role_id=NULL,active=FALSE,updated_at=NOW()
                WHERE guild_id=$1 AND LOWER(name)<>LOWER($2)""",
                GUILD_ID,
                FAMILY_NAME,
            )
            family = await connection.fetchrow(
                "SELECT id FROM families WHERE guild_id=$1 AND LOWER(name)=LOWER($2)",
                GUILD_ID,
                FAMILY_NAME,
            )
            if family is None:
                await connection.execute(
                    """INSERT INTO families(guild_id,name,role_id,added_by)
                    VALUES($1,$2,$3,$4)""",
                    GUILD_ID,
                    FAMILY_NAME,
                    FAMILY_MEMBER_ROLE_ID,
                    811292492481298432,
                )
            else:
                await connection.execute(
                    """UPDATE families SET name=$2,role_id=$3,active=TRUE,updated_at=NOW()
                    WHERE id=$1""",
                    family["id"],
                    FAMILY_NAME,
                    FAMILY_MEMBER_ROLE_ID,
                )

        logging.info("Connected to PostgreSQL")

    async def ensure_instance_lock(self, *, force: bool = False) -> None:
        async with self._instance_lock_check:
            connection = self._instance_lock_connection
            last_check = self._last_instance_lock_check_monotonic
            if connection is None or connection.is_closed():
                self._last_instance_lock_check_monotonic = None
                raise BotInstanceLockLost("The bot instance lock connection was lost")
            if (
                not force
                and last_check is not None
                and monotonic() - last_check < INSTANCE_LOCK_HEARTBEAT_SECONDS
            ):
                return
            try:
                await connection.execute("SELECT 1")
            except Exception as error:
                self._last_instance_lock_check_monotonic = None
                raise BotInstanceLockLost(
                    "The bot instance lock connection was lost"
                ) from error
            self._last_instance_lock_check_monotonic = monotonic()

    async def close_voice_sessions(
        self, observed_at: datetime | None = None
    ) -> None:
        """Close every open voice lease at a bounded observation time."""
        if self.pool is None:
            return
        await self.ensure_instance_lock(force=True)
        if observed_at is None:
            observed_at = datetime.now(timezone.utc)

        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock($1)", VOICE_SESSION_LOCK_ID
                )
                await connection.execute(
                    """UPDATE voice_sessions SET
                    ended_at=GREATEST(started_at,LEAST(
                        last_observed_at+INTERVAL '3 minutes',$1::TIMESTAMPTZ,NOW()
                    )),
                    duration_seconds=GREATEST(0,EXTRACT(EPOCH FROM(
                        GREATEST(started_at,LEAST(
                            last_observed_at+INTERVAL '3 minutes',
                            $1::TIMESTAMPTZ,NOW()
                        ))-started_at
                    ))::INTEGER) WHERE ended_at IS NULL""",
                    observed_at,
                )

    async def close(self, observed_at: datetime | None = None) -> None:
        if self.pool is None and self._instance_lock_connection is None:
            return

        pool = self.pool
        try:
            if pool is not None:
                await self.close_voice_sessions(observed_at)
        finally:
            try:
                if pool is not None:
                    try:
                        await pool.close()
                    finally:
                        self.pool = None
            finally:
                lock_connection = self._instance_lock_connection
                self._instance_lock_connection = None
                self._last_instance_lock_check_monotonic = None
                if lock_connection is not None and not lock_connection.is_closed():
                    try:
                        await lock_connection.execute(
                            "SELECT pg_advisory_unlock($1)", VOICE_INSTANCE_LOCK_ID
                        )
                    except Exception:
                        logging.exception("Could not release the bot instance lock")
                    finally:
                        await lock_connection.close()
        logging.info("PostgreSQL connections closed")

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
            for member in family_members:
                await self.sync_user_family_roles(member)
        except asyncpg.PostgresError:
            logging.exception("Could not save users to PostgreSQL")
            raise

    async def sync_user_family_roles(self, member: disnake.Member) -> None:
        if member.guild.id != GUILD_ID or member.bot:
            return
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        role_ids = {role.id for role in member.roles}
        # Mafia temporarily holds administrator roles, including the developer's.
        held_roles = await self.pool.fetch(
            """SELECT role_ids FROM mafia_role_holds
            WHERE guild_id=$1 AND discord_id=$2 AND restored_at IS NULL""",
            member.guild.id, member.id,
        )
        for row in held_roles:
            role_ids.update(row["role_ids"])
        recruiters = await self.pool.fetch(
            """SELECT role_id FROM application_staff_roles
            WHERE guild_id=$1 AND access_level=2
            UNION SELECT member_role_id AS role_id FROM department_config
            WHERE guild_id=$1 AND department='recruiters'""",
            member.guild.id,
        )
        rank = family_rank_from_roles(role_ids)
        positions = recruitment_positions_from_roles(
            role_ids, {row["role_id"] for row in recruiters if row["role_id"] is not None}
        )
        await self.pool.execute(
            """UPDATE users SET family_rank=$2,recruitment_positions=$3,updated_at=NOW()
            WHERE discord_id=$1 AND (family_rank IS DISTINCT FROM $2
                OR recruitment_positions IS DISTINCT FROM $3::TEXT[])""",
            member.id, rank, positions,
        )

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
        if family_name != FAMILY_NAME:
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
        await self.ensure_instance_lock()
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock($1)", VOICE_SESSION_LOCK_ID
                )
                was_active = await connection.fetchval(
                    "SELECT is_active FROM users WHERE discord_id=$1", discord_id
                )
                if was_active is None:
                    return False
                await connection.execute(
                    """UPDATE users SET is_active=FALSE,profile_status='Неактивен',
                    inactive_reason=$2,inactive_at=NOW(),updated_at=NOW(),
                    roles_removed_at=CASE WHEN $3 THEN NOW() ELSE roles_removed_at END
                    WHERE discord_id=$1""",
                    discord_id,
                    reason,
                    roles_removed,
                )
                await connection.execute(
                    """WITH underfilled_channels AS (
                        SELECT session.channel_id
                        FROM voice_sessions AS session
                        JOIN users ON users.discord_id=session.discord_id
                        WHERE session.ended_at IS NULL
                        GROUP BY session.channel_id
                        HAVING COUNT(*) FILTER (WHERE users.is_active)<2
                    )
                    UPDATE voice_sessions AS session SET
                    ended_at=GREATEST(session.started_at,LEAST(
                        session.last_observed_at+INTERVAL '3 minutes',NOW()
                    )),
                    duration_seconds=GREATEST(0,EXTRACT(EPOCH FROM(
                        GREATEST(session.started_at,LEAST(
                            session.last_observed_at+INTERVAL '3 minutes',NOW()
                        ))-session.started_at
                    ))::INTEGER)
                    WHERE session.ended_at IS NULL AND (
                        NOT EXISTS(
                            SELECT 1 FROM users
                            WHERE users.discord_id=session.discord_id
                              AND users.is_active
                        )
                        OR session.channel_id IN (
                            SELECT channel_id FROM underfilled_channels
                        )
                    )"""
                )
                return bool(was_active)

    async def record_message_activity(self, discord_id: int) -> None:
        if self.pool is not None:
            await self.pool.execute("UPDATE users SET last_message_at=NOW(),updated_at=NOW() WHERE discord_id=$1 AND is_active=TRUE", discord_id)

    async def reconcile_voice_sessions(
        self,
        voice_channels_by_user: dict[int, int],
        *,
        observed_at: datetime,
        recover: bool = False,
    ) -> dict[int, int]:
        """Align open sessions with channels containing two active profiles."""
        if self.pool is None:
            return {}
        await self.ensure_instance_lock()

        discord_ids = list(voice_channels_by_user)
        channel_ids = [voice_channels_by_user[user_id] for user_id in discord_ids]
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "SELECT pg_advisory_xact_lock($1)", VOICE_SESSION_LOCK_ID
                )
                if recover:
                    await connection.execute(
                        """UPDATE voice_sessions SET
                        ended_at=GREATEST(started_at,LEAST(
                            last_observed_at+INTERVAL '3 minutes',
                            $1::TIMESTAMPTZ,NOW()
                        )),
                        duration_seconds=GREATEST(0,EXTRACT(EPOCH FROM(
                            GREATEST(started_at,LEAST(
                                last_observed_at+INTERVAL '3 minutes',
                                $1::TIMESTAMPTZ,NOW()
                            ))-started_at
                        ))::INTEGER)
                        WHERE ended_at IS NULL""",
                        observed_at,
                    )
                else:
                    await connection.execute(
                        """WITH current_voice AS (
                            SELECT * FROM UNNEST(
                                $1::BIGINT[],$2::BIGINT[]
                            ) AS voice(discord_id,channel_id)
                        ), active_voice AS (
                            SELECT current_voice.*,
                                COUNT(*) OVER (
                                    PARTITION BY current_voice.channel_id
                                ) AS active_count
                            FROM current_voice JOIN users
                                ON users.discord_id=current_voice.discord_id
                               AND users.is_active=TRUE
                        ), qualified_voice AS (
                            SELECT discord_id,channel_id FROM active_voice
                            WHERE active_count>=2
                        )
                        UPDATE voice_sessions AS session SET
                        ended_at=GREATEST(session.started_at,LEAST(
                            session.last_observed_at+INTERVAL '3 minutes',
                            $3::TIMESTAMPTZ,NOW()
                        )),
                        duration_seconds=GREATEST(0,EXTRACT(EPOCH FROM(
                            GREATEST(session.started_at,LEAST(
                                session.last_observed_at+INTERVAL '3 minutes',
                                $3::TIMESTAMPTZ,NOW()
                            ))-session.started_at
                        ))::INTEGER)
                        WHERE session.ended_at IS NULL AND (
                            session.last_observed_at+INTERVAL '3 minutes'
                                <LEAST($3::TIMESTAMPTZ,NOW())
                            OR NOT EXISTS(
                                SELECT 1 FROM qualified_voice
                                WHERE qualified_voice.discord_id=session.discord_id
                                  AND qualified_voice.channel_id=session.channel_id
                            )
                        )""",
                        discord_ids,
                        channel_ids,
                        observed_at,
                    )

                await connection.execute(
                    """WITH current_voice AS (
                        SELECT * FROM UNNEST(
                            $1::BIGINT[],$2::BIGINT[]
                        ) AS voice(discord_id,channel_id)
                    ), active_voice AS (
                        SELECT current_voice.*,
                            COUNT(*) OVER (
                                PARTITION BY current_voice.channel_id
                            ) AS active_count
                        FROM current_voice JOIN users
                            ON users.discord_id=current_voice.discord_id
                           AND users.is_active=TRUE
                    ), qualified_voice AS (
                        SELECT discord_id,channel_id FROM active_voice
                        WHERE active_count>=2
                    )
                    INSERT INTO voice_sessions(
                        discord_id,channel_id,started_at,last_observed_at
                    ) SELECT discord_id,channel_id,
                        LEAST($3::TIMESTAMPTZ,NOW()),
                        LEAST($3::TIMESTAMPTZ,NOW())
                    FROM qualified_voice
                    ON CONFLICT(discord_id) WHERE ended_at IS NULL DO UPDATE SET
                        channel_id=EXCLUDED.channel_id,
                        last_observed_at=GREATEST(
                            voice_sessions.last_observed_at,
                            EXCLUDED.last_observed_at
                        )""",
                    discord_ids,
                    channel_ids,
                    observed_at,
                )
                rows = await connection.fetch(
                    """SELECT discord_id,id FROM voice_sessions
                    WHERE ended_at IS NULL
                      AND discord_id=ANY($1::BIGINT[])""",
                    discord_ids,
                )
                return {row["discord_id"]: row["id"] for row in rows}

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

    async def create_promotion_request(
        self, guild_id: int, discord_id: int, from_rank: str, target_rank: str,
        server_name: str | None, achievements: str, evidence: str,
    ) -> asyncpg.Record:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        ranks = list(FAMILY_RANK_ROLE_IDS)
        if guild_id != GUILD_ID or ranks.index(target_rank) <= ranks.index(from_rank):
            raise ValueError("Invalid promotion")
        return await self.pool.fetchrow(
            """INSERT INTO promotion_requests(
                guild_id,discord_id,from_rank,target_rank,server_name,achievements,evidence
            ) VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING *""",
            guild_id, discord_id, from_rank, target_rank, server_name, achievements, evidence,
        )

    async def set_promotion_log_message(self, request_id: int, message_id: int) -> None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        await self.pool.execute(
            "UPDATE promotion_requests SET log_message_id=$2 WHERE id=$1 AND status='pending'",
            request_id, message_id,
        )

    async def cancel_unpublished_promotion(self, request_id: int) -> None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        await self.pool.execute(
            """UPDATE promotion_requests SET status='cancelled'
            WHERE id=$1 AND status='pending' AND log_message_id IS NULL""", request_id,
        )

    @asynccontextmanager
    async def promotion_review_lock(self, request_id: int):
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        async with self.pool.acquire() as connection:
            # A session lock permits a durable checkpoint before the Discord request.
            key = f"promotion:{request_id}"
            await connection.execute("SELECT pg_advisory_lock(hashtextextended($1,0))", key)
            try:
                yield connection
            finally:
                await connection.execute("SELECT pg_advisory_unlock(hashtextextended($1,0))", key)

    async def get_promotion_request(
        self, connection: asyncpg.Connection, guild_id: int, request_id: int,
    ) -> asyncpg.Record | None:
        return await connection.fetchrow(
            "SELECT * FROM promotion_requests WHERE guild_id=$1 AND id=$2", guild_id, request_id,
        )

    async def start_promotion_role_change(
        self, connection: asyncpg.Connection, request_id: int, reviewer_id: int,
    ) -> None:
        await connection.execute(
            """UPDATE promotion_requests SET role_change_started=TRUE,reviewed_by=$2
            WHERE id=$1 AND status='pending'""", request_id, reviewer_id,
        )

    async def finish_promotion_request(
        self, connection: asyncpg.Connection, request_id: int, status: str, reviewer_id: int,
    ) -> asyncpg.Record | None:
        if status not in {"approved", "declined"}:
            raise ValueError("Invalid promotion decision")
        async with connection.transaction():
            request = await connection.fetchrow(
                """UPDATE promotion_requests SET status=$2,reviewed_by=$3,reviewed_at=NOW()
                WHERE id=$1 AND status='pending' RETURNING *""", request_id, status, reviewer_id,
            )
            if request is not None and status == "approved":
                await connection.execute(
                    "UPDATE users SET family_rank=$2,updated_at=NOW() WHERE discord_id=$1",
                    request["discord_id"], request["target_rank"],
                )
            return request

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
        self,
        *,
        notification_only: bool = False,
        discord_id: int | None = None,
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
            AND ($2::BIGINT IS NULL OR u.discord_id=$2)
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
            discord_id,
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

    async def get_daily_warning_users(
        self, *, discord_id: int | None = None
    ) -> list[asyncpg.Record]:
        if self.pool is None:
            return []
        return await self.pool.fetch(
            """SELECT u.*, voice_seconds_since(
                u.discord_id, NOW()-INTERVAL '24 hours'
            )::INTEGER AS daily_voice_seconds
            FROM users u WHERE u.is_active=TRUE
            AND ($1::BIGINT IS NULL OR u.discord_id=$1)
            AND NOT EXISTS(SELECT 1 FROM leave_requests l WHERE l.discord_id=u.discord_id
                AND l.status='approved' AND l.ends_at>NOW())
            AND COALESCE(u.guild_joined_at,u.created_at)<=NOW()-INTERVAL '24 hours'
            AND (u.last_activity_warning_at IS NULL
                OR u.last_activity_warning_at<=NOW()-INTERVAL '24 hours')
            AND (COALESCE(u.last_message_at,u.guild_joined_at,u.created_at)
                <NOW()-INTERVAL '24 hours' OR voice_seconds_since(
                    u.discord_id, NOW()-INTERVAL '24 hours'
                )<1800)""",
            discord_id,
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
        family_role_id: int,
    ) -> None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        await self.pool.execute(
            """INSERT INTO application_guild_config(
                guild_id,panel_channel_id,category_id,reviewer_role_id,reviewer_role_ids,
                accepted_role_id,family_role_id
            ) VALUES($1,$2,$3,$4,ARRAY[]::BIGINT[],$4,$4) ON CONFLICT(guild_id) DO UPDATE SET
                panel_channel_id=EXCLUDED.panel_channel_id,
                category_id=EXCLUDED.category_id,
                accepted_role_id=EXCLUDED.accepted_role_id,
                family_role_id=EXCLUDED.family_role_id,
                updated_at=NOW()""",
            guild_id, panel_channel_id, category_id,
            family_role_id,
        )

    async def save_recruiter_application_config(
        self, guild_id: int, panel_channel_id: int, category_id: int, message_id: int
    ) -> None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        await self.pool.execute(
            """INSERT INTO recruiter_application_config
            (guild_id,panel_channel_id,category_id,panel_message_id) VALUES($1,$2,$3,$4)
            ON CONFLICT(guild_id) DO UPDATE SET panel_channel_id=$2,
                category_id=$3,panel_message_id=$4""",
            guild_id, panel_channel_id, category_id, message_id,
        )

    async def get_recruiter_application_config(self, guild_id: int) -> asyncpg.Record | None:
        if self.pool is None:
            return None
        return await self.pool.fetchrow(
            "SELECT * FROM recruiter_application_config WHERE guild_id=$1", guild_id
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

    async def is_application_blocked(self, guild_id: int, discord_id: int) -> bool:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        return bool(await self.pool.fetchval(
            """SELECT EXISTS(SELECT 1 FROM application_blocks
            WHERE guild_id=$1 AND discord_id=$2)""",
            guild_id, discord_id,
        ))

    async def block_applications(
        self, guild_id: int, discord_id: int, reason: str, blocked_by: int
    ) -> None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        await self.pool.execute(
            """INSERT INTO application_blocks(guild_id,discord_id,reason,blocked_by)
            VALUES($1,$2,$3,$4) ON CONFLICT(guild_id,discord_id) DO UPDATE SET
                reason=EXCLUDED.reason,blocked_by=EXCLUDED.blocked_by,
                created_at=NOW()""",
            guild_id, discord_id, reason, blocked_by,
        )

    async def unblock_applications(self, guild_id: int, discord_id: int) -> bool:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        result = await self.pool.execute(
            "DELETE FROM application_blocks WHERE guild_id=$1 AND discord_id=$2",
            guild_id, discord_id,
        )
        return result != "DELETE 0"

    async def get_application_cooldown_until(
        self, guild_id: int, discord_id: int, application_type: str = "family"
    ) -> datetime | None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        created_at = await self.pool.fetchval(
            """SELECT MAX(created_at) FROM applications
            WHERE guild_id=$1 AND discord_id=$2 AND NOT cooldown_reset
                AND application_type=$3""",
            guild_id, discord_id, application_type,
        )
        if created_at is None:
            return None
        return created_at + timedelta(days=APPLICATION_COOLDOWN_DAYS)

    async def reset_application_cooldown(self, guild_id: int, discord_id: int) -> None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        await self.pool.execute(
            """UPDATE applications SET cooldown_reset=TRUE
            WHERE guild_id=$1 AND discord_id=$2 AND NOT cooldown_reset""",
            guild_id, discord_id,
        )

    async def has_open_application(
        self, discord_id: int, guild_id: int, application_type: str = "family"
    ) -> bool:
        if self.pool is None:
            return False
        return bool(await self.pool.fetchval(
            """SELECT EXISTS(SELECT 1 FROM applications WHERE discord_id=$1
            AND guild_id=$2 AND application_type=$3
            AND status IN('pending','review','interview'))""",
            discord_id, guild_id, application_type,
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
        application_type: str = "family",
    ) -> asyncpg.Record:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        return await self.pool.fetchrow(
            """INSERT INTO applications(
                guild_id,family_name,game_server,discord_id,username,game_nickname,static_id,ooc_age,goal,screenshot_url,application_type
            ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING *""",
            guild_id, family_name, game_server, discord_id, username,
            game_nickname, static_id, ooc_age, goal, screenshot_url, application_type,
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

    async def decline_open_applications_for_departed_member(
        self, discord_id: int, guild_id: int, reviewer_id: int
    ) -> list[asyncpg.Record]:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        return await self.pool.fetch(
            """UPDATE applications SET status='declined',reviewer_id=$3,updated_at=NOW()
            WHERE discord_id=$1 AND guild_id=$2
            AND status IN('pending','review','interview') RETURNING *""",
            discord_id, guild_id, reviewer_id,
        )

    async def get_application(self, application_id: int) -> asyncpg.Record | None:
        if self.pool is None:
            return None
        return await self.pool.fetchrow("SELECT * FROM applications WHERE id=$1", application_id)

    async def get_finalized_ticket_applications(
        self, guild_id: int
    ) -> list[asyncpg.Record]:
        if self.pool is None:
            return []
        return await self.pool.fetch(
            """SELECT * FROM applications WHERE guild_id=$1
            AND status IN('accepted','declined')
            AND ticket_channel_id IS NOT NULL AND ticket_message_id IS NOT NULL
            ORDER BY updated_at DESC""",
            guild_id,
        )

    async def set_application_json_path(self, application_id: int, path: str) -> None:
        if self.pool is not None:
            await self.pool.execute(
                "UPDATE applications SET application_json_path=$2,updated_at=NOW() WHERE id=$1",
                application_id, path,
            )

    async def set_application_transcript(self, application_id: int, path: str) -> None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        await self.pool.execute(
            """UPDATE applications SET transcript_path=$2,transcript_saved_at=NOW(),
            transcript_log_message_id=NULL,updated_at=NOW() WHERE id=$1""",
            application_id, path,
        )

    async def set_application_transcript_log_message(self, application_id: int, message_id: int) -> None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        await self.pool.execute(
            "UPDATE applications SET transcript_log_message_id=$2 WHERE id=$1", application_id, message_id,
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

    async def get_active_blacklist_entry_by_message(
        self, message_id: int
    ) -> asyncpg.Record | None:
        if self.pool is None:
            return None
        return await self.pool.fetchrow(
            """SELECT * FROM family_blacklist
            WHERE channel_message_id=$1 AND active=TRUE""",
            message_id,
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

    async def create_giveaway(
        self,
        guild_id: int,
        channel_id: int,
        prize: str,
        created_by: int,
        earliest_end_at: datetime,
        latest_end_at: datetime,
        draw_at: datetime,
        winner_count: int,
        required_role_id: int | None,
        claim_minutes: int,
    ) -> asyncpg.Record:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        return await self.pool.fetchrow(
            """INSERT INTO giveaways(
                guild_id,channel_id,prize,created_by,earliest_end_at,
                latest_end_at,draw_at,winner_count,required_role_id,claim_minutes
            ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING *""",
            guild_id,
            channel_id,
            prize,
            created_by,
            earliest_end_at,
            latest_end_at,
            draw_at,
            winner_count,
            required_role_id,
            claim_minutes,
        )

    async def set_giveaway_message(self, giveaway_id: int, message_id: int) -> None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        await self.pool.execute(
            "UPDATE giveaways SET message_id=$2 WHERE id=$1",
            giveaway_id,
            message_id,
        )

    async def get_giveaway(
        self, giveaway_id: int, guild_id: int
    ) -> asyncpg.Record | None:
        if self.pool is None:
            return None
        return await self.pool.fetchrow(
            "SELECT * FROM giveaways WHERE id=$1 AND guild_id=$2",
            giveaway_id,
            guild_id,
        )

    async def get_open_giveaways(self, guild_id: int) -> list[asyncpg.Record]:
        if self.pool is None:
            return []
        return await self.pool.fetch(
            """SELECT * FROM giveaways WHERE guild_id=$1
            AND status IN('active','drawing','claiming')
            ORDER BY created_at DESC""",
            guild_id,
        )

    async def get_due_giveaway_ids(self) -> list[int]:
        if self.pool is None:
            return []
        rows = await self.pool.fetch(
            """SELECT id FROM giveaways
            WHERE (status='active' AND draw_at<=NOW())
               OR status='drawing'
               OR (status='claiming' AND claim_deadline<=NOW())
            ORDER BY COALESCE(claim_deadline,draw_at),id"""
        )
        return [row["id"] for row in rows]

    async def get_giveaway_entry_count(self, giveaway_id: int) -> int:
        if self.pool is None:
            return 0
        return int(
            await self.pool.fetchval(
                "SELECT COUNT(*) FROM giveaway_entries WHERE giveaway_id=$1",
                giveaway_id,
            )
        )

    async def get_giveaway_candidate_ids(self, giveaway_id: int) -> list[int]:
        if self.pool is None:
            return []
        rows = await self.pool.fetch(
            """SELECT entry.discord_id FROM giveaway_entries AS entry
            WHERE entry.giveaway_id=$1 AND NOT EXISTS(
                SELECT 1 FROM giveaway_winners AS winner
                WHERE winner.giveaway_id=entry.giveaway_id
                  AND winner.discord_id=entry.discord_id
            ) ORDER BY entry.joined_at,entry.discord_id""",
            giveaway_id,
        )
        return [row["discord_id"] for row in rows]

    async def get_current_giveaway_winners(
        self, giveaway_id: int
    ) -> list[asyncpg.Record]:
        if self.pool is None:
            return []
        return await self.pool.fetch(
            """SELECT * FROM giveaway_winners WHERE giveaway_id=$1
            AND invalidated_at IS NULL ORDER BY selected_at,id""",
            giveaway_id,
        )

    async def join_giveaway(
        self, giveaway_id: int, guild_id: int, discord_id: int
    ) -> str:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                giveaway = await connection.fetchrow(
                    """SELECT status,draw_at FROM giveaways
                    WHERE id=$1 AND guild_id=$2 FOR UPDATE""",
                    giveaway_id,
                    guild_id,
                )
                if giveaway is None:
                    return "missing"
                if giveaway["status"] != "active" or giveaway["draw_at"] <= datetime.now(timezone.utc):
                    return "expired"
                inserted = await connection.fetchval(
                    """INSERT INTO giveaway_entries(giveaway_id,discord_id)
                    VALUES($1,$2) ON CONFLICT DO NOTHING RETURNING discord_id""",
                    giveaway_id,
                    discord_id,
                )
                return "joined" if inserted is not None else "already"

    async def leave_giveaway(
        self, giveaway_id: int, guild_id: int, discord_id: int
    ) -> str:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                giveaway = await connection.fetchrow(
                    """SELECT status,draw_at FROM giveaways
                    WHERE id=$1 AND guild_id=$2 FOR UPDATE""",
                    giveaway_id,
                    guild_id,
                )
                if giveaway is None:
                    return "missing"
                if giveaway["status"] != "active" or giveaway["draw_at"] <= datetime.now(timezone.utc):
                    return "expired"
                result = await connection.execute(
                    """DELETE FROM giveaway_entries
                    WHERE giveaway_id=$1 AND discord_id=$2""",
                    giveaway_id,
                    discord_id,
                )
                return "left" if result != "DELETE 0" else "not_joined"

    async def prepare_giveaway_draw(
        self, giveaway_id: int, guild_id: int, mode: str
    ) -> tuple[asyncpg.Record, int] | None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        if mode not in {"scheduled", "end", "reroll"}:
            raise ValueError("Unknown giveaway draw mode")

        async with self.pool.acquire() as connection:
            async with connection.transaction():
                giveaway = await connection.fetchrow(
                    """SELECT * FROM giveaways
                    WHERE id=$1 AND guild_id=$2 FOR UPDATE""",
                    giveaway_id,
                    guild_id,
                )
                if giveaway is None:
                    return None

                status = giveaway["status"]
                now = datetime.now(timezone.utc)
                if mode == "scheduled":
                    if status == "active":
                        if giveaway["draw_at"] > now:
                            return None
                    elif status == "claiming":
                        if giveaway["claim_deadline"] is None or giveaway["claim_deadline"] > now:
                            return None
                        await connection.execute(
                            """UPDATE giveaway_winners SET invalidated_at=NOW(),
                            invalidation_reason='Время подтверждения истекло'
                            WHERE giveaway_id=$1 AND invalidated_at IS NULL
                            AND confirmed_at IS NULL""",
                            giveaway_id,
                        )
                    elif status != "drawing":
                        return None
                elif mode == "end":
                    if status != "active":
                        return None
                else:
                    if status not in {"finished", "claiming"}:
                        return None
                    await connection.execute(
                        """UPDATE giveaway_winners SET invalidated_at=NOW(),
                        invalidation_reason='Перевыбор администратором'
                        WHERE giveaway_id=$1 AND invalidated_at IS NULL""",
                        giveaway_id,
                    )

                if status != "drawing":
                    giveaway = await connection.fetchrow(
                        """UPDATE giveaways SET status='drawing',claim_deadline=NULL,
                        finished_at=NULL WHERE id=$1 RETURNING *""",
                        giveaway_id,
                    )
                current_count = int(
                    await connection.fetchval(
                        """SELECT COUNT(*) FROM giveaway_winners
                        WHERE giveaway_id=$1 AND invalidated_at IS NULL""",
                        giveaway_id,
                    )
                )
                return giveaway, max(giveaway["winner_count"] - current_count, 0)

    async def complete_giveaway_draw(
        self, giveaway_id: int, winner_ids: list[int]
    ) -> asyncpg.Record | None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                giveaway = await connection.fetchrow(
                    "SELECT * FROM giveaways WHERE id=$1 FOR UPDATE",
                    giveaway_id,
                )
                if giveaway is None or giveaway["status"] != "drawing":
                    return None
                for discord_id in winner_ids:
                    await connection.execute(
                        """INSERT INTO giveaway_winners(
                            giveaway_id,discord_id,confirmed_at
                        ) VALUES($1,$2,CASE WHEN $3=0 THEN NOW() ELSE NULL END)""",
                        giveaway_id,
                        discord_id,
                        giveaway["claim_minutes"],
                    )

                current_count = int(
                    await connection.fetchval(
                        """SELECT COUNT(*) FROM giveaway_winners
                        WHERE giveaway_id=$1 AND invalidated_at IS NULL""",
                        giveaway_id,
                    )
                )
                unconfirmed_count = int(
                    await connection.fetchval(
                        """SELECT COUNT(*) FROM giveaway_winners
                        WHERE giveaway_id=$1 AND invalidated_at IS NULL
                        AND confirmed_at IS NULL""",
                        giveaway_id,
                    )
                )
                if current_count and unconfirmed_count:
                    return await connection.fetchrow(
                        """UPDATE giveaways SET status='claiming',
                        claim_deadline=NOW()+($2*INTERVAL '1 minute')
                        WHERE id=$1 RETURNING *""",
                        giveaway_id,
                        giveaway["claim_minutes"],
                    )
                return await connection.fetchrow(
                    """UPDATE giveaways SET status='finished',claim_deadline=NULL,
                    finished_at=NOW() WHERE id=$1 RETURNING *""",
                    giveaway_id,
                )

    async def claim_giveaway(
        self, giveaway_id: int, guild_id: int, discord_id: int
    ) -> str:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                giveaway = await connection.fetchrow(
                    """SELECT * FROM giveaways
                    WHERE id=$1 AND guild_id=$2 FOR UPDATE""",
                    giveaway_id,
                    guild_id,
                )
                if giveaway is None:
                    return "missing"
                if (
                    giveaway["status"] != "claiming"
                    or giveaway["claim_deadline"] is None
                    or giveaway["claim_deadline"] <= datetime.now(timezone.utc)
                ):
                    return "expired"
                winner = await connection.fetchrow(
                    """SELECT * FROM giveaway_winners WHERE giveaway_id=$1
                    AND discord_id=$2 AND invalidated_at IS NULL FOR UPDATE""",
                    giveaway_id,
                    discord_id,
                )
                if winner is None:
                    return "not_winner"
                if winner["confirmed_at"] is not None:
                    return "already"
                await connection.execute(
                    "UPDATE giveaway_winners SET confirmed_at=NOW() WHERE id=$1",
                    winner["id"],
                )
                unconfirmed = await connection.fetchval(
                    """SELECT EXISTS(SELECT 1 FROM giveaway_winners
                    WHERE giveaway_id=$1 AND invalidated_at IS NULL
                    AND confirmed_at IS NULL)""",
                    giveaway_id,
                )
                if not unconfirmed:
                    await connection.execute(
                        """UPDATE giveaways SET status='finished',claim_deadline=NULL,
                        finished_at=NOW() WHERE id=$1""",
                        giveaway_id,
                    )
                return "claimed"

    async def cancel_giveaway(
        self, giveaway_id: int, guild_id: int, cancelled_by: int
    ) -> asyncpg.Record | None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                giveaway = await connection.fetchrow(
                    """UPDATE giveaways SET status='cancelled',cancelled_by=$3,
                    claim_deadline=NULL,finished_at=NOW()
                    WHERE id=$1 AND guild_id=$2
                    AND status IN('active','drawing','claiming') RETURNING *""",
                    giveaway_id,
                    guild_id,
                    cancelled_by,
                )
                if giveaway is not None:
                    await connection.execute(
                        """UPDATE giveaway_winners SET invalidated_at=NOW(),
                        invalidation_reason='Розыгрыш отменён'
                        WHERE giveaway_id=$1 AND invalidated_at IS NULL""",
                        giveaway_id,
                    )
                return giveaway

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

    async def get_dm_notifications_enabled(self) -> bool:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        enabled = await self.pool.fetchval(
            "SELECT enabled FROM dm_notification_settings WHERE id=1"
        )
        return enabled is not False

    async def set_dm_notifications_enabled(
        self, enabled: bool, updated_by: int
    ) -> None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        await self.pool.execute(
            """INSERT INTO dm_notification_settings(id,enabled,updated_by)
            VALUES(1,$1,$2) ON CONFLICT(id) DO UPDATE SET
                enabled=EXCLUDED.enabled,updated_by=EXCLUDED.updated_by,
                updated_at=NOW()""",
            enabled, updated_by,
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
            """SELECT COALESCE(MAX(access_level),0) FROM (
                SELECT guild_id,role_id,access_level FROM access_level_roles
                UNION ALL
                SELECT guild_id,role_id,access_level FROM application_staff_roles
            ) AS roles
            WHERE guild_id=$1 AND role_id=ANY($2::BIGINT[])""",
            guild_id, list(role_ids),
        ))

    async def remove_deleted_role_references(self, guild_id: int, role_id: int) -> None:
        """Remove/disable every configurable reference to a deleted Discord role."""
        if self.pool is None:
            return
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    "DELETE FROM access_level_roles WHERE guild_id=$1 AND role_id=$2",
                    guild_id, role_id,
                )
                await connection.execute(
                    "DELETE FROM application_staff_roles WHERE guild_id=$1 AND role_id=$2",
                    guild_id, role_id,
                )
                await connection.execute(
                    """DELETE FROM department_config WHERE guild_id=$1
                    AND (member_role_id=$2 OR head_role_id=$2)""", guild_id, role_id,
                )
                await connection.execute(
                    """UPDATE game_servers SET role_id=NULL,updated_at=NOW()
                    WHERE guild_id=$1 AND role_id=$2""",
                    guild_id, role_id,
                )
                await connection.execute(
                    """UPDATE families SET role_id=NULL,active=FALSE,updated_at=NOW()
                    WHERE guild_id=$1 AND role_id=$2""",
                    guild_id, role_id,
                )
                await connection.execute(
                    """UPDATE giveaways SET required_role_id=NULL
                    WHERE guild_id=$1 AND required_role_id=$2
                    AND status IN('active','drawing','claiming')""",
                    guild_id, role_id,
                )

    async def record_protected_log_messages(
        self, guild_id: int, channel_id: int, message_ids: list[int]
    ) -> None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        await self.pool.executemany(
            """INSERT INTO protected_log_messages(message_id,guild_id,channel_id)
            VALUES($1,$2,$3) ON CONFLICT DO NOTHING""",
            [(message_id, guild_id, channel_id) for message_id in message_ids],
        )

    async def get_protected_log_message_ids(
        self, guild_id: int, channel_id: int, message_ids: set[int]
    ) -> set[int]:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        return {
            row["message_id"] for row in await self.pool.fetch(
                """SELECT message_id FROM protected_log_messages
                WHERE guild_id=$1 AND channel_id=$2 AND message_id=ANY($3::BIGINT[])""",
                guild_id, channel_id, list(message_ids),
            )
        }

    async def prune_protected_log_messages(self, before_id: int) -> None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        await self.pool.execute("DELETE FROM protected_log_messages WHERE message_id<$1", before_id)

    async def record_discord_link_violation(
        self, guild_id: int, member_id: int, message_id: int,
        occurred_at: datetime, since: datetime,
    ) -> bool | None:
        """Return whether this is a repeat, or None for an already handled message."""
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                # Serialize simultaneous messages, including across connections.
                await connection.execute(
                    "SELECT pg_advisory_xact_lock($1)", member_id,
                )
                inserted = await connection.fetchval(
                    """INSERT INTO discord_link_violations
                    (guild_id,member_id,message_id,occurred_at) VALUES($1,$2,$3,$4)
                    ON CONFLICT DO NOTHING RETURNING message_id""",
                    guild_id, member_id, message_id, occurred_at,
                )
                if inserted is None:
                    return None
                return await connection.fetchval(
                    """SELECT EXISTS(SELECT 1 FROM discord_link_violations
                    WHERE guild_id=$1 AND member_id=$2 AND message_id<>$3
                    AND occurred_at >= $4 AND occurred_at <= $5)""",
                    guild_id, member_id, message_id, since, occurred_at,
                )

    async def record_security_role_removals(
        self, guild_id: int, member_id: int, role_ids: list[int], removed_at: datetime
    ) -> None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        await self.pool.executemany(
            """INSERT INTO security_role_removals(guild_id,member_id,role_id,removed_at)
            VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING""",
            [(guild_id, member_id, role_id, removed_at) for role_id in role_ids],
        )

    async def get_security_role_removals(
        self, guild_id: int, member_id: int, since: datetime
    ) -> list[asyncpg.Record]:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        return await self.pool.fetch(
            """SELECT role_id,removed_at FROM security_role_removals
            WHERE guild_id=$1 AND member_id=$2 AND removed_at >= $3""",
            guild_id, member_id, since,
        )

    async def get_nickname_warning(self, guild_id: int, member_id: int) -> asyncpg.Record | None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        return await self.pool.fetchrow(
            "SELECT * FROM nickname_warnings WHERE guild_id=$1 AND member_id=$2", guild_id, member_id,
        )

    async def save_nickname_warning(
        self, guild_id: int, member_id: int, nickname: str, deadline: datetime
    ) -> asyncpg.Record:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        return await self.pool.fetchrow(
            """INSERT INTO nickname_warnings(guild_id,member_id,nickname,deadline)
            VALUES($1,$2,$3,$4) ON CONFLICT(guild_id,member_id)
            DO UPDATE SET nickname=EXCLUDED.nickname RETURNING *""",
            guild_id, member_id, nickname, deadline,
        )

    async def clear_nickname_warning(self, guild_id: int, member_id: int) -> None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        await self.pool.execute(
            "DELETE FROM nickname_warnings WHERE guild_id=$1 AND member_id=$2", guild_id, member_id,
        )

    async def mark_nickname_penalty(self, guild_id: int, member_id: int) -> None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        await self.pool.execute(
            "UPDATE nickname_warnings SET penalized_at=NOW() WHERE guild_id=$1 AND member_id=$2",
            guild_id, member_id,
        )

    async def get_due_nickname_warnings(self, guild_id: int) -> list[asyncpg.Record]:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        return await self.pool.fetch(
            "SELECT * FROM nickname_warnings WHERE guild_id=$1 AND deadline<=NOW() AND penalized_at IS NULL",
            guild_id,
        )

    async def clear_security_role_removals(self, guild_id: int, member_id: int) -> None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        await self.pool.execute(
            "DELETE FROM security_role_removals WHERE guild_id=$1 AND member_id=$2",
            guild_id, member_id,
        )

    async def get_application_staff_roles(self, guild_id: int) -> list[asyncpg.Record]:
        if self.pool is None:
            return []
        return await self.pool.fetch(
            """SELECT r.*,s.name AS server_name FROM application_staff_roles r
            JOIN game_servers s ON s.id=r.server_id AND s.guild_id=r.guild_id
            WHERE r.guild_id=$1 ORDER BY s.name,r.access_level""", guild_id,
        )

    async def set_application_staff_roles(
        self, guild_id: int, server_id: int, recruiter_role_id: int, high_role_id: int
    ) -> None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                server = await connection.fetchval(
                    "SELECT id FROM game_servers WHERE guild_id=$1 AND id=$2 FOR UPDATE",
                    guild_id, server_id,
                )
                if server is None:
                    raise ValueError("Game server no longer exists")
                await connection.execute(
                    "DELETE FROM application_staff_roles WHERE guild_id=$1 AND server_id=$2",
                    guild_id, server_id,
                )
                await connection.executemany(
                    """INSERT INTO application_staff_roles(guild_id,server_id,role_id,access_level)
                    VALUES($1,$2,$3,$4)""",
                    [(guild_id, server_id, recruiter_role_id, 2),
                     (guild_id, server_id, high_role_id, 4)],
                )

    async def get_ticket_applications(self, guild_id: int) -> list[asyncpg.Record]:
        if self.pool is None:
            return []
        return await self.pool.fetch(
            """SELECT * FROM applications WHERE guild_id=$1
            AND ticket_channel_id IS NOT NULL ORDER BY id""", guild_id,
        )

    async def get_game_servers(self, guild_id: int) -> list[asyncpg.Record]:
        if self.pool is None:
            return []
        return await self.pool.fetch(
            "SELECT * FROM game_servers WHERE guild_id=$1 ORDER BY name", guild_id
        )

    async def get_game_server(self, guild_id: int, server_id: int) -> asyncpg.Record | None:
        if self.pool is None:
            return None
        return await self.pool.fetchrow(
            "SELECT * FROM game_servers WHERE guild_id=$1 AND id=$2", guild_id, server_id
        )

    async def upsert_game_server(
        self, guild_id: int, name: str, role_id: int | None, added_by: int
    ) -> asyncpg.Record:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        existing = await self.pool.fetchrow(
            "SELECT id FROM game_servers WHERE guild_id=$1 AND LOWER(name)=LOWER($2)",
            guild_id, name,
        )
        if existing is not None:
            return await self.pool.fetchrow(
                """UPDATE game_servers SET name=$2,role_id=$3,updated_at=NOW()
                WHERE id=$1 RETURNING *""", existing["id"], name, role_id,
            )
        return await self.pool.fetchrow(
            """INSERT INTO game_servers(guild_id,name,role_id,added_by)
            VALUES($1,$2,$3,$4) RETURNING *""", guild_id, name, role_id, added_by,
        )

    async def delete_game_server(self, guild_id: int, name: str) -> asyncpg.Record | None:
        if self.pool is None:
            return None
        return await self.pool.fetchrow(
            """DELETE FROM game_servers WHERE guild_id=$1 AND LOWER(name)=LOWER($2)
            RETURNING *""", guild_id, name,
        )

    async def get_families(self, guild_id: int, *, active_only: bool = True) -> list[asyncpg.Record]:
        if self.pool is None:
            return []
        suffix = " AND active=TRUE" if active_only else ""
        return await self.pool.fetch(
            f"SELECT * FROM families WHERE guild_id=$1{suffix} ORDER BY name", guild_id
        )

    async def get_family(self, guild_id: int, family_id: int) -> asyncpg.Record | None:
        if self.pool is None:
            return None
        return await self.pool.fetchrow(
            "SELECT * FROM families WHERE guild_id=$1 AND id=$2 AND active=TRUE",
            guild_id, family_id,
        )

    async def get_family_by_name(self, guild_id: int, name: str) -> asyncpg.Record | None:
        if self.pool is None:
            return None
        return await self.pool.fetchrow(
            """SELECT * FROM families WHERE guild_id=$1 AND LOWER(name)=LOWER($2)
            AND active=TRUE""", guild_id, name,
        )

    async def upsert_family(
        self, guild_id: int, name: str, role_id: int, added_by: int
    ) -> asyncpg.Record:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        existing = await self.pool.fetchrow(
            "SELECT id FROM families WHERE guild_id=$1 AND LOWER(name)=LOWER($2)",
            guild_id, name,
        )
        if existing is not None:
            return await self.pool.fetchrow(
                """UPDATE families SET name=$2,role_id=$3,active=TRUE,updated_at=NOW()
                WHERE id=$1 RETURNING *""", existing["id"], name, role_id,
            )
        return await self.pool.fetchrow(
            """INSERT INTO families(guild_id,name,role_id,added_by)
            VALUES($1,$2,$3,$4) RETURNING *""", guild_id, name, role_id, added_by,
        )

    async def delete_family(self, guild_id: int, name: str) -> asyncpg.Record | None:
        if self.pool is None:
            return None
        return await self.pool.fetchrow(
            """UPDATE families SET active=FALSE,updated_at=NOW()
            WHERE guild_id=$1 AND LOWER(name)=LOWER($2) AND active=TRUE RETURNING *""",
            guild_id, name,
        )

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

    async def create_mafia_game(self, guild_id: int, channel_id: int, state: dict) -> int:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        return await self.pool.fetchval(
            """INSERT INTO mafia_games(guild_id,channel_id,state)
            VALUES($1,$2,$3::JSONB) RETURNING id""",
            guild_id, channel_id, json.dumps(state),
        )

    async def get_mafia_game(self, guild_id: int, game_id: int | None = None) -> dict | None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        row = await self.pool.fetchrow(
            """SELECT * FROM mafia_games WHERE guild_id=$1
            AND (($2::BIGINT IS NULL AND active=TRUE) OR id=$2)
            ORDER BY id DESC LIMIT 1""", guild_id, game_id,
        )
        if row is None:
            return None
        result = dict(row)
        result["state"] = json.loads(result["state"])
        return result

    @asynccontextmanager
    async def mafia_transaction(self, guild_id: int, game_id: int):
        """Serialize actions/timers and commit state with final results atomically."""
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    "SELECT * FROM mafia_games WHERE guild_id=$1 AND id=$2 FOR UPDATE",
                    guild_id, game_id,
                )
                if row is None:
                    yield None
                    return
                game = dict(row)
                game["state"] = json.loads(game["state"])
                original = json.dumps(game["state"], sort_keys=True)
                old_public = self.mafia_public_state(game["state"])
                yield game
                state = game["state"]
                if json.dumps(state, sort_keys=True) == original:
                    return
                active = state["phase"] not in {"finished", "cancelled"}
                if not active and row['active'] and (
                    state.get('rooms_enabled') or state.get('room_key') or state.get('rooms')
                ):
                    state['cleanup_at'] = datetime.now(timezone.utc).timestamp() + MAFIA_CLEANUP_DELAY_SECONDS
                channel_id = state.get("rooms", {}).get("events", game["channel_id"])
                await connection.execute(
                    """UPDATE mafia_games SET state=$2::JSONB,active=$3,
                    revision=revision+1,
                    message_id=CASE WHEN channel_id<>$4 THEN NULL ELSE message_id END,
                    message_revision=CASE WHEN channel_id<>$4 THEN 0 ELSE message_revision END,
                    channel_id=$4 WHERE id=$1""",
                    game_id, json.dumps(state), active, channel_id,
                )
                public = self.mafia_public_state(state)
                if public != old_public:
                    await connection.execute(
                        "INSERT INTO mafia_events(game_id,payload) VALUES($1,$2::JSONB)",
                        game_id, json.dumps(public),
                    )
                for event in personal_events(json.loads(original), state):
                    await connection.execute(
                        'INSERT INTO mafia_notifications(game_id,discord_id,payload) VALUES($1,$2,$3::JSONB)',
                        game_id, event['user_id'], json.dumps(event),
                    )
                if state["phase"] == "finished":
                    for uid, player in state["players"].items():
                        team = "mafia" if player["role"] == "mafia" else "town"
                        won = team == state["winner"]
                        await connection.execute(
                            """INSERT INTO mafia_results(
                                game_id,guild_id,discord_id,role,won,survived,points
                            ) VALUES($1,$2,$3,$4,$5,$6,$7)
                            ON CONFLICT(game_id,discord_id) DO NOTHING""",
                            game_id, guild_id, int(uid), player["role"], won,
                            player["alive"], 3 if won else 1,
                        )

    async def pending_mafia_games(self, guild_id: int) -> list[asyncpg.Record]:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        return await self.pool.fetch(
            """SELECT id FROM mafia_games WHERE guild_id=$1
            AND (active=TRUE
                OR EXISTS(SELECT 1 FROM mafia_role_holds h WHERE h.game_id=mafia_games.id AND h.restored_at IS NULL)
                OR (NOT COALESCE((state->>'rooms_deleted')::BOOLEAN,FALSE) AND (
                    message_revision<revision OR state ? 'cleanup_at'
                    OR EXISTS(SELECT 1 FROM mafia_events e WHERE e.game_id=mafia_games.id AND e.message_id IS NULL))))
            ORDER BY id""", guild_id,
        )

    async def set_mafia_message(self, game_id: int, message_id: int, revision: int) -> None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        await self.pool.execute(
            "UPDATE mafia_games SET message_id=$2,message_revision=$3 WHERE id=$1",
            game_id, message_id, revision,
        )

    async def mafia_leaderboard(
        self, guild_id: int, metric: str = "points", days: int = 0,
        user_id: int | None = None,
    ) -> list[asyncpg.Record]:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        orders = {
            "points": "points DESC,wins DESC,games DESC",
            "wins": "wins DESC,win_rate DESC,games DESC",
            "games": "games DESC,wins DESC,points DESC",
            "win_rate": "win_rate DESC,wins DESC,games DESC",
            "mafia_wins": "mafia_wins DESC,wins DESC,games DESC",
            "town_wins": "town_wins DESC,wins DESC,games DESC",
            "survived": "survived DESC,wins DESC,games DESC",
        }
        if metric not in orders or days not in {0, 7, 30}:
            raise ValueError("Invalid leaderboard filter")
        minimum = 5 if metric == "win_rate" else 1
        return await self.pool.fetch(
            f"""SELECT discord_id,COUNT(*) AS games,
            COUNT(*) FILTER(WHERE won) AS wins,
            COUNT(*) FILTER(WHERE won AND role='mafia') AS mafia_wins,
            COUNT(*) FILTER(WHERE won AND role<>'mafia') AS town_wins,
            COUNT(*) FILTER(WHERE survived) AS survived,
            SUM(points) AS points,
            ROUND(100.0*COUNT(*) FILTER(WHERE won)/COUNT(*),1) AS win_rate
            FROM mafia_results WHERE guild_id=$1
            AND ($2::INTEGER=0 OR finished_at>=NOW()-($2*INTERVAL '1 day'))
            AND ($3::BIGINT IS NULL OR discord_id=$3)
            GROUP BY discord_id HAVING COUNT(*) >= $4
            ORDER BY {orders[metric]},discord_id LIMIT 10""",
            guild_id, days, user_id, minimum,
        )

    @staticmethod
    def mafia_public_state(state: dict) -> dict:
        return {key: state[key] for key in ('phase', 'round', 'event', 'paused', 'winner')}

    async def get_mafia_saved_access(self, guild_id: int, discord_id: int) -> int:
        if self.pool is None:
            return 0
        return await self.pool.fetchval(
            """SELECT COALESCE(MAX(access_level),0) FROM mafia_role_holds
            WHERE guild_id=$1 AND discord_id=$2 AND restored_at IS NULL""",
            guild_id, discord_id,
        )

    async def hold_mafia_roles(self, game_id: int, guild_id: int, discord_id: int,
                               role_ids: list[int], access_level: int) -> None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        await self.pool.execute(
            """INSERT INTO mafia_role_holds(game_id,guild_id,discord_id,role_ids,access_level)
            VALUES($1,$2,$3,$4,$5) ON CONFLICT(game_id,discord_id) DO UPDATE SET
            role_ids=ARRAY(SELECT DISTINCT unnest(mafia_role_holds.role_ids || EXCLUDED.role_ids)),
            access_level=GREATEST(mafia_role_holds.access_level,EXCLUDED.access_level),restored_at=NULL""",
            game_id, guild_id, discord_id, role_ids, access_level,
        )

    async def get_mafia_role_holds(self, game_id: int) -> list[asyncpg.Record]:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        return await self.pool.fetch(
            "SELECT * FROM mafia_role_holds WHERE game_id=$1 AND restored_at IS NULL", game_id,
        )

    async def finish_mafia_role_hold(self, game_id: int, discord_id: int) -> None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                result = await connection.execute(
                    """UPDATE mafia_role_holds SET restored_at=NOW()
                    WHERE game_id=$1 AND discord_id=$2 AND restored_at IS NULL""",
                    game_id, discord_id,
                )
                if result != 'UPDATE 0':
                    await connection.execute('UPDATE mafia_games SET revision=revision+1 WHERE id=$1', game_id)

    async def has_other_mafia_role_hold(self, guild_id: int, discord_id: int, game_id: int) -> bool:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        return await self.pool.fetchval(
            """SELECT EXISTS(SELECT 1 FROM mafia_role_holds
            WHERE guild_id=$1 AND discord_id=$2 AND game_id<>$3 AND restored_at IS NULL)""",
            guild_id, discord_id, game_id,
        )

    async def get_mafia_events(self, game_id: int) -> list[asyncpg.Record]:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        return await self.pool.fetch(
            "SELECT * FROM mafia_events WHERE game_id=$1 AND message_id IS NULL ORDER BY id", game_id,
        )

    async def mark_mafia_event_sent(self, event_id: int, message_id: int) -> None:
        if self.pool is None:
            raise RuntimeError("Database is not connected")
        await self.pool.execute("UPDATE mafia_events SET message_id=$2 WHERE id=$1", event_id, message_id)

    async def get_mafia_notifications(self, guild_id: int) -> list[asyncpg.Record]:
        if self.pool is None:
            raise RuntimeError('Database is not connected')
        return await self.pool.fetch(
            '''SELECT n.* FROM mafia_notifications n JOIN mafia_games g ON g.id=n.game_id
            WHERE g.guild_id=$1 AND n.processed_at IS NULL AND n.next_attempt_at<=NOW()
            ORDER BY n.id LIMIT 100''', guild_id,
        )

    async def finish_mafia_notification(self, notification_id: int, message_id: int | None = None) -> None:
        if self.pool is None:
            raise RuntimeError('Database is not connected')
        await self.pool.execute(
            'UPDATE mafia_notifications SET processed_at=NOW(),message_id=$2 WHERE id=$1',
            notification_id, message_id,
        )

    async def retry_mafia_notification(self, notification_id: int) -> None:
        if self.pool is None:
            raise RuntimeError('Database is not connected')
        await self.pool.execute(
            """UPDATE mafia_notifications SET attempts=attempts+1,
            next_attempt_at=NOW()+INTERVAL '30 seconds',
            processed_at=CASE WHEN attempts>=2 THEN NOW() ELSE NULL END WHERE id=$1""",
            notification_id,
        )
