# AGENTS.md

Руководство для агентов, работающих над репозиторием `pg-tasks`.

## О проекте

`pg-tasks` — расширение PostgreSQL, добавляющее функционал для управления
очередями задач для внешних процессов. Вдохновлено
[pgmq](https://github.com/pgmq/pgmq).

Ключевые ограничения/принципы:
- Только `plpgsql` и чистый SQL, **без внешних зависимостей**.
- **Без** background workers.
- **Без** JSON — только нативные типы.
- Максимальное переиспользование планов запросов (сгенерированные функции — SQL
  функции, чтобы PG собирал generic-планы).
- Воркеры могут быть написаны на любом языке.

## Структура репозитория

- `pg_tasks/pg_tasks.control` — метаданные расширения (версия `1.3`,
  `relocatable = false`, `trusted = true`, схема `tasks`).
- `pg_tasks/pg_tasks--1.0.sql` — полный скрипт версии 1.0 (устанавливается при
  `CREATE EXTENSION` в свежей БД).
- `pg_tasks/pg_tasks--1.0--1.1.sql` — миграция 1.0 → 1.1.
- `pg_tasks/pg_tasks--1.1--1.2.sql` — миграция 1.1 → 1.2 (FK cascade и
  исправления `register()`: `notification_interval`, NULL-безопасность,
  `max_retries`, `retry`).
- `pg_tasks/pg_tasks--1.2--1.3.sql` — миграция 1.2 → 1.3 (`acquire` через
  `id = ANY(ARRAY(...))` вместо `UPDATE ... FROM (подзапрос)`; перегенерирует
  `acquire` для уже зарегистрированных типов).
- `pg_tasks/pg_tasks.sql` — "плоский" скрипт для прямой установки (см. README).
- `README.md` — документация (на русском).
- `docker-compose.yml` — test PostgreSQL 16, монтирует исходники расширения в
  `SHAREDIR/extension`, публикует хост-порт `5433`.
- `tests/` — тесты pytest (подключаются к БД на `localhost:5433`).
- `requirements-dev.txt` — зависимости для запуска тестов.
- `test.sql` — ручной smoke-тест.
- `LICENSE` — MIT.

## Сборка, установка, тестирование

Инструментов сборки нет: расширение состоит только из SQL.

Прямая установка (без `CREATE EXTENSION`):
```shell
psql -f pg_tasks/pg_tasks.sql your_db
```

Установка как расширение:
```sql
CREATE SCHEMA tasks;
CREATE EXTENSION pg_tasks SCHEMA tasks;
```

Дымовой тест (внимание: удаляет схему `tasks`):
```shell
psql -f test.sql your_db
```

Тесты pytest (тестовая БД должна быть поднята):
```shell
pip install -r requirements-dev.txt
docker compose up -d --wait
pytest
```

Тесты подключаются к `postgresql://tester:pg_tasks@localhost:5433/pg_tasks`
(переопределяется переменной окружения `PG_TASKS_DSN`). Каждый тест ставит
свежее расширение и откатывает изменения; конкурентные сценарии используют
fixture `committed_db` с autocommit.

## Архитектура

Расширение создает в схеме `tasks` (название схемы зафиксировано в control-файле):

- `tasks.template` — таблица-шаблон. Пользовательские таблицы задач создаются
  через `LIKE tasks.template INCLUDING DEFAULTS INCLUDING IDENTITY INCLUDING INDEXES`
  с добавлением произвольных полей (например, `payload`).
- `tasks.types` — реестр зарегистрированных типов задач. **Только для чтения**,
  пользователь не должен менять записи вручную.
- `tasks.last_notifications` — UNLOGGED таблица для троттлинга NOTIFY.
- `tasks.register(schema_name, table_name, timeout, max_retries,
  notification_interval, storage_time)` — регистрирует таблицу задачи и
  динамически создает для нее объекты.
- `tasks.unregister(schema_name, table_name)` — идемпотентно удаляет служебные
  объекты и запись из реестра.
- `tasks.create_stats_view()` — пересоздает агрегированное представление
  `tasks.stats` поверх представлений `stats_of_*`.

На каждый зарегистрированный тип задач генерируются (через `EXECUTE format(...)`):
- `acquire_<schema>_<table>` — взять задачи (`FOR UPDATE SKIP LOCKED`).
- `finish_<schema>_<table>` — отметить выполненной.
- `retry_<schema>_<table>` — отложить повтор.
- `cancel_<schema>_<table>` — отменить.
- `reset_<schema>_<table>` — сбросить состояние.
- `clean_<schema>_<table>` — удалить старые завершенные.
- `stats_of_<schema>_<table>` — представление статистики.

Триггер `notify_on_new` на таблице задач рассылает `pg_notify('pg_tasks', ...)`.

## Соглашения и подводные камни

### Версионирование расширения
- Изменения объекта расширения требуют новой версии: bump
  `default_version` в `pg_tasks.control` и добавь файл миграции
  `pg_tasks--<old>--<new>.sql`.
- Миграции должны быть идемпотентными насколько возможно (см. исправление
  `unregister` в `1.0--1.1`; FK `last_notifications` пересоздан с
  `ON DELETE CASCADE` в `1.1--1.2`; `register()` исправлен в `1.1--1.2`).
- Если меняется текст генерируемых функций, миграция должна перегенерировать
  их для типов из `tasks.types` и отвязать от расширения
  (`ALTER EXTENSION pg_tasks DROP FUNCTION ...`): объекты, созданные скриптом
  расширения, иначе становятся его членами, а созданные `register()` — нет.

### Производительность `acquire`
- SQL-функции исполняются по generic-плану, `max_num` планировщику неизвестен.
  Форма `UPDATE t ... FROM (SELECT id ... LIMIT max_num) a WHERE t.id = a.id`
  планируется как hash join с seq scan всей таблицы (на 10M строк ~2 с на
  вызов). Используй `WHERE id = ANY(ARRAY(SELECT ...))` — всегда индекс по PK.
  Регрессию ловит `test_acquire_does_not_scan_whole_table`.
- `pg_tasks.sql` — плоская копия текущей версии, держи его в синхроне с
  актуальным `pg_tasks--<version>.sql` при правках.

### Динамический DDL через `format()`
- Имена объектов форматируются с `%I` (identifier), литералы — с `%L`
  (literal). Не смешивай.
- `register()` использует смесь: для `'tasks'` как схемы-владельца — `%1$I`,
  для `format('acquire_%s_%s', ...)` — `%2$s` (уже заквотированная строка).
  Будь внимателен, где аргумент уже экранирован, а где нет.
- Значение `NULL` в `format()` с плейсхолдером `%s` превращается в пустую
  строку (не `NULL`). Литералы, участвующие в SQL-логике (`%5$s`/`%5$L`),
  форматируй как `%L`, чтобы получить корректный `NULL`.
- `max_retries` трактуется как общее число попыток `acquire` включительно
  (условие `execution_count < max_retries`), а `retry(planned_to := NULL)`
  перевзводит задачу на `now()`.
- `unregister()` конструирует имена через `'DROP FUNCTION tasks.acquire_%s_%s'`
  с `%s` — при нестандартных именах таблиц/схем это может сломаться, но это
  текущее принятое поведение.

### Данные и типы
- `timeout`, `notification_interval`, `storage_time` — типы `interval`,
  могут быть `NULL` (NULL = выключено/вечно). Учитывай NULL-семантику во всех
  сгенерированных запросах.
- Задачи: внутренние (внутри транзакции с БД) и внешние (без удержания
  транзакции) — см. README.

### Документация
- Проект документирован на русском. Новые функции/параметры описывай в
  `README.md`, раздел "Публичные объекты".

## Проверка изменений

Перед фиксацией:
1. Убедись, что SQL валиден: `psql -f pg_tasks/pg_tasks.sql` в тестовой БД.
2. Прогони `test.sql`.
3. Проверь путь миграции: установи старую версию, затем примени
   `pg_tasks--<old>--<new>.sql`.
