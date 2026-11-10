/* SSD助手 —— 前端 */
(function () {
  "use strict";

  var API = "";
  var state = {
    page: "overview",
    expandedSet: {},
    overview: null,
    events: null,
    history: null,
    bench: null,
    selected: null,       // 概况相关：多选的 disk_key 列表（不是设备名）
    historyKey: null,     // 历史页当前选中的 disk_key
    historyYear: null,    // 历史页当前查看的年份
    benchKey: null,       // 测速页当前选中的 disk_key
    benchGauges: null,
    benchPlayer: null,   // { pending: [], shown: {read,write}, playing: false }
    refreshing: false,   // 正在重新扫描磁盘
    autoRefreshed: false, // 本次会话是否已经自动补扫过
    seenKeys: {},        // 出现过的 disk_key：临时拔盘再插回来要能恢复勾选
    timer: null,
    benchTimer: null
  };

  /* ------------------------------------------------------------ 工具 */

  function $(sel, root) { return (root || document).querySelector(sel); }
  function el(tag, cls, html) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (html !== undefined) node.innerHTML = html;
    return node;
  }
  function esc(str) {
    return String(str === null || str === undefined ? "" : str)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function getJSON(path) {
    return fetch(API + path, { headers: { Accept: "application/json" } }).then(function (res) {
      if (!res.ok) throw new Error(path + " -> " + res.status);
      return res.json();
    });
  }
  function postJSON(path, body) {
    return fetch(API + path, {
      method: "POST",
      // 自定义头是后端判断"这条请求来自本应用页面"的依据，跨站网页带不上
      headers: { "Content-Type": "application/json", "X-Requested-With": "SSDPlus" },
      body: JSON.stringify(body || {})
    }).then(function (res) { return res.json(); });
  }

  function fmtSize(bytes, digits) {
    // 缺数据要显示「—」而不是「0GB」：0GB 会让人以为这盘从来没写过
    if (bytes === null || bytes === undefined || bytes === "") return "—";
    if (!bytes) return "0GB";
    var units = ["B", "KB", "MB", "GB", "TB", "PB"];
    var i = 0, v = bytes;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    if (digits === undefined) digits = v >= 10 ? 0 : 1;
    return v.toFixed(digits) + units[i];
  }
  function fmtTraffic(tb) {
    if (tb >= 1) return (tb >= 10 ? tb.toFixed(0) : tb.toFixed(1)) + "T";
    var gb = tb * 1024;
    if (gb >= 1) return (gb >= 10 ? gb.toFixed(0) : gb.toFixed(1)) + "G";
    return (gb * 1024).toFixed(0) + "M";
  }

  function fmtTemp(v) { return (v === null || v === undefined || v === 0) ? "—" : v + "℃"; }
  function fmtHealth(v) { return (v === null || v === undefined) ? "—" : v + "%"; }
  function fmtHours(v) { return (v === null || v === undefined) ? "—" : v + "小时"; }
  function fmtCount(v, unit) { return (v === null || v === undefined) ? "—" : v + (unit || ""); }
  /* 自动备份按"上次备份 + 间隔"轮转，这里算出下一次大概什么时候跑 */
  function fmtNextBackup(cfg) {
    if (!cfg.backup_enabled) return "自动备份已关闭";
    var days = Number(cfg.backup_interval_days) || 1;
    if (!cfg.backup_last_at) {
      var soon = new Date(Date.now() + 60 * 1000);
      return "安装后首次采样时执行（" + soon.getFullYear() + "年" + (soon.getMonth() + 1) + "月" +
        soon.getDate() + "日 " + pad2(soon.getHours()) + ":" + pad2(soon.getMinutes()) + " 起）";
    }
    var next = (Number(cfg.backup_last_at) + days * 86400) * 1000;
    var delta = Math.round((next - Date.now()) / 60000);   // 分钟
    var when = fmtTime(Math.floor(next / 1000));
    if (delta <= 0) return when + "（下次采样时执行）";
    if (delta < 60) return when + "（约 " + delta + " 分钟后）";
    if (delta < 60 * 48) return when + "（约 " + Math.round(delta / 60) + " 小时后）";
    return when + "（约 " + Math.round(delta / 1440) + " 天后）";
  }

  function pad2(n) { return n < 10 ? "0" + n : "" + n; }

  function fmtTime(ts) {
    if (!ts) return "—";
    var d = new Date(ts * 1000);
    function p(n) { return n < 10 ? "0" + n : "" + n; }
    return d.getFullYear() + "年" + (d.getMonth() + 1) + "月" + d.getDate() + "日 " +
      p(d.getHours()) + ":" + p(d.getMinutes());
  }
  /* 手机端事件列表空间窄，只显示「11-05 09:12」 */
  function fmtTimeBrief(ts) {
    if (!ts) return "—";
    var d = new Date(ts * 1000);
    function p(n) { return n < 10 ? "0" + n : "" + n; }
    return p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " +
      p(d.getHours()) + ":" + p(d.getMinutes());
  }

  function fmtBytes(n) {
    n = Number(n) || 0;
    if (n >= 1073741824) return (n / 1073741824).toFixed(2) + " GB";
    if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
    if (n >= 1024) return Math.round(n / 1024) + " KB";
    return n + " B";
  }

  function fmtDateTimeShort(ts) {
    if (!ts) return "—";
    var d = new Date(ts * 1000);
    function p(n) { return n < 10 ? "0" + n : "" + n; }    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " +
      p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }

  function toast(msg) {
    var node = $("#toast");
    node.textContent = msg;
    node.classList.add("show");
    clearTimeout(node._t);
    node._t = setTimeout(function () { node.classList.remove("show"); }, 2200);
  }

  var ICONS = {
    disk: '<svg viewBox="0 0 20 20"><rect x="1.5" y="6.2" width="14.2" height="7.6" rx="1.8"/><path d="M15.1 8.2v3.6M16.5 8.2v3.6"/></svg>',
    count: '<svg viewBox="0 0 20 20"><rect x="3" y="4" width="14" height="12" rx="2.2"/><path d="M3 8.4h14"/></svg>',
    capacity: '<svg viewBox="0 0 20 20"><ellipse cx="10" cy="6" rx="6.4" ry="2.6"/><path d="M3.6 6v8c0 1.4 2.9 2.6 6.4 2.6s6.4-1.2 6.4-2.6V6"/></svg>',
    chevron: '<svg viewBox="0 0 20 20"><path d="M8 5l5 5-5 5"/></svg>',
    chevronDown: '<svg viewBox="0 0 20 20"><path d="M5 8l5 5 5-5"/></svg>',
    write: '<svg viewBox="0 0 20 20"><path d="M10 3.4v10.4M6.4 10.4L10 14l3.6-3.6M3.6 16.4h12.8"/></svg>',
    chart: '<svg viewBox="0 0 20 20"><path d="M3 16.5V9.5M7.7 16.5V5.5M12.3 16.5v-4M17 16.5V3.5"/></svg>',
    alert: '<svg viewBox="0 0 20 20"><path d="M10 3.4 18 16.6H2z"/><path d="M10 8.2v3.4M10 14.2v.2"/></svg>',
    speed: '<svg viewBox="0 0 20 20"><path d="M3.2 14.6a8 8 0 1 1 13.6 0"/><path d="M10 12.2l3.4-3.6"/><circle cx="10" cy="12.6" r="1.3"/></svg>'
  };

  /* ------------------------------------------------------------ 选择器 */

  /* 一块盘的身份一律用 disk_key（sn:<序列号>）。设备名重启后会在
     nvme0n1 / sda 之间乱跳，只能拿来显示，不能拿来当主键。 */
  function keyOf(disk) {
    return (disk && (disk.key || disk.device)) || "";
  }

  function modelOf(key) {
    var list = (state.overview && state.overview.disks) || [];
    for (var i = 0; i < list.length; i += 1) {
      if (keyOf(list[i]) === key) return list[i].model || list[i].device || key;
    }
    return key;
  }

  function mountSelector(host, opts) {
    var disks = opts.disks || [];
    var selected = opts.selected || [];
    var single = !!opts.single;
    var wrap = el("div", "selector");
    // selected 里可能留着暂时拔掉、当前不在的盘，算标签时只数实际存在的
    var present = selected.filter(function (k) {
      return disks.some(function (d) { return keyOf(d) === k; });
    });
    var label;
    if (single) {
      label = present.length ? modelOf(present[0]) : "请选择硬盘";
    } else {
      label = present.length === disks.length ? "全部硬盘"
        : (present.length ? present.map(modelOf).join("、") : "请选择硬盘");
    }
    wrap.innerHTML =
      '<button class="selector-btn" type="button"><span class="label">' + esc(label) +
      '</span><span class="caret">▾</span></button><div class="selector-panel"></div>';
    var panel = $(".selector-panel", wrap);

    disks.forEach(function (d) {
      var optKey = keyOf(d);
      var item = el("label", "opt");
      var checked = selected.indexOf(optKey) >= 0;
      item.innerHTML = '<input type="' + (single ? "radio" : "checkbox") + '"' +
        (checked ? " checked" : "") + '><span class="name">' + esc(d.model || d.device) +
        '</span><span class="dev">' + esc(d.device) + '</span>';
      var input = $("input", item);
      input.addEventListener("change", function () {
        var next;
        if (single) {
          next = [optKey];
          panel.classList.remove("open");
        } else {
          next = selected.slice();
          var idx = next.indexOf(optKey);
          if (input.checked && idx < 0) next.push(optKey);
          if (!input.checked && idx >= 0) next.splice(idx, 1);
          if (!next.length) { input.checked = true; return; }
        }
        opts.onChange(next);
      });
      panel.appendChild(item);
    });

    $(".selector-btn", wrap).addEventListener("click", function (e) {
      e.stopPropagation();
      var open = panel.classList.contains("open");
      document.querySelectorAll(".selector-panel.open").forEach(function (p) { p.classList.remove("open"); });
      if (!open) panel.classList.add("open");
    });
    host.appendChild(wrap);
  }

  /* ------------------------------------------------------------ 概况页 */

  function statusOrb(state) {
    var glyph = state === "ok"
      ? '<path d="M5.4 11.3l3.7 3.7 7.3-7.6"/>'
      : state === "empty"
        ? '<path d="M6.4 11h9.2"/>'
        : '<path d="M11 5.6v6.6M11 15.4v.2"/>';
    return '<span class="status-orb"><svg viewBox="0 0 22 22" aria-hidden="true">' + glyph + '</svg></span>';
  }

  function fmtSizeRaw(bytes) {
    if (!bytes) return "0";
    var units = ["B", "KB", "MB", "GB", "TB", "PB"], i = 0, v = bytes;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    return v >= 100 ? String(Math.round(v)) : (Math.round(v * 10) / 10).toString();
  }

  function fmtSizeUnit(bytes) {
    if (!bytes) return "B";
    var units = ["B", "KB", "MB", "GB", "TB", "PB"], i = 0, v = bytes;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    return units[i];
  }

  /* value 在这里统一转义，调用方直接传原值即可（不要再 esc 一遍，会双重转义） */
  function factRow(label, value, desc, extraClass) {
    var shown = (value === null || value === undefined || value === "") ? "—" : esc(value);
    return '<div class="fact' + (extraClass ? " " + extraClass : "") + '">' +
      '<span class="fact-label">' + esc(label) + '</span>' +
      '<span class="fact-value">' + shown + '</span>' +
      (desc ? '<span class="fact-desc">' + esc(desc) + '</span>' : '') + '</div>';
  }

  function fmtMinutes(v) {
    if (v === null || v === undefined) return null;
    if (v >= 60) return Math.floor(v / 60) + " 小时 " + (v % 60) + " 分";
    return v + " 分钟";
  }

  function fmtInt(v, unit) {
    if (v === null || v === undefined) return null;
    return v + (unit || "");
  }

  function fmtLifeDays(days) {
    if (days === null || days === undefined) return "—";
    if (days >= 365 * 30) return "30 年以上";
    if (days >= 365) return Math.floor(days / 365) + " 年 " + Math.floor((days % 365) / 30) + " 个月";
    if (days >= 30) return Math.floor(days / 30) + " 个月";
    return days + " 天";
  }

  function lifeBand(d) {
    var life = d.life || {};
    var main, sub, pct = null, tone = "";
    if (life.status === "ok") {
      main = "预计还可使用 " + fmtLifeDays(life.remaining_days);
      sub = "每年消耗约 " + life.per_year_pct + "% 寿命" +
        (life.write_per_day ? " · 日均写入 " + fmtSize(life.write_per_day) : "");
      pct = life.used_pct;
      if (life.remaining_days !== null && life.remaining_days < 365) tone = " is-warn";
    } else if (life.status === "flat") {
      main = "暂未观测到异常消耗";
      sub = "已用寿命 " + (life.used_pct === null || life.used_pct === undefined ? "—" : life.used_pct + "%") +
        " · 已记录 " + (life.span_days || 0) + " 天";
      pct = life.used_pct;
    } else {
      main = "需要更多数据才能预估";
      sub = life.note || "持续采集几天后即可估算剩余寿命";
      pct = null;
    }
    var track = "";
    if (pct !== null && pct !== undefined && pct > 0) {
      var w = Math.max(2, Math.min(100, pct));
      track = '<div class="life-track"><i style="width:' + w + '%"></i></div>';
    }
    return '<div class="life-band' + tone + '">' +
      '<div class="life-text"><span class="life-label">寿命预估</span>' +
      '<b>' + esc(main) + '</b><span class="life-sub">' + esc(sub) + '</span></div>' +
      track + '</div>';
  }

  function detailSection(title, rows) {
    if (!rows.length) return "";
    return '<section class="detail-block"><h4>' + esc(title) + '</h4>' +
      '<div class="fact-grid">' + rows.join("") + '</div></section>';
  }

  function diskDetail(d) {
    if (!d.smart_ok) {
      return lifeBand(d) +
        '<div class="detail-empty">该硬盘未提供 SMART 健康信息：' +
        esc(d.smart_error || "可能通过 USB 桥接或阵列卡接入，被系统屏蔽了底层 SMART 透传") + '</div>';
    }
    var life = [], run = [], rel = [], base = [];
    // 两种协议的 SMART 字段不是一一对应：NVMe 有「介质错误」「错误日志」，
    // SATA 只有重映射/CRC/预留空间，展示时按协议取舍，别让缺的项占着格子显示"—"
    var isNVMe = /nvme/i.test(d.protocol || "");

    life.push(factRow("剩余寿命", fmtHealth(d.health), "厂商预估剩余寿命"));
    life.push(factRow("已用寿命", d.percent_used === null || d.percent_used === undefined ? null : d.percent_used + "%",
      isNVMe ? "NVMe 已消耗寿命占比" : "按 SMART 健康属性折算"));
    // 厂商把 241/242 记成 MB/GB 的盘，单位是按物理约束推算的，标注一下来源
    var volNote = d.volume_estimated ? "厂商计数单位不规范，按容量与通电时长换算" : "";
    life.push(factRow("累计写入", fmtSize(d.tbw), volNote || "出厂以来写入总量"));
    life.push(factRow("累计读取", fmtSize(d.tbr), volNote || "出厂以来读取总量"));

    run.push(factRow("当前温度", fmtTemp(d.temp), "主控实时温度"));
    run.push(factRow("通电时长", fmtHours(d.power_on_hours), "累计上电时间"));
    run.push(factRow("通电次数", fmtInt(d.power_cycles, " 次"), "累计开关机次数"));
    run.push(factRow("异常断电", fmtInt(d.unsafe_shutdowns, " 次"), "非正常掉电次数"));

    // SATA 没有 NVMe 的「介质错误」计数器，硬凑会显示成"—"，干脆不占格子
    if (isNVMe) {
      rel.push(factRow("介质错误", fmtInt(d.media_errors, " 次"), "无法纠正的错误数",
        d.media_errors > 0 ? "is-warn" : ""));
    }
    if (d.available_spare !== null && d.available_spare !== undefined) {
      rel.push(factRow(isNVMe ? "可用备用块" : "可用预留空间", d.available_spare + "%",
        isNVMe ? "预留空间剩余比例" : "出厂预留空间剩余"));
    }
    if (d.num_err_log_entries !== null && d.num_err_log_entries !== undefined) {
      rel.push(factRow("错误日志条目", fmtInt(d.num_err_log_entries, " 条"), "主控异常事件总数"));
    }
    if (d.critical_warning !== null && d.critical_warning !== undefined) {
      rel.push(factRow("关键警告", "0x" + ("0" + d.critical_warning.toString(16)).slice(-2),
        "0x00 即全部正常"));
    }
    if (d.reallocated_sectors !== null && d.reallocated_sectors !== undefined) {
      rel.push(factRow("重映射扇区", fmtInt(d.reallocated_sectors), "已替换的坏扇区"));
      rel.push(factRow("待映射扇区", fmtInt(d.pending_sectors), "待替换的疑似坏扇区"));
      rel.push(factRow("不可纠正扇区", fmtInt(d.uncorrectable_sectors), "无法纠正的扇区数"));
      rel.push(factRow("接口 CRC 错误", fmtInt(d.crc_errors), "接口传输错误"));
    }

    base.push(factRow("型号", d.model, "厂商型号标识"));
    base.push(factRow("序列号", d.serial, "厂商唯一标识"));
    base.push(factRow("固件版本", d.firmware, "可升级修复问题"));
    base.push(factRow("容量", fmtSize(d.capacity, 1), "标称可用容量"));

    return lifeBand(d) +
      '<div class="detail-sections">' +
      detailSection("寿命与写入量", life) +
      detailSection("运行状态", run) +
      detailSection("可靠性", rel) +
      detailSection("设备信息", base) +
      '</div>';
  }

  function diskCard(d) {
    var open = !!state.expandedSet[d.key || d.device];
    var tone = d.status === "warn" ? " warn" : (d.status === "bad" ? " bad" : "");
    var healthTone = d.status === "ok" ? "ok" : (d.status === "warn" ? "warn" : (d.status === "bad" ? "bad" : "unknown"));
    var statusText = d.status === "ok" ? "健康" : (d.status === "warn" ? "需关注" : (d.status === "bad" ? "异常" : "未知"));

    var html = '<article class="disk-card' + tone + '" data-device="' + esc(d.device) +
      '" data-key="' + esc(d.key || d.device) + '">';
    html += '<div class="disk-main">';
    html += '<div class="disk-id">';
    html += '<span class="disk-icon">' + ICONS.disk + '</span>';
    html += '<div class="disk-titles"><div class="disk-line">' +
      '<span class="disk-name" title="' + esc(d.model) + '">' + esc(d.model) + '</span>' +
      (d.protocol ? '<span class="proto">' + esc(d.protocol) + '</span>' : '') +
      '</div><div class="disk-sub"><span>' + esc(d.device) + '</span>' +
      '</div></div>';
    html += '</div>';

    html += '<div class="disk-facts">';
    html += '<div class="fact-mini"><span>健康状态</span><b class="v-' + healthTone + '">' +
      (d.health === null || d.health === undefined ? "—" : d.health + "%") + '</b></div>';
    html += '<div class="fact-mini"><span>温度</span><b>' + fmtTemp(d.temp) + '</b></div>';
    html += '<div class="fact-mini"><span>累计写入</span><b>' + (d.smart_ok ? fmtSize(d.tbw) : "—") + '</b></div>';
    html += '<div class="fact-mini"><span>累计读取</span><b>' + (d.smart_ok ? fmtSize(d.tbr) : "—") + '</b></div>';
    html += '</div></div>';

    html += '<div class="disk-detail' + (open ? "" : " is-hidden") + '">' + diskDetail(d) + '</div>';
    html += '</article>';
    return html;
  }

  function eventsCard() {
    var items = state.events || [];
    var html = '<section class="card events-card"><div class="chart-head"><h2>最近事件</h2>' +
      '<div class="chart-tools"><span class="chip">' +
      (items.length ? "最近 " + Math.min(items.length, 12) + " 条" : "暂无记录") + '</span>' +
      (items.length ? '<button class="btn btn-sm" id="btn-clear-events" type="button">清空</button>' : '') +
      '</div></div>';
    if (!items.length) {
      html += '<div class="events-empty">还没有告警或变化记录。健康度下降、温度过高、' +
        '介质增加、异常断电等情况会自动记录在这里。</div>';
    } else {
      html += '<ul class="event-list">';
      items.slice(0, 12).forEach(function (e) {
        var eName = e.model || e.device || "";
        html += '<li class="ev ev-' + esc(e.level || "info") + '">' +
          '<i class="ev-dot"></i><span class="ev-time">' +
          (window.innerWidth <= 720 ? fmtTimeBrief(e.ts) : fmtTime(e.ts)) + '</span>' +
          '<span class="ev-name" title="' + esc(eName) + '">' + esc(eName) + '</span>' +
          '<span class="ev-msg">' + esc(e.message || "") + '</span></li>';
      });
      html += '</ul>';
    }
    return html + '</section>';
  }

  function renderOverview() {
    var data = state.overview;
    var view = $("#view");
    if (!data) { view.innerHTML = '<div class="loading">正在采集磁盘信息…</div>'; return; }

    var s = data.summary;
    var tone = s.state === "ok" ? "ok" : (s.state === "warn" ? "warn" : (s.state === "bad" ? "bad" : "empty"));

    // 概况顶部只留一条运行状态，具体数字在下面的每块盘里看
    var html = '<section class="card status-card' + (tone === "ok" ? "" : " " + tone) + '">' +
      '<div class="status-main">' +
      statusOrb(tone) +
      '<div class="status-text"><h2>' + esc(tone === "ok" ? "SSD助手运行正常" : s.title) + '</h2>' +
      '<p>' + esc(s.desc) + '</p></div>' +
      '<button class="btn btn-sm status-btn" type="button" id="go-settings">助手设置</button>' +
      '</div></section>';

    html += '<section class="disk-list">';
    if (!data.disks.length) {
      html += '<div class="card"><div class="empty">尚未识别到固态硬盘</div></div>';
    } else {
      data.disks.forEach(function (d) { html += diskCard(d); });
    }
    html += '</section>';
    html += eventsCard();

    view.innerHTML = html;
    view.dataset.page = "overview";

    view.querySelectorAll(".disk-card").forEach(function (card) {
      var head = card.querySelector(".disk-main");
      if (!head) return;
      head.addEventListener("click", function () {
        // 正在框选文字时不触发折叠，方便复制
        var selection = window.getSelection();
        if (selection && selection.toString().trim().length > 0) return;

        var device = card.getAttribute("data-key") || card.getAttribute("data-device");
        var detail = card.querySelector(".disk-detail");
        if (!detail) return;
        var open = !detail.classList.toggle("is-hidden");
        card.classList.toggle("is-open", open);
        if (open) { state.expandedSet[device] = true; } else { delete state.expandedSet[device]; }
      });
    });

    var clearEvents = $("#btn-clear-events");
    if (clearEvents) {
      clearEvents.addEventListener("click", function () {
        if (!window.confirm("确认清空全部事件记录？")) return;
        postJSON("api/events/clear", {}).then(function (res) {
          toast(res && res.removed ? "已清空 " + res.removed + " 条记录" : "已清空");
          loadEvents();
        }).catch(function () { toast("清空失败"); });
      });
    }

    var go = $("#go-settings");
    if (go) go.addEventListener("click", function () { location.hash = "#/about"; });

    mountRefreshButton();
  }

  /* 概览页右上角：立刻重新扫描。采样间隔 10 分钟，
     拔掉硬盘后不点它就得等下一轮才从列表里消失 */
  function mountRefreshButton() {
    var host = $("#topbar-right");
    if (!host || host.querySelector("#btn-refresh")) return;
    var btn = el("button", "btn btn-sm", "刷新");
    btn.type = "button";
    btn.id = "btn-refresh";
    btn.title = "重新扫描磁盘列表（拔出硬盘后不用等自动采样）";
    btn.addEventListener("click", function () { refreshDisks(false); });
    host.appendChild(btn);
  }

  function refreshDisks(silent) {
    if (state.refreshing) return Promise.resolve();
    state.refreshing = true;
    var btn = $("#btn-refresh");
    if (btn && !silent) {
      btn.disabled = true;
      btn.textContent = "刷新中…";
    }
    var before = ((state.overview || {}).disks || []).length;
    return postJSON("api/refresh", {}).then(function (res) {
      state.refreshing = false;
      if (btn) { btn.disabled = false; btn.textContent = "刷新"; }
      if (!res || !res.ok) {
        if (!silent) toast((res && res.error) || "刷新失败");
        return null;
      }
      return loadOverview().then(function () {
        var after = ((state.overview || {}).disks || []).length;
        // 手动刷新总是给反馈；自动刷新只在盘数真的变了时才提示
        if (!silent) {
          toast("已刷新，当前 " + after + " 块硬盘");
        } else if (after !== before) {
          toast(after > before ? "发现新硬盘，现在 " + after + " 块"
            : "硬盘已移除，现在 " + after + " 块");
        }
      });
    }).catch(function () {
      state.refreshing = false;
      if (btn) { btn.disabled = false; btn.textContent = "刷新"; }
      if (!silent) toast("刷新失败");
    });
  }

  /* 打开应用时如果快照已经放了半分钟以上，静默补扫一次：
     拔盘后直接进来看，不会还是十分钟前的列表。
     每次会话只补一次，来回切页不重复扫。 */
  function autoRefreshIfStale() {
    if (state.autoRefreshed) return;
    var last = (state.overview || {}).last_sample_at || 0;
    if (!last) return;
    if (Math.floor(Date.now() / 1000) - last > 30) {
      state.autoRefreshed = true;
      refreshDisks(true);
    }
  }

  /* ------------------------------------------------------------ 历史页 */

  function emptyHeatmap() {
    var cols = 53, rows = 7, cell = 11, gap = 4;
    var w = cols * (cell + gap) - gap, h = rows * (cell + gap) - gap;
    var seed = 20261104;
    function rnd() {
      seed = (seed * 9301 + 49297) % 233280;
      return seed / 233280;
    }
    var rects = "";
    for (var c = 0; c < cols; c += 1) {
      for (var r = 0; r < rows; r += 1) {
        var v = rnd();
        var op = v > 0.82 ? 0.30 : v > 0.6 ? 0.18 : v > 0.32 ? 0.10 : 0.045;
        rects += '<rect x="' + (c * (cell + gap)) + '" y="' + (r * (cell + gap)) +
          '" width="' + cell + '" height="' + cell + '" rx="3" fill="#2f6feb" opacity="' + op + '"/>';
      }
    }
    return '<svg viewBox="0 0 ' + w + ' ' + h + '" width="' + w + '" height="' + h + '">' + rects + '</svg>';
  }

  function historyEmpty() {
    return '<section class="card empty-hero">' +
      '<div class="empty-heat">' + emptyHeatmap() + '</div>' +
      '<h3>还没有选择硬盘</h3>' +
      '<p>选择一块硬盘，查看它这一年里每天的读写量与温度分布</p>' +
      '<button class="btn btn-primary" type="button" id="btn-pick-disk">选择硬盘</button>' +
      '</section>';
  }

  function mountYearSwitcher(years, current, onChange) {
    var host = $("#topbar-right");
    if (!host || !years.length) return;

    var earlier = years.filter(function (y) { return y < current; }).sort(function (a, b) { return b - a; })[0];
    var later = years.filter(function (y) { return y > current; }).sort(function (a, b) { return a - b; })[0];

    var wrap = el("div", "year-switch");

    var prev = el("button", "year-btn", "‹");
    prev.type = "button";
    prev.disabled = !earlier;
    prev.title = earlier ? earlier + " 年" : "没有更早的数据";
    if (earlier) prev.addEventListener("click", function () { onChange(earlier); });

    var label = el("span", "year-label", current + " 年");

    var next = el("button", "year-btn", "›");
    next.type = "button";
    next.disabled = !later;
    next.title = later ? later + " 年" : "没有更新的数据";
    if (later) next.addEventListener("click", function () { onChange(later); });

    wrap.appendChild(prev);
    wrap.appendChild(label);
    wrap.appendChild(next);
    host.appendChild(wrap);
  }

  function mountExportButton(key) {
    var host = $("#topbar-right");
    if (!host || !key) return;   // 没选盘就没有可导出的数据，按钮不出现
    var btn = el("button", "btn", "导出 JSON");
    btn.type = "button";
    btn.disabled = !key;
    btn.title = "导出该硬盘的全部采样记录（JSON，可用于日后导入）";
    btn.addEventListener("click", function () {
      var k = state.historyKey;
      if (!k) { toast("请先选择硬盘"); return; }
      // 走 fetch 拿 blob 再保存，而不是 <a download>：后者是浏览器原生发起的
      // 请求，带不上自定义头，在铁威马 TOS 上会被平台网关直接 403。
      toast("正在导出 " + modelOf(k) + " 的记录（JSON）…");
      fetch(API + "api/export?key=" + encodeURIComponent(k), { credentials: "include" })
        .then(function (res) {
          if (!res.ok) throw new Error("HTTP " + res.status);
          return res.blob();
        })
        .then(function (blob) {
          var name = "ssdplus-" + String(k).replace(/[^A-Za-z0-9._-]+/g, "_") + ".json";
          var url = URL.createObjectURL(blob);
          var link = document.createElement("a");
          link.href = url;
          link.download = name;
          document.body.appendChild(link);
          link.click();
          document.body.removeChild(link);
          setTimeout(function () { URL.revokeObjectURL(url); }, 1200);
        })
        .catch(function (err) { toast("导出失败：" + err.message); });
    });
    host.appendChild(btn);
  }

  /* -------------------------------------------------- 热力图悬停：当天明细 */
  var dayCache = {};
  var dayTipSeq = 0;

  function fmtRate(v) {
    v = Number(v) || 0;
    if (!v) return "0";
    var mb = v / 1048576;
    if (mb >= 1) return (mb >= 100 ? Math.round(mb) : Math.round(mb * 10) / 10) + " MB/s";
    return Math.round(v / 1024) + " KB/s";
  }

  function loadDay(key, date) {
    var ck = key + "|" + date;
    if (dayCache[ck]) return Promise.resolve(dayCache[ck]);
    return getJSON("api/day?key=" + encodeURIComponent(key) + "&date=" + date)
      .then(function (res) { dayCache[ck] = res || { points: [], total: 0 }; return dayCache[ck]; })
      .catch(function () { return { points: [], total: 0 }; });
  }

  function dayTipHtml(data, metric, date) {
    var pts = (data && data.points) || [];
    if (!pts.length) {
      return '<div class="tip-day"><div class="tip-empty">这一天没有采样记录</div></div>';
    }
    var field = metric === "read" ? "read" : (metric === "write" ? "write" : "temp");
    var label = metric === "read" ? "读取速率" : (metric === "write" ? "写入速率" : "温度");

    var start = new Date(date + "T00:00:00").getTime() / 1000;
    var vals = [];
    pts.forEach(function (p) {
      var v = p[field];
      if (typeof v === "number" && v > 0) vals.push({ ts: p.ts, v: v });
    });
    if (!vals.length) {
      return '<div class="tip-day"><div class="tip-empty">这一天没有' + label + '记录</div></div>';
    }

    var W = 232, H = 46;
    var raw = vals.map(function (o) { return o.v; }).sort(function (a, b) { return a - b; });
    var max = raw[raw.length - 1];
    var min = metric === "temp" ? raw[0] - 2 : 0;
    // 速率图用 98% 分位当纵轴上限：一次极端尖峰不会把日常波动压成一条直线，
    // 超过上限的点贴顶显示（仍然是最高点，只是不再拉扁整张图）。
    var top = metric === "temp" ? max : Math.max(
      raw[Math.min(raw.length - 1, Math.floor(raw.length * 0.95))], 1);
    if (top <= min) top = min + 1;

    var line = "", area = "";
    vals.forEach(function (o, i) {
      var x = Math.min(W, Math.max(0, (o.ts - start) / 86400 * W));
      var v = Math.min(o.v, top);
      var y = H - 3 - (v - min) / (top - min) * (H - 8);
      line += (i ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1);
    });
    area = line + "L" + W + " " + H + "L0 " + H + "Z";

    var peak = vals.reduce(function (a, b) { return b.v > a.v ? b : a; }, vals[0]);
    var pk = new Date(peak.ts * 1000);
    var peakTime = ("0" + pk.getHours()).slice(-2) + ":" + ("0" + pk.getMinutes()).slice(-2);
    var peakVal = metric === "temp" ? Math.round(peak.v) + "℃" : fmtRate(peak.v);

    var total = data.total || pts.length;
    return '<div class="tip-day">' +
      '<div class="tip-day-head"><span>' + esc(label) + ' · 24 小时</span>' +
      '<em>共 ' + total.toLocaleString("zh-CN") + ' 条</em></div>' +
      '<svg class="tip-spark" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none">' +
      '<path d="' + area + '" fill="' + (metric === "temp" ? "rgba(239,156,58,.16)" : "rgba(47,111,235,.13)") + '"/>' +
      '<path d="' + line + '" fill="none" stroke="' + (metric === "temp" ? "#ef9c3a" : "#2f6feb") +
      '" stroke-width="1.4" stroke-linejoin="round"/></svg>' +
      '<div class="tip-axis"><span>00</span><span>06</span><span>12</span><span>18</span><span>24</span></div>' +
      '<div class="tip-peak">峰值 <b>' + peakTime + "</b> · " + peakVal + '</div>' +
      '</div>';
  }

  function showDayTip(tip, item, device, metric) {
    var seq = ++dayTipSeq;
    var slot = document.createElement("div");
    slot.className = "tip-day";
    slot.innerHTML = '<div class="tip-load">正在读取当天明细…</div>';
    tip.appendChild(slot);
    return new Promise(function (resolve) {
      setTimeout(function () {
        if (seq !== dayTipSeq) { resolve(); return; }
        loadDay(device, item.date || item.d).then(function (data) {
          if (seq !== dayTipSeq) { resolve(); return; }
          slot.outerHTML = dayTipHtml(data, metric, item.date || item.d);
          resolve();
        });
      }, 90);
    });
  }

  function historyCharts(data) {
    var daily = (data.daily || []).filter(function (row) {
      return row.date && Number(row.date.slice(0, 4)) === state.historyYear;
    });
    var weekNames = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

    function tipFor(row) {
      var parts = row.date.split("-");
      var d = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
      var lines = ["<b>" + parts[0] + "年" + Number(parts[1]) + "月" + Number(parts[2]) + "日</b> " +
        weekNames[d.getDay()]];
      lines.push("读取 " + (row.read ? fmtSize(row.read) : "0"));
      lines.push("写入 " + (row.write ? fmtSize(row.write) : "0"));
      lines.push("温度 " + (row.temp === null || row.temp === undefined ? "—" : row.temp + "℃"));
      return lines.join("<br>");
    }

    function buildItems(field) {
      return daily
        .filter(function (row) { return typeof row[field] === "number" && row[field] !== null; })
        .map(function (row) { return { date: row.date, value: row[field], tip: tipFor(row) }; });
    }

    var year = state.historyYear;

    // 悬停明细按 disk_key 取：设备名会变，靠它查会把别的盘的曲线画到这块盘上
    var dev = data.key || state.historyKey;
    Charts.heatmap($("#chart-read"), {
      year: year,
      items: buildItems("read"),
      colors: ["#ebedf0", "#c6f0d8", "#7fd8a8", "#3fb37f", "#1f7a55"],
      onTip: function (tip, item) { return showDayTip(tip, item, dev, "read"); }
    });

    Charts.heatmap($("#chart-write"), {
      year: year,
      items: buildItems("write"),
      colors: ["#ebedf0", "#c9dcff", "#8fb6ff", "#4b83f0", "#1e4fbf"],
      onTip: function (tip, item) { return showDayTip(tip, item, dev, "write"); }
    });

    Charts.heatmap($("#chart-temp"), {
      year: year,
      items: buildItems("temp"),
      colors: ["#ebedf0", "#fde2b3", "#f7c46c", "#ef9c3a", "#d9661f"],
      onTip: function (tip, item) { return showDayTip(tip, item, dev, "temp"); }
    });
  }

  /* 历史页顶部的硬盘基本信息条 */
  function diskBrief(d) {
    var statusTone = d.status === "ok" ? "ok" : (d.status === "warn" ? "warn" : (d.status === "bad" ? "bad" : "unknown"));
    var statusText = d.status === "ok" ? "健康" : (d.status === "warn" ? "需关注" : (d.status === "bad" ? "异常" : "未知"));

    return '<section class="card disk-brief">' +
      '<div class="brief-top">' +
      '<div class="disk-id">' +
      '<span class="disk-icon">' + ICONS.disk + '</span>' +
      '<div class="disk-titles"><div class="disk-line">' +
      '<span class="disk-name" title="' + esc(d.model) + '">' + esc(d.model) + '</span>' +
      (d.protocol ? '<span class="proto">' + esc(d.protocol) + '</span>' : '') +
      '</div><div class="disk-sub"><span>' + esc(d.device) + '</span>' +
      '</div></div></div>' +
      '<div class="disk-facts">' +
      '<div class="fact-mini"><span>健康状态</span><b class="v-' + statusTone + '">' +
      (d.health === null || d.health === undefined ? "—" : d.health + "%") + '</b></div>' +
      '<div class="fact-mini"><span>温度</span><b>' + fmtTemp(d.temp) + '</b></div>' +
      '<div class="fact-mini"><span>累计写入</span><b>' + (d.smart_ok ? fmtSize(d.tbw) : "—") + '</b></div>' +
      '<div class="fact-mini"><span>累计读取</span><b>' + (d.smart_ok ? fmtSize(d.tbr) : "—") + '</b></div>' +
      '</div></div>' +
      '</section>';
  }

  function renderHistory() {
    var view = $("#view");
    var disks = (state.overview && state.overview.disks) || [];
    var key = state.historyKey;
    view.dataset.page = "history";

    if (!key) {
      view.innerHTML = historyEmpty();
    } else {
      var cur = null;
      disks.forEach(function (d) { if (!cur && keyOf(d) === key) cur = d; });
      view.innerHTML =
        (cur ? diskBrief(cur) : "") +
        '<section class="card chart-card">' +
        '<div class="chart-head"><h2>每日读取</h2></div>' +
        '<div class="chart-host" id="chart-read"></div></section>' +

        '<section class="card chart-card">' +
        '<div class="chart-head"><h2>每日写入</h2></div>' +
        '<div class="chart-host" id="chart-write"></div></section>' +

        '<section class="card chart-card">' +
        '<div class="chart-head"><h2>每日温度</h2></div>' +
        '<div class="chart-host" id="chart-temp"></div></section>';
    }

    updateTopbarSelector(disks, key ? [key] : [], function (next) {
      state.historyKey = next[0] || null;
      state.historyYear = null;
      loadHistory();
    }, true);

    if (key && state.history) {
      mountYearSwitcher(state.history.years || [], state.historyYear, function (year) {
        state.historyYear = year;
        renderHistory();
      });
    }
    mountHistoryRefreshButton(key);
    mountExportButton(key);

    if (!key) {
      var pick = $("#btn-pick-disk");
      if (pick) {
        pick.addEventListener("click", function () {
          var trigger = document.querySelector(".selector-btn");
          if (trigger) trigger.click();
        });
      }
      return;
    }
    if (state.history) historyCharts(state.history);
  }

  /* ------------------------------------------------------------ 测速页 */

  function fmtMbps(v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return (v >= 100 ? Math.round(v) : Math.round(v * 10) / 10) + " MB/s";
  }

  function gaugeMax(v) {
    if (!v) return 500;
    var steps = [100, 200, 500, 1000, 2000, 4000, 8000, 16000];
    for (var i = 0; i < steps.length; i += 1) {
      if (v <= steps[i]) return steps[i];
    }
    return 16000;
  }

  function growGauge(gauge, value) {
    if (!gauge) return;
    if (value > gauge.getMax() * 0.92) gauge.setMax(value * 1.3);
  }

  function benchHasHistory(key) {
    if (!key) return false;
    var items = (state.bench || {}).items || [];
    for (var i = 0; i < items.length; i += 1) {
      if (items[i].disk_key === key) return true;
    }
    return false;
  }

  /* 测速页顶部：选择硬盘 + 开始/再次测速，两个按钮常驻 */
  function mountBenchTopbar(disks, key, running) {
    updateTopbarSelector(disks, key ? [key] : [], function (next) {
      state.benchKey = next[0] || null;
      state.benchRun = null;          // 换盘后不保留上一块盘的结果
      renderBench();
      if (state.benchKey) loadBench();
    }, true);

    var host = $("#topbar-right");
    var label = running ? "停止测速" : (benchHasHistory(key) ? "再次测速" : "开始测速");
    // 测速中按钮变成可点的「停止测速」，用深色和主操作区分开
    var btn = el("button", running ? "btn btn-sm btn-dark" : "btn btn-primary", label);
    btn.type = "button";
    btn.id = "btn-bench";
    btn.disabled = running ? false : !key;
    btn.title = running
      ? "立刻中断本次测速，未完成的成绩不计入历史"
      : (key ? (benchHasHistory(key) ? "对该盘重新测速" : "对该盘测速") : "请先选择硬盘");
    if (running) {
      btn.addEventListener("click", function () { stopBench(); });
    } else if (key) {
      btn.addEventListener("click", function () { startBench(key); });
    }
    host.appendChild(btn);
  }

  function stopBench() {
    var btn = $("#btn-bench");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "正在停止…";
    }
    postJSON("api/bench/stop", {}).then(function (res) {
      if (!res.ok) {
        toast(res.error || "停止失败");
        if (btn) { btn.disabled = false; btn.textContent = "停止测速"; }
      }
    }).catch(function () {
      if (btn) { btn.disabled = false; btn.textContent = "停止测速"; }
    });
  }

  function startBench(key) {
    state.benchRun = { key: key, running: true, stage: "准备中", percent: 0, result: null, live: null };
    state.benchPlayer = { pending: [], shown: { read: 0, write: 0 }, playing: false };
    renderBench();
    postJSON("api/bench/start", { key: key }).then(function (res) {
      if (!res.ok) {
        toast(res.error || "启动失败");
        state.benchRun = null;
        renderBench();
        return;
      }
      pollBench();
    });
  }

  function benchMetaLine(key) {
    if (!key) return "选择硬盘后即可开始测速";
    var last = null;
    ((state.bench || {}).items || []).forEach(function (item) {
      if (!last && item.disk_key === key) last = item;
    });
    if (!last) return "该盘尚未测速";
    return "该盘最近测速 " + fmtTime(last.ts) + " · 读取 " + fmtMbps(last.read_mbps) +
      " · 写入 " + fmtMbps(last.write_mbps);
  }

  function renderBench() {
    var view = $("#view");
    var disks = (state.overview && state.overview.disks) || [];
    var key = state.benchKey;
    var run = state.benchRun && state.benchRun.key === key ? state.benchRun : null;
    var running = !!(run && run.running);
    view.dataset.page = "bench";

    mountBenchTopbar(disks, key, running);

    // 默认就展示仪表盘 + 全部历史记录，不因为没选盘而空着

    var result = run && run.result ? run.result : null;
    var plan = (state.bench || {}).plan || null;
    // 没有可写挂载点时说明原因（未挂载 / 进了 RAID 池），别只写"未测"让人猜
    var hint = "";
    if (plan && key) {
      if (plan.write_dir) {
        hint = "写入 " + plan.write_dir + (plan.read_device ? " · 直读 " + plan.read_device : "");
      } else {
        hint = (plan.read_device ? "直读 " + plan.read_device + " · " : "") +
          (plan.write_reason || "写入未测");
      }
    }

    var html = '<section class="card bench-gauge-card">';
    html += '<div class="bench-progress-wrap' + (running ? '' : ' is-hidden') + '" id="bench-progress-wrap">' +
      '<div class="bench-progress"><i id="bench-progress-bar" style="width:' + (run ? run.percent : 0) + '%"></i></div>' +
      '<span class="bench-stage" id="bench-stage">' + esc((run && run.stage) || "") + '</span></div>';
    html += '<div class="gauge-row"><div class="gauge-item" id="gauge-read"></div>' +
      '<div class="gauge-item" id="gauge-write"></div></div>';
    html += '<div class="bench-bar"><div class="bench-meta" id="bench-meta">' +
      esc(benchMetaLine(key)) + '</div>' +
      (hint ? '<span class="bench-plan">' + esc(hint) + '</span>' : '') + '</div></section>';

    html += '<section class="card table-card"><div class="table-head">' +
      '<h2>历史测速</h2>' +
      '<button class="btn btn-sm btn-dark" id="btn-clear" type="button">清空记录</button></div>' +
      '<table class="table bench-history"><thead><tr><th>时间</th><th>硬盘</th><th>读取</th><th>写入</th><th>说明</th></tr></thead><tbody>';
    var items = (state.bench || {}).items || [];
    if (!items.length) {
      html += '<tr><td colspan="5"><div class="empty">暂无测速记录</div></td></tr>';
    } else {
      items.forEach(function (row) {
        var when = window.innerWidth <= 720 ? fmtTimeBrief(row.ts) : fmtTime(row.ts);
        html += '<tr><td class="c-muted">' + when + '</td>' +
          '<td><div class="device-cell">' + ICONS.disk +
          '<span class="bench-name" title="' + esc(row.model || row.device) + '">' +
          esc(row.model || row.device) + '</span>' +
          (row.model ? '<span class="bench-dev">' + esc(row.device) + '</span>' : '') + '</div></td>' +
          '<td class="c-ok">' + fmtMbps(row.read_mbps) + '</td>' +
          '<td class="c-blue">' + fmtMbps(row.write_mbps) + '</td>' +
          '<td class="c-muted"><span class="bench-note" title="' + esc(row.note || "") + '">' +
          esc(row.note || "") + '</span></td></tr>';
      });
    }
    html += '</tbody></table></section>';
    view.innerHTML = html;

    // 本次刚跑完用本次结果；否则回落到该盘最近一条记录，页面刷新后也能看到上次成绩
    var lastRow = null;
    ((state.bench || {}).items || []).forEach(function (item) {
      if (!lastRow && item.disk_key === key) lastRow = item;
    });
    // 点「再次测速」的瞬间先把指针归零，别让上一次的旧成绩一直挂着，
    // 等第一轮数据回来再往回摆；刻度仍按旧成绩算，免得数字归零时表盘跟着缩一圈
    var ticking = !!(run && run.running);
    var readScale = result ? result.read_mbps : (lastRow ? lastRow.read_mbps : 0);
    var writeScale = result ? result.write_mbps : (lastRow ? lastRow.write_mbps : 0);
    var readInit = ticking ? 0 : readScale;
    var writeInit = ticking ? 0 : writeScale;
    state.benchGauges = {
      read: Charts.gauge($("#gauge-read"), { label: "Read", value: readInit, max: gaugeMax(readScale) }),
      write: Charts.gauge($("#gauge-write"), { label: "Write", value: writeInit, max: gaugeMax(writeScale) })
    };

    var clear = $("#btn-clear");
    if (clear) {
      clear.addEventListener("click", function () {
        if (!window.confirm("确认清空全部测速记录？")) return;
        postJSON("api/bench/clear", {}).then(function () {
          toast("已清空");
          loadBench();
        });
      });
    }
  }

  /* 指针播放器：按队列一轮一轮地转，轮次太快也不会漏 */
  function ensurePlayer() {
    if (!state.benchPlayer) {
      state.benchPlayer = { pending: [], shown: { read: 0, write: 0 }, playing: false };
    }
    return state.benchPlayer;
  }

  /* 每轮实测 8 秒，节奏够慢，指针直接跟着走，不再排队补动画 */
  function collectRounds(st) {
    var player = ensurePlayer();
    var rounds = st.rounds || {};
    var gauges = state.benchGauges;
    ["read", "write"].forEach(function (phase) {
      var arr = rounds[phase] || [];
      if (arr.length <= player.shown[phase]) return;
      var value = arr[arr.length - 1];
      player.shown[phase] = arr.length;
      var gauge = phase === "read" ? gauges && gauges.read : gauges && gauges.write;
      if (gauge) {
        growGauge(gauge, value);
        gauge.setValue(value);
      }
    });
  }

  /* 测的过程中指针跟着瞬时速度摆；读写之间那 3 秒归零，让人看清一段结束了 */
  function applyLiveGauge(st) {
    var live = st.live || {};
    var gauges = state.benchGauges;
    if (!live.phase || !gauges) return;
    var gauge = live.phase === "write" ? gauges.write : gauges.read;
    if (!gauge) return;
    if (live.gap) {
      gauge.setValue(0);
      return;
    }
    if (typeof live.rate === "number") {
      growGauge(gauge, live.rate);
      gauge.setValue(live.rate);
    }
  }

  /* 队列播完且测速已结束时，把指针落到本次最高成绩 */
  function settleIfDone() {
    var run = state.benchRun;
    var player = state.benchPlayer;
    if (!run || run.running || !player || player.pending.length) return;
    if (!run.result || run.settled) return;
    var gauges = state.benchGauges;
    if (gauges) {
      growGauge(gauges.read, run.result.read_mbps);
      growGauge(gauges.write, run.result.write_mbps || 0);
      gauges.read.setValue(run.result.read_mbps);
      gauges.write.setValue(run.result.write_mbps || 0);
    }
    run.settled = true;
    var meta = $("#bench-meta");
    if (meta) {
      meta.textContent = "本次测速完成 · 读取 " + fmtMbps(run.result.read_mbps) +
        " · 写入 " + fmtMbps(run.result.write_mbps);
    }
    setTimeout(function () {
      if (state.page === "bench") loadBench();
    }, 1200);
  }

  function applyBenchLive(res) {
    var st = (res && res.state) || {};
    var run = state.benchRun;
    if (!run) return;
    run.stage = st.stage;
    run.percent = st.percent || 0;
    run.running = !!st.running;

    applyLiveGauge(st);
    collectRounds(st);

    var bar = $("#bench-progress-bar");
    if (bar) bar.style.width = (run.percent || 0) + "%";
    var stage = $("#bench-stage");
    if (stage) stage.textContent = run.stage || "";
    var wrap = $("#bench-progress-wrap");
    if (wrap) wrap.classList.toggle("is-hidden", !run.running);

    if (!run.running) {
      if (st.error && !run.errorShown) {
        run.errorShown = true;
        toast(st.error);
      }
      if (st.cancelled && !run.stopNoticed) {
        // 本次作废：丢掉临时结果，表盘按历史成绩重画
        run.stopNoticed = true;
        state.benchRun = null;
        toast("已停止测速，本次不计入历史");
        loadBench();
        return;
      }
      if (st.result && (st.key || st.device) === run.key) run.result = st.result;
      var btn = $("#btn-bench");
      if (btn) {
        btn.disabled = false;
        btn.textContent = (st.result || benchHasHistory(run.key)) ? "再次测速" : "开始测速";
      }
      settleIfDone();
    }
  }

  function pollBench() {
    clearTimeout(state.benchTimer);
    var key = state.benchRun ? state.benchRun.key : state.benchKey;
    var q = key ? ("?key=" + encodeURIComponent(key)) : "";
    getJSON("api/bench" + q).then(function (res) {
      state.bench = res;
      if (state.page !== "bench") return;
      if (!state.benchRun || !state.benchRun.running) {
        applyBenchLive(res);
        return;
      }
      applyBenchLive(res);
      if (state.benchRun && state.benchRun.running) {
        state.benchTimer = setTimeout(pollBench, 300);
      }
    }).catch(function () {
      state.benchTimer = setTimeout(pollBench, 1500);
    });
  }

  /* ------------------------------------------------------------ 关于页 */

  function mountHelp(info) {
    var host = $("#topbar-right");
    if (!host) return;
    var rows = [
      ["磁盘数量", info.disks + " 块"],
      ["数据库", fmtBytes(info.db_size) + " · " + (info.sample_count || 0).toLocaleString("zh-CN") + " 条明细"],
      ["明细保留", (info.raw_keep_days || 90) + " 天，更早为天级记录"],
      ["数据目录", info.data_dir],
      ["查询工具", info.smartctl]
    ];
    var roll = info.rollup || {};
    if (roll.done === false) {
      rows.splice(1, 0, ["历史整理", "进行中，还剩 " + (roll.pending || 0).toLocaleString("zh-CN") + " 条"]);
    }
    if (info.skipped && info.skipped.length) {
      var names = info.skipped.slice(0, 2).map(function (s) { return s.model || s.device; }).join("、");
      if (info.skipped.length > 2) names += " 等";
      rows.push(["未纳入统计", info.skipped.length + " 块（" + names + "）"]);
    }
    var rowsHtml = rows.map(function (pair) {
      return '<dt>' + esc(pair[0]) + '</dt><dd>' + esc(pair[1]) + '</dd>';
    }).join("");
    var wrap = el("div", "help-wrap");
    wrap.innerHTML =
      '<button class="help-btn" type="button" aria-label="运行信息">?</button>' +
      '<div class="help-tip"><dl class="help-list">' + rowsHtml + '</dl></div>';
    host.appendChild(wrap);
  }

  function mountAboutActions(info) {
    var host = $("#topbar-right");
    if (!host) return;
    host.innerHTML = "";

    var reset = el("button", "btn btn-sm", "恢复默认");
    reset.type = "button";
    reset.id = "btn-reset";
    reset.title = "把上面的数值恢复成默认设置";
    reset.addEventListener("click", function () {
      if (!window.confirm("把所有数值恢复为默认设置？\n\n备份目录和已有的备份记录不会变动。")) return;
      reset.disabled = true;
      postJSON("api/settings/reset", {}).then(function (r) {
        if (!r.ok) { toast(r.error || "恢复失败"); reset.disabled = false; return; }
        toast(r.changed ? "已恢复 " + r.changed + " 项默认值" : "当前已是默认值");
        renderAbout();
      });
    });
    host.appendChild(reset);

    var save = el("button", "btn btn-primary btn-sm", "保存设置");
    save.type = "button";
    save.id = "btn-save";
    host.appendChild(save);
    mountHelp(info);
  }

  function helpDot(text) {
    return '<div class="help-wrap"><button class="help-btn" type="button" aria-label="说明">?</button>' +
      '<div class="help-tip help-tip-text">' + esc(text) + '</div></div>';
  }

  /* 「采集与测速」问号里的说明：把"每天写多少"算清楚，回应怕频繁采集伤盘的顾虑 */
  function sampleVolumeText(info, cfg) {
    // 只有样本够多时才用实测均值；刚装的库文件里还有表和索引的空页，除下来会虚高
    var perRow = 130;
    if (info.sample_count >= 5000 && info.db_size > 0) {
      perRow = Math.round(info.db_size / info.sample_count);
    }
    perRow = Math.max(80, Math.min(400, perRow));

    var interval = Math.max(5, Number(cfg.sample_interval) || 60);
    var perDay = Math.round(86400 / interval);
    var dayBytes = perDay * perRow;
    var disks = Math.max(1, Number(info.disks) || 1);
    var dayText = dayBytes >= 1048576
      ? (dayBytes / 1048576).toFixed(1) + " MB"
      : Math.round(dayBytes / 1024) + " KB";

    var keep = Math.max(7, Number(cfg.raw_keep_days) || 90);
    var rawTotal = dayBytes * disks * keep;
    var rawText = rawTotal >= 1073741824
      ? (rawTotal / 1073741824).toFixed(1) + " GB"
      : Math.round(rawTotal / 1048576) + " MB";
    // 小时层 + 天层的固定开销：每块盘每小时一条、每天一条，5 年后也只到这个量级
    var coldPerDiskYear = 8760 * 40 + 365 * 90;
    var cold5y = (coldPerDiskYear * disks * 5) / 1048576;
    var capText = cold5y >= 1024
      ? (cold5y / 1024).toFixed(1) + " GB"
      : Math.round(cold5y) + " MB";

    return "采样间隔：系统每隔多久读取一次硬盘SMART信息，" +
      "数值越小，历史曲线颗粒越细、硬盘唤醒越频繁。\n\n" +
      "为您计算：每块硬盘每 " + interval + " 秒记录 1 条，一天 " + perDay.toLocaleString("zh-CN") +
      " 条，约 " + dayText + "。这样的精细记录只保留最近 " + keep + " 天（" +
      disks + " 块盘合计约 " + rawText + "），更早的数据会压缩成小时级别和天级别来记录，" +
      "所以记录数据不会随年数无限上涨——" + disks + " 块盘长期稳定在 " + capText + " 以内。\n\n" +
      "压缩不会丢历史数据；每天读写多少、温度峰值、健康度变化这些都会永久保留，" +
      "只是几年前某一天的波形记录会变成小时级别或天级别粒度。\n\n" +
      "采集时只是读取SMART，不往硬盘里写任何数据，对固态硬盘寿命的影响可以忽略。";
  }

  function renderAbout() {
    var view = $("#view");
    view.innerHTML = '<div class="loading">正在加载…</div>';
    view.dataset.page = "about";
    Promise.all([getJSON("api/about"), getJSON("api/settings"), getJSON("api/backup/list")]).then(function (res) {
      var info = res[0], cfg = res[1], snaps = (res[2] && res[2].items) || [];
      // 「上次备份」不再单独占一行，直接标在下面列表里那份最新的快照上
      var lastBackupName = cfg.backup_last_path ? String(cfg.backup_last_path).split("/").pop() : "";
      var nextBackup = fmtNextBackup(cfg);

      var snapRows = snaps.length
        ? snaps.map(function (s) {
            var isLast = lastBackupName && s.name === lastBackupName;
            return '<div class="snap-row">' +
              '<div class="snap-main"><div class="snap-line">' +
              '<span class="snap-name">' + esc(s.name) + '</span>' +
              (isLast ? '<i class="snap-flag">上次备份</i>' : '') + '</div>' +
              '<em>' + fmtBytes(s.bytes) + ' · ' + fmtTime(s.time) + '</em></div>' +
              '<div class="snap-acts">' +
              '<button class="btn btn-sm snap-restore" type="button" data-path="' +
              esc(s.path) + '">恢复</button>' +
              '<button class="btn btn-sm snap-del" type="button" data-path="' +
              esc(s.path) + '" data-name="' + esc(s.name) + '">删除</button>' +
              '</div></div>';
          }).join("")
        : '<p class="form-hint">还没有备份快照。点「立即备份」会生成一份，之后按间隔自动生成。</p>';

      view.innerHTML =
        '<section class="card settings-card">' +
        '<div class="chart-head"><h2>采样与测速</h2>' +
        helpDot(sampleVolumeText(info, cfg)) + '</div>' +
        '<div class="form-grid">' +
        '<div class="form-item"><span>采样间隔（秒）</span>' +
        '<input id="cfg-interval" class="input" type="number" min="60" step="10" value="' + cfg.sample_interval + '"></div>' +
        '<label class="form-item"><span>分钟级明细保留（天）</span>' +
        '<input id="cfg-raw-keep" class="input" value="' + (cfg.raw_keep_days || 90) + '"></label>' +
        '<label class="form-item"><span>测速文件大小（MB）</span>' +
        '<input id="cfg-bench" class="input" value="' + cfg.bench_size_mb + '"></label>' +
        '</div>' +
        '</section>' +

        '<section class="card settings-card">' +
        '<div class="chart-head"><h2>面板告警</h2>' +
        helpDot("检测到硬盘健康度偏低、温度过高、介质错误增加、异常断电等情况时，会记录到概况页的「最近事件」里。" +
          "同一块硬盘的同一个问题，在通知频率内只提醒一次。") + '</div>' +
        '<div class="form-grid">' +
        '<label class="form-item"><span>健康度低于（%）</span>' +
        '<input id="cfg-alert-health" class="input" value="' + cfg.notify_health_below + '"></label>' +
        '<label class="form-item"><span>温度高于（℃）</span>' +
        '<input id="cfg-alert-temp" class="input" value="' + cfg.alert_temp_high + '"></label>' +
        '<label class="form-item"><span>通知频率（秒）</span>' +
        '<input id="cfg-alert-interval" class="input" value="' + cfg.alert_interval_seconds + '"></label>' +
        '</div>' +
        '</section>' +

        '<section class="card settings-card">' +
        '<div class="chart-head"><h2>备份与恢复</h2>' +
        '<label class="switch"><input type="checkbox" id="cfg-backup-enabled"' +
        (cfg.backup_enabled ? " checked" : "") + '><span></span>自动备份</label></div>' +
        '<div class="form-grid">' +
        '<label class="form-item"><span class="field-label">备份间隔（天）' +
        helpDot("下次备份：" + nextBackup + "\n\n" +
          "系统会按此间隔生成一份数据库快照，到点备份，不影响日常采样。\n\n" +
          "恢复备份会和快照里的记录合并，" +
          "不会覆盖现在已有的数据。") +
        '</span>' +
        '<input id="cfg-backup-interval" class="input" value="' + cfg.backup_interval_days + '"></label>' +
        '<label class="form-item"><span>保留份数</span>' +
        '<input id="cfg-backup-keep" class="input" value="' + cfg.backup_keep + '"></label>' +
        '<label class="form-item"><span>占用上限（MB）</span>' +
        '<input id="cfg-backup-keep-mb" class="input" value="' + (cfg.backup_keep_mb || 500) + '"></label>' +
        '</div>' +
        '<label class="form-item form-wide"><span>备份目录</span>' +
        '<input id="cfg-backup-dir" class="input" value="' + esc(cfg.backup_dir) + '"></label>' +
        '<div class="form-actions">' +
        '<button class="btn btn-sm" id="btn-backup-now" type="button">立即备份</button>' +
        '<button class="btn btn-sm" id="btn-import" type="button">导入 JSON</button>' +
        '<button class="btn btn-sm btn-danger" id="btn-clear-data" type="button">清空历史数据</button>' +
        '<input type="file" id="file-import" accept=".json,application/json" hidden>' +
        '</div>' +
        '<div class="snap-list">' + snapRows + '</div>' +
        '</section>' +

        '';

      mountAboutActions(info);

      // 采集间隔不允许低于 60 秒：输入时就标红提示，离开输入框直接拉回 60，
      // 让用户一眼知道下限，而不是保存后才被后端悄悄改掉
      var intervalInput = $("#cfg-interval");
      if (intervalInput) {
        var fixInterval = function () {
          var value = parseInt(intervalInput.value, 10);
          if (isNaN(value) || value < 60) {
            intervalInput.value = 60;
            intervalInput.classList.add("is-flash");
            setTimeout(function () { intervalInput.classList.remove("is-flash"); }, 600);
          }
          intervalInput.classList.remove("is-warn");
        };
        intervalInput.addEventListener("input", function () {
          var value = parseInt(intervalInput.value, 10);
          intervalInput.classList.toggle("is-warn", !isNaN(value) && value < 60);
        });
        intervalInput.addEventListener("blur", fixInterval);
        intervalInput.addEventListener("change", fixInterval);
        intervalInput.addEventListener("keydown", function (e) {
          if (e.key === "Enter") { fixInterval(); this.blur(); }
        });
      }

      $("#btn-save").addEventListener("click", function () {
        if (intervalInput) {
          var value = parseInt(intervalInput.value, 10);
          if (isNaN(value) || value < 60) intervalInput.value = 60;
        }
        postJSON("api/settings", {
          sample_interval: $("#cfg-interval").value,
          raw_keep_days: $("#cfg-raw-keep").value,
          notify_health_below: $("#cfg-alert-health").value,
          bench_size_mb: $("#cfg-bench").value,
          alert_enabled: true,
          alert_temp_high: $("#cfg-alert-temp").value,
          alert_interval_seconds: $("#cfg-alert-interval").value,
          backup_enabled: $("#cfg-backup-enabled").checked,
          backup_interval_days: $("#cfg-backup-interval").value,
          backup_keep: $("#cfg-backup-keep").value,
          backup_keep_mb: $("#cfg-backup-keep-mb").value,
          backup_dir: $("#cfg-backup-dir").value
        }).then(function (r) {
          toast(r.ok ? "设置已保存" : (r.error || "保存失败"));
        });
      });

      $("#btn-backup-now").addEventListener("click", function () {
        var btn = this;
        btn.disabled = true;
        postJSON("api/backup/now", {}).then(function (r) {
          if (r.ok) {
            toast("已备份到 " + r.path.split("/").pop() + "（" + fmtBytes(r.bytes) + "）");
            renderAbout();
          } else {
            toast(r.error || "备份失败");
            btn.disabled = false;
          }
        });
      });

      Array.prototype.forEach.call(view.querySelectorAll(".snap-restore"), function (btn) {
        btn.addEventListener("click", function () {
          var path = btn.getAttribute("data-path") || "";
          if (!path) return;
          if (!window.confirm("把这份快照里的记录合并到当前数据库？\n\n" + path.split("/").pop() +
              "\n\n已存在的时间点会自动跳过，不会重复也不会覆盖现有数据。")) return;
          btn.disabled = true;
          postJSON("api/backup/restore", { path: path }).then(function (r) {
            if (!r.ok) { toast(r.error || "恢复失败"); btn.disabled = false; return; }
            toast("恢复完成：合并 " + r.added + " 条采样");
            loadOverview().then(function () { renderAbout(); });
          });
        });
      });

      Array.prototype.forEach.call(view.querySelectorAll(".snap-del"), function (btn) {
        btn.addEventListener("click", function () {
          var path = btn.getAttribute("data-path") || "";
          var name = btn.getAttribute("data-name") || path.split("/").pop();
          if (!path) return;
          if (!window.confirm("删除这份备份？\n\n" + name + "\n\n文件会从备份目录里直接移除，不能撤销。")) return;
          btn.disabled = true;
          postJSON("api/backup/delete", { path: path }).then(function (r) {
            if (!r.ok) { toast(r.error || "删除失败"); btn.disabled = false; return; }
            toast("已删除 " + (r.name || name));
            renderAbout();
          });
        });
      });

      var clearData = $("#btn-clear-data");
      if (clearData) {
        clearData.addEventListener("click", function () {
          var btn = this;
          if (!window.confirm(
            "清空所有历史数据，重新开始记录？\n\n" +
            "会删除：全部采样明细、每小时/每日汇总、测速记录、事件告警。\n" +
            "会保留：硬盘身份信息与当前 SMART 状态、备份文件。\n\n" +
            "此操作不可撤销，建议先点「立即备份」。")) return;
          btn.disabled = true;
          btn.textContent = "正在清空…";
          postJSON("api/clear-data", {}).then(function (r) {
            btn.disabled = false;
            btn.textContent = "清空历史数据";
            if (!r.ok) { toast(r.error || "清空失败"); return; }
            var n = r.samples || 0;
            toast("已清空历史数据（" + n.toLocaleString("zh-CN") + " 条采样）");
            loadOverview().then(function () { renderAbout(); });
          }).catch(function () {
            btn.disabled = false;
            btn.textContent = "清空历史数据";
          });
        });
      }

      var fileInput = $("#file-import");
      $("#btn-import").addEventListener("click", function () { fileInput.click(); });
      fileInput.addEventListener("change", function () {
        var file = fileInput.files && fileInput.files[0];
        if (!file) return;
        var reader = new FileReader();
        reader.onload = function () {
          var payload;
          try {
            payload = JSON.parse(reader.result);
          } catch (err) {
            toast("文件不是合法的 JSON");
            return;
          }
          toast("正在导入…");
          postJSON("api/import", payload).then(function (r) {
            if (!r.ok) { toast(r.error || "导入失败"); return; }
            var disks = (r.disks || []).length;
            var days = (r.disks || []).reduce(function (a, b) { return a + (b.days || 0); }, 0);
            toast("导入完成：新增 " + r.added + " 条采样" +
              (days ? "、" + days + " 天汇总" : "") + "，涉及 " + disks + " 块盘");
            loadOverview().then(function () { renderAbout(); });
          });
        };
        reader.readAsText(file);
        fileInput.value = "";
      });
    }).catch(function (err) {
      console.warn(err);
      view.innerHTML = '<div class="card"><div class="empty">设置加载失败，请刷新页面</div></div>';
    });
  }

  /* ------------------------------------------------------------ 顶栏 */

  /* 硬盘下拉单独占一个容器，夹在标题和右侧按钮之间。
     放在 topbar-right 里的话，历史页要求"下拉独占第二行并拉通"就没法做——
     负 margin 会把标题挤走。容器建一次就复用。 */
  function topbarSelectorHost() {
    var bar = document.querySelector(".topbar");
    if (!bar) return $("#topbar-right");
    var host = bar.querySelector("#topbar-sel");
    if (!host) {
      host = el("div", "");
      host.id = "topbar-sel";
      var right = bar.querySelector("#topbar-right");
      if (right) bar.insertBefore(host, right);
      else bar.appendChild(host);
    }
    host.innerHTML = "";
    return host;
  }

  function updateTopbarSelector(disks, selected, onChange, single) {
    var host = topbarSelectorHost();
    host.innerHTML = "";
    // 下拉挪出去之后，右侧容器不再顺带被清空：这里补一次，
    // 否则每次重绘都会往 topbar-right 里再塞一个按钮
    var right = $("#topbar-right");
    if (right) right.innerHTML = "";
    if (!disks.length) return;
    var list = (selected || []).slice();
    // 多选场景默认全选；单选（历史页）默认不选，交给用户主动选择
    if (!single && !list.length) list = disks.map(keyOf);
    mountSelector(host, {
      disks: disks,
      selected: list,
      single: single,
      onChange: onChange
    });
  }

  /* ------------------------------------------------------------ 加载 */

  function loadEvents() {
    return getJSON("api/events?limit=20").then(function (res) {
      state.events = res.items || [];
      if (state.page === "overview" && state.overview) renderOverview();
    }).catch(function () {});
  }

  function loadOverview() {
    return getJSON("api/overview").then(function (data) {
      state.overview = data;
      var valid = data.disks.map(keyOf);
      valid.forEach(function (k) { state.seenKeys[k] = true; });
      if (!state.selected) {
        state.selected = valid;
      } else {
        // 临时拔掉的盘先留在记忆里：插回来还能自动恢复勾选。
        // 只有用户手动取消的才会真正从 selected 里去掉。
        var kept = state.selected.filter(function (k) {
          return valid.indexOf(k) >= 0 || state.seenKeys[k];
        });
        state.selected = kept.length ? kept : valid;
      }
      if (state.page === "overview") renderOverview();
      if (state.page === "history") renderHistory();
      if (state.page === "bench") renderBench();
    }).catch(function (err) {
      console.warn(err);
      if (state.page === "overview") {
        $("#view").innerHTML = '<div class="card"><div class="empty">无法连接后端服务，请检查应用运行状态</div></div>';
      }
    });
  }

  function loadHistory(onDone) {
    var key = state.historyKey;
    if (!key) {
      state.history = null;
      if (state.page === "history") renderHistory();
      if (onDone) onDone(true);
      return Promise.resolve();
    }
    return getJSON("api/history?keys=" + encodeURIComponent(key)).then(function (data) {
      state.history = data;
      var years = data.years || [];
      if (!state.historyYear || years.indexOf(state.historyYear) < 0) {
        state.historyYear = years.length ? years[0] : new Date().getFullYear();
      }
      if (state.page === "history") renderHistory();
      if (onDone) onDone(true);
    }).catch(function (err) {
      console.warn("[ssdplus] history", err);
      toast("历史数据加载失败");
      if (state.page === "history") {
        var view = $("#view");
        view.dataset.page = "error";
        view.innerHTML = '<section class="card"><div class="empty-state">' +
          '<h3>历史数据加载失败</h3><p>请刷新页面重试；若持续出现，请查看应用日志。</p></div></section>';
      }
      if (onDone) onDone(false);
    });
  }

  /* 历史页右上角：立刻重新拉取当前硬盘的历史数据。
     采样在后台每 60 秒写一条，但页面只会在进入时拉一次，
     不加这个按钮就得靠反复刷新网页才能看到最新的一天。 */
  function refreshHistory() {
    var btn = $("#btn-history-refresh");
    if (!btn || btn.disabled) return;
    btn.disabled = true;
    btn.textContent = "刷新中…";
    dayCache = {};   // 悬停明细也一起失效，别让今天的格子还弹旧数据
    loadHistory(function (ok) {
      if (btn) { btn.disabled = false; btn.textContent = "刷新"; }
      if (ok) toast("已刷新历史数据");
    });
  }

  function mountHistoryRefreshButton(key) {
    var host = $("#topbar-right");
    if (!host || !key || host.querySelector("#btn-history-refresh")) return;
    var btn = el("button", "btn btn-sm", "刷新");
    btn.type = "button";
    btn.id = "btn-history-refresh";
    btn.title = "立即重新拉取该硬盘的最新历史数据";
    btn.addEventListener("click", function () { refreshHistory(); });
    host.appendChild(btn);
  }

  function loadBench() {
    var q = state.benchKey ? ("?key=" + encodeURIComponent(state.benchKey)) : "";
    return getJSON("api/bench" + q).then(function (data) {
      state.bench = data;
      if (state.page === "bench") renderBench();
      if (data.state && data.state.running) pollBench();
    });
  }

  /* ------------------------------------------------------------ 路由 */

  var TITLES = { overview: "硬盘概况", history: "历史数据", bench: "硬盘测速", about: "应用设置" };

  function route() {
    var hash = (location.hash || "#/overview").replace("#/", "");
    var page = TITLES[hash] ? hash : "overview";
    state.page = page;
    $("#page-title").textContent = TITLES[page];
    var topbar = document.querySelector(".topbar");
    if (topbar) topbar.setAttribute("data-page", page);
    document.querySelectorAll(".nav-item").forEach(function (item) {
      item.classList.toggle("is-active", item.getAttribute("data-page") === page);
    });
    $("#topbar-right").innerHTML = "";
    var selHost = $("#topbar-sel");
    if (selHost) selHost.innerHTML = "";   // 切到不需要选盘的页面时清掉下拉
    clearInterval(state.timer);

    if (page === "about") { renderAbout(); return; }

    var ready = state.overview ? Promise.resolve() : loadOverview();
    ready.then(function () {
      if (page === "overview") {
        renderOverview();
        loadEvents();
        autoRefreshIfStale();   // 快照太旧就静默补一次扫描
        state.timer = setInterval(function () {
          getJSON("api/overview").then(function (data) {
            state.overview = data;
            if (state.page === "overview") renderOverview();
          }).catch(function () {});
        }, 8000);
      } else if (page === "history") {
        if (state.historyKey) {
          renderHistory();
          loadHistory();
        } else {
          renderHistory();
        }
      } else if (page === "bench") {
        renderBench();
        loadBench();
      }
    });
  }

  window.addEventListener("resize", function () {
    clearTimeout(window._rz);
    window._rz = setTimeout(function () {
      if (state.page === "history") renderHistory();
      if (state.page === "bench") renderBench();
    }, 200);
  });

  window.addEventListener("hashchange", route);
  route();

  // 兜底：如果脚本或后端有问题，别让页面一直停在“正在加载…”
  setTimeout(function () {
    var view = document.getElementById("view");
    if (view && view.querySelector(".loading")) {
      view.innerHTML = '<div class="card"><div class="empty">' +
        "页面加载超时：前端资源可能缺失，或后端接口无响应。<br>" +
        "请刷新页面；若持续出现，请查看 " +
        '<code>应用数据目录/info.log</code>，或卸载后重新安装应用。</div></div>';
    }
  }, 8000);
})();
