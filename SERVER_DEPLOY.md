# GitHub → сервер → тестовий запуск

Docker на локальному Windows не потрібен: GitHub зберігає код і запускає тести, Linux-сервер виконує бота та PostgreSQL. Перший запуск виконуємо в окремій тестовій Telegram-групі, після підтвердження власника. Автоматичний деплой через SSH не налаштований: GitHub Actions лише перевіряє код.

## Публікація на GitHub

Корінь репозиторію — **upp_support_bot**, тобто bot.py, Dockerfile та .github лежать на верхньому рівні. Батьківська папка з .venv312 не входить до репозиторію. Рекомендований доступ — Private.

До Git додано **порожній .env (0 байтів)** за запитом власника; .env.example містить назви змінних і шаблони. Ключі, резервні копії й кеші виключені з Git. На сервері секрети заповнюються окремо. Оскільки .env тепер відстежується Git, правило .gitignore саме по собі не захищає його зміни: заповнений файл не комітьте й не надсилайте на GitHub. CI перевіряє порожність файлу, але не може запобігти витоку, якщо секрет уже потрапив у push.

Після push відкрийте вкладку Actions → Tests. Workflow збирає Dockerfile.test, запускає справжній PostgreSQL 16 і весь набір pytest, потім Ruff. Telegram-токен і SSH-доступ цьому workflow не потрібні. Очікуваний результат — **усі тести пройдено, без skipped**. Сам факт наявності workflow не означає, що він вже виконувався; перед запуском бота перевірте успішний run саме для потрібного commit SHA. Поточні локальні зміни відкритого доступу ще не перевірені цим workflow; результати й обмеження наведено у VALIDATION.md.

## Доступ до сервера

Потрібні адреса/SSH-аліас, користувач, ОС та вже налаштований спосіб входу. Приклад `ssh deploy@SERVER` є шаблоном. Паролі, токени і приватні ключі не передавайте в чат. Перший SSH host fingerprint звіряйте з даними хостинг-провайдера, не вимикайте StrictHostKeyChecking.

На Linux-сервері потрібні Git, Docker Engine і Compose plugin. Інструкція встановлення залежить від дистрибутива: [офіційна документація Docker Engine](https://docs.docker.com/engine/install/). Спочатку перевірте:

```bash
git --version
docker info
docker compose version
```

Для приватного репозиторію налаштуйте на сервері окремий read-only deploy key GitHub. Публічний ключ додається в Repository → Settings → Deploy keys без Allow write access. Приватна частина залишається на сервері. Не додавайте токен доступу в URL git clone.

## Перший clone на новий сервер

Нижче — команди для **нового розгортання** в домашній папці користувача. Репозиторій: https://github.com/Levis-Artur/vol_support_bot. Якщо папка, контейнер або volume вже існують, спочатку перевірте їх призначення; не очищуйте їх.

```bash
git clone https://github.com/Levis-Artur/vol_support_bot.git support-bot
cd support-bot
git rev-parse HEAD
docker compose -p support-bot-tests -f docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from tests
```

При будь-якій помилці тестів зупиніть розгортання й дослідіть її. Ця тестова БД ізольована, не використовує volume робочого бота, не публікує портів і не потребує .env. Тести можна запустити ще до налаштування Telegram.

## Секрети тільки на сервері

Після clone файл .env вже існує, але порожній. Заповніть його за прикладом; наявну непорожню конфігурацію не перезаписуйте:

```bash
umask 077
git update-index --skip-worktree .env
if [ ! -s .env ]; then cp .env.example .env; fi
chmod 600 .env
nano .env
```

Заповніть BOT_TOKEN тестового бота, ADMIN_IDS, IT_IDS, IT_GROUP_ID окремої тестової групи, DB_PASSWORD — довгий випадковий пароль. Можна використати захищений генератор паролів. skip-worktree приховує локальні зміни від звичайних операцій Git у цьому clone; це не захист секретів від навмисного додавання. Залишайте версію .env у Git порожньою. Docker build context виключає .env незалежно від її вмісту. Подробиці отримання Telegram ID — у FIRST_TEST_RUN.md.

Compose формує підключення до PostgreSQL самостійно; DATABASE_URL для контейнера не потрібна. Переконайтеся, що серверні змінні середовища не перевизначають значення з .env. Перевіряйте без виведення секретів:

```bash
docker compose -p support-bot config --quiet
```

Успішний config не доводить справжність токена або тестове призначення групи. До фактичного запуску власник має підтвердити перше підключення до Telegram і використання тестової групи.

## Запуск тільки після підтвердження

```bash
docker compose -p support-bot up --build -d
docker compose -p support-bot ps
docker compose -p support-bot logs --tail=100 bot
```

Завжди використовуйте **той самий project name support-bot**, щоб повторні запуски використовували той самий volume БД. Бот і PostgreSQL не відкривають вхідних портів. Для polling потрібен вихідний доступ до Telegram; домен, HTTPS-сертифікат і вебсервер не потрібні.

Healthcheck перевіряє БД, а не Telegram. Після запуску надішліть боту /id та виконайте контрольний сценарій з FIRST_TEST_RUN.md. Не запускайте другий екземпляр з тим самим токеном на Windows або іншому сервері.

## Подальше оновлення

Перед оновленням перевірте успішний CI для обраного коміту. Для переходу до відкритого прийому заявок **спочатку зупиніть бота й виконайте резервне копіювання та пробне відновлення за [MIGRATION_OPEN_ACCESS.md](MIGRATION_OPEN_ACCESS.md)**. Нова версія автоматично мігрує старі заявки при старті; після міграції вони поступово надійдуть у налаштовану ІТ-групу. Після цього у тому самому каталозі:

```bash
git pull --ff-only
git rev-parse HEAD
docker compose -p support-bot config --quiet
docker compose -p support-bot up --build -d
docker compose -p support-bot ps
docker compose -p support-bot logs --tail=100 bot
```

Якщо git pull відмовляє через локальні зміни, з'ясуйте причину, не застосовуйте reset --hard. Зупинка без видалення даних: `docker compose -p support-bot stop`. Не використовуйте down -v, volume prune або system prune для робочого середовища. Зміна DB_PASSWORD у .env не змінює пароль уже створеної ролі PostgreSQL.

Опис застосування Compose на сервері: [офіційна документація](https://docs.docker.com/compose/how-tos/production/).
