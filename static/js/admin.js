(function () {
  var I18N = (typeof window !== "undefined" && window.MqttBridgeI18n) ? window.MqttBridgeI18n : {};
  function formatTimeAgo(tsSeconds) {
    if (!tsSeconds) return "-";
    var now = Date.now() / 1000;
    var d = Math.max(0, now - tsSeconds);
    if (d < 1) return I18N.just_now || "just now";
    if (d < 60) return Math.floor(d) + (I18N.s_ago || "s ago");
    if (d < 3600) return Math.floor(d / 60) + (I18N.m_ago || "m ago");
    return Math.floor(d / 3600) + (I18N.h_ago || "h ago");
  }

  function formatUptime(startedAtTs) {
    if (!startedAtTs) return "-";
    var s = Math.max(0, Math.floor(Date.now() / 1000 - startedAtTs));
    var h = Math.floor(s / 3600);
    var m = Math.floor((s % 3600) / 60);
    var sec = s % 60;
    return h + "h " + m + "m " + sec + "s";
  }

  function formatDateTime(tsSeconds) {
    if (!tsSeconds) return "-";
    var n = Number(tsSeconds);
    if (!isFinite(n) || n <= 0) return "-";
    try {
      return new Date(n * 1000).toLocaleString();
    } catch (e) {
      return "-";
    }
  }

  new Vue({
    el: "#mqtt_bridge_app",
    delimiters: ["[[", "]]"],
    data: function () {
      return {
        connection: {
          status: "disconnected",
          error: null,
          connected_at_ts: null,
        },
        stats: null,
        lastStats: null,
        rate: {
          publish_per_s: 0,
          inbound_per_s: 0,
        },
        socket: null,
      };
    },
    computed: {
      connectionBadgeClass: function () {
        if (this.connection.status === "connected") return "bg-success";
        if (this.connection.status === "connecting") return "bg-warning text-dark";
        if (this.connection.status === "error") return "bg-danger";
        return "bg-secondary";
      },
      connectionLabel: function () {
        if (this.connection.status === "connected") return I18N.connected || "Connected";
        if (this.connection.status === "connecting") return I18N.connecting || "Connecting...";
        if (this.connection.status === "error") return I18N.error || "Error";
        return I18N.disconnected || "Disconnected";
      },
      uptimeText: function () {
        return this.stats ? formatUptime(this.stats.started_at_ts) : "-";
      },
      lastPublishAgo: function () {
        return this.stats ? formatTimeAgo(this.stats.last_publish_ts) : "-";
      },
      lastInboundAgo: function () {
        return this.stats ? formatTimeAgo(this.stats.last_inbound_ts) : "-";
      },
      connectedAtText: function () {
        return formatDateTime(this.connection && this.connection.connected_at_ts);
      },
    },
    methods: {
      handleMessage: function (payload) {
        if (!payload || !payload.operation) return;
        if (payload.operation === "connectionStatus") {
          this.connection = payload.data || this.connection;
          return;
        }
        if (payload.operation === "stats") {
          this.lastStats = this.stats;
          this.stats = payload.data || null;
          if (this.stats && this.stats.connection) {
            this.connection = this.stats.connection;
          }
          this.computeRates();
        }
      },
      computeRates: function () {
        if (!this.stats || !this.lastStats) return;
        var dt = 1.0;
        var dp = (this.stats.publish_count || 0) - (this.lastStats.publish_count || 0);
        var di = (this.stats.inbound_count || 0) - (this.lastStats.inbound_count || 0);
        this.rate.publish_per_s = Math.max(0, dp / dt);
        this.rate.inbound_per_s = Math.max(0, di / dt);
      },
      initSocket: function () {
        var self = this;
        var socket = io();
        self.socket = socket;
        socket.on("connect", function () {
          socket.emit("subscribeData", ["MqttBridge"]);
        });
        socket.on("MqttBridge", function (payload) {
          self.handleMessage(payload);
        });
      },
    },
    mounted: function () {
      this.initSocket();
    },
  });
})();

