# Web Admin Stage 6A: production smoke

## Проверенное состояние

Production smoke выполнен **2026-08-22 UTC** на хосте `production-host`.

- deployed Git revision: `722e420` (`Format Stage 6A code`);
- предыдущая production revision: `067a6a3`;
- обновление выполнено fast-forward;
- Alembic revision: `8d44cacc791e` (`head`).

Stage 6A на этой revision объединён с `main`; GitHub Actions завершился успешно.
До production локально прошли Ruff lint, `ruff format --check`, `uv lock
--check`, `git diff --check`, hermetic suite `841 passed, 10 skipped` и полный
PostgreSQL suite `851 passed`. Отдельный реальный PostgreSQL smoke завершился
маркером `STAGE6A_SMOKE_OK`, после cleanup число synthetic rows во всех
затронутых таблицах было равно нулю.

## Deployment и rollback checkpoint

Перед обновлением production worktree был чистым. Создан PostgreSQL backup в
custom format, а его каталог проверен через `pg_restore --list`. Production env
files также сохранены без документирования их содержимого. Каталог резервных
копий имел права `700`, файлы — `600`. Изменений `pyproject.toml` и `uv.lock`
между старой и новой revision не было.

Эти проверенные database и environment backups являются rollback checkpoint для
deployment. В рамках smoke rollback не потребовался.

## Состояние сервисов

После deployment:

- `kanami.service` активен;
- `kanami-web-admin.service` активен;
- failed systemd units отсутствуют;
- Discord Gateway подключён, синхронизировано 12 команд;
- Discord reference provisioning и Voice startup reconciliation завершились
  успешно;
- Bot Control включён и слушает только `127.0.0.1:8765`.

В последних service logs отсутствовали `ERROR`, `CRITICAL`, `Exception` и
`Traceback`.

## Web Admin и network security

Web Admin успешно работал на explicitly configured private bind
`192.168.50.10:8000` и публиковался через nginx/reverse proxy по HTTPS.
`https://kanami.example.com/admin/` перенаправлял неаутентифицированный запрос на
`/admin/login`, а login начинал Discord OAuth flow.

На HTTPS boundary подтверждены CSP, HSTS, `X-Frame-Options: DENY`,
`X-Content-Type-Options`, `Referrer-Policy` и `Permissions-Policy`. OAuth cookie
имел атрибуты `Secure`, `HttpOnly` и `SameSite=Lax`. Значения OAuth/Bot Control
секретов, database URL, session cookie и OAuth state в smoke-отчёт не включены.

## OWNER и managed ADMIN

Оба configured permanent OWNER accounts отображались в UI и сохраняли
защищённую роль OWNER без возможности revoke.

Проверен полный managed ADMIN lifecycle:

1. OWNER выдал доступ текущему non-bot участнику через Web Admin UI.
2. Пользователь появился в списке Managed ADMIN, а в `audit_events` был создан
   `web_admin.access_granted`.
3. Managed ADMIN успешно вошёл через Discord OAuth и видел обычные страницы Web
   Admin, но не видел OWNER-only ссылки «Администраторы» и «Журнал аудита».
4. OWNER отозвал доступ; active grant исчез, а в `audit_events` был создан
   `web_admin.access_revoked`.
5. Повторный OAuth бывшего ADMIN завершился fresh authorization отказом
   «Доступ к Kanami Admin запрещён».
6. OWNER-only Audit Log показал revoke и grant newest-first.

Финальный production контроль подтвердил `active_grants = 0`, одну историческую
grant-запись и два соответствующих audit events. Персональный Discord ID
проверенного managed ADMIN намеренно не зафиксирован.

## Заключение

Web Admin Stage 6A production-validated на revision `722e420`: migration
применена, оба сервиса здоровы, OWNER/ADMIN authorization, grant/revoke,
audit history и fresh deny после revoke подтверждены. Web Admin read-side
остался read-only, а mutations выполнялись через ограниченный loopback Bot
Control path.
