// 前端少量交互辅助。Alpine.js 负责大部分页面内交互，此处仅放置可复用的工具函数：
// 1) 复制到剪贴板；2) 全局 Toast 助手；3) 防重复提交；4) 全局确认弹窗 Alpine store。

// 复制文本到剪贴板（供 API Key 等一次性展示场景使用）。
// 优先使用 Clipboard API（需安全上下文 HTTPS/localhost），
// 不可用时回退到临时 textarea + execCommand('copy')。
// 成功返回 true，失败返回 false（并已弹出失败提示，调用方无需重复提示）。
window.copyToClipboard = async function (text, btn) {
  let ok = false;
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      ok = true;
    } else {
      // 回退方案：临时 textarea + execCommand
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.top = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      ok = document.execCommand("copy");
      document.body.removeChild(ta);
    }
    if (ok && btn) {
      const original = btn.textContent;
      btn.textContent = "已复制";
      setTimeout(() => {
        btn.textContent = original;
      }, 1500);
    }
  } catch (err) {
    ok = false;
  }
  if (!ok) {
    window.toast("复制失败，请手动选择文本复制", "error");
  }
  return ok;
};

// 客户端 Toast：直接渲染到 base.html 的 #client-toast-host。
// category：info / success / warning / error。
window.toast = function (message, category) {
  const host = document.getElementById("client-toast-host");
  if (!host) return;

  const styles = {
    success: "bg-green-50 text-green-800 border-green-200",
    error: "bg-red-50 text-red-800 border-red-200",
    warning: "bg-amber-50 text-amber-800 border-amber-200",
    info: "bg-sky-50 text-sky-800 border-sky-200",
  };
  const cat = category || "info";
  const card = document.createElement("div");
  card.className =
    "pointer-events-auto mb-2 flex items-start justify-between gap-3 rounded-lg border px-4 py-3 text-sm shadow-lg transition-opacity " +
    (styles[cat] || styles.info);

  const text = document.createElement("span");
  text.textContent = message;
  const close = document.createElement("button");
  close.type = "button";
  close.className = "shrink-0 opacity-60 hover:opacity-100";
  close.textContent = "✕";
  const remove = function () {
    card.style.opacity = "0";
    setTimeout(() => card.remove(), 200);
  };
  close.addEventListener("click", remove);
  card.appendChild(text);
  card.appendChild(close);
  host.appendChild(card);
  if (cat === "success" || cat === "info") {
    setTimeout(remove, 6000);
  }
};

// POST 表单防重复提交：提交后禁用 submit 按钮并显示处理中状态。
window.guardFormSubmit = function (form) {
  if (!form || form.dataset.submitting === "true") {
    return false;
  }
  form.dataset.submitting = "true";
  form.setAttribute("aria-busy", "true");

  const controls = form.querySelectorAll('button[type="submit"], input[type="submit"]');
  controls.forEach((control) => {
    control.disabled = true;
    control.setAttribute("aria-disabled", "true");
    const loadingText = control.dataset.loadingText || "处理中...";
    if (control.tagName === "INPUT") {
      control.dataset.originalValue = control.value;
      control.value = loadingText;
    } else {
      control.dataset.originalText = control.textContent;
      control.textContent = loadingText;
    }
  });
  return true;
};

document.addEventListener("submit", (event) => {
  if (event.defaultPrevented) return;
  const form = event.target;
  if (!(form instanceof HTMLFormElement)) return;
  if (form.method.toLowerCase() !== "post") return;
  if (!window.guardFormSubmit(form)) {
    event.preventDefault();
  }
});

// 注册 Alpine 全局 store：统一的确认弹窗（替代浏览器原生 confirm）。
// 用法：在表单上 `x-data @submit.prevent="$store.confirm.ask($el, '提示语')"`，
// 用户点「确认」后以原生 form.submit() 提交（不再触发 @submit，避免二次拦截）。
document.addEventListener("alpine:init", () => {
  Alpine.store("confirm", {
    open: false,
    message: "",
    _form: null,
    // 弹出确认框，缓存待提交的表单
    ask(form, message) {
      this._form = form;
      this.message = message || "确定执行该操作？";
      this.open = true;
    },
    // 确认：提交缓存表单并关闭
    yes() {
      const form = this._form;
      this.open = false;
      this._form = null;
      if (form && window.guardFormSubmit(form)) form.submit();
    },
    // 取消：仅关闭，不做任何操作
    no() {
      this.open = false;
      this._form = null;
    },
  });
});
