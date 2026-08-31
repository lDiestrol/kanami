# Публичное развёртывание Kanami Web Admin

Web Admin публикуется только через TLS reverse proxy. Процесс Web Admin не имеет
`DISCORD_TOKEN`, работает от отдельного пользователя, использует отдельную
PostgreSQL role с минимально необходимыми правами и вызывает
bot-profile/managed-access операции только через `127.0.0.1:8765`.
Control API никогда не публикуется и не слушает LAN.

## Поддерживаемые схемы

### 1. Standalone: Caddy и Kanami на одной VM

Рекомендуемый вариант, если готового reverse proxy нет:

```text
Internet
  -> Caddy :443
  -> 127.0.0.1:8000
  -> Kanami Web Admin
```

Оставьте `WEB_ADMIN_HOST=127.0.0.1` и используйте
[`deploy/caddy/Caddyfile.example`](../deploy/caddy/Caddyfile.example). Caddy получает
публичный сертификат автоматически и штатно перенаправляет HTTP на HTTPS; отдельный
redirect block не нужен. `tls internal` для публичного домена не используется.

### 2. Существующий proxy на той же VM

```text
Internet
  -> Nginx/Caddy/Traefik :443
  -> 127.0.0.1:8000
  -> Kanami Web Admin
```

Kanami Caddy не устанавливается. Для Nginx используйте
[`deploy/nginx/kanami-same-host.conf.example`](../deploy/nginx/kanami-same-host.conf.example)
и приложенный snippet. Bind и здесь остаётся loopback без дополнительных флагов.

### 3. Центральный proxy на отдельной VM

```text
Internet
  -> Nginx/Caddy/Traefik на proxy VM :443
  -> private IP Kanami VM:8000
  -> Kanami Web Admin
```

На Kanami VM задайте ровно один адрес внутреннего интерфейса:

```dotenv
WEB_ADMIN_HOST=192.168.50.10
WEB_ADMIN_ALLOW_PRIVATE_BIND=true
```

Поддерживаются literal RFC1918 IPv4 и IPv6 ULA. Hostname, wildcard `0.0.0.0`/`::`,
link-local и публичный IP отклоняются при startup. При private bind процесс пишет
warning о необходимости firewall. Firewall Kanami VM обязан разрешать TCP/8000
только с IP proxy VM. Используйте
[`deploy/nginx/kanami-remote-proxy.conf.example`](../deploy/nginx/kanami-remote-proxy.conf.example).

Цепочка `Internet -> Nginx -> Caddy -> Web Admin` технически допустима, но обычно
не нужна: два proxy усложняют TLS, диагностику и заголовки. Штатно выбирайте один.

## Домен, TLS и Discord OAuth

Для публичного URL `https://kanami.example.com/admin/` callback должен в точности
равняться:

```text
https://kanami.example.com/admin/auth/discord/callback
```

То же значение задаётся в `WEB_ADMIN_DISCORD_REDIRECT_URI` и Discord Developer
Portal. В production обязательно `WEB_ADMIN_COOKIE_SECURE=true`. OAuth scope остаётся
только `identify`; `guilds`, `administrator` и другие scopes не нужны.

Uvicorn запускается с отключённым доверием к proxy headers. Авторизация, redirect URI
и Secure cookie не вычисляются из `Host`, `Forwarded` или `X-Forwarded-*`. Примеры
Nginx дополнительно очищают эти входные заголовки. Поэтому trusted-proxy модель в
приложении не нужна.

Приложение централизованно добавляет CSP, `nosniff`, `DENY` framing,
`Referrer-Policy: no-referrer`, минимальную Permissions Policy и `Cache-Control:
no-store`. HSTS задаётся на TLS proxy, а не HTTP backend. Включайте HSTS только после
настройки DNS/TLS и успешных HTTPS, OAuth и write-action smoke tests: браузер
запоминает политику и до истечения `max-age` может отказаться открывать HTTP. Поэтому
во всех Caddy/Nginx examples годовой HSTS по умолчанию закомментирован и включается
оператором только после этих проверок. Оба Nginx examples содержат отдельный port 80
server block, перенаправляющий запросы на HTTPS; основной Web Admin proxy остаётся на
443.

Все официальные reverse-proxy examples публикуют только Web Admin namespace и
используют единый routing contract:

```text
/          -> 302 /admin/
/admin/*   -> Kanami Web Admin
остальные публичные paths -> 404
```

Поэтому пользователь может открыть просто `https://kanami.example.com/`, после
чего reverse proxy перенаправит браузер на `/admin/`. Произвольные application
paths, включая `/control`, наружу не проксируются. Bot Control на TCP/8765
по-прежнему доступен только через loopback и не является частью публичного HTTP
routing.

## Sessions, write authorization и abuse protection

