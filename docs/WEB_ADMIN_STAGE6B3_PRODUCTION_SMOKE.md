# Web Admin Stage 6B.3: production smoke

## Проверенное состояние

Stage 6B.3 развёрнут и production-validated на хосте `production-host`.

- deployed Git revision: `599584a` (`Polish server settings admin UI`);
- предыдущая production revision: `cb72124`
  (`Document Stage 6B.2 production smoke`);
- deployment выполнен fast-forward: `cb72124 -> 599584a`;
- dependency changes отсутствовали;
- migration changes отсутствовали;
- Alembic после deployment: `3e7b9c2a6f41 (head)`.

Stage объединён с `main`; перед deployment `main == origin/main == 599584a`.
Stage 6B.3 не менял write semantics, database schema, environment contract или
security boundaries Stage 6B.2.

## Local validation и CI

Перед commit targeted tests завершились результатом `43 passed`. Расширенный
Web Admin/server-settings regression без `TEST_DATABASE_URL` дал
`251 passed, 2 skipped`. Финальный полный suite с PostgreSQL 17 и реальным
`TEST_DATABASE_URL` завершился результатом:

```text
932 passed, 0 skipped, 29 warnings
```

После проверки `TEST_DATABASE_URL` был удалён из PowerShell environment. Сам URL
и PostgreSQL credentials не документируются.

Финальные локальные gates:

- Ruff format: `189 files already formatted`;
- Ruff check: `All checks passed!`;
- `uv lock --check`: успешно, 44 packages;
- `git diff --check`: успешно;
- UTF-8 strict, final newline и отсутствие trailing whitespace подтверждены;
- BOM и mojibake отсутствовали;
- secret-like и production snowflake-like additions отсутствовали.

Feature branch содержала один commit и пять изменённых файлов. GitHub PR не имел
конфликтов, единственная CI-проверка завершилась успешно, после чего commit был
fast-forward объединён в `main` и опубликован.

## Pre-deployment

Preflight подтвердил точный deploy diff из пяти файлов:

```text
docs/STATUS.md
src/discord_stats_bot/web/audit_log.py
src/discord_stats_bot/web/server_settings_page.py
tests/test_web_admin_audit_log.py
tests/test_web_server_settings.py
```

Dependency и migration files не менялись. До deployment:

- production Git revision: `cb72124`;
- Alembic: `3e7b9c2a6f41 (head)`;
- Web Admin runtime baseline key count: 4;
- `kanami` и `kanami-web-admin` active;
- Web Admin слушал `192.168.50.10:8000`;
- Bot Control слушал только `127.0.0.1:8765`;
- failed systemd units: 0;
- recent `ERROR`/`CRITICAL`/`Exception`/`Traceback`: отсутствовали.

Pre-deploy database snapshot содержал одну строку `guild_server_settings` и пять
событий `web_admin.server_setting_changed`. Все пять относились к
`autorole_role`; persisted transitions в newest-first порядке были:

```text
env -> value
value -> env
value -> value
value -> value
env -> value
```

Оба `value -> value` были реальными изменениями выбранного Discord value, а не
no-op. Конкретные значения, Discord IDs и имя production-роли не
документируются. Последний state до deployment оставался active DB autorole
override.

## Rollback checkpoint

До `git pull` создан rollback checkpoint:

```text
/root/kanami-stage6b3-20260822-225738
```

Checkpoint содержит pre-deploy Git HEAD/status, оба service env-файла,
PostgreSQL custom-format dump, список `pg_restore` и SHA-256 dump. Каталог имел
mode `700`, sensitive files — `600`. `pg_restore` list содержал 100 entries, а
database dump был ненулевого размера. Содержимое env-файлов и dump не
документируется.

## Deployment

Production checkout обновлён fast-forward:

```text
cb72124 -> 599584a
```

После pull `main == origin/main == 599584a`. Dependencies и migrations не
менялись, поэтому dependency/migration operations не выполнялись, а Alembic
остался на `3e7b9c2a6f41 (head)`.

