// 圆形表盘安全区校验（在手表页面里跑）
//
// 原理：圆屏看不见的像素集合 = 与圆心距离 > 半径的点。
// 对一个圆角矩形元素，它离圆心最远的点一定落在四个「圆角圆心 + 圆角半径」上，
// 所以只要 max(|c_i - C| + r_i) <= R，这个元素就完整可见。
// 非圆角元素（r=0）退化为「四个外角」的距离判断。
//
// 用法：agent-browser eval "$(cat scripts/safe_area_check.js)"
(function () {
  var W = window.innerWidth, H = window.innerHeight;
  var R = Math.min(W, H) / 2, CX = W / 2, CY = H / 2;

  // 注意：Chrome 的 getComputedStyle 对 border-radius 返回的是「声明值」而不是
  // 绘制时钳制后的值 —— `border-radius: 999px` 会原样返回 999px。若直接拿来算，
  // 胶囊按钮会被误判成越界上千像素。这里按 CSS 规范补上钳制（相邻圆角之和超过
  // 边长时按最小比例整体缩小）。
  function radii(el) {
    var s = getComputedStyle(el);
    function p(v) { return parseFloat(v) || 0; }
    var k = [p(s.borderTopLeftRadius), p(s.borderTopRightRadius),
             p(s.borderBottomRightRadius), p(s.borderBottomLeftRadius)];
    var r = el.getBoundingClientRect();
    var w = r.width, h = r.height;
    var f = 1;
    if (w > 0) {
      if (k[0] + k[1] > w) { f = Math.min(f, w / (k[0] + k[1])); }
      if (k[2] + k[3] > w) { f = Math.min(f, w / (k[2] + k[3])); }
    }
    if (h > 0) {
      if (k[0] + k[3] > h) { f = Math.min(f, h / (k[0] + k[3])); }
      if (k[1] + k[2] > h) { f = Math.min(f, h / (k[1] + k[2])); }
    }
    if (f < 1) { for (var i = 0; i < 4; i++) { k[i] *= f; } }
    return k;
  }

  // 完全透明的元素（例如未弹出的 toast）不代表真实可见内容，跳过
  function invisible(el) {
    var node = el;
    while (node && node.nodeType === 1) {
      if (parseFloat(getComputedStyle(node).opacity) === 0) { return true; }
      node = node.parentElement;
    }
    return false;
  }

  // 元素实际可见的范围 = 自身矩形 ∩ 所有祖先裁剪框（滚动容器 / overflow:hidden）
  function visibleRect(el) {
    var r = el.getBoundingClientRect();
    var box = { left: r.left, top: r.top, right: r.right, bottom: r.bottom };
    var node = el.parentElement;
    while (node && node !== document.documentElement) {
      var s = getComputedStyle(node);
      var ox = s.overflowX, oy = s.overflowY;
      if (ox !== "visible" || oy !== "visible") {
        var c = node.getBoundingClientRect();
        box.left = Math.max(box.left, c.left);
        box.top = Math.max(box.top, c.top);
        box.right = Math.min(box.right, c.right);
        box.bottom = Math.min(box.bottom, c.bottom);
      }
      node = node.parentElement;
    }
    return box;
  }

  function measure(el) {
    var rect = el.getBoundingClientRect();
    if (!rect.width || !rect.height) { return null; }
    var vis = visibleRect(el);
    if (vis.right <= vis.left || vis.bottom <= vis.top) { return null; }
    var k = radii(el);
    var pts = [
      [vis.left + k[0], vis.top + k[0], k[0]],
      [vis.right - k[1], vis.top + k[1], k[1]],
      [vis.right - k[2], vis.bottom - k[2], k[2]],
      [vis.left + k[3], vis.bottom - k[3], k[3]]
    ];
    var worst = 0, at = null;
    for (var i = 0; i < pts.length; i++) {
      var d = Math.hypot(pts[i][0] - CX, pts[i][1] - CY) + pts[i][2];
      if (d > worst) { worst = d; at = [Math.round(pts[i][0]), Math.round(pts[i][1])]; }
    }
    var clipped = vis.right - vis.left < rect.width - 0.5 || vis.bottom - vis.top < rect.height - 0.5;
    return {
      rect: [Math.round(rect.left), Math.round(rect.top), Math.round(rect.width), Math.round(rect.height)],
      worst: Math.round(worst * 10) / 10,
      margin: Math.round((R - worst) * 10) / 10,
      at: at,
      clipped: clipped
    };
  }

  function label(el) {
    var cls = (typeof el.className === "string" ? el.className : "").split(" ").filter(Boolean)[0] || el.tagName;
    var text = (el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 12);
    return cls + (el.id ? "#" + el.id : "") + (text ? " " + JSON.stringify(text) : "");
  }

  // 固定在表盘上的「外框」元素：必须完整落在圆内
  // （注意不含 .sheet 全屏遮罩：它是 inset:0 的半透明蒙层，本来就铺满整屏，
  //   被圆形表盘裁掉四角是预期行为，有意义的是它内部的 .sheet-card。）
  var CHROME = ".pill, .fab, .band, .sheet-card, .center, .toast";
  var ANY = ".pill, .band, .fab, .friend, .bubble, .stamp, .time-sep, .sheet-card, " +
            ".sheet-title, .stickers, .sticker, .center, .toast, #composer button, #composer input";

  var result = { viewport: [W, H], radius: R, chrome: [], inner: [], failures: [] };

  document.querySelectorAll(CHROME).forEach(function (el) {
    if (!el.getClientRects().length) { return; }
    if (el.closest(".hidden") || invisible(el)) { return; }
    var m = measure(el);
    if (!m) { return; }
    var row = { el: label(el), rect: m.rect, worst: m.worst, margin: m.margin };
    result.chrome.push(row);
    if (m.worst > R) { result.failures.push({ level: "chrome", el: row.el, over: Math.round((m.worst - R) * 10) / 10 }); }
  });

  document.querySelectorAll(ANY).forEach(function (el) {
    if (!el.getClientRects().length) { return; }
    if (el.closest(".hidden") || invisible(el)) { return; }
    if (el.matches(CHROME)) { return; }
    var m = measure(el);
    if (!m) { return; }
    result.inner.push({ el: label(el), worst: m.worst, margin: m.margin, clipped: m.clipped });
    if (!m.clipped && m.worst > R) {
      result.failures.push({ level: "inner", el: label(el), over: Math.round((m.worst - R) * 10) / 10 });
    }
  });

  return JSON.stringify(result);
})()
