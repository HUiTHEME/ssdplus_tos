/* SSD助手 —— 铁威马 TOS 7 平台适配层
 *
 * 页面由平台 nginx 从 /usr/www/ssdplus 直接发出（地址 /ssdplus/），
 * 接口必须走平台代理 /v2/proxy/ssdplus/（规范 8.9），并且每次请求都要同时
 * 带 Cookie 与 X-Csrf-Token（规范 8.10）。
 *
 * 做法是包一层 window.fetch，业务代码一行都不用改：
 *   · 以 / 开头的相对接口地址自动补上 /v2/proxy/<appid> 前缀
 *   · 自动补 credentials:'include'（TMSESSNAME 是 HttpOnly，JS 读不到）
 *   · 自动补 X-Csrf-Token 头
 *
 * 只有在平台环境（地址形如 /ssdplus/…）下才启用；本地直开端口预览时
 * 第一段路径为空，保持原来的相对地址，方便调试。
 */
(function () {
  "use strict";

  var seg = (location.pathname.split("/").filter(Boolean)[0] || "");
  if (!seg) return;                 // 本地预览 / 直连模式，不拦截接口

  var BASE = "/v2/proxy/" + seg;

  function readCookie(name) {
    var prefix = name + "=";
    return document.cookie.split(";").map(function (s) { return s.trim(); })
      .filter(function (s) { return s.indexOf(prefix) === 0; })
      .map(function (s) { return s.slice(prefix.length); })[0] || "";
  }

  /* 平台会话失效时网关不是回 401，而是回 200 的
   * {"is_login":false,"msg":"please login"}。这时刷新当前页面让平台
   * 自己走登录流程（它会 302 到登录页）。用 sessionStorage 记一次，
   * 万一刷新后仍然没登录态，就不再刷新，改成提示，避免死循环。 */
  var REDIRECT_KEY = "ssdplus.loginRedirected";
  var redirected = false;

  function showLoginNotice() {
    if (document.getElementById("ssdplus-login-notice")) return;
    var box = document.createElement("div");
    box.id = "ssdplus-login-notice";
    box.setAttribute("style", [
      "position:fixed", "inset:0", "z-index:2147483647",
      "display:flex", "align-items:center", "justify-content:center",
      "background:#f5f7fa", "font-family:-apple-system,'PingFang SC',sans-serif"
    ].join(";"));
    box.innerHTML =
      '<div style="background:#fff;border:1px solid #e5e8ec;border-radius:8px;' +
      'padding:32px 40px;text-align:center;max-width:420px">' +
      '<div style="font-size:15px;font-weight:600;color:#1f2329;margin-bottom:10px">' +
      '平台登录已失效</div>' +
      '<div style="font-size:13px;color:#6b7684;line-height:1.8">' +
      '请回到 TOS 桌面重新登录后，再点一次 SSD助手 图标。</div></div>';
    (document.body || document.documentElement).appendChild(box);
  }

  function leaveToPlatformLogin() {
    if (redirected) return;
    redirected = true;
    try {
      if (window.sessionStorage.getItem(REDIRECT_KEY)) {
        showLoginNotice();
        return;
      }
      window.sessionStorage.setItem(REDIRECT_KEY, "1");
    } catch (e) { /* 隐私模式下 sessionStorage 不可用，直接刷新一次 */ }
    location.reload();
  }

  var nativeFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    var url = input;
    init = init || {};
    init.credentials = "include";
    init.headers = Object.assign({}, init.headers || {});
    var csrf = readCookie("X-Csrf-Token");
    if (csrf) init.headers["X-Csrf-Token"] = csrf;

    if (typeof url === "string") {
      var abs = /^(?:[a-z]+:)?\/\//i.test(url);   // http://、https://、//host
      if (!abs) {
        // 业务代码里的接口地址都是相对路径（api/xxx 或 /api/xxx）：
        // 页面本身在 /ssdplus/，直接发出去会打到静态站点上，必须转到平台代理。
        var rest = url.charAt(0) === "/" ? url : "/" + url;
        if (rest.indexOf("/v2/proxy/") !== 0) url = BASE + rest;
      }
    }

    return nativeFetch(url, init).then(function (res) {
      var type = res.headers.get("Content-Type") || "";
      if (type.indexOf("application/json") < 0) return res;
      // 克隆一份看是不是平台登录失效，原响应仍然交给业务代码
      return res.clone().json().then(function (body) {
        if (body && body.is_login === false) {
          leaveToPlatformLogin();
        } else if (body) {
          // 拿到本应用的真实应答，说明平台会话是好的，清掉上次的重定向标记
          try { window.sessionStorage.removeItem(REDIRECT_KEY); } catch (e) { }
        }
        return res;
      }).catch(function () { return res; });
    });
  };
})();
