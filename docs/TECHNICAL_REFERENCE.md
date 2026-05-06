# MqttBridge – Technical Reference

## Module Structure

Core files:

| File | Responsibility |
| --- | --- |
| `plugins/MqttBridge/__init__.py` | Main plugin class, MQTT connection, proxy hooks, filtering, stats |
| `plugins/MqttBridge/forms/SettingForms.py` | Admin settings form (host/port/login/password, prefix, filters) |
| `plugins/MqttBridge/templates/main_mqtt_bridge.html` | Admin UI with Vue‑based live stats |
| `plugins/MqttBridge/static/js/admin.js` | Vue app, WebSocket client, stats rendering |
| `plugins/MqttBridge/translations/*.json` | Localized strings for the admin interface |

The plugin is a standard `BasePlugin` with actions:

- `proxy` – receives property changes from `ObjectManager`.
- `cycle` – runs a periodic task for pushing stats to WebSocket.

---

## Runtime Architecture

### High‑level flow

1. `initialization()` is called:
   - `_rebuild_whitelist_cache()` parses configuration into in‑memory sets.
   - `_connect_mqtt()` creates an MQTT client and connects to the broker.
2. When any object property changes in osysHome, `ObjectManager` invokes:
   - `MqttBridge.changeProperty(obj, prop, value)` because the plugin declares `proxy`.
3. `changeProperty(...)`:
   - checks readiness, echo suppression and filters;
   - resolves a topic;
   - publishes to MQTT.
4. MQTT client subscriptions deliver inbound messages to `_on_message(...)`:
   - parses topic;
   - applies `_mark_suppressed(...)` to avoid echo;
   - calls `setProperty` for the target object/property.
5. `cycle` action periodically calls `cyclic_task()`:
   - collects stats snapshot;
   - sends it to `wsServer` via `sendDataToWebsocket`.
6. `admin.js` subscribes to `MqttBridge` messages in `wsServer` and updates the Vue model.

---

## MQTT Integration

### Client lifecycle

The plugin uses `paho-mqtt`:

- `_connect_mqtt()`:
  - creates a `mqtt.Client` instance;
  - configures username/password if provided;
  - sets callbacks: `_on_connect`, `_on_disconnect`, `_on_message`;
  - starts the network loop in the background (`loop_start()`).
- `_reconnect_mqtt()`:
  - stops and disconnects the current client if any;
  - resets status;
  - calls `_connect_mqtt()` again.

Connection state is tracked in:

- `_connection_status`: `"disconnected" | "connecting" | "connected" | "error"`;
- `_connection_error`: last error string (if any);
- `_connected_at_ts`: UNIX timestamp when the last successful connection was established.

On connect/disconnect the plugin pushes a `connectionStatus` payload via WebSocket.

### Topics and Subscriptions

The plugin uses a single logical prefix:

```text
prefix = config["topic_prefix"] or "home1"
```

Outbound topics are generated in `changeProperty(...)` based on a resolved class path.

Inbound subscription:

- On successful connect the plugin subscribes to:

```text
{prefix}/#
```

- `_on_message(...)` receives messages, parses the topic path and applies updates if:
  - inbound sync is enabled in config;
  - filtering rules allow the target object/class.

---

## Topic Format and Class Path Resolution

### Topic Format

Two shapes are supported:

1. **With class path**

```text
{prefix}/{class_path}/{object}/{property}
```

Where `class_path` is a `/`‑joined list of class names, e.g.:

```text
home1/Devices/Lights/LivingRoom/ceiling/on
```

2. **Without class path**

```text
{prefix}/{object}/{property}
```

Used when the object has no parents or when the class path cannot be resolved.

### Class Path Resolution

The plugin avoids direct SQL queries and uses the in‑memory object tree:

- it calls `getObject(object_name)` from `app.core.lib.object`;
- reads the `.parents` attribute of the returned object;
- the list in `.parents` is bottom‑up, so it is reversed to construct a top‑down path;
- the resulting list is joined with `/` to form `class_path`.

Results are cached in `_object_path_cache` to avoid repeated lookups.

Cache invalidation happens in `changeObject(...)`:

- when an object is created/updated/deleted, both `_object_path_cache` and `_meta_cache` entries for that object are dropped.

---

## Filtering and Caching

### Configuration Fields

From `SettingsForm` and plugin config:

- `whitelist_classes` – text (comma/newline separated class names).
- `whitelist_objects` – text (comma/newline separated object names).
- `blacklist_classes` – text (comma/newline separated class names).
- `blacklist_objects` – text (comma/newline separated object names).
- `enable_inbound` – boolean toggle.

### In‑Memory Sets

At startup and after saving config, `_rebuild_whitelist_cache()` parses the four lists into:

