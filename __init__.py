import time
from threading import Lock

import paho.mqtt.client as mqtt
from flask import redirect

from app.core.lib.object import getObject, updateProperty
from app.core.main.BasePlugin import BasePlugin
from plugins.MqttBridge.forms.SettingForms import SettingsForm


class MqttBridge(BasePlugin):
    def __init__(self, app):
        super().__init__(app, __name__)
        self.title = "MqttBridge"
        self.version = 1
        self.description = "MQTT shared-state bridge for object properties"
        self.category = "Communication"
        self.actions = ["proxy", "cycle"]

        self._client = None
        self._connection_status = "disconnected"  # disconnected, connecting, connected, error
        self._connection_error = None
        self._connected_at_ts = None
        self._object_path_cache = {}
        self._meta_cache = {}  # object_name -> (allowed: bool, class_path: str)
        self._cache_lock = Lock()

        # Cached whitelist sets to avoid parsing config on every property change.
        self._whitelist_classes_set = set()
        self._whitelist_objects_set = set()
        self._blacklist_classes_set = set()
        self._blacklist_objects_set = set()

        # Runtime statistics (for reactive admin UI)
        self._stats_lock = Lock()
        self._started_at_ts = time.time()
        self._publish_count = 0
        self._publish_error_count = 0
        self._inbound_count = 0
        self._inbound_error_count = 0
        self._setproperty_error_count = 0
        self._suppressed_count = 0
        self._last_publish_ts = None
        self._last_inbound_ts = None
        self._suppress_echo = {}
        self._outbound_echo = {}
        self._suppress_lock = Lock()
        self._suppress_ttl = 5.0

    def initialization(self):
        self._rebuild_whitelist_cache()
        self._connect_mqtt()

    def admin(self, request):
        settings = SettingsForm()
        if request.method == "GET":
            settings.host.data = self.config.get("host", "")
            settings.port.data = self.config.get("port", 1883)
            settings.login.data = self.config.get("login", "")
            settings.password.data = self.config.get("password", "")
            settings.topic_prefix.data = self.config.get("topic_prefix", "home1")
            settings.whitelist_classes.data = self.config.get("whitelist_classes", "")
            settings.whitelist_objects.data = self.config.get("whitelist_objects", "")
            settings.blacklist_classes.data = self.config.get("blacklist_classes", "")
            settings.blacklist_objects.data = self.config.get("blacklist_objects", "")
            settings.enable_inbound.data = self.config.get("enable_inbound", True)
        else:
            if settings.validate_on_submit():
                self.config["host"] = settings.host.data.strip()
                self.config["port"] = settings.port.data
                self.config["login"] = settings.login.data.strip()
                self.config["password"] = settings.password.data
                self.config["topic_prefix"] = settings.topic_prefix.data.strip().strip("/")
                self.config["whitelist_classes"] = settings.whitelist_classes.data or ""
                self.config["whitelist_objects"] = settings.whitelist_objects.data or ""
                self.config["blacklist_classes"] = settings.blacklist_classes.data or ""
                self.config["blacklist_objects"] = settings.blacklist_objects.data or ""
                self.config["enable_inbound"] = bool(settings.enable_inbound.data)
                self.saveConfig()

                self._rebuild_whitelist_cache()
                self._reconnect_mqtt()
                return redirect("MqttBridge")

        content = {
            "form": settings,
            "connection_status": self._connection_status,
            "connection_error": self._connection_error,
        }
        return self.render("main_mqtt_bridge.html", content)

    def cyclic_task(self):
        # Push stats to UI via WebSocket once per second.
        try:
            self.sendDataToWebsocket("stats", self._get_stats_snapshot())
        except Exception as ex:
            self.logger.debug("MqttBridge stats push error: %s", ex)
        self.event.wait(1.0)

    def changeProperty(self, obj: str, prop: str, value) -> None:
        if not self._is_ready_to_publish():
            return

        if self._is_suppressed(obj, prop, value):
            return

        allowed, class_path = self._get_meta(obj)
        if not allowed:
            return

        if not class_path:
            topic = f"{self._topic_prefix()}/{obj}/{prop}"
        else:
            topic = f"{self._topic_prefix()}/{class_path}/{obj}/{prop}"
        self._mqtt_publish(topic, value)

    def changeObject(
        self,
        event: str,
        object_name: str,
        property_name: str = None,
        method_name: str = None,
        new_value: str = None,
    ) -> None:
        with self._cache_lock:
            self._object_path_cache.pop(object_name, None)
            self._meta_cache.pop(object_name, None)

    def executedMethod(self, object_name: str, method_name: str) -> None:
        """Proxy hook for executed methods (required for proxy action)."""
        # For now we don't mirror method executions, only properties.
        # This method exists to satisfy ObjectManager.callMethod notifications.
        return

    def _reconnect_mqtt(self):
        if self._client is not None:
            try:
                self._client.loop_stop()
                self._client.disconnect()
            except Exception:
                pass
        self._client = None
        self._connect_mqtt()

    def _connect_mqtt(self):
        host = (self.config.get("host", "") or "").strip()
        if not host:
            self._connection_status = "disconnected"
            self._connection_error = "Host not configured"
            self._connected_at_ts = None
            self._push_connection_status()
            return

        self._connection_status = "connecting"
        self._connection_error = None
        self._connected_at_ts = None
        self._push_connection_status()

        try:
            port = int(self.config.get("port", 1883))
        except Exception:
            port = 1883

        try:
            self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
            self._client.on_connect = self._on_connect
            self._client.on_disconnect = self._on_disconnect
            self._client.on_message = self._on_message

            login = (self.config.get("login", "") or "").strip()
            password = self.config.get("password", "") or ""
            if login and password:
                self._client.username_pw_set(login, password)

            self._client.connect(host, port, keepalive=60)
            self._client.loop_start()
        except Exception as ex:
            self.logger.error("MqttBridge connection failed: %s", ex, exc_info=True)
            self._client = None
            self._connection_status = "error"
            self._connection_error = str(ex)
            self._connected_at_ts = None
            self._push_connection_status()

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            self.logger.error("MqttBridge MQTT connect failed with code: %s", rc)
            self._connection_status = "error"
            self._connection_error = f"Connect code: {rc}"
            self._connected_at_ts = None
            self._push_connection_status()
            return
        self.logger.info("MqttBridge connected to broker")
        self._connection_status = "connected"
        self._connection_error = None
        self._connected_at_ts = time.time()
        self._push_connection_status()
        if self.config.get("enable_inbound", True):
            topic = f"{self._topic_prefix()}/#"
            client.subscribe(topic)
            self.logger.info("MqttBridge subscribed: %s", topic)

    def _on_disconnect(self, client, userdata, rc):
        if rc == 0:
            self._connection_status = "disconnected"
            self._connection_error = None
            self._connected_at_ts = None
            self._push_connection_status()
            return
        self.logger.warning("MqttBridge disconnected with code: %s", rc)
        self._connection_status = "disconnected"
        self._connection_error = f"Disconnect code: {rc}"
        self._connected_at_ts = None
        self._push_connection_status()

    def _on_message(self, client, userdata, msg):
        if not self.config.get("enable_inbound", True):
            return
        try:
            topic = msg.topic or ""
            parts = [p for p in topic.split("/") if p]
            # Supported formats:
            # 1) {prefix}/{class_path}/{object}/{property}  (>=4 parts incl. prefix)
            # 2) {prefix}/{object}/{property}              (=3 parts incl. prefix)
            if len(parts) < 3:
                return

            prefix = self._topic_prefix()
            if not parts or parts[0] != prefix:
                return

            object_name = parts[-2]
            property_name = parts[-1]
            payload = msg.payload.decode("utf-8") if msg.payload else ""

            if self._is_outbound_echo(object_name, property_name, payload):
                return

            allowed, _ = self._get_meta(object_name)
            if not allowed:
                return

            self._mark_suppressed(object_name, property_name, payload)
            try:
                updateProperty(f"{object_name}.{property_name}", payload, source=self.name)
            except Exception:
                with self._stats_lock:
                    self._setproperty_error_count += 1
                raise

            with self._stats_lock:
                self._inbound_count += 1
                self._last_inbound_ts = time.time()
        except Exception as ex:
            with self._stats_lock:
                self._inbound_error_count += 1
            self.logger.error("MqttBridge message processing error: %s", ex, exc_info=True)

    def _topic_prefix(self) -> str:
        value = (self.config.get("topic_prefix", "home1") or "home1").strip().strip("/")
        return value or "home1"

    def _is_ready_to_publish(self) -> bool:
        return self._client is not None and self._client.is_connected()

    def _mqtt_publish(self, topic: str, value):
        try:
            with self._stats_lock:
                self._publish_count += 1
                self._last_publish_ts = time.time()
            self._mark_outbound_echo(topic, value)
            self._client.publish(topic, str(value))
        except Exception as ex:
            with self._stats_lock:
                self._publish_error_count += 1
            self.logger.error("MqttBridge publish error (%s): %s", topic, ex, exc_info=True)

    def _parse_list_config(self, key: str) -> set[str]:
        value = self.config.get(key, "") or ""
        normalized = value.replace("\r", "\n").replace(",", "\n")
        parts = [p.strip() for p in normalized.split("\n")]
        return {p for p in parts if p}

    def _rebuild_whitelist_cache(self) -> None:
        with self._cache_lock:
            self._whitelist_classes_set = self._parse_list_config("whitelist_classes")
            self._whitelist_objects_set = self._parse_list_config("whitelist_objects")
            self._blacklist_classes_set = self._parse_list_config("blacklist_classes")
            self._blacklist_objects_set = self._parse_list_config("blacklist_objects")
            self._meta_cache.clear()

    def _get_meta(self, object_name: str) -> tuple[bool, str]:
        with self._cache_lock:
            cached = self._meta_cache.get(object_name)
            if cached is not None:
                return cached

        # Do the expensive lookup outside the lock.
        obj = getObject(object_name)
        if not obj:
            meta = (False, "")
            with self._cache_lock:
                self._meta_cache[object_name] = meta
            return meta

        parents = getattr(obj, "parents", None) or []
        class_path = "/".join(reversed(parents)) if parents else ""

        class_whitelist = self._whitelist_classes_set
        object_whitelist = self._whitelist_objects_set
        class_blacklist = self._blacklist_classes_set
        object_blacklist = self._blacklist_objects_set

        # Replicate original _is_allowed logic:
        object_classes = set(parents or [])

        # Blacklist has priority over whitelist/defaults.
        if object_name in object_blacklist:
            allowed = False
        elif class_blacklist and bool(object_classes.intersection(class_blacklist)):
            allowed = False
        elif object_whitelist and object_name not in object_whitelist:
            allowed = False
        else:
            if class_whitelist:
                allowed = bool(object_classes.intersection(class_whitelist))
            else:
                internal = {"Users", "Permissions", "_permissions"}
                if object_name.startswith("_"):
                    allowed = False
                elif object_name in internal:
                    allowed = False
                elif object_classes.intersection(internal):
                    allowed = False
                else:
                    allowed = True

        meta = (allowed, class_path)
        with self._cache_lock:
            self._meta_cache[object_name] = meta
        return meta

    def _resolve_class_path(self, object_name: str) -> str:
        cached = self._object_path_cache.get(object_name)
        if cached is not None:
            return cached

        try:
            obj = getObject(object_name)
            if not obj:
                self._object_path_cache[object_name] = ""
                return ""

            # parents уже собраны в ObjectManager (ObjectsStorage._getParents)
            parents = getattr(obj, "parents", None) or []
            if not parents:
                # No class hierarchy info -> topic should be prefix/object/property
                self._object_path_cache[object_name] = ""
                return ""

            # In this codebase `parents` is built bottom-up: childClass -> parentClass -> ...
            # Topic format expects top-down path, so we reverse it.
            class_path = "/".join(reversed(parents))
            self._object_path_cache[object_name] = class_path
            return class_path
        except Exception as ex:
            self.logger.error(
                "MqttBridge class path error for %s: %s", object_name, ex, exc_info=True
            )
            return ""

    def _mark_suppressed(self, obj: str, prop: str, value):
        key = f"{obj}.{prop}"
        with self._suppress_lock:
            self._suppress_echo[key] = (str(value), time.time() + self._suppress_ttl)

    def _is_suppressed(self, obj: str, prop: str, value) -> bool:
        now = time.time()
        key = f"{obj}.{prop}"
        value = str(value)
        with self._suppress_lock:
            expired_keys = [k for k, (_, exp) in self._suppress_echo.items() if exp < now]
            for k in expired_keys:
                self._suppress_echo.pop(k, None)

            item = self._suppress_echo.get(key)
            if not item:
                return False
            expected, expires = item
            if expires < now:
                self._suppress_echo.pop(key, None)
                return False
            if expected == value:
                self._suppress_echo.pop(key, None)
                with self._stats_lock:
                    self._suppressed_count += 1
                return True
            return False

    def _mark_outbound_echo(self, topic: str, value) -> None:
        parts = [p for p in (topic or "").split("/") if p]
        if len(parts) < 3:
            return
        key = f"{parts[-2]}.{parts[-1]}"
        with self._suppress_lock:
            self._outbound_echo[key] = (str(value), time.time() + self._suppress_ttl)

    def _is_outbound_echo(self, obj: str, prop: str, value) -> bool:
        now = time.time()
        key = f"{obj}.{prop}"
        value = str(value)
        with self._suppress_lock:
            expired_keys = [k for k, (_, exp) in self._outbound_echo.items() if exp < now]
            for k in expired_keys:
                self._outbound_echo.pop(k, None)

            item = self._outbound_echo.get(key)
            if not item:
                return False
            expected, expires = item
            if expires < now:
                self._outbound_echo.pop(key, None)
                return False
            if expected == value:
                self._outbound_echo.pop(key, None)
                with self._stats_lock:
                    self._suppressed_count += 1
                return True
            return False

    def _push_connection_status(self):
        try:
            self.sendDataToWebsocket(
                "connectionStatus",
                {
                    "status": self._connection_status,
                    "error": self._connection_error,
                    "connected_at_ts": self._connected_at_ts,
                },
            )
        except Exception:
            # wsServer may be unavailable during startup; ignore.
            pass

    def _get_stats_snapshot(self) -> dict:
        with self._stats_lock:
            publish_count = self._publish_count
            publish_error_count = self._publish_error_count
            inbound_count = self._inbound_count
            inbound_error_count = self._inbound_error_count
            setproperty_error_count = self._setproperty_error_count
            suppressed_count = self._suppressed_count
            last_publish_ts = self._last_publish_ts
            last_inbound_ts = self._last_inbound_ts

        with self._cache_lock:
            meta_cache_size = len(self._meta_cache)
            object_path_cache_size = len(self._object_path_cache)
            whitelist_classes_count = len(self._whitelist_classes_set)
            whitelist_objects_count = len(self._whitelist_objects_set)
            blacklist_classes_count = len(self._blacklist_classes_set)
            blacklist_objects_count = len(self._blacklist_objects_set)
            whitelist_classes = sorted(self._whitelist_classes_set)
            whitelist_objects = sorted(self._whitelist_objects_set)
            blacklist_classes = sorted(self._blacklist_classes_set)
            blacklist_objects = sorted(self._blacklist_objects_set)

        port_raw = self.config.get("port", 1883)
        try:
            port_value = int(port_raw)
        except Exception:
            port_value = 1883

        return {
            "started_at_ts": self._started_at_ts,
            "publish_count": publish_count,
            "publish_error_count": publish_error_count,
            "inbound_count": inbound_count,
            "inbound_error_count": inbound_error_count,
            "setproperty_error_count": setproperty_error_count,
            "suppressed_count": suppressed_count,
            "last_publish_ts": last_publish_ts,
            "last_inbound_ts": last_inbound_ts,
            "connection": {
                "status": self._connection_status,
                "error": self._connection_error,
                "connected_at_ts": self._connected_at_ts,
            },
            "caches": {
                "meta_cache_size": meta_cache_size,
                "object_path_cache_size": object_path_cache_size,
            },
            "whitelist": {
                "classes_count": whitelist_classes_count,
                "objects_count": whitelist_objects_count,
                "classes": whitelist_classes,
                "objects": whitelist_objects,
            },
            "blacklist": {
                "classes_count": blacklist_classes_count,
                "objects_count": blacklist_objects_count,
                "classes": blacklist_classes,
                "objects": blacklist_objects,
            },
            "outbound_policy": {
                "mode": (
                    "whitelist"
                    if (whitelist_classes_count > 0 or whitelist_objects_count > 0)
                    else "default"
                ),
                "default_deny_internal": True,
                "blacklist_priority": True,
            },
            "config": {
                "topic_prefix": self._topic_prefix(),
                "enable_inbound": bool(self.config.get("enable_inbound", True)),
                "host": (self.config.get("host", "") or "").strip(),
                "port": port_value,
            },
        }