Cookies содержат только случайные opaque ID, имеют `HttpOnly`, `SameSite=Lax`,
ограниченный `Path`, абсолютный lifetime (по умолчанию 8 часов) и в production
`Secure`. Session и CSRF находятся в bounded server-side memory и исчезают при
restart. `WEB_ADMIN_COOKIE_SECURE=false` разрешён только для loopback HTTP разработки.

Перед nickname/avatar update/reset приложение сначала проверяет session и CSRF,
затем повторно определяет OWNER/ADMIN и проверяет current non-bot guild membership. Потерянная
авторизация или ошибка БД даёт нейтральный 403, отзывает session и не вызывает control
API. Такой порядок не позволяет запросу с неверным CSRF использовать DB check как
authorization oracle. Успешно авторизованные writes ограничены bounded in-memory
limiter: 10 операций на session за 60 секунд, максимум 1024 ключа; restart очищает его.

`GET /admin/administrators` и оба POST доступны только fresh-authorized OWNER.
Два ID из `WEB_ADMIN_ALLOWED_USER_IDS` остаются постоянными OWNER, показываются
без revoke controls и не заменяются DB grants. Managed ADMIN являются
дополнительными; grant допускает только current non-bot member, а mutation идёт
через фиксированные Bot Control endpoints. Единственное прямое write-направление
Web Admin — Rules management и связанный audit outbox; остальные изменения идут
через Bot Control.

Nginx примеры добавляют мягкие network limits для login, callback и settings. Они
используют реальный TCP client address самого Nginx, а не forwarded headers. Vanilla
Caddy не имеет удобного встроенного rate limiter; сторонний plugin ради этого не
требуется. При необходимости network/WAF limiting для Caddy обеспечивается внешним
edge или firewall, а application limiter продолжает защищать writes.

## Health check

`GET /admin/health` намеренно остаётся без session для systemd/proxy monitoring. Он
выполняет реальный `SELECT 1`, возвращает `200 {"status":"healthy"}` или
`503 {"status":"unhealthy"}` и не раскрывает имя БД, connection string, latency или
текст исключения.

## Firewall

Same-host deployment:

```text
public:   TCP/80, TCP/443 -> reverse proxy
loopback: 127.0.0.1:8000  -> Web Admin
loopback: 127.0.0.1:8765  -> bot control (ALWAYS)
loopback: 127.0.0.1:5432  -> PostgreSQL (recommended)
```

Remote-proxy deployment:

```text
Kanami VM TCP/8000 <- ONLY reverse-proxy VM private IP
Kanami VM TCP/8765 <- loopback only, no exceptions
Kanami VM TCP/5432 <- loopback only in the recommended architecture
```

Пример UFW — замените placeholder после проверки адреса, не копируйте вслепую:

```bash
sudo ufw allow from REVERSE_PROXY_PRIVATE_IP to any port 8000 proto tcp
sudo ufw deny 8000/tcp
```

Порядок UFW rules зависит от существующей политики; сначала проверьте `ufw status
numbered` и не потеряйте SSH-доступ. Kanami не меняет iptables/nftables/UFW
автоматически. SSH не относится к Web Admin. Порты 8000, 8765 и 5432 нельзя открывать
в Internet.

## Разделение процессов и secrets

Рекомендуются users `kanami` и `kanami-web`. Пример Web Admin unit находится в
[`deploy/systemd/kanami-web-admin.service.example`](../deploy/systemd/kanami-web-admin.service.example).

```text
/etc/kanami/kanami.env
  root:kanami 0640
  DISCORD_TOKEN
  DISCORD_BOT_CONTROL_SHARED_SECRET

/etc/kanami/kanami-web-admin.env
  root:kanami-web 0640
  DATABASE_URL (отдельная least-privilege PostgreSQL role)
  DISCORD_GUILD_ID
  REPORT_TIMEZONE
  GAME_TRACKING_ENABLED
  GAME_CONFIRM_INTERVAL_SECONDS
  WEB_ADMIN_DISCORD_CLIENT_SECRET
  WEB_ADMIN_BOT_CONTROL_SHARED_SECRET
  (никогда не DISCORD_TOKEN)
```

systemd читает `EnvironmentFile` до privilege drop, поэтому runtime user может не
иметь прямого доступа к файлу. Shared control secret должен быть отдельным случайным
значением не короче 32 символов и совпадать в bot/web env. После добавления Rules
Admin web connection создаётся с `read_only=False`, поэтому PostgreSQL connection
не получает `default_transaction_read_only=on`. Это не означает право записи во
всю схему: production Web Admin DB role должна получать только необходимые
`SELECT` grants для dashboard/authorization/operations и только необходимые
`INSERT`/`UPDATE`/`DELETE` grants для `rulesets` и `audit_events` (плюс доступ к
используемым ими sequences). Не выдавайте этой роли ownership схемы/database,
`CREATE`, `TRUNCATE` или неограниченные write grants. Реальные секреты и настоящий
`.env` не сохраняются в Git.

