// 前端少量交互辅助。Alpine.js 负责大部分页面内交互，此处仅放置可复用的工具函数：
// 1) 复制到剪贴板；2) 全局 Toast 助手；3) 全局确认弹窗 Alpine store。

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

// 客户端 Toast：派发 window 事件，由 base.html 的 toastHost 组件接收并渲染。
// category：info / success / warning / error。
window.toast = function (message, category) {
  window.dispatchEvent(
    new CustomEvent("toast", {
      detail: { message: message, category: category || "info" },
    })
  );
};

// 注册 Alpine 全局 store：统一的确认弹窗（替代浏览器原生 confirm）。
// 用法：在表单上 `x-data @submit.prevent="$store.confirm.ask($el, '提示语')"`，
// 用户点「确认」后以原生 form.submit() 提交（不再触发 @submit，避免二次拦截）。
document.addEventListener("alpine:init", () => {
  // Toast 宿主组件：监听 window 的 "toast" 事件，渲染并自动消失。
  Alpine.data("toastHost", () => ({
    items: [],
    styles: {
      success: "bg-green-50 text-green-800 border-green-200",
      error: "bg-red-50 text-red-800 border-red-200",
      warning: "bg-amber-50 text-amber-800 border-amber-200",
      info: "bg-sky-50 text-sky-800 border-sky-200",
    },
    init() {
      window.addEventListener("toast", (e) => this.add(e.detail));
    },
    add(detail) {
      const id = Date.now() + Math.random();
      this.items.push({
        id: id,
        message: detail.message,
        category: detail.category || "info",
        show: true,
      });
      setTimeout(() => this.remove(id), 6000);
    },
    remove(id) {
      const it = this.items.find((x) => x.id === id);
      if (it) it.show = false;
      // 等过渡结束后移除，避免突兀
      setTimeout(() => {
        this.items = this.items.filter((x) => x.id !== id);
      }, 200);
    },
  }));

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
      if (form) form.submit();
    },
    // 取消：仅关闭，不做任何操作
    no() {
      this.open = false;
      this._form = null;
    },
  });
});
