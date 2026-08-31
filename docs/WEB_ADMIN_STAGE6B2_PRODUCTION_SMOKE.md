# Web Admin Stage 6B.2: production smoke

## Проверенное состояние

Stage 6B.2 развёрнут и production-validated на хосте `production-host`.

- deployed Git revision: `a585149` (`Add web admin server settings UI`);
- предыдущая production revision: `980233d`
  (`Document Stage 6B.1 production smoke`);
- deployment выполнен fast-forward: `980233d -> a585149`;
- dependency changes отсутствовали;
- migration changes отсутствовали;
- Alembic после deployment: `3e7b9c2a6f41 (head)`.

Stage 6B.2 объединён с `main`; GitHub CI завершился успешно. Перед deployment
полный suite с PostgreSQL 17 и реальным `TEST_DATABASE_URL` завершился
результатом `922 passed, 0 skipped, 29 warnings`.

## Pre-deployment и rollback checkpoint

До deployment создан rollback checkpoint:

```text
/root/kanami-stage6b2-20260822-221125
```

В checkpoint сохранены previous Git HEAD/status, `kanami.env`,
`kanami-web-admin.env`, PostgreSQL dump в custom format, список `pg_restore` и
SHA-256 dump. Каталог имел mode `700`, sensitive files — `600`.
`pg_restore --list` успешно прочитал dump; проверенный `database.list` содержал
100 entries. Содержимое env-файлов, secrets и database dump не документируется.

## Deployment

Production checkout обновлён fast-forward до `a585149`. Dependencies и
migrations не менялись, поэтому Alembic остался на `3e7b9c2a6f41 (head)`.

После restart только `kanami.service`:

- service остался active;
- application configuration успешно validated;
- effective состояния `autorole`, `audit_log`, `anniversaries` и
  `member_returns` были `enabled`;
- Bot Control слушал только `127.0.0.1:8765`;
- синхронизировано 12 Discord application commands;
- Discord Gateway подключился;
- reference provisioning завершился успешно;
- Voice startup reconciliation завершился успешно с `failed=0`.

В journal присутствовали только ожидаемые PyNaCl/davey warnings.

## Bot Control options smoke

Проверен новый read-only endpoint:

```text
GET /control/v1/server-settings/options
```

Запрос без bearer вернул `HTTP 401` и `control_unauthorized`. Запрос с
корректным bearer вернул `HTTP 200`. Safe summary ответа:

```text
TOP_LEVEL_KEYS=channels,roles
ROLES_IS_LIST=True
CHANNELS_IS_LIST=True
ROLE_COUNT=7
CHANNEL_COUNT=11
ROLE_FIELDS_OK=true
CHANNEL_FIELDS_OK=true
```

Подтверждён bounded contract: role содержит только `id`/`name`, channel — только
`id`/`name`/`type`. Конкретные Discord IDs и display names в документ не
включены.

## Web Admin startup и security

После restart `kanami-web-admin.service`:

- service остался active;
- Uvicorn слушал explicitly configured private bind `192.168.50.10:8000`;
- Bot Control остался loopback-only на `127.0.0.1:8765`;
- anonymous local `/admin/server-settings` вернул `HTTP 303` с redirect на
  `/admin/login`;
- anonymous public HTTPS `/admin/server-settings` вернул `HTTP/2 303` с тем же
  redirect;
- подтверждены CSP, no-store, Referrer-Policy, nosniff, frame и
  Permissions-Policy headers;
- на public HTTPS boundary также подтверждён HSTS;
- failed systemd units отсутствовали;
- recent logs не содержали `ERROR`, `CRITICAL`, `Exception` или `Traceback`.

OAuth state/codes, cookies, session и Bot Control secrets, database URL и
production env contents в отчёт не включены.

## ENV baseline configuration finding

При первом browser smoke Server Settings UI показывал все четыре setting как
«Отключено» с source `ENV`, хотя bot runtime показывал все четыре feature как
enabled.

Причиной было разделение environment двух standalone processes. Bot process уже
получал четыре baseline из своего env-файла, а environment Web Admin изначально
не содержал:

- `DISCORD_AUTOROLE_ID`;
- `DISCORD_AUDIT_LOG_CHANNEL_ID`;
- `DISCORD_ANNIVERSARY_CHANNEL_ID`;
- `DISCORD_RETURN_CHANNEL_ID`.

