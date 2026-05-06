# MqttBridge – Техническая справка

## Структура модуля

Ключевые файлы:

| Файл | Ответственность |
| --- | --- |
| `plugins/MqttBridge/__init__.py` | Основной класс плагина, подключение к MQTT, proxy‑хуки, фильтрация, статистика |
| `plugins/MqttBridge/forms/SettingForms.py` | Форма настроек (host/port/login/password, prefix, whitelist/blacklist) |
| `plugins/MqttBridge/templates/main_mqtt_bridge.html` | Админ‑шаблон с Vue и WebSocket‑метриками |
| `plugins/MqttBridge/static/js/admin.js` | Vue‑приложение, клиент Socket.IO, отрисовка статистики |
| `plugins/MqttBridge/translations/*.json` | Локализованные строки интерфейса |

Плагин наследует `BasePlugin` и объявляет действия:

- `proxy` – принимает события `changeProperty` от `ObjectManager`;
- `cycle` – выполняет периодическую задачу для отправки статистики в WebSocket.

---

## Архитектура выполнения

### Общий сценарий

1. При старте вызывается `initialization()`:
   - `_rebuild_whitelist_cache()` парсит конфиг в наборы whitelist/blacklist;
   - `_connect_mqtt()` создаёт MQTT‑клиент и подключается к брокеру.
2. Когда в osysHome меняется любое свойство объекта, `ObjectManager` вызывает:
   - `MqttBridge.changeProperty(obj, prop, value)` (из‑за объявленного `proxy`).
3. `changeProperty(...)`:
   - проверяет готовность, подавление эхо и фильтры;
   - вычисляет MQTT‑топик;
   - публикует сообщение.
4. Подписки MQTT приносят входящие сообщения в `_on_message(...)`:
   - парсит топик;
   - вызывает `_mark_suppressed(...)`, чтобы при следующем `changeProperty` не было повторной публикации;
   - вызывает `setProperty` для целевого объекта/свойства.
5. Действие `cycle` запускает `cyclic_task()`:
   - собирает снэпшот статистики;
   - отправляет его через `sendDataToWebsocket` в `wsServer`.
6. `admin.js` подписывается на данные типа `MqttBridge` в `wsServer` и обновляет модель Vue.

---

## Интеграция с MQTT

### Жизненный цикл клиента

Используется `paho-mqtt`:

- `_connect_mqtt()`:
  - создаёт экземпляр `mqtt.Client`;
  - настраивает username/password, если заданы;
  - вешает колбэки `_on_connect`, `_on_disconnect`, `_on_message`;
  - запускает сетевой цикл в фоне (`loop_start()`).
- `_reconnect_mqtt()`:
  - останавливает текущий клиент (если есть);
  - сбрасывает состояние подключения;
  - вызывает `_connect_mqtt()` заново.

Состояние подключения:

- `_connection_status`: `"disconnected" | "connecting" | "connected" | "error"`;
- `_connection_error`: последняя ошибка (строкой);
- `_connected_at_ts`: UNIX‑время последнего успешного подключения.

При смене состояния вызывается отправка `connectionStatus` через WebSocket.

### Топики и подписки

Плагин использует единый префикс:

```text
prefix = config["topic_prefix"] or "home1"
```

Исходящие топики формируются в `changeProperty(...)` на основе вычисленного `class_path`.

Подписка при успешном подключении:

```text
{prefix}/#
```

`_on_message(...)`:

- парсит массив сегментов топика;
- вычисляет `object_name`, `property_name`, `value`;
- проверяет:
  - включён ли inbound sync;
  - проходят ли объект/класс фильтрацию;
- при успехе вызывает `_mark_suppressed(...)` и `setProperty(...)`.

---

## Формат топиков и вычисление class_path

### Формат топиков

Поддерживаются две формы:

1. **С путём классов**

```text
{prefix}/{class_path}/{object}/{property}
```

Например:

```text
home1/Devices/Lights/LivingRoom/ceiling/on
```

2. **Без путя классов**

```text
{prefix}/{object}/{property}
```

Используется, если у объекта нет родителей / class_path нельзя вычислить.

### Вычисление class_path

Плагин не ходит напрямую в базу; он использует объектное хранилище:

- вызывает `getObject(object_name)` из `app.core.lib.object`;
- читает атрибут `.parents` у возвращённого объекта;
- список `.parents` хранится снизу вверх, поэтому разворачивается;
- результат соединяется через `/` — получаем `class_path`.

Результат кешируется в `_object_path_cache`, чтобы не перегружать хранилище.

Инвалидация кеша:

- в `changeObject(...)` при изменении/удалении объекта:
  - очищаются записи для данного `object_name` в `_object_path_cache` и `_meta_cache`.

---

## Фильтрация и кеширование

### Поля конфигурации

Из `SettingsForm` и конфига плагина:

- `whitelist_classes` – список имён классов;
- `whitelist_objects` – список имён объектов;
- `blacklist_classes` – список имён классов;
- `blacklist_objects` – список имён объектов;
- `enable_inbound` – включение/выключение входящей синхронизации.

### Наборы в памяти

