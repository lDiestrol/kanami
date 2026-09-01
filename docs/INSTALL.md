# Установка Kanami

Эта инструкция описывает первый официальный deployment-путь: чистая Debian 13
VM, `systemd` и локальный PostgreSQL. Предполагаются базовые навыки Linux и
доступ `sudo`.

## Перед началом

Ориентир для небольшого/среднего Discord-сервера: минимум 1 vCPU, 1 GB RAM и
16 GB disk; рекомендуется 2 vCPU, 2 GB RAM и 32 GB disk. Нужны DNS/HTTPS-доступ
к Discord, GitHub и Python package index. Реальный размер БД зависит от
активности, retention settings и глубины audit/statistics history.

## Подготовка Discord

1. Откройте [Discord Developer Portal](https://discord.com/developers/applications)
   и создайте Application.
2. На странице **Bot** создайте Bot user. Скопируйте token в password manager;
   не вставляйте его в команды shell, issue или Git.
3. В **Privileged Gateway Intents** включите **Server Members Intent**.
   **Presence Intent** нужен только при явно включённом Game Tracking; Message
   Content Intent Kanami не использует.
4. На странице **OAuth2 > URL Generator** выберите scopes `bot` и
   `applications.commands`, затем пригласите приложение на нужный сервер.
5. Не выдавайте `Administrator`. Для базовой статистики дайте боту видеть
   отслеживаемые Voice/Stage-каналы и guild text channels, сообщения которых
   должны учитываться. Если включаете Audit Logging, разрешите в
   выбранном audit-канале `View Channel`, `Send Messages` и `Embed Links`.
   Эти же права нужны в канале `DISCORD_ANNIVERSARY_CHANNEL_ID`, если включены
   автоматические поздравления.
   `Manage Roles` требуется только для Autorole, причём роль Kanami должна быть
   выше автоматически выдаваемой роли.
6. В Discord включите Developer Mode, нажмите правой кнопкой на сервер и
   выберите **Copy Server ID**. Это значение `DISCORD_GUILD_ID`; инструкция не
   привязана к конкретному ID.

Текущий runtime включает gateway intents guilds, members, moderation, voice
states и guild messages. Он не включает message content, typing или DM intents;
presences включаются только через `GAME_TRACKING_ENABLED=true`.

## Kanami Manager: установка, диагностика и lifecycle

Новая официальная установка копирует committed `scripts/manager.sh` из
установленного checkout в `/usr/local/bin/kanami` с owner `root:root` и mode
`0755`. После установки доступны:

```bash
kanami help
kanami version
kanami status
kanami doctor
kanami logs
kanami logs --lines 50
sudo kanami start
sudo kanami stop
sudo kanami restart
sudo kanami update
kanami menu
```

Для разработки те же команды можно запускать как `bash ./scripts/manager.sh ...`.
Команды `help`, `version`, `status`, `doctor` и `logs` остаются read-only и не
требуют root со стороны Manager. `status` показывает короткую сводку checkout и
systemd services, а `doctor` проверяет foundation установки и возвращает
ненулевой exit code при обязательных ошибках. Отдельный Web Admin остаётся
optional: его отсутствие или inactive-state не делают основную bot installation
нездоровой. Недоступные без root проверки systemd показываются как
`WARN`/`SKIP`, а не считаются подтверждённой поломкой.

D2.10 добавляет в `kanami doctor` отдельный read-only раздел Production trust.
Он проверяет canonical `/opt/kanami`, `.git`, `scripts`, updater, Manager source,
systemd unit source, `.venv`, bot executable и fixed `/usr/bin/bash`/`stat`.
Root-owned anchors должны иметь UID/GID 0, не быть symlink или group/other
writable. `.venv` остаётся намеренным исключением: `kanami:kanami`, owner-writable
и с executable bot entrypoint. Только полностью подтверждённая boundary даёт
`Update readiness: READY`; любое обязательное нарушение делает doctor
`UNHEALTHY`. Doctor не требует root, не читает secrets и ничего не repair-ит.
Legacy writable checkout требует manual review/migration/reinstall из trusted
source до privileged update.

`kanami logs` через фиксированный `/usr/bin/journalctl` показывает последние 100
записей только `kanami.service` и всегда отключает pager. Число записей можно
задать как `kanami logs --lines N`, где `N` находится в диапазоне 1..1000.
Фактический доступ зависит от system journal permissions: Manager не запускает
`sudo` и возвращает ошибку journalctl вызывающему shell. Вывод является raw
application/system journal output; перед публикацией в issue или chat его нужно
проверить на чувствительные данные. Follow mode и Web Admin logs пока не
поддерживаются.

Первая lifecycle-команда `sudo kanami restart` перезапускает только обязательный
`kanami.service`, проверяет его LoadState и active-state после restart. Manager
не запускает `sudo` сам и не перезапускает optional
`kanami-web-admin.service`. Прямой CLI-вызов является явным намерением и не
запрашивает дополнительное подтверждение.

D2.7 добавляет root-only `sudo kanami start` и `sudo kanami stop` для того же
`kanami.service` через фиксированный `/usr/bin/systemctl`. Start является
идемпотентным: уже active service повторно не запускается, а после фактического
start успех выводится только после active check. Stop выполняет штатный stop и
считает действие успешным только при подтверждённом конечном state `inactive`;
`failed` и другие состояния не маскируются под успех. Прямые CLI-вызовы не
требуют confirmation. Manager не выполняет auto-sudo, не затрагивает Web Admin
и не меняет enable/disable boot policy.

D2.9 добавляет `sudo kanami update` как тонкую оболочку над canonical
`/opt/kanami/scripts/update.sh`. Manager не повторяет Git/uv/Alembic/systemd
workflow: сначала он проверяет root и bootstrap trust цепочки `/opt/kanami`,
`scripts` и `update.sh` (без symlink, UID/GID 0, без group/other write), затем
отдельным process запускает `/usr/bin/bash /opt/kanami/scripts/update.sh` и
передаёт его output и exit code. Direct CLI-вызов считается явным намерением и
confirmation не требует. Полный ownership invariant checkout затем повторно
проверяет сам updater как defense-in-depth.

При запуске `kanami` без аргументов интерактивное меню открывается только когда
stdin и stdout являются TTY. В pipeline, cron и CI manager показывает help и не
ожидает input. Явная команда `kanami menu` сохраняет пункты 1–4 для Status,
Doctor, Version и Help, оставляет `5. Restart bot`, `6. Logs`, `7. Start bot` и
`8. Stop bot`, добавляет `9. Update` и сохраняет `0. Exit`. Logs в menu использует
default 100 и после завершения или ошибки возвращает пользователя в menu без
confirmation. Menu Start также не требует confirmation. Menu Stop требует
`y`, `Y`, `yes` или `YES`; пустой ввод, любой другой ответ и EOF отменяют stop.
Menu restart требует отдельного подтверждения: `y`, `Y`, `yes` или `YES`
подтверждают restart; пустой ввод, любой другой ответ и EOF отменяют действие.
Ошибка restart показывается без аварийного закрытия menu. Menu Update также
принимает только `y`, `Y`, `yes` или `YES`; cancellation возвращает в menu. После
фактического запуска updater текущая menu session завершается при любом его exit
code, потому что updater мог уже refresh-нуть `/usr/local/bin/kanami`. Для новой
session нужно снова запустить `kanami`.

Manager не читает env-файлы и пока не выполняет PostgreSQL, Alembic или HTTP
проверки. Install, Web Admin lifecycle, enable/disable,
backup/restore/rollback и другие lifecycle actions пока отсутствуют.

## Автоматизированная установка

Клонируйте repository обычным пользователем и изучите script перед запуском:

```bash
git clone https://github.com/lDiestrol/kanami.git
cd kanami
less scripts/install.sh
sudo ./scripts/install.sh
```

Installer поддерживает именно Debian 13 и:

- до создания Kanami database, checkout и config запрашивает через обязательный
  usable `/dev/tty` Discord Bot Token, Discord Guild ID и IANA timezone;
- вводит Bot Token без echo, принимает только непустое значение из letters,
  digits, `.`, `_` и `-` и никогда не показывает значение в summary;
- валидирует Guild ID как decimal Discord snowflake в диапазоне
  `1..18446744073709551615`, а timezone — через Python `zoneinfo`; пустой
  timezone означает `UTC`;
- после проверки timezone предлагает опционально подготовить Web Admin; ответ
  по умолчанию — `No`, поэтому Core-only установка не создаёт web user, role,
  env, unit или runtime;
- при выборе Web Admin запрашивает OAuth Client ID, скрытый Client Secret,
  точный HTTPS callback и один или несколько OWNER Discord User IDs, после чего
  показывает единый summary без secret values;
- устанавливает `ca-certificates`, `git`, Python/venv, `tzdata`, PostgreSQL и
  `openssl`;
- создаёт system user `kanami` без interactive login;
- клонирует текущую checked-out branch вместе с `.git` в `/opt/kanami` и
  сохраняет исходный upstream `origin` для обновлений; production checkout,
  `.git` и tracked source остаются `root:root` и недоступны для записи service
  user;
- создаёт узкое writable-исключение `/opt/kanami/.venv` с owner
  `kanami:kanami`; остальной checkout не передаётся service user;
- устанавливает copy `/opt/kanami/scripts/manager.sh` как
  `/usr/local/bin/kanami` (`root:root`, `0755`), без symlink на source checkout;
- создаёт service home `/var/lib/kanami`, устанавливает pinned `uv` в
  `/opt/kanami-uv`, хранит его cache вне Git tree в `/var/cache/kanami/uv` и
  выполняет `uv sync --frozen --no-dev`;
- при чистой локальной PostgreSQL создаёт database `discord_stats_prod`, role
  `kanami_app` и случайный hex password;
- после безопасного summary и подтверждения создаёт
  `/etc/kanami/kanami.env` с готовыми обязательными core settings, mode `0640`
  и owner `root:kanami`;
- применяет `alembic upgrade head` и устанавливает `kanami.service`.

При выбранном Web Admin installer дополнительно создаёт system user
`kanami-web`, отдельный runtime `/var/lib/kanami-web/.venv`, protected env
`/etc/kanami/kanami-web-admin.env` (`root:kanami-web`, `0640`), отдельную
PostgreSQL role `kanami_web_readonly` и canonical
`kanami-web-admin.service`. Web runtime синхронизируется из того же trusted
root-owned `/opt/kanami` и committed `uv.lock` через pinned `uv`, а executable
находится в `/var/lib/kanami-web/.venv/bin/kanami-web-admin`.
Explicit DB policy хранится в
`deploy/postgresql/kanami-web-admin-grants.sql` и применяется после migrations.
Она закрывает `PUBLIC` CONNECT/TEMPORARY для созданной Kanami database и
`PUBLIC` CREATE для её `public` schema, затем выдаёт web role только CONNECT,
schema USAGE, текущие SELECT, scoped Rules/audit writes и нужные sequences.

OAuth Client Secret для deterministic systemd EnvironmentFile принимается как
непустое значение только из letters, digits, `.`, `_` и `-`; длина и число
сегментов не предполагаются. Redirect обязан быть absolute HTTPS URL с hostname
и точным path `/admin/auth/discord/callback`, без credentials, query и fragment.
OWNER IDs нормализуются в непустой deduplicated comma-separated список.

Скрипт не перезаписывает существующий config, database, role или install tree.
Если он видит неоднозначное частичное состояние PostgreSQL, то останавливается
и предлагает ручную настройку вместо смены существующего password.

Interactive wizard не принимает Bot Token через argument, CLI option или
environment variable и не имеет visible-input fallback. Без usable `/dev/tty`
installer завершается до установки packages и до создания production state.
Packages устанавливаются до окончательной локальной проверки timezone; отказ на
`Continue installation? [Y/n]` не откатывает packages, но происходит до
создания Kanami database, `/opt/kanami` и `/etc/kanami/kanami.env`.

Первый запуск намеренно остаётся ручным: installer не включает и не стартует
bot service автоматически. Сначала оператор проверяет production trust через
`kanami doctor`.

Для production рекомендуется устанавливать поддерживаемую branch, обычно
`main`; installer не хардкодит её и сохраняет branch исходного checkout.
Публичный repository с HTTPS `origin` обновляется без Git credentials. Для
private repository/fork заранее настройте deploy key или другой credential
mechanism, доступный root-контексту deployment updater. Не помещайте PAT, private
key или token в `kanami.env`, repository и примеры команд.

## Конфигурация

Installer уже записывает реальные `DISCORD_TOKEN`, `DISCORD_GUILD_ID`,
`REPORT_TIMEZONE` и сгенерированный `DATABASE_URL`; ручной замены обязательных
placeholder values больше нет. Для optional settings откройте защищённый
env-файл:

```bash
sudoedit /etc/kanami/kanami.env
```

Не меняйте сгенерированный `DATABASE_URL`, если не переносите PostgreSQL.
Optional Audit Logging, Autorole и автоматические годовщины включаются
отдельными ID. Все поддерживаемые
параметры и defaults описаны в [CONFIGURATION.md](CONFIGURATION.md).

Никогда не коммитьте env-файл. Если Discord token стал известен третьим лицам,
сразу regenerate его в Developer Portal и обновите config.

## PostgreSQL вручную

Этот раздел нужен, если автоматическое создание не подходит или PostgreSQL уже
настроен. Выполните команды из `psql` под PostgreSQL administrator и выберите
свой сильный случайный password:

```sql
CREATE ROLE kanami_app LOGIN PASSWORD 'replace_with_a_random_password';
CREATE DATABASE discord_stats_prod OWNER kanami_app;
```

Затем задайте без кавычек shell следующий URL в config (special characters в
password должны быть percent-encoded):

```dotenv
DATABASE_URL=postgresql+asyncpg://kanami_app:replace_me@127.0.0.1:5432/discord_stats_prod
```

Kanami принимает только SQLAlchemy driver `postgresql+asyncpg`. Приложение не
создаёт таблицы на старте: миграции — отдельный обязательный deployment step.

## Миграции и первый запуск

Installer уже выполняет `upgrade head`. Перед первым стартом можно проверить
revision, передав Alembic только database URL:

```bash
DATABASE_URL="$(sudo sed -n 's/^DATABASE_URL=//p' /etc/kanami/kanami.env)"
sudo -u kanami env DATABASE_URL="$DATABASE_URL" \
  /opt/kanami/.venv/bin/alembic -c /opt/kanami/alembic.ini current
sudo -u kanami env DATABASE_URL="$DATABASE_URL" \
  /opt/kanami/.venv/bin/alembic -c /opt/kanami/alembic.ini heads
unset DATABASE_URL
```

Затем включите service:

```bash
kanami doctor
sudo systemctl enable kanami
sudo kanami start
kanami logs
```

Unit запускает `/opt/kanami/.venv/bin/discord-stats-bot` от пользователя
`kanami`, с working directory `/opt/kanami` и EnvironmentFile
`/etc/kanami/kanami.env`. Миграции unit автоматически не запускает.

Если Web Admin выбран в wizard, installer подготавливает его отдельный user,
runtime, env, DB role и unit, но намеренно не включает и не запускает service.
Web env никогда не содержит `DISCORD_TOKEN` или Bot Control settings и не имеет
доступа к core env. До первого запуска настройте reverse proxy/TLS/domain,
сверьте OAuth redirect и выполните шаги из
[WEB_ADMIN_DEPLOYMENT.md](WEB_ADMIN_DEPLOYMENT.md). Это завершение публичного
deployment относится к D2.13, а не к installer foundation D2.12.

## Проверка установки

- `systemctl is-active kanami` выводит `active`;
- `alembic current` совпадает с единственным revision из `alembic heads`;
- бот отображается online на настроенном сервере;
- одиннадцать guild slash-команд появились после sync;
- `/help` отвечает;
- `/topmessages` отвечает, а новое guild-сообщение увеличивает суточный агрегат;
- `journalctl -u kanami` не содержит traceback, `ERROR` или `CRITICAL`.

Если команды не появились, сначала проверьте правильность
`DISCORD_GUILD_ID`, scope `applications.commands` и startup log. Не публикуйте
token при обращении за помощью.

## Обновление

Для installation, уже соответствующей canonical D2.8 ownership model, используйте:

```bash
sudo kanami update
```

Manager до запуска проверяет bootstrap trust updater path и запускает canonical
script через fixed `/usr/bin/bash`, не полагаясь на executable bit. Direct
command не запрашивает confirmation, не скрывает output и возвращает exit code
updater-а. Если установленная копия Manager недоступна, допустимый manual
fallback после отдельной проверки canonical ownership выглядит так:

```bash
sudo /usr/bin/bash /opt/kanami/scripts/update.sh
```

Не запускайте checkout-local updater через `sudo` на старой installer-v2
installation, где `/opt/kanami` или `scripts/update.sh` принадлежит `kanami`.
Такой script находится в недоверенной writable service-user области: его
внутренняя ownership validation не может восстановить доверие к коду, уже
запущенному с EUID 0. Перед следующим privileged update необходимы manual
review и migration/reinstall из trusted source; выполнять blind recursive
`chown` не следует.

Перед Git operations скрипт проверяет canonical ownership boundary: checkout,
`.git` и source tree должны принадлежать `root:root` и не быть writable для
group/other, а отдельная `.venv` должна принадлежать `kanami:kanami` и быть
writable для service user. Затем updater от root проверяет чистоту Git tree,
выполняет fast-forward-only pull, после чего под пользователем `kanami`
выполняет locked dependency sync и Alembic migration, обновляет unit,
перезапускает service и показывает итоговый commit. Сразу после успешного pull
он также обновляет regular copy `/usr/local/bin/kanami` из committed
`/opt/kanami/scripts/manager.sh`, сохраняя `root:root` и mode `0755`. Если этот
source отсутствует, не является regular readable file или является symlink,
update завершается до dependency sync, migrations и restart. При ошибке любого
следующего этапа более поздние этапы также не выполняются. Локальные изменения,
user data и config updater не удаляет.

Если все canonical markers D2.12 Web Admin присутствуют, updater перед
любыми update mutations проверяет exact owner/group/mode runtime, env и unit, а
также требует точный systemd state `inactive`. Active или недостоверный state
останавливает update до `git pull`: D2.12 не управляет Web lifecycle. После
этого updater выполняет отдельный frozen/no-dev sync от `kanami-web` в
`/var/lib/kanami-web/.venv`. При полном отсутствии Web Admin этот этап
пропускается; неоднозначное частичное состояние завершает update с ошибкой без
recursive permission repair. После migrations ACL policy применяется повторно,
canonical web unit обновляется как `root:root`/`0644` и выполняется
`daemon-reload`; Web Admin не запускается и не перезапускается.

Внутренняя проверка updater-а подтверждает invariant уже доверенной canonical
installation и обнаруживает последующий ownership drift. Она не является
bootstrap-механизмом доверия или migration старого all-`kanami` checkout.

Updater refresh-ит Manager сразу после успешного pull, до dependency sync,
migrations и restart. Поэтому более поздняя ошибка может оставить installation
частично обновлённой, включая уже заменённый `/usr/local/bin/kanami`. D2.9 не
выполняет rollback: изучите исходный updater output, затем используйте
`kanami status`, `kanami doctor`, logs и при необходимости manual recovery.

Backup PostgreSQL перед значимым production update остаётся ответственностью
оператора; автоматическая backup/restore policy пока не входит в проект.
