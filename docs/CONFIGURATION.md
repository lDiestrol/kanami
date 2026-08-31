# Конфигурация Kanami

Kanami читает environment variables через `pydantic-settings`. Локально можно
использовать неотслеживаемый `.env`; systemd deployment читает
`/etc/kanami/kanami.env`. Имена Kanami settings case-sensitive; не добавляйте в
проектный `.env` неизвестные параметры.

## Discord

| Переменная | Обязательна | Default | Назначение и пример |
| --- | --- | --- | --- |
| `DISCORD_TOKEN` | Да | — | Секрет Bot token: `replace_me`. Непустое значение. Никогда не публиковать и не коммитить; при утечке regenerate. |
| `DISCORD_GUILD_ID` | Да | — | Положительный Discord server ID: `123456789012345678`. Команды и сбор ограничены этим guild. |
| `DISCORD_AUDIT_LOG_CHANNEL_ID` | Нет | feature off | Baseline ID text-канала для audit embeds. Без DB override отсутствие значения выключает общий Audit Logging и retention; при включённом return feature остаётся только history ingestion. |
| `DISCORD_AUTOROLE_ID` | Нет | feature off | Baseline ID роли для новых non-bot участников. Нужны `Manage Roles` и корректная role hierarchy. |
| `DISCORD_ANNIVERSARY_CHANNEL_ID` | Нет | feature off | Baseline ID text-канала автоматических поздравлений. Нужны `View Channel`, `Send Messages` и `Embed Links`. |
| `DISCORD_RETURN_CHANNEL_ID` | Нет | feature off | Baseline ID text-канала сообщений о возвращении. Нужны `View Channel`, `Send Messages` и `Embed Links`. |
| `DISCORD_GUEST_ROLE_ID` | Нет | не показывается | Stable ID уровня «Гость» для отображения в `/profile`. |
| `DISCORD_INITIATED_ROLE_ID` | Нет | не показывается | Stable ID уровня «Посвящённый» для отображения в `/profile`. |
| `DISCORD_GUARDIAN_ROLE_ID` | Нет | не показывается | Stable ID уровня «Страж» для отображения в `/profile`. |
| `DISCORD_PURPLE_ROLE_ID` | Нет | доступ закрыт | Stable ID ручной роли «Фиолетовый»; разрешает просмотр чужих member profiles. |
| `DISCORD_GOLD_ROLE_ID` | Нет | доступ закрыт | Stable ID ручной роли «Золотой»; разрешает просмотр чужих member profiles. |
| `RULES_ACCEPTED_ROLE_ID` | Нет | role grant off | Stable ID роли, выдаваемой после PostgreSQL-backed принятия текущих правил. Нужны `Manage Roles` и корректная role hierarchy. Наличие роли не считается доказательством принятия. |
| `MEMBER_RETURN_MIN_ABSENCE_SECONDS` | Нет | `86400` | Минимальное отсутствие перед `member.returned`; положительное число секунд. Значение по умолчанию подавляет выходы/возвраты короче 24 часов. |
| `GAME_TRACKING_ENABLED` | Нет | `false` | Включает сбор только Discord Activities типа Playing. Требует вручную разрешённый Presence Intent в Discord Developer Portal. Migration сама feature не включает. |
| `GAME_CONFIRM_INTERVAL_SECONDS` | Нет | `60` | Период batched PostgreSQL checkpoint открытых игровых sessions. Положительное число секунд; Discord API polling не выполняется. |
| `DISCORD_BOT_CONTROL_ENABLED` | Нет | `false` | Включает узкий loopback HTTP control interface для серверного профиля самого бота. |
| `DISCORD_BOT_CONTROL_HOST` | Нет | `127.0.0.1` | Принимается только точное `127.0.0.1`; внешний bind запрещён. |
| `DISCORD_BOT_CONTROL_PORT` | Нет | `8765` | TCP port от 1 до 65535. |
| `DISCORD_BOT_CONTROL_SHARED_SECRET` | При включении | — | Отдельный случайный секрет длиной не менее 32 символов. Не является Discord token и должен совпадать с web-side secret. |

Token не передавайте как command-line argument. Для production env-файла
используйте минимум `0640` и доступ только `root`/service group.

Role ID иерархии Profile v1 являются optional bot runtime configuration. Права
никогда не определяются по display name роли: собственный профиль доступен всегда,
а просмотр чужого разрешается только при совпадении роли участника с настроенным
`DISCORD_PURPLE_ROLE_ID` или `DISCORD_GOLD_ROLE_ID`. Незаданные privileged ID
работают fail-closed. Автоматическая выдача и progression ролей в Profile v1 не
реализованы.