`WebSettings` использует эти baseline для SELECT-only вычисления и отображения
effective source. Web Admin не читает bot env автоматически. Для исправления те
же четыре несекретных Discord configuration baseline были безопасно переданы в
standalone Web Admin env без вывода значений.

После restart Web Admin runtime check подтвердил `RUNTIME_BASELINE_KEY_COUNT=4`,
все четыре baseline были present, permanent OWNER count был равен 2, а Bot
Control configuration — configured. После этого UI корректно показывал
человекочитаемые role/channel display names с source `ENV`.

## Server Settings read smoke

Permanent OWNER вошёл через Discord OAuth. На `/admin/` появилась ссылка
«Настройки сервера»; OWNER-only ссылки «Администраторы» и «Журнал аудита»
остались доступны OWNER.

На `/admin/server-settings` корректно отобразились четыре карточки:

- «Автоматическая роль»;
- «Журнал аудита»;
- «Поздравления с годовщиной»;
- «Возвращения участников».

Все четыре показывали реальные display names current Discord objects и source
`ENV`. Dropdown options загрузились из Bot Control runtime; raw IDs пользователю
не отображались.

## Browser no-op write smoke

До browser write:

```text
SERVER_SETTINGS_ROWS=0
SETTING_CHANGE_AUDIT_EVENTS=0
```

Через UI для autorole выбран режим «Использовать ENV». Точное состояние уже
было `ENV`, поэтому Bot Control вернул `changed=False`, а PRG показал
«Настройка уже имела это значение».

После no-op `guild_server_settings` осталась пустой, а число
`web_admin.server_setting_changed` events осталось равным нулю. Web Admin и Bot
Control logs подтвердили `setting=autorole_role`, `mode=env`, `changed=False`.

Таким образом проверен путь browser session → CSRF → fresh OWNER authorization
→ server-side Bot Control bearer/trusted actor → Discord-side env validation →
no-op → PRG. No-op не создал DB row или audit event и не изменил Discord
configuration. Actor Discord ID не документируется.

## Real value write smoke

После no-op через UI выполнен реальный autorole write в режиме `value`. UI
показал «Настройка сохранена», а после PRG source изменился на `DB`.

Server-side состояние после write:

```text
SERVER_SETTINGS_ROWS=1
SETTING_CHANGE_AUDIT_EVENTS=1
```

Web Admin и Bot Control logs подтвердили `setting=autorole_role`, `mode=value`,
`changed=True`. Проверены browser write, Bot Control mutation, DB override,
единственный audit event, cache invalidation и runtime apply без restart. Имя и
Discord ID выбранной роли не документируются.

## End-to-end autorole smoke

С активным DB autorole override выполнен controlled leave/rejoin участника.
Discord audit/log channel показал последовательность participant left,
participant joined и participant roles updated; выбранная через Web Admin роль
была фактически выдана.

Это подтвердило полный путь Web Admin → DB override → runtime refresh → member
join → Discord autorole mutation без restart bot service. Username участника,
имя роли, event IDs и Discord IDs в документ не включены.

## Финальное состояние

Последний подтверждённый read-only snapshot:

- Git: `a585149`, `main == origin/main`;
- Alembic: `3e7b9c2a6f41 (head)`;
- `guild_server_settings`: 1 row;
- `web_admin.server_setting_changed`: 1 event;
- Web Admin runtime baseline key count: 4;
- Web Admin private listener: `192.168.50.10:8000`;
- Bot Control listener: `127.0.0.1:8765`;
- `kanami` и `kanami-web-admin` active;
- failed systemd units: 0;
- recent `ERROR`/`CRITICAL`/`Exception`/`Traceback`: отсутствовали.

На момент этого snapshot real DB autorole override оставался активным. Возврат
к `ENV` не выполнялся и не утверждается.

## Заключение

**Stage 6B.2 production-validated на revision `a585149`.**

Подтверждены authenticated bounded options read, OWNER Server Settings UI,
correct ENV presentation после передачи baseline standalone Web Admin,
безопасный no-op, реальный `value` write с DB/audit записью, runtime apply без
restart и end-to-end выдача выбранной autorole при controlled rejoin. Web Admin
PostgreSQL connection остаётся read-only, а mutations по-прежнему принадлежат
bot process и проходят только через loopback Bot Control.
