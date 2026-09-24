// 诊断：打印圆屏几何相关的实际测量值（在手表页面里跑）
// 用法：agent-browser eval "$(cat scripts/round_probe.js)"
(function () {
  function rect(el) {
    if (!el) { return null; }
    var r = el.getBoundingClientRect();
    return [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)];
  }
  var device = document.getElementById("device");
  var app = document.getElementById("app");
  var pill = document.querySelector("#chatView .pill") || document.querySelector(".pill");
  var band = document.querySelector(".band:not(.hidden), #messageList");
  var fab = document.getElementById("composeBtn");
  var cs = getComputedStyle(device);
  return JSON.stringify({
    viewport: [innerWidth, innerHeight],
    deviceClient: [device.clientWidth, device.clientHeight],
    deviceRect: rect(device),
    appRect: rect(app),
    rOnDevice: cs.getPropertyValue("--r").trim(),
    rOnApp: getComputedStyle(app).getPropertyValue("--r").trim(),
    colToken: getComputedStyle(band).getPropertyValue("--col").trim(),
    bandRect: rect(band),
    bandWidthComputed: getComputedStyle(band).width,
    pillRect: rect(pill),
    fabRect: rect(fab),
    pillTopComputed: pill ? getComputedStyle(pill).top : null
  });
})()