Rules v1 всегда считает таблицу `rule_acceptances` источником истины.
`RULES_ACCEPTED_ROLE_ID` задаёт только optional Discord consequence: после
успешной идемпотентной записи бот пытается выдать роль. Отсутствующая/удалённая
роль, недостаточные права или role hierarchy не отменяют уже зафиксированное
принятие. После исправления Discord-конфигурации повторное нажатие безопасно
повторяет попытку выдачи роли без второй строки acceptance.

### Game Tracking

По умолчанию Game Tracking выключен и Kanami не запрашивает privileged Presence
Intent. Для production-включения:

1. применить актуальную Alembic migration;
2. в Discord Developer Portal открыть Bot → Privileged Gateway Intents и
   разрешить **Presence Intent**;
3. задать `GAME_TRACKING_ENABLED=true`;
4. при необходимости изменить `GAME_CONFIRM_INTERVAL_SECONDS`;
5. перезапустить Kanami и проверить journal на успешный Game startup
   reconciliation;
6. запустить игру и убедиться, что в `game_sessions` появилась открытая строка.

Включение Presence Intent в Developer Portal без environment flag не активирует
feature, а применение migration не меняет runtime самостоятельно. Сохраняются
только guild/user, display game name, optional Discord application ID и три UTC
границы session. Online/idle/dnd, platform status, Custom Status, Spotify,
details/state, party и Rich Presence secrets не сохраняются.

Для Operations page standalone Web Admin также читает
`VOICE_CHECKPOINT_INTERVAL_SECONDS`, `GAME_TRACKING_ENABLED` и
`GAME_CONFIRM_INTERVAL_SECONDS` через общий
`RuntimeSettings`. В раздельном web environment следует повторить фактические
значения bot process, чтобы `/admin/system` честно показывал Enabled/Disabled и
checkpoint interval. Эти переменные не дают Web Admin Discord credentials и не
создают write capability. Они являются только display-side snapshot
конфигурации: установка `GAME_TRACKING_ENABLED=true` в web env не включает
Presence Intent и не запускает tracker; feature запускается исключительно
основным bot process с тем же значением в `/etc/kanami/kanami.env`.

Начиная со Stage 6B.1 эти четыре optional ID являются baseline, а не
неизменяемой runtime-конфигурацией. Для каждого setting таблица
`guild_server_settings` независимо хранит один из режимов: `env` использует
baseline, `value` переопределяет его положительным Discord ID, `disabled` явно
выключает feature даже при заданном env ID. Отсутствующая DB row эквивалентна
четырём режимам `env`, поэтому migration сама по себе не меняет production
поведение. Изменения применяются bot process без restart.

Stage 6B.2 Server Settings read-side использует те же baseline через общий
`RuntimeSettings`. Bot и standalone Web Admin являются отдельными processes и
могут получать разные env-файлы; Web Admin не читает
`/etc/kanami/kanami.env` автоматически. Если bot process использует ENV
baseline, в environment standalone Web Admin необходимо передать те же значения:

```dotenv
DISCORD_AUTOROLE_ID=<discord-role-id>
DISCORD_AUDIT_LOG_CHANNEL_ID=<discord-channel-id>
DISCORD_ANNIVERSARY_CHANNEL_ID=<discord-channel-id>
DISCORD_RETURN_CHANNEL_ID=<discord-channel-id>
```

Они нужны Web Admin только для правильного SELECT-only вычисления и отображения
effective значения с source `ENV`. Без них bot runtime продолжит использовать
свой env корректно, но Web Admin покажет, что соответствующий ENV baseline
выключен. Web Admin PostgreSQL connection остаётся read-only, Discord Bot token
ему не передаётся, а actual writes всё равно выполняются через loopback Bot
Control. DB override `value`/`disabled` имеет приоритет над baseline; mode `env`
возвращает effective значение к baseline, настроенному для обоих processes.

## PostgreSQL

| Переменная | Обязательна | Default | Назначение и пример |
| --- | --- | --- | --- |
| `DATABASE_URL` | Да | — | SQLAlchemy URL строго с asyncpg: `postgresql+asyncpg://kanami_app:replace_me@127.0.0.1:5432/discord_stats_prod`. |

Password с reserved URL characters должен быть percent-encoded. URL является
секретом. Alembic использует ту же переменную, но не требует Discord settings.

### Первичная публикация Rules v1.0

Migration `b6e2c8f91a47` создаёт только schema. Она намеренно не знает production
guild ID и не вставляет правила. После `alembic upgrade head` и успешного
reference provisioning убедитесь, что configured guild уже есть в `guilds`, а
затем отдельно выполните транзакционный seed:

```bash
psql "postgresql://kanami_app:...@127.0.0.1:5432/discord_stats_prod" \
  -v guild_id=123456789012345678 \
  -f /opt/kanami/scripts/publish_rules_v1.sql
```

