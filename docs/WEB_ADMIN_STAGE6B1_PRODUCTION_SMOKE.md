# Web Admin Stage 6B.1: production smoke

## Проверенное состояние

Production deployment и smoke выполнены **2026-08-22 UTC** на хосте
`production-host`.

- deployed Git revision: `20dbd6a` (`Add server settings foundation`);
- предыдущая production revision: `002d5fb`;
- обновление выполнено fast-forward;
- Alembic до deployment: `8d44cacc791e`;
- Alembic после deployment: `3e7b9c2a6f41` (`head`).

Stage 6B.1 на revision `20dbd6a` объединён с `main`; GitHub CI завершился
успешно. `pyproject.toml` и `uv.lock` между production revisions не менялись.

## Pre-deployment и rollback checkpoint

Перед deployment подтверждены чистый production worktree, Git HEAD
`002d5fb`, целевой `origin/main` `20dbd6a`, оба активных service и отсутствие
failed systemd units. Все четыре существующих env baseline были настроены:
autorole, audit log channel, anniversary channel и return channel. Bot Control
был включён только на `127.0.0.1:8765`, а Web Admin использовал loopback Bot
Control URL. Shared secrets были настроены, но их значения не выводились.

Создан rollback checkpoint:

```text
/root/kanami-stage6b1-20260822-212910
```

В checkpoint сохранены старый Git HEAD, `git status`, `kanami.env`,
`kanami-web-admin.env`, PostgreSQL dump в custom format, список `pg_restore` и
SHA-256 dump. Каталог имел mode `700`, sensitive backup files — `600`.
`pg_restore --list` успешно прочитал dump. Содержимое env-файлов и database dump
в этот документ не включено. Свободного места было достаточно; rollback не
потребовался.

## Migration

После fast-forward production checkout находился на `20dbd6a`, а code Alembic
head был равен `3e7b9c2a6f41`. К production DB применён переход:

```text
8d44cacc791e -> 3e7b9c2a6f41
add guild server settings
```

После migration production DB находилась на `3e7b9c2a6f41 (head)`. Создана
таблица `guild_server_settings`; сразу после migration в ней было `0` строк.
Migration самостоятельно не создала DB overrides, поэтому существующие env
значения остались baseline/default и production-поведение не изменилось.

## Bot startup

После restart `kanami.service`:

- service остался active;
- application configuration успешно validated;
- effective состояния `autorole`, `audit_log`, `anniversaries` и
  `member_returns` были `enabled`;
- Bot Control был включён на `127.0.0.1:8765`;
- синхронизировано 12 Discord application commands;
- Discord Gateway подключился;
- reference provisioning завершился с `users=109`, `members=109` и
  `voice_channels=13`;
- Voice startup reconciliation завершился с `connected=7`, `disconnected=0`,
  `joined=0`, `moved=0`, `left=0`, `unchanged=7`, `stale=0`, `failed=0`.

После startup таблица `guild_server_settings` по-прежнему содержала `0` строк.
Это подтвердило backward compatibility: при отсутствии DB overrides все четыре
feature получили effective settings из env baseline.

В journal присутствовали ожидаемые warnings зависимостей PyNaCl/davey; они не
являлись ошибкой Stage 6B.1.

## Web Admin startup и network security

После restart `kanami-web-admin.service` service остался active, а application
startup завершился успешно. Uvicorn слушал explicitly configured private bind:

```text
http://192.168.50.10:8000
```

Ожидаемый warning о private non-loopback bind присутствовал. Локальный запрос
`/admin/` вернул `HTTP 303` с redirect на `/admin/login`. В ответе были CSP,
`Permissions-Policy`, `Referrer-Policy`, `X-Content-Type-Options` и
`X-Frame-Options`.

Публичный HTTPS endpoint `https://kanami.example.com/admin/` вернул `HTTP/2 303`
с `Location: /admin/login`. На HTTPS boundary подтверждены nginx, HSTS, CSP,
`Permissions-Policy`, `Referrer-Policy`, `X-Content-Type-Options` и
`X-Frame-Options`.

OAuth state/codes/cookies, OAuth и Bot Control secrets, database URL и session
secrets в проверочные результаты не включены.

## Финальное read-only состояние

До Bot Control smoke повторно подтверждены:

- Git revision `20dbd6a`;
- Alembic `3e7b9c2a6f41 (head)`;
- `guild_server_settings`: `0` строк;
- оба service active;
- failed systemd units: `0`;
- в recent logs отсутствовали `ERROR`, `CRITICAL`, `Exception` и `Traceback`.

## Bot Control no-op smoke

В production выполнен безопасный smoke нового фиксированного endpoint:

```text
POST /control/v1/server-settings
```

Trusted actor был существующим permanent OWNER; его Discord ID не
документируется. Использован payload:

```json
{"setting":"audit_log_channel","mode":"env"}
```

До вызова `guild_server_settings` содержала `0` строк, а число
`web_admin.server_setting_changed` events было равно `0`. Endpoint вернул:

```text
HTTP 200
```

```json
{"changed":false,"setting":"audit_log_channel","mode":"env","value":null}
```

После вызова число строк и audit events осталось равным нулю:

```text
HTTP_OK=true
DB_ROWS_UNCHANGED=true
AUDIT_COUNT_UNCHANGED=true
```

Bot log подтвердил успешную операцию для `setting=audit_log_channel`,
`mode=env`, `changed=False`; actor Discord ID намеренно не зафиксирован.

Smoke подтвердил authentication path Bot Control, trusted actor header,
server-settings endpoint, Discord-side validation env baseline target и точное
распознавание configured-state no-op. No-op не создал DB row или audit event и
не изменил production Discord configuration.

## Заключение

**Stage 6B.1 production-validated.**

Подтверждены migration, tri-state server-settings persistence foundation,
backward-compatible env fallback при нулевом числе overrides, startup с
effective settings, фиксированный Bot Control server-settings endpoint и no-op
семантика без лишней DB/audit записи. После deployment bot и Web Admin работали
штатно.

Web Admin UI для server settings в Stage 6B.1 не реализован; это следующий
Stage 6B.2.
