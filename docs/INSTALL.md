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

## Автоматизированная установка

Клонируйте repository обычным пользователем и изучите script перед запуском:

```bash
git clone https://github.com/lDiestrol/kanami.git
cd kanami
less scripts/install.sh
sudo ./scripts/install.sh
```

Installer поддерживает именно Debian 13 и:

- устанавливает `ca-certificates`, `git`, Python/venv, PostgreSQL и `openssl`;
- создаёт system user `kanami` без interactive login;
- клонирует текущую checked-out branch вместе с `.git` в `/opt/kanami` и
  сохраняет исходный upstream `origin` для обновлений;
- создаёт service home `/var/lib/kanami`, устанавливает pinned `uv` в
  `/opt/kanami-uv`, хранит его cache вне Git tree в `/var/cache/kanami/uv` и
  выполняет `uv sync --frozen --no-dev`;
- при чистой локальной PostgreSQL создаёт database `discord_stats_prod`, role
  `kanami_app` и случайный hex password;
- создаёт `/etc/kanami/kanami.env` с mode `0640`, owner `root:kanami`;
- применяет `alembic upgrade head` и устанавливает `kanami.service`.

Скрипт не перезаписывает существующий config, database, role или install tree.
Если он видит неоднозначное частичное состояние PostgreSQL, то останавливается
и предлагает ручную настройку вместо смены существующего password.

Первый запуск намеренно остаётся ручным: installer не стартует service, пока
`DISCORD_TOKEN` и `DISCORD_GUILD_ID` являются placeholders.

Для production рекомендуется устанавливать поддерживаемую branch, обычно
`main`; installer не хардкодит её и сохраняет branch исходного checkout.
Публичный repository с HTTPS `origin` обновляется без Git credentials. Для
private repository/fork заранее настройте deploy key или другой credential
mechanism, доступный пользователю `kanami` с home `/var/lib/kanami`. Не помещайте
PAT, private key или token в `kanami.env`, repository и примеры команд.

## Конфигурация

Откройте защищённый env-файл:

```bash
sudoedit /etc/kanami/kanami.env
```

Замените:

```dotenv
DISCORD_TOKEN=replace_me
DISCORD_GUILD_ID=123456789012345678
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
sudo systemctl enable --now kanami
systemctl status kanami --no-pager
journalctl -u kanami -n 100 --no-pager
```

Unit запускает `/opt/kanami/.venv/bin/discord-stats-bot` от пользователя
`kanami`, с working directory `/opt/kanami` и EnvironmentFile
`/etc/kanami/kanami.env`. Миграции unit автоматически не запускает.

Основной installer не создаёт optional `kanami-web` user, отдельный
`kanami-web-admin.service` или `/etc/kanami/kanami-web-admin.env`. При
развёртывании Web Admin не подключайте к нему основной bot env: используйте
раздельный env example и настройте конкретный Git `safe.directory` по инструкции
[WEB_ADMIN_DEPLOYMENT.md](WEB_ADMIN_DEPLOYMENT.md#git-metadata-при-раздельных-service-users).

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

Для установленного экземпляра:

```bash
sudo /opt/kanami/scripts/update.sh
```

Скрипт проверит чистоту Git tree, выполнит fast-forward-only pull, locked
dependency sync и Alembic migration, обновит unit, перезапустит service и
покажет итоговый commit. При ошибке dependency/migration следующие этапы не
выполняются. Локальные изменения, user data и config он не удаляет.

Backup PostgreSQL перед значимым production update остаётся ответственностью
оператора; автоматическая backup/restore policy пока не входит в проект.
