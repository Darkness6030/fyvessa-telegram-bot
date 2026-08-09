# Fyvessa Telegram Bot + Mini App

Один Python-проект для Telegram-бота и клиентского Mini App. Web-интерфейс рендерится Jinja2 и стилизуется Tailwind CSS; отдельного frontend-проекта и web-админки нет.

## Локальный запуск

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
set -a && source .env && set +a
.venv/bin/python main.py
```

Для полноценного запуска нужен настоящий `TELEGRAM_TOKEN`. Адрес `MINI_APP_URL` для Telegram должен быть публичным HTTPS URL. Публичный каталог можно открыть в браузере, но персональные API-запросы работают только внутри Telegram: frontend передаёт `Telegram.WebApp.initData` в заголовке `Authorization` при каждом запросе.

## Каталог

Источник товаров — `assets/products.xlsx`, лист `products`. `sku` нельзя менять после публикации товара: по нему синхронизация находит существующую запись. Новые категории создаются автоматически по колонке `category`. Товары, удалённые из полной таблицы, скрываются, а не удаляются из БД.

Администратор после изменения файла запускает в боте `/sync_products`. При старте web-приложения синхронизация также выполняется автоматически. В будущем `src/catalog.py` получит Google Sheets adapter, возвращающий тот же `CatalogRow`.

## Структура

- `src/main_flow.py` — только пользовательские Telegram-сценарии;
- `src/admin_flow.py` — все административные Telegram-сценарии;
- `src/routes.py` — клиентские Mini App routes;
- `src/catalog.py` — валидация и синхронизация Excel;
- `src/models.py` — модели `rewire_sqlmodel`;
- `templates/`, `static/` — Jinja2 + Tailwind клиентский интерфейс;
- `docs/GOAL.md`, `docs/RULES.md`, `docs/PLAN.md` — цель, ограничения и этапы проекта;
- `docs/TZ_AUDIT.md` — сверка текущей реализации с каждым блоком исходного ТЗ.
