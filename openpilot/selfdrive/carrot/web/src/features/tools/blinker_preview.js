const LABELS = {
  ko: {
    shortcut: "점멸 미리보기",
    title: "방향지시등 점멸 미리보기",
    close: "닫기",
    description: "이 화면에서만 점멸합니다.",
    pattern: "점멸 방식",
    simultaneous: "동시 점멸",
    alternating: "좌우 교대",
    play: "화면 미리보기",
    simultaneousTiming: "양쪽 200ms 켜짐 · 100ms 꺼짐",
    alternatingTiming: "왼쪽·오른쪽 400ms씩 교대",
    reduced: "동작 줄이기가 켜져 있어 점멸 주기를 글로 표시합니다.",
  },
  en: {
    shortcut: "Signal preview",
    title: "Turn signal preview",
    close: "Close",
    description: "Screen preview only.",
    pattern: "Flash pattern",
    simultaneous: "Simultaneous",
    alternating: "Left–right alternating",
    play: "Screen preview",
    simultaneousTiming: "Both on for 200 ms, off for 100 ms",
    alternatingTiming: "Left and right alternate every 400 ms",
    reduced: "Reduced motion is on. The timing is shown above.",
  },
  zh: {
    shortcut: "闪烁预览",
    title: "转向灯闪烁预览",
    close: "关闭",
    description: "仅在此页面预览。",
    pattern: "闪烁方式",
    simultaneous: "同时闪烁",
    alternating: "左右交替",
    play: "屏幕预览",
    simultaneousTiming: "两侧同时亮起 200 毫秒，熄灭 100 毫秒",
    alternatingTiming: "左右每 400 毫秒交替一次",
    reduced: "已开启减少动态效果。闪烁时序见上方说明。",
  },
};

const previewButton = document.getElementById("btnToolsBlinkerPreview");
const dialog = document.getElementById("toolsBlinkerPreview");

if (previewButton && dialog) {
  const play = dialog.querySelector('input[type="checkbox"]');
  const timing = dialog.querySelector("[data-blinker-timing]");
  const labels = dialog.querySelectorAll("[data-blinker-label]");

  function update() {
    const words = LABELS[document.documentElement.lang] || LABELS.en;
    const mode = dialog.querySelector('input[name="blinker-preview-mode"]:checked').value;
    previewButton.textContent = words.shortcut;
    for (const label of labels) label.textContent = words[label.dataset.blinkerLabel];
    timing.textContent = words[`${mode}Timing`];
    dialog.dataset.mode = mode;
    dialog.dataset.running = String(dialog.open && play.checked && !document.hidden);
  }

  previewButton.addEventListener("click", () => {
    dialog.showModal();
    update();
  });
  dialog.addEventListener("change", update);
  dialog.addEventListener("close", () => {
    play.checked = false;
    update();
  });
  document.addEventListener("visibilitychange", update);
  window.addEventListener("carrot:languagechange", update);
  update();
}
