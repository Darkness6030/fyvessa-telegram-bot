# Fyvessa Telegram Bot + Mini App

Один Python-проект для Telegram-бота и клиентского Mini App. Web-интерфейс рендерится Jinja2 и стилизуется Tailwind CSS; отдельного frontend-проекта и web-админки нет.

## Локальный запуск

```bash
cp .env.example .env
docker compose up --build
```

Compose поднимает приложение на `http://localhost:8080` и PostgreSQL 16 во внутренней сети. Данные БД сохраняются в именованном volume `postgres_data`. Для полноценного запуска нужен настоящий `TELEGRAM_TOKEN`. Адрес `MINI_APP_URL` для Telegram должен быть публичным HTTPS URL. Публичный каталог можно открыть в браузере, но персональные API-запросы работают только внутри Telegram: frontend передаёт `Telegram.WebApp.initData` в заголовке `Authorization` при каждом запросе.

Для запуска Python вне контейнера сначала поднимите `database`, опубликуйте его порт либо укажите доступный PostgreSQL в `DATABASE_URL`. SQLite больше не используется.

## Каталог

Источник товаров — `assets/products.xlsx`, лист `products`. `sku` нельзя менять после публикации товара: по нему синхронизация находит существующую запись. Новые категории создаются автоматически по колонке `category`. Товары, удалённые из полной таблицы, скрываются, а не удаляются из БД.

Администратор после изменения файла запускает в боте `/sync_products`. При старте web-приложения синхронизация также выполняется автоматически. В будущем `src/catalog.py` получит Google Sheets adapter, возвращающий тот же `CatalogRow`.

## Структура

- `src/main_flow.py` — только пользовательские Telegram-сценарии;
- `src/admin_flow.py` — все административные Telegram-сценарии;
- `src/routes.py` — клиентские Mini App routes;
- `src/catalog.py` — валидация и синхронизация Excel;
- `src/models.py` — модели `rewire_sqlmodel`;
- `Dockerfile`, `docker-compose.yml` — приложение и PostgreSQL 16;
- `templates/`, `static/` — Jinja2 + Tailwind клиентский интерфейс;
- `docs/GOAL.md`, `docs/RULES.md`, `docs/PLAN.md` — цель, ограничения и этапы проекта;
- `docs/TZ_AUDIT.md` — сверка текущей реализации с каждым блоком исходного ТЗ.