Для `psql` нужен обычный PostgreSQL DSN без SQLAlchemy suffix `+asyncpg`.
Скрипт является только initial bootstrap: он блокирует существующую guild row и
вставляет точный v1.0 content/metadata, только если для guild ещё нет ни одного
ruleset. Отсутствующий guild или любая существующая версия вызывают ошибку до
`INSERT` и откат всей транзакции. Скрипт никогда не архивирует и не заменяет
правила; версии 1.1+ в будущем публикуются только application/Web Admin логикой.
Перед выполнением подставьте только фактический `DISCORD_GUILD_ID`; deployment и
production insertion в рамках реализации Rules v1 не выполняются.

## Web Admin

Web Admin — отдельный процесс и требует `DATABASE_URL` и существующий
`DISCORD_GUILD_ID`, который однозначно ограничивает member list/detail одним
сервером, а также отдельный Discord OAuth2 confidential client. Discord Bot token
ему не нужен и для OAuth identity не используется. Для lifetime voice aggregate
он переиспользует общий optional `VOICE_MIN_SESSION_SECONDS` с default `10`.

| Переменная | Обязательна | Default | Назначение |
| --- | --- | --- | --- |
| `WEB_ADMIN_HOST` | Нет | `127.0.0.1` | Literal IP отдельного HTTP process. Loopback работает без opt-in; допустим конкретный RFC1918 IPv4 или IPv6 ULA. Hostname, wildcard, link-local и public IP запрещены. |
| `WEB_ADMIN_ALLOW_PRIVATE_BIND` | Нет | `false` | Явное разрешение private non-loopback bind. Не влияет на OAuth redirect/cookie policy и никогда не применяется к bot control. |
| `WEB_ADMIN_PORT` | Нет | `8000` | TCP port от 1 до 65535. |
| `WEB_ADMIN_DISCORD_CLIENT_ID` | Да | — | Положительный decimal ID Discord OAuth2 application. |
| `WEB_ADMIN_DISCORD_CLIENT_SECRET` | Да | — | Непустой OAuth2 client secret; хранить как секрет отдельно от Bot token. |
| `WEB_ADMIN_DISCORD_REDIRECT_URI` | Да | — | Точный callback URI с path `/admin/auth/discord/callback`, без credentials/query/fragment. Должен совпадать с Discord Developer Portal. |
| `WEB_ADMIN_ALLOWED_USER_IDS` | Нет | пустой список | Comma-separated список постоянных OWNER Discord user ID. Пробелы и пустые элементы игнорируются, дубликаты удаляются; active DB grants могут только добавлять ADMIN и не понижают OWNER. |
| `WEB_ADMIN_COOKIE_SECURE` | Нет | `true` | Не выводится автоматически из request. `false` явно разрешён только вместе с loopback HTTP redirect URI для локальной разработки/SSH tunnel. |
| `WEB_ADMIN_SESSION_LIFETIME_SECONDS` | Нет | `28800` | Абсолютный срок server-side session от 300 до 86400 секунд; sliding renewal нет. |
| `WEB_ADMIN_BOT_CONTROL_URL` | Нет | feature off | Точный base URL `http://127.0.0.1:<port>` без credentials/path/query/fragment. Задаётся только вместе с shared secret. |
| `WEB_ADMIN_BOT_CONTROL_SHARED_SECRET` | Нет | feature off | Отдельный случайный секрет длиной не менее 32 символов, совпадающий с bot-side значением. Задаётся только вместе с URL. |

Для локального `http://localhost:8000` требуется явно задать
`WEB_ADMIN_COOKIE_SECURE=false`. Для HTTPS используется `true`; HTTP с non-loopback
redirect host и HTTPS с insecure cookie отклоняются при startup. Callback URI
никогда не строится из `Host` или `X-Forwarded-*`. Uvicorn не доверяет
`Forwarded`/`X-Forwarded-*`; security decisions полностью задаются конфигурацией.

Stage 3C OAuth `identify` подтверждает только identity пользователя и сам по себе
недостаточен для доступа. Authorization назначает OWNER для каждого ID из
`WEB_ADMIN_ALLOWED_USER_IDS`, иначе ADMIN при active `web_admin_access_grants`,
после чего требует текущий non-bot membership в `DISCORD_GUILD_ID`
(`left_at IS NULL`) по данным PostgreSQL. Только после этих проверок создаётся
session. Невалидный, нулевой, отрицательный или non-decimal OWNER ID останавливает
startup с validation error. Публичная панель обязана
работать за HTTPS reverse proxy. Same-host proxy использует безопасный loopback
default; remote proxy требует `WEB_ADMIN_ALLOW_PRIVATE_BIND=true` и firewall,
разрешающий TCP/8000 только с proxy VM.
Sessions и незавершённые OAuth transactions хранятся только в памяти процесса и
исчезают при restart; миграции и `WEB_ADMIN_SESSION_SECRET` не нужны.

