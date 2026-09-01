REVOKE CONNECT, TEMPORARY ON DATABASE discord_stats_prod FROM PUBLIC;
REVOKE ALL ON DATABASE discord_stats_prod FROM kanami_web_readonly;
GRANT CONNECT ON DATABASE discord_stats_prod TO kanami_web_readonly;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM kanami_web_readonly;
GRANT USAGE ON SCHEMA public TO kanami_web_readonly;

GRANT SELECT ON TABLE
    guilds,
    discord_users,
    guild_members,
    voice_channels,
    voice_sessions,
    voice_intervals,
    daily_text_activity,
    audit_events,
    user_achievements,
    web_admin_access_grants,
    rulesets,
    rule_acceptances,
    guild_server_settings,
    game_sessions,
    operational_health_observations
TO kanami_web_readonly;

GRANT INSERT, UPDATE, DELETE ON TABLE rulesets TO kanami_web_readonly;
GRANT INSERT ON TABLE audit_events TO kanami_web_readonly;
GRANT USAGE, SELECT ON SEQUENCE rulesets_id_seq, audit_events_id_seq
TO kanami_web_readonly;