`_rebuild_whitelist_cache()` преобразует строки в наборы:

- `_whitelist_classes_set`
- `_whitelist_objects_set`
- `_blacklist_classes_set`
- `_blacklist_objects_set`

Все имена предварительно тримятся и нормализуются.

### Кеш метаданных

`_meta_cache` хранит по объекту:

- `allowed` – можно ли публиковать outbound;
- `class_path` – вычисленный путь классов (может быть пустой строкой).

`_get_meta(object_name)`:

- если объект есть в `_meta_cache`, возвращает кеш;
- иначе:
  - вычисляет `class_path` с помощью `getObject().parents`;
  - применяет логику whitelist/blacklist;
  - записывает результат в кеш.

### Логика фильтрации

Эффективные правила:

1. **Приоритет blacklist**:
   - если класс или объект попадает в blacklist, outbound запрещён.
2. **Whitelist (при наличии)**:
   - если whitelist для классов или объектов не пуст, разрешены только элементы, попавшие в whitelist.
3. **Поведение по умолчанию**:
   - если whitelist пуст, outbound разрешён (за вычетом внутренних/служебных объектов, см. код плагина).

Та же логика используется и для inbound (решение, применять ли входящее сообщение).

---

## Подавление эхо (защита от петель)

Проблема: одно MQTT‑сообщение, пришедшее из Дома 1, меняет свойство в Доме 2, после чего Д2 публикует то же значение обратно, и процесс зацикливается.

Решение:

- `_suppress_echo`: словарь в памяти, ключ `(object, property, value)`, значение — метка времени;
- `_suppress_ttl`: TTL (секунды), по умолчанию ~5 секунд;
- `_suppress_lock`: `threading.Lock` для синхронизации.

### Входящий путь

1. `_on_message(...)` распарсивает топик в `object_name`, `property_name` и `value`.
2. Перед `setProperty` выполняется `_mark_suppressed(object_name, property_name, value)`:
   - записывает кортеж с текущим временем в `_suppress_echo`.
3. Затем вызывается `setProperty(...)` для обновления свойства.

### Исходящий путь

В начале `changeProperty(...)`:

- вызывается `_is_suppressed(object_name, property_name, value)`:
  - удаляет просроченные записи (старше `_suppress_ttl`);
  - проверяет наличие кортежа `(object, property, value)` в словаре;
  - если найден — удаляет его и возвращает `True`, останавливая публикацию.

В итоге:

- одно входящее MQTT‑сообщение -> одна смена свойства -> **без повторной публикации** для того же значения.

---

## Статистика и WebSocket

### Внутренние счётчики

Плагин отслеживает:

- `_publish_count`
- `_publish_error_count`
- `_inbound_count`
- `_inbound_error_count`
- `_setproperty_error_count`
- `_suppressed_count`
- `_started_at_ts`
- `_last_publish_ts`
- `_last_inbound_ts`

Обновление под защитой `_stats_lock`.

### Снэпшот для UI

`_get_stats_snapshot()` возвращает словарь с:

- таймстемпами (`started_at_ts`, `last_publish_ts`, `last_inbound_ts`);
- счётчиками publish/inbound/errors;
- количеством подавленных эхо;
- размерами `_meta_cache` и `_object_path_cache`;
- размерами и содержимым whitelist/blacklist;
- краткой конфигурацией (prefix, inbound, host, port);
- информацией об `outbound_policy` (режим whitelist/default).

### Канал WebSocket

`cyclic_task()` периодически делает:

```python
self.sendDataToWebsocket("stats", self._get_stats_snapshot())
```

Колбэки подключения/разрыва делают:

```python
self.sendDataToWebsocket("connectionStatus", {...})
```

`sendDataToWebsocket` из `app.core.lib.common` пробрасывает данные в модуль `wsServer`, который рассылает события подписчикам, подписанным на тип `"MqttBridge"`.

### Frontend (Vue)

`static/js/admin.js`:

- создаёт Socket.IO клиент `io()`;
- при подключении отправляет:

```javascript
socket.emit("subscribeData", ["MqttBridge"]);
```

- слушает события `"MqttBridge"` и вызывает `handleMessage(payload)`:
  - `operation === "connectionStatus"` — обновление `connection`;
  - `operation === "stats"` — обновление `stats` + пересчёт скоростей.

Скорость считается по разнице двух последних снэпшотов.

---

## Локализация интерфейса

Шаблон `main_mqtt_bridge.html` использует `{{ _('...') }}` для всех подписей. Переводы лежат в:

- `translations/en.json`
- `translations/ru.json`

JS‑часть (`admin.js`) использует объект `window.MqttBridgeI18n`, инициализированный в шаблоне, чтобы статусы (`Connected`, `Error`, `s ago` и т.д.) также были локализованы.

---

## Зависимости

Явные зависимости модуля:

- `paho-mqtt` – MQTT‑клиент;
- ядро osysHome:
  - `BasePlugin`;
  - `ObjectManager` + `getObject`, `setProperty`;
  - модуль `wsServer` для WebSocket‑обновлений.

Список Python‑зависимостей см. в `plugins/MqttBridge/requirements.txt`.

