// 前端少量交互辅助。Alpine.js 负责大部分页面内交互，此处仅放置可复用的工具函数。

// 复制文本到剪贴板（供 API Key 等一次性展示场景使用）。
window.copyToClipboard = async function (text, btn) {
  try {
    await navigator.clipboard.writeText(text);
    if (btn) {
      const original = btn.textContent;
      btn.textContent = "已复制";
      setTimeout(() => {
        btn.textContent = original;
      }, 1500);
    }
  } catch (err) {
    window.alert("复制失败，请手动选择文本复制");
  }
};