- `_whitelist_classes_set`
- `_whitelist_objects_set`
- `_blacklist_classes_set`
- `_blacklist_objects_set`

All names are trimmed and normalized before being put into sets.

### Metadata Cache

`_meta_cache` stores per‑object metadata:

- `allowed` – boolean flag (true if outbound publish is allowed).
- `class_path` – resolved class path string (may be empty).

`_get_meta(object_name)`:

- if the object is in `_meta_cache`, returns the cached tuple;
- otherwise:
  - resolves class path via `getObject().parents`;
  - applies whitelist/blacklist logic;
  - stores the result in `_meta_cache`.

### Filtering Logic

The effective rules are:

1. **Blacklist has priority**:
   - if an object’s class or object name matches blacklist, outbound is denied.
2. **Whitelist (if non‑empty)**:
   - if whitelist for classes or objects is defined, only matching items are allowed.
3. **Default behavior**:
   - if whitelist is empty, outbound is allowed except for internal defaults (like system/service objects; see plugin code).

This logic is reused for both outbound and inbound decisions.

---

## Echo Suppression (Loop Prevention)

When a property is updated because of an inbound MQTT message, you must avoid publishing the same change back to MQTT, otherwise two homes may start bouncing values indefinitely.

The plugin uses:

- `_suppress_echo`: an in‑memory dict keyed by `(object, property, value)` with timestamps.
- `_suppress_ttl`: time‑to‑live (seconds) for entries (default 5 seconds).
- `_suppress_lock`: a `threading.Lock` to protect updates.

### Inbound Path

1. `_on_message(...)` parses topic and decides the target:
   - `object_name`, `property_name`, `value`.
2. Before calling `setProperty`, it calls `_mark_suppressed(object_name, property_name, value)`:
   - stores the tuple with the current timestamp.
3. It then calls `setProperty(...)` to change the local property.

### Outbound Path

At the beginning of `changeProperty(...)`:

- plugin calls `_is_suppressed(object_name, property_name, value)`:
  - removes expired entries (older than `_suppress_ttl`);
  - checks if the `(object, property, value)` tuple is in the dict;
  - if present, removes it and returns `True` which stops publishing.

As a result:

- one inbound change leads to one local property update but **no outbound re‑publish** for the same value.

---

## Stats and WebSocket Integration

### Internal Counters

The plugin tracks:

- `_publish_count`
- `_publish_error_count`
- `_inbound_count`
- `_inbound_error_count`
- `_setproperty_error_count`
- `_suppressed_count`
- `_started_at_ts`
- `_last_publish_ts`
- `_last_inbound_ts`

Mutations are protected by `_stats_lock`.

### Snapshot for UI

`_get_stats_snapshot()` returns a dict with:

- `started_at_ts`, `last_publish_ts`, `last_inbound_ts`
- `publish_count`, `publish_error_count`
- `inbound_count`, `inbound_error_count`
- `setproperty_error_count`
- `suppressed_count`
- `caches` (sizes of `_meta_cache` and `_object_path_cache`)
- `whitelist` / `blacklist` sizes and values
- `config` summary (prefix, inbound enabled, host, port)
- `outbound_policy` (whether whitelist is effectively active, etc.)

### WebSocket Channel

`cyclic_task()` periodically calls:

```python
self.sendDataToWebsocket("stats", self._get_stats_snapshot())
```

Connection status updates call:

```python
self.sendDataToWebsocket("connectionStatus", {...})
```

`app.core.lib.common.sendDataToWebsocket` routes this to the `wsServer` plugin, which broadcasts under the `typeData` equal to the plugin name (`"MqttBridge"`).

### Frontend (Vue)

`static/js/admin.js`:

- connects with `io()` (Socket.IO client);
- on connect, sends:

```javascript
socket.emit("subscribeData", ["MqttBridge"]);
```

- listens for `"MqttBridge"` events and calls `handleMessage(payload)`:
  - if `operation === "connectionStatus"` – updates `connection`.
  - if `operation === "stats"` – updates `stats` and recomputes rates.

Rate estimation is done by comparing the last two `stats` snapshots.

---

## Admin‑side Localization

The template `main_mqtt_bridge.html` uses `{{ _('...') }}` for all visible labels. Per‑language translations live in:

- `translations/en.json`
- `translations/ru.json`

Additionally, the Vue app uses an injected `window.MqttBridgeI18n` object for time/status strings in JS, which is also populated from the same `_('...')` calls in the template to keep localization consistent.

---

## Dependencies

The module explicitly depends on:

- `paho-mqtt` – MQTT client;
- osysHome core:
  - `BasePlugin`
  - `ObjectManager` + `getObject`, `setProperty`
  - `wsServer` WebSocket integration.

See `plugins/MqttBridge/requirements.txt` for Python packages.

