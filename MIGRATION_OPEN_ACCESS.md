# Перехід до відкритого прийому заявок

Міграція `migrations/001_open_access.sql` виконується автоматично при запуску нової версії в одній транзакції зі схемою. **Для наявної БД спочатку зупиніть старого бота, створіть резервну копію й перевірте відновлення.** Наведені команди виконує адміністратор на Linux-сервері в Bash із каталогу проєкту. Використовуйте той самий Compose project name, що й раніше; нижче це `support-bot`. Зміна назви створить інший volume замість використання наявної БД.

## 1. Резервна копія до оновлення

Зупиняємо лише бота; PostgreSQL залишається запущеним. Не виконуйте `down -v`, `volume prune`, `system prune` чи видалення бази.

```bash
set -euo pipefail
umask 077
docker compose -p support-bot stop bot
mkdir -p backups
chmod 700 backups
backup="backups/support-before-open-access-$(date -u +%Y%m%dT%H%M%SZ).dump"
test ! -e "$backup"
docker compose -p support-bot exec -T db pg_dump -U support -d support -Fc > "$backup"
test -s "$backup"
docker compose -p support-bot exec -T db pg_restore --list < "$backup" > /dev/null
```

Пароль не передається в командному рядку; використовується локальне підключення всередині контейнера PostgreSQL. Якщо локальна політика автентифікації змінена, налаштуйте захищений `.pgpass` адміністратора. Не друкуйте `.env`. Скопіюйте dump у захищене сховище поза цим сервером; файли `backups/` виключені з Git та Docker context. `pg_restore --list` перевіряє читання архіву, але не замінює пробного відновлення.

## 2. Перевірка відновлення в нову окрему БД

У тій самій Bash-сесії, де задана змінна `backup`:

```bash
restore_db="support_restore_test_$(date -u +%Y%m%dT%H%M%SZ)"
docker compose -p support-bot exec -T db createdb -U support "$restore_db"
docker compose -p support-bot exec -T db pg_restore -U support -d "$restore_db" \
  --exit-on-error --no-owner --no-privileges < "$backup"
docker compose -p support-bot exec -T db psql -U support -d "$restore_db" \
  -v ON_ERROR_STOP=1 -c 'SELECT count(*) AS restored_tickets FROM tickets;'
docker compose -p support-bot exec -T db psql -U support -d support \
  -v ON_ERROR_STOP=1 -c 'SELECT count(*) AS original_tickets FROM tickets;'
```

Порівняйте кількості. Якщо будь-яка команда завершується помилкою, не переходьте до міграції. Команди не замінюють робочу БД; створена тестова копія залишається для перевірки й має ті самі обмеження доступу до даних. Її видалення тут не передбачене.

## 3. Оновлення та міграція без підключення до Telegram

Після успішного CI для потрібного коміту:

```bash
git pull --ff-only
git rev-parse HEAD
docker compose -p support-bot config --quiet
{ cat schema.sql; cat migrations/001_open_access.sql; } | \
  docker compose -p support-bot exec -T db psql -U support -d support \
    -v ON_ERROR_STOP=1 --single-transaction
```

Цей крок змінює тільки PostgreSQL і не запускає бота. `--single-transaction` та `ON_ERROR_STOP` забезпечують відкат усього кроку при помилці, зокрема при неможливості записати аудит. Прямий запуск SQL без транзакції не рекомендований.

Міграція:

- Переводить `pending_verification` у `new` лише для незаблокованих авторів.
- Зберігає номери, авторів, час створення, текст, фото, виконавців і маркери доставки.
- Записує один перехід у `ticket_events`; `actor_id=NULL` означає системну міграцію.
- Призначає `next_delivery_at` з інтервалом 10 секунд за номером заявки. Worker обробляє до 5 заявок за цикл із паузами між ними; міграція не надсилає повідомлень.
- При повторному виконанні не змінює вже переведені заявки й не дублює події.
- Залишає `verified` та старі адміністративні події для сумісності, але прибирає їхній вплив на доступ. Не розблоковує користувачів, зокрема ІТ.
- Старі заявки заблокованих авторів залишаються архівними. Лише явна `/unblock ID` пізніше переводить їх у `new`, записує адміністратора в аудит і ставить на поступову доставку.

Перевірка лише агрегатів, без персональних даних:

```bash
docker compose -p support-bot exec -T db psql -U support -d support -v ON_ERROR_STOP=1 -c \
  "SELECT t.status, u.blocked, count(*) FROM tickets t JOIN users u ON u.telegram_id=t.creator_id GROUP BY t.status,u.blocked ORDER BY t.status,u.blocked;"
```

Не повинно залишитися `pending_verification` із `blocked=false`. Загальна кількість заявок має відповідати резервній копії. На тестовій копії можна виконати ту саму SQL-міграцію, замінивши `-d support` на `-d "$restore_db"`, і повторити її для перевірки ідемпотентності.

## 4. Запуск і ручна перевірка

Після перевірки налаштувань потрібного бота й групи:

```bash
docker compose -p support-bot up --build -d
docker compose -p support-bot ps
docker compose -p support-bot logs --tail=100 bot
```

Повторна стартова міграція безпечна. Перевірте сценарій із [FIRST_TEST_RUN.md](FIRST_TEST_RUN.md) у тестовій групі на синтетичних даних. Не спрямовуйте копію робочої БД у сторонню тестову групу: вона містить справжні заявки.

Якщо потрібне відновлення, спочатку зупиніть бота, збережіть поточний стан окремим dump і перевірте копію в окремій БД. Заміна робочої БД потребує окремого узгодженого плану; цей документ не містить команд знищення даних. Не запускайте стару версію бота поверх мігрованої БД: вона поверне старі правила підтвердження.

Формат резервної копії: [PostgreSQL 16 — pg_dump](https://www.postgresql.org/docs/16/app-pgdump.html).