За основу web-файла используйте отдельный безопасный пример
[`deploy/systemd/kanami-web-admin.env.example`](../deploy/systemd/kanami-web-admin.env.example),
а не `/etc/kanami/kanami.env` основного bot process. Как минимум следующие
несекретные runtime settings должны явно совпадать в обоих env:

| Переменная | Зачем нужна Web Admin |
| --- | --- |
| `DISCORD_GUILD_ID` | Ограничивает все read models одним configured guild. |
| `REPORT_TIMEZONE` | Сохраняет общие календарные границы и отображение времени. |
| `VOICE_CHECKPOINT_INTERVAL_SECONDS` | Синхронизирует Voice freshness threshold на `/admin/system`; не запускает checkpoint в Web Admin. |
| `GAME_TRACKING_ENABLED` | Только отображает фактическое bot-side состояние на `/admin/system`. |
| `GAME_CONFIRM_INTERVAL_SECONDS` | Только отображает фактический bot-side checkpoint interval. |

`GAME_TRACKING_ENABLED=true` в web env **не запускает tracker**, не включает
Presence Intent и не создаёт Discord runtime: этим владеет только
`kanami.service` с bot env. Расхождение двух env не меняет работу tracker, но
делает Operations page фактически неверной. Аналогично рекомендуется повторить
`VOICE_MIN_SESSION_SECONDS`, потому что Web Dashboard использует общий threshold.
`VOICE_CHECKPOINT_INTERVAL_SECONDS` является существующим bot setting и теперь
также входит в безопасный shared runtime context; это не новый config knob.

W1.3 не меняет это разделение: минутные
`operational_health_observations` пишет и очищает только основной
`kanami.service` с обычной bot-side DB role. Web Admin читает историю через свою
отдельную role и не должен получать для этой таблицы `INSERT`/`DELETE`. Перед
первым запуском W1.3 штатно выполните `alembic upgrade head`; новый env key не
требуется.
Сразу после deployment окна 24h/7d корректно отображаются как частичные.
Если ранее созданная web role получила read grants через разовые команды без
`ALTER DEFAULT PRIVILEGES`, после migration отдельно выдайте ей только
`GRANT SELECT ON TABLE operational_health_observations` (в примере роль
`kanami_web_readonly`). Не выдавайте этой роли доступ к sequence и права
`INSERT`, `UPDATE`, `DELETE` или `TRUNCATE`. Штатный `kanami_app` владеет
database и получает bot-side write права как owner.

### Git metadata при раздельных service users

Checkout `/opt/kanami` остаётся во владении `kanami:kanami`, а Web Admin
запускается как `kanami-web` без write-доступа к repository. Современный Git
отклоняет repository другого владельца как `dubious ownership`, пока оператор
не разрешит конкретный путь для конкретного web user. После создания home
настройте ровно `/opt/kanami`:

```bash
sudo install -d -m 0750 -o kanami-web -g kanami-web /var/lib/kanami-web
sudo runuser -u kanami-web -- env HOME=/var/lib/kanami-web \
  git config --global --add safe.directory /opt/kanami
sudo runuser -u kanami-web -- env HOME=/var/lib/kanami-web \
  git -C /opt/kanami rev-parse --short HEAD
```

Пример unit явно задаёт `HOME=/var/lib/kanami-web`, поэтому application Git
subprocess читает тот же user-scoped config. `safe.directory` снимает только
Git-проверку доверия и не выдаёт filesystem write permissions. Не меняйте owner
checkout, не добавляйте `kanami-web` write-доступ и не используйте
`safe.directory=*`.

Эта настройка намеренно остаётся deployment-командой. `scripts/install.sh` и
`scripts/update.sh` управляют только основным `kanami.service`, не создают
`kanami-web`/его home/web env и не должны молча изменять global Git config
optional web-пользователя. Application code сохраняет безопасный fallback
`Unknown`, если Git или metadata недоступны.

## Checklist перед публикацией

- [ ] Public DNS настроен.
- [ ] Установлен действующий публичный TLS certificate.
- [ ] `WEB_ADMIN_COOKIE_SECURE=true`.
- [ ] OAuth redirect совпадает в точности.
- [ ] Настроены allowed admin IDs.
- [ ] Web Admin запущен отдельным Linux user.
- [ ] Shared runtime settings явно синхронизированы в отдельном web env.
- [ ] Для `kanami-web` разрешён только `safe.directory=/opt/kanami`; Git metadata видна.
- [ ] Web DB role имеет только scoped Rules/audit writes и необходимые read grants.
- [ ] Bot control слушает только loopback.
- [ ] Firewall проверен.
- [ ] TCP/8000 не доступен напрямую из Internet.
- [ ] TCP/8765 не опубликован.
- [ ] TCP/5432 не опубликован.
- [ ] Secrets отсутствуют в repository и логах.
- [ ] Сделан backup.
- [ ] Выполнен OAuth login smoke test.
- [ ] Выполнены nickname/avatar/reset smoke tests.
- [ ] Оба env OWNER видны и защищены; managed ADMIN grant/revoke smoke выполнен.
