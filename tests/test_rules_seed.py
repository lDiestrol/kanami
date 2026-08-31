from pathlib import Path

SEED_PATH = Path("scripts/publish_rules_v1.sql")


def normalized_seed() -> str:
    return " ".join(SEED_PATH.read_text(encoding="utf-8").split())


def test_initial_seed_locks_existing_guild_and_rejects_missing_guild() -> None:
    sql = normalized_seed()

    assert "BEGIN;" in sql and sql.endswith("COMMIT;")
    assert "FROM guilds WHERE id = v_guild_id FOR UPDATE" in sql
    assert "IF NOT FOUND THEN RAISE EXCEPTION" in sql
    assert "bootstrap guild % does not exist" in sql


def test_repeat_initial_seed_fails_before_insert_without_mutating_rulesets() -> None:
    sql = normalized_seed()
    guard = (
        "IF EXISTS ( SELECT 1 FROM rulesets WHERE guild_id = v_guild_id ) THEN "
        "RAISE EXCEPTION"
    )

    assert guard in sql
    assert sql.index(guard) < sql.index("INSERT INTO rulesets")
    assert sql.count("INSERT INTO rulesets") == 1
    assert "UPDATE rulesets" not in sql
    assert "DELETE FROM rulesets" not in sql
    assert "status = 'archived'" not in sql


def test_initial_seed_remains_fixed_to_unchanged_v1_metadata() -> None:
    sql = normalized_seed()

    assert "'1.0', 'Правила сервера'" in sql
    assert "'published', 'Первоначальная редакция', false" in sql
    assert "Пирожочек, прежде чем вступить на этот сервер" in sql