## Web Admin restart

Stage 6B.3 затрагивал только Web Admin runtime code, поэтому перезапущен только
`kanami-web-admin.service`. Bot process не перезапускался: его PID остался
неизменным, тогда как PID Web Admin изменился.

После restart:

- `kanami` и `kanami-web-admin` active;
- failed systemd units: 0;
- listeners остались `192.168.50.10:8000` и loopback-only
  `127.0.0.1:8765`;
- anonymous `GET /admin/server-settings` вернул `HTTP 303` с redirect на
  `/admin/login`;
- подтверждены `Cache-Control: no-store`, CSP с `script-src 'none'`,
  Permissions-Policy, `Referrer-Policy: no-referrer`, nosniff и frame deny;
- startup завершился успешно;
- recent `ERROR`/`CRITICAL`/`Exception`/`Traceback` отсутствовали.

Ожидаемый warning о deliberately configured private non-loopback bind за
reverse proxy сохранился.

## Server Settings browser smoke

Permanent OWNER открыл `/admin/server-settings` без выполнения write actions.
Production UI подтвердил active DB autorole override:

- source отображался как `Web Admin / DB`;
- current human-readable role отображалась без raw Discord ID;
- этот же effective object имел `selected` в dropdown;
- browser больше не показывал первый реальный option как active по умолчанию.

Видимая channel-настройка с source `ENV` также показывала matching current value
в dropdown.
Имя production autorole и Discord role/channel IDs в документ не включены.

## Audit browser smoke

Permanent OWNER открыл OWNER-only `/admin/audit`. Страница успешно показала
существующие persisted `web_admin.server_setting_changed` со setting label
«Автоматическая роль», action «Изменена настройка» и semantic transitions:

```text
ENV -> Web Admin
Web Admin -> ENV
Web Admin -> Web Admin
Web Admin -> Web Admin
ENV -> Web Admin
```

Renderer использовал persisted `details_data.setting_key` и значения `source`
из `before_data`/`after_data`: `env`, `value` и `disabled`. Поля `value` из этих
snapshot не отображались, поэтому raw server-setting Discord value IDs не были
раскрыты. Runtime enrichment и Discord lookup не использовались; historical
rendering не зависел от Bot Control/options.

Существующие `web_admin.access_granted` и `web_admin.access_revoked` продолжили
отображаться, audit page не вернул 500 и остался OWNER-only. Существующее
отображение actor/target ID для access history Stage 6B.3 не менял; сами IDs в
этот документ не включены.

## Финальное состояние

После read-only browser smoke:

- Git: `599584a`, `main == origin/main`;
- Alembic: `3e7b9c2a6f41 (head)`;
- `guild_server_settings`: 1 row;
- `web_admin.server_setting_changed`: 5 events;
- `kanami` и `kanami-web-admin` active;
- failed systemd units: 0;
- Web Admin listener: `192.168.50.10:8000`;
- Bot Control listener: `127.0.0.1:8765`;
- bot process не перезапускался;
- Web Admin работал новым process после restart;
- recent `ERROR`/`CRITICAL`/`Exception`/`Traceback`: отсутствовали.

Counts до и после browser smoke совпали: одна settings row и пять setting-change
events. Read-only проверка не создала новых DB rows или audit events. Active DB
autorole override остался на месте; возврат к `ENV` не выполнялся и не
утверждается.

## Заключение

**Stage 6B.3 production-validated на revision `599584a`.**

Подтверждены matching selected state для effective DB/ENV объектов,
нейтральный fallback без ложного active option, человекочитаемое persisted
`web_admin.server_setting_changed` presentation и отсутствие raw setting value
ID. Grant/revoke audit history и прежние authorization/security boundaries не
сломаны. Web Admin PostgreSQL connection остаётся read-only, Bot Control —
loopback-only, JavaScript, migrations, dependencies и новые environment
requirements не добавлялись.
