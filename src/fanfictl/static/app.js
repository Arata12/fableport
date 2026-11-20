// Reading progress bar + progressive-enhancement controls.
(function () {
  var bar = document.getElementById("reader-progress");
  var body = document.querySelector(".reader-body");
  if (bar && body) {
    function update() {
      var rect = body.getBoundingClientRect();
      var total = rect.height - window.innerHeight * 0.5;
      var done = Math.min(Math.max(-rect.top + window.innerHeight * 0.3, 0), total);
      bar.style.width = (total > 0 ? (done / total) * 100 : 0) + "%";
    }
    window.addEventListener("scroll", update, { passive: true });
    window.addEventListener("resize", update);
    update();
  }

  // Unified Translation provider switcher (Settings).
  var radios = document.querySelectorAll('input[name="provider_backend"]');
  var panels = document.querySelectorAll("[data-provider-panel]");
  function syncProviderPanels() {
    var active = null;
    radios.forEach(function (r) {
      if (r.checked) active = r.value;
    });
    if (!active) return;
    panels.forEach(function (panel) {
      var show = panel.getAttribute("data-provider-panel") === active;
      panel.hidden = !show;
      panel.querySelectorAll("input, select, textarea").forEach(function (field) {
        if (!show) {
          if (field.hasAttribute("required")) {
            field.setAttribute("data-was-required", "1");
            field.removeAttribute("required");
          }
        } else if (field.getAttribute("data-was-required") === "1") {
          field.setAttribute("required", "");
        }
      });
    });
  }
  if (radios.length && panels.length) {
    radios.forEach(function (r) {
      r.addEventListener("change", syncProviderPanels);
    });
    syncProviderPanels();
  }

  // One-click local presets in Settings -> Local / Remote server.
  document.querySelectorAll("[data-engine-preset]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var panel = btn.closest("[data-provider-panel]") || btn.closest("form");
      if (!panel) return;
      var mode = btn.getAttribute("data-api-mode");
      var host = btn.getAttribute("data-host");
      var model = btn.getAttribute("data-model");
      var modeEl = panel.querySelector('[name="api_mode"]');
      var hostEl = panel.querySelector('[name="ollama_host"]');
      var modelEl = panel.querySelector('[name="ollama_model"]');
      if (modeEl && mode) modeEl.value = mode;
      if (hostEl && host) hostEl.value = host;
      if (modelEl && model) modelEl.value = model;
    });
  });

  // Auto-suggest matching default model when toggling Colab engine (vLLM <-> Ollama).
  var colabEngine = document.querySelector('select[name="colab_engine"]');
  var colabModel = document.querySelector('input[name="colab_model"]');
  if (colabEngine && colabModel) {
    var defaults = {
      vllm: "Qwen/Qwen2.5-14B-Instruct-AWQ",
      ollama: "gemma4:26b",
    };
    colabEngine.addEventListener("change", function () {
      var val = colabModel.value.trim();
      var target = defaults[colabEngine.value];
      if (!target) return;
      if (!val || val === defaults.vllm || val === defaults.ollama) {
        colabModel.value = target;
      }
      colabModel.placeholder = target;
    });
  }

  // Auto-grow the import URL composer cleanly from 1 line up to multi-line batches.
  document.querySelectorAll("textarea[data-autogrow]").forEach(function (el) {
    function resize() {
      el.style.height = "auto";
      el.style.height = Math.min(el.scrollHeight, 220) + "px";
    }
    el.addEventListener("input", resize);
    resize();
  });

  // Confirmation prompt for destructive actions (e.g. Delete work).
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      var msg = form.getAttribute("data-confirm");
      if (msg && !window.confirm(msg)) {
        event.preventDefault();
      }
    });
  });
})();
