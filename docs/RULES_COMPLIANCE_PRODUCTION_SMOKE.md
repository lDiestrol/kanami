# Rules Compliance R3A: production smoke

## Проверенное состояние

Rules Compliance Foundation R3A развёрнут и production-smoke-verified на хосте
`production-host`.

- deployed Git revision: `560703e`
  (`feat(rules): add compliance foundation`);
- предыдущая production revision: `e425b43`;
- Alembic до rollout: `e1a7c4d92b60`;
- Alembic после rollout: `a4f6c8d21e73 (head)`;
- migration: `a4f6c8d21e73_add_rules_compliance_foundation.py`;
- `kanami.service` и `kanami-web-admin.service`: active/running.

R3A вычисляет compliance по persisted published Rules history и реальным
acceptance. Smoke не создавал новую версию правил, synthetic acceptance или
Discord enforcement.

## Preflight и backup

Перед rollout подтверждено исходное production-состояние:

- Git revision: `e425b43`;
- Alembic: `e1a7c4d92b60`.

Создан rollback backup:

```text
/var/backups/kanami/r3a-YYYYMMDD-HHMMSS
```

Содержимое backup, production credentials и Discord IDs в документ не
включаются.

## Deployment и migration

Production checkout обновлён до `560703e`. Затем успешно применена reversible
migration `a4f6c8d21e73`, после чего Alembic показал новый head.

Migration не изменяла существующие Rules или acceptance data и не создавала
фиктивную Rules version. Current production Rules 1.0 сохранила прежнее
состояние.

## Проверка schema

Прямая проверка PostgreSQL подтвердила новую колонку:

```text
table: rulesets
column: reacceptance_grace_days
type: smallint
nullable: YES
```

Также присутствует constraint `ck_rulesets_reacceptance_grace_days` с
контрактом:

```sql
reacceptance_grace_days IS NULL
OR (
    requires_reacceptance
    AND reacceptance_grace_days BETWEEN 1 AND 365
)
```

Для существующей Rules 1.0 подтверждено:

```text
status = published
requires_reacceptance = false
reacceptance_grace_days = NULL
```

## Service restart и Discord startup smoke

После rollout оба процесса успешно запущены:

- `kanami.service`: active/running;
- `kanami-web-admin.service`: active/running;
- Discord application command sync: `commands=16`;
- Discord Gateway подключился успешно.

Reference provisioning завершился со значениями:

```text
users=110
members=110
voice_channels=13
```

Voice startup reconciliation:

```text
connected=5
failed=0
```

Game startup reconciliation:

```text
observed=17
closed=17
started=17
unchanged=0
```

В startup/journal smoke не обнаружено новых `ERROR`, `Exception` или
`Traceback`.

## Web Admin browser smoke

Authenticated production browser smoke страницы `/admin/rules` подтвердил
read-only блок «Подтверждение правил» для current Rules version 1.0.

Отображённое состояние:

```text
current version = 1.0
status = published
required checkpoint = 1.0
checkpoint semantics = первая опубликованная версия
deadline = отсутствует
compliant = 4
pending = 106
overdue = 0
total = 110
```

Таким образом UI корректно применил baseline checkpoint: даже если первая
опубликованная версия имеет `requires_reacceptance=false`, пользователь должен
иметь хотя бы один реальный acceptance этой или более новой опубликованной
версии.

## Прямая PostgreSQL verification

Отдельный read-only PostgreSQL smoke подтвердил те же aggregate counts:

```text
total = 110
compliant = 4
pending = 106
overdue = 0
```

Совпадение Web Admin и прямого PostgreSQL read подтверждает current non-bot
member scope и baseline semantics. Поскольку checkpoint не имеет grace period,
106 пользователей без qualifying acceptance остаются pending и автоматически
не становятся overdue.

## Независимость от managed Discord publication

Во время этого smoke managed Discord publication была отключена. Compliance
summary при этом вычислялась корректно из persisted published Rules history и
acceptance. Наличие managed Discord message не является источником compliance и
не требуется для read-only расчёта.

## Что не проверялось и остаётся вне R3A

В рамках rollout и smoke не реализовывались и не проверялись:

- reminders;
- grace/background workers;
- enforcement;
- role removal или другие role mutations;
- DM;
- new-member onboarding;
- Rules diff, rollback или scheduled publication;
- forced reacceptance lifecycle с новой реальной Rules version.

Отдельно остаётся pending ранее запланированный production scenario: публикация
новой реальной Rules version, successful DB commit, automatic Bot Control sync,
обновление существующего managed message с сохранением message ID и проверка
нового mandatory checkpoint. Фиктивная версия только ради smoke не создавалась.

## Заключение

**Rules Compliance R3A production-smoke-verified на revision `560703e` и
Alembic head `a4f6c8d21e73`.**

Подтверждены nullable bounded grace schema, baseline checkpoint для Rules 1.0,
совпадающие Web Admin/PostgreSQL aggregate counts `4/106/0/110`, независимость
compliance от managed Discord publication и успешный startup обоих production
services без новых runtime errors.
