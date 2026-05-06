# MqttBridge - Shared-state bridge via MQTT

![MqttBridge Icon](static/MqttBridge.png)

`MqttBridge` synchronizes osysHome object property changes between different homes using MQTT. It publishes outbound property updates to MQTT and (optionally) subscribes to inbound updates to apply them back to local objects.

## Description

The module bridges the internal osysHome object model to a shared MQTT topic space, using the `proxy` action to receive property change events and the `cycle` action to update the admin UI statistics.

## Main Features

- ✅ **Outbound synchronization (proxy)**: when a property changes locally, `MqttBridge` publishes it to MQTT.
- ✅ **Inbound synchronization (subscribe)**: when inbound MQTT messages are received, the module updates local properties (configurable).
- ✅ **Topic mapping**: property updates are mapped to a predictable MQTT topic format.
- ✅ **Outbound filtering (Whitelist/Blacklist)**: controls which classes/objects are allowed to be published.
- ✅ **Loop prevention**: echo suppression avoids infinite feedback loops.
- ✅ **Admin UI**: live connection status and synchronization statistics.

## MQTT Topic Format

Two topic shapes are used depending on whether an object has a class hierarchy:

1. If `class_path` is available:
   - `{prefix}/{class_path}/{object}/{property}`
2. If the object has no parents / class path:
   - `{prefix}/{object}/{property}`

Where:

- `prefix` is configured in the module settings (default `home1`)
- `class_path` comes from the object’s inheritance chain (class hierarchy)
- `object` is the osysHome object name
- `property` is the property name

## Filtering Policy (Outbound)

Publication to MQTT is controlled by:

- **Blacklist** (takes priority)
- **Whitelist**

If the whitelist is not set, “default allowed” behavior is used, but internal defaults may still be denied (see module logic).

## Admin Panel

The module provides:

- **MQTT connection + live status**
- **Statistics**: publishes / inbound updates / suppressed echoes
- **Outbound filter policy view**: shows allowed/denied counts
- **Settings**:
  - MQTT host, port, login, password
  - `topic_prefix`
  - whitelist/blacklist lists
  - toggle inbound sync

## Actions

- `proxy` - mirrors property changes (outbound MQTT publish)
- `cycle` - periodic UI stats updates

## Requirements

- `paho-mqtt`

See `plugins/MqttBridge/requirements.txt` for the exact dependency list.

## Version

Current version: **1.0**