Stage 4 добавляет страницу `/admin/settings/bot-profile`: она меняет только
guild-specific nickname и avatar собственного bot member в настроенном
`DISCORD_GUILD_ID`. Web Admin не получает `DISCORD_TOKEN` и обращается к Discord
process только по фиксированным control operations через loopback с Bearer shared
secret. Все POST формы требуют session CSRF token. Avatar принимается как PNG или
JPEG не более 8 MiB; проверяются MIME type и сигнатура содержимого, имя/расширение
файла не считаются доказательством формата. Пустая control-конфигурация оставляет
страницу fail-closed: профиль и формы изменения недоступны.

Перед каждым bot-profile write после CSRF повторяются role resolution и current
membership checks; отказ/ошибка БД отзывает session и не вызывает control API.
`GET /admin/health` публичен, но возвращает только общий `healthy`/`unhealthy`.
Подробности deployment: [WEB_ADMIN_DEPLOYMENT.md](WEB_ADMIN_DEPLOYMENT.md).

В production bot process и Web Admin должны получать **раздельные env-файлы или
systemd credentials/drop-ins**. Bot-side env содержит `DISCORD_TOKEN` и
`DISCORD_BOT_CONTROL_SHARED_SECRET`; web-side env содержит OAuth settings и
`WEB_ADMIN_BOT_CONTROL_SHARED_SECRET`, но не содержит `DISCORD_TOKEN`. Shared
secret генерируется отдельно от всех Discord credentials, хранится с теми же
ограничениями доступа и ротируется одновременным restart обоих процессов.
Рекомендуется отдельный Linux user (например `kanami-web`), которому недоступен
`/etc/kanami/kanami.env`. Для `/etc/kanami/kanami-web-admin.env` используйте
отдельный deployment example; не копируйте bot env целиком. Web env обязан явно
получить `DISCORD_GUILD_ID`, `REPORT_TIMEZONE`,
`VOICE_CHECKPOINT_INTERVAL_SECONDS`, `GAME_TRACKING_ENABLED` и
`GAME_CONFIRM_INTERVAL_SECONDS`, а также рекомендуется передать общий
`VOICE_MIN_SESSION_SECONDS`. Полный пример и настройка Git metadata для
раздельных service users описаны в
[WEB_ADMIN_DEPLOYMENT.md](WEB_ADMIN_DEPLOYMENT.md#разделение-процессов-и-secrets).

## Reporting и timezone

| Переменная | Обязательна | Default | Назначение и пример |
| --- | --- | --- | --- |
| `REPORT_TIMEZONE` | Нет | `UTC` | Валидное IANA timezone name, например `Asia/Yekaterinburg`. Определяет локальную границу «сегодня» в Discord-статистике и Web Dashboard, календарную дату годовщин, snapshot text-статистики при возвращении и отображение audit time. |

## Voice

| Переменная | Обязательна | Default | Назначение и пример |
| --- | --- | --- | --- |
| `VOICE_MIN_SESSION_SECONDS` | Нет | `10` | Положительный whole-session threshold для статистики. Короткие sessions сохраняются, но не входят в основные отчёты. |
| `VOICE_CHECKPOINT_INTERVAL_SECONDS` | Нет | `60` | Положительный interval периодического подтверждения открытых voice sessions. |

## Retention

Все значения — положительное целое число дней.

| Переменная | Обязательна | Default | Назначение |
| --- | --- | --- | --- |
| `AUDIT_TRANSIENT_RETENTION_DAYS` | Нет | `90` | Активный срок хранения transient audit events. Important events имеют `expires_at = NULL`. |
| `RAW_MESSAGE_RETENTION_DAYS` | Нет | `90` | Зарезервированный validated setting. Raw per-message collector/table не реализованы; настройка не применяется к постоянным `daily_text_activity`. |
| `SERVER_EVENT_RETENTION_DAYS` | Нет | `365` | Зарезервированный validated setting. Отдельный server-event collector/table не реализованы. |

Voice sessions/intervals автоматически по возрасту не удаляются. Настройка
backup и юридически необходимой retention policy остаётся задачей оператора.

## Logging

| Переменная | Обязательна | Default | Назначение и пример |
| --- | --- | --- | --- |
| `LOG_LEVEL` | Нет | `INFO` | Один из `CRITICAL`, `ERROR`, `WARNING`, `INFO`, `DEBUG`. Логи идут в stdout и попадают в journal systemd. |

При диагностике не добавляйте token или полный `DATABASE_URL` в log messages и
публичные отчёты.
