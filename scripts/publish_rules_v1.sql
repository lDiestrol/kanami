\set ON_ERROR_STOP on

-- Run only after the Rules v1 migration, passing the configured guild ID:
-- psql "postgresql://..." -v guild_id=123456789012345678 -f scripts/publish_rules_v1.sql
-- (psql needs a PostgreSQL DSN without SQLAlchemy's +asyncpg driver suffix.)
-- This is an initial-only bootstrap, not a general publication mechanism.
-- It fails before INSERT if the guild is missing or has any ruleset already.

BEGIN;

SELECT set_config('kanami.rules_bootstrap_guild_id', :'guild_id', true);

DO $bootstrap$
DECLARE
    v_guild_id BIGINT := current_setting('kanami.rules_bootstrap_guild_id')::BIGINT;
BEGIN
    PERFORM id
    FROM guilds
    WHERE id = v_guild_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Rules v1 bootstrap guild % does not exist', v_guild_id;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM rulesets
        WHERE guild_id = v_guild_id
    ) THEN
        RAISE EXCEPTION
            'Rules v1 bootstrap refused: guild % already has a ruleset',
            v_guild_id;
    END IF;
END;
$bootstrap$;

INSERT INTO rulesets (
    guild_id,
    version,
    title,
    content,
    status,
    change_summary,
    requires_reacceptance,
    created_by,
    created_at,
    published_at
)
VALUES (
    current_setting('kanami.rules_bootstrap_guild_id')::BIGINT,
    '1.0',
    'Правила сервера',
    $rules$
Пирожочек, прежде чем вступить на этот сервер, пожалуйста, ознакомься с правилами:

1. Уважай других участников сервера.

2. Воздерживайся от токсичности, провокаций и намеренного разжигания конфликтов.

3. Не поднимай острые и потенциально конфликтные темы, такие как религия, политика и т. п.

4. Оскорбления на расовой, национальной, религиозной и иной дискриминационной основе строго запрещены.

5. Не спамь, не флуди и не злоупотребляй массовыми упоминаниями участников.

6. NSFW-контент допускается только в специально отведённом для этого канале. Публикация такого контента в остальных каналах запрещена. Даже в NSFW-канале запрещён контент, нарушающий правила Discord или законодательство.

7. Не используй сервер для нежелательной рекламы, массовых рассылок и приглашений на сторонние проекты без согласования с администрацией.

Правила нужны не для того, чтобы ограничивать общение, а чтобы всем на сервере было комфортно. ❤️

Нажимая кнопку «✅ Принимаю правила», ты подтверждаешь, что ознакомился с правилами сервера и согласен их соблюдать.
$rules$,
    'published',
    'Первоначальная редакция',
    false,
    NULL,
    CURRENT_TIMESTAMP,
    CURRENT_TIMESTAMP
);

COMMIT;
