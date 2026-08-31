# Разработка

## Быстрый старт

Нужны Python 3.13 и [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/lDiestrol/kanami.git
cd discord-bot
uv sync
```

Создайте локальный `.env` на основе `.env.example`, если запускаете application.
Не коммитьте реальные credentials.

```bash
uv run python -m discord_stats_bot
```

## Проверки

```bash
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv lock --check
git diff --check
```

Обычный test suite не требует PostgreSQL. Integration tests запускаются только
при заданном `TEST_DATABASE_URL` на отдельную disposable test database:

```bash
TEST_DATABASE_URL='postgresql+asyncpg://user:password@127.0.0.1:5432/kanami_test' \
  uv run pytest -m integration
```

Никогда не направляйте integration tests на production database.

GitHub Actions автоматически запускает тот же набор проверок для push и pull
request в `main` на Python 3.13. CI поднимает disposable PostgreSQL 17 и передаёт
`TEST_DATABASE_URL` только на временную CI database, поэтому полный `pytest`
включает PostgreSQL integration tests без production credentials. Приведённые
выше команды приблизительно соответствуют CI; для полного локального прогона
дополнительно задайте `TEST_DATABASE_URL` на отдельную disposable test database.

## Миграции

Application startup не применяет Alembic автоматически:

```bash
DATABASE_URL='postgresql+asyncpg://user:password@127.0.0.1:5432/kanami_dev' \
  uv run alembic upgrade head
uv run alembic history
```

Изменения SQLAlchemy schema должны сопровождаться review Alembic migration и
model/migration tests. Работайте в отдельной feature branch; commit/push и
deployment выполняются осознанно после review.
