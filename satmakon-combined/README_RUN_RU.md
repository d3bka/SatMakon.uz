# SATMakon: запуск бота и формы заявок

## 1. Безопасность

Старый Telegram-токен был показан в логах. Отзовите его через `@BotFather`:

1. `/revoke`
2. выберите `@makonadminbot`
3. получите новый токен
4. внесите новый токен в `.env`

Не загружайте `.env` в GitHub.

## 2. Настройка

Скопируйте `.env.example` в `.env`:

```powershell
Copy-Item .env.example .env
```

Заполните минимум:

```env
TELEGRAM_BOT_TOKEN=НОВЫЙ_ТОКЕН
ADMIN_GROUP_CHAT_ID=-5522358410
```

`DEEPSEEK_API_KEY` необязателен для приёма заявок. Без оплаченного DeepSeek бот попросит номер телефона и всё равно отправит заявку администраторам.

## 3. Установка

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 4. Проверка группы

```powershell
.\.venv\Scripts\python.exe test_group.py
```

В группе должно появиться тестовое сообщение.

## 5. Запуск

```powershell
.\.venv\Scripts\python.exe bot.py
```

После запуска:

- сайт: `http://127.0.0.1:8000/`
- проверка API: `http://127.0.0.1:8000/health`
- форма отправляет заявки на `/api/leads`
- Telegram-бот работает через polling

Открывайте сайт по адресу `http://127.0.0.1:8000/`, а не двойным кликом по `index.html`.

## 6. Публичный сайт

Локальный адрес `127.0.0.1` доступен только на вашем компьютере. Для реальных посетителей backend нужно развернуть как web service на VPS/Render/Railway и установить:

```env
LEAD_API_HOST=0.0.0.0
LEAD_ALLOWED_ORIGINS=https://ВАШ-ДОМЕН
```

Самый простой вариант — размещать сайт и API вместе из этого проекта. Тогда форма использует `/api/leads` и отдельная настройка URL не нужна.

Если сайт остаётся на GitHub Pages, перед кодом приложения задайте публичный backend URL:

```html
<script>
  window.SATMAKON_LEAD_API_URL = "https://api.example.com/api/leads";
</script>
```
