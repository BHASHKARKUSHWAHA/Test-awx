/* AWX dashboard integration for the AI Agent.
 *
 * This file is served by the AI proxy and injected into AWX's index.html with
 * nginx sub_filter. Nothing inside the AWX container or image is modified, so
 * an AWX upgrade cannot be broken by this integration - at worst the nav item
 * stops finding its anchor and the floating launcher is used instead.
 *
 * It adds:
 *   - a "AI Agent" item in the AWX left navigation (when the nav is found);
 *   - a floating launcher button that always works;
 *   - a slide-over panel that hosts the agent UI in an iframe, so the AWX SPA
 *     keeps its own router and state untouched.
 */
(function () {
  "use strict";

  if (window.__awxAiAgentInjected) return;
  window.__awxAiAgentInjected = true;

  var PANEL_ID = "awx-ai-agent-panel";
  var BUTTON_ID = "awx-ai-agent-launcher";
  var NAV_ID = "awx-ai-agent-nav";
  var AGENT_URL = "/ai-agent/";
  var LABEL = "AI Agent";
  var ROBOT = "🤖";

  // ----------------------------------------------------------------- styles
  var style = document.createElement("style");
  style.textContent = [
    "#" + PANEL_ID + "{position:fixed;top:0;right:0;height:100vh;width:min(560px,100vw);",
    "  z-index:2147483000;box-shadow:-8px 0 28px rgba(0,0,0,.45);transform:translateX(100%);",
    "  transition:transform .22s ease;background:#0f1419;display:flex;flex-direction:column}",
    "#" + PANEL_ID + ".open{transform:translateX(0)}",
    "#" + PANEL_ID + " header{display:flex;align-items:center;justify-content:space-between;",
    "  padding:8px 12px;background:#161c24;color:#e6edf3;font:600 14px system-ui,sans-serif;border-bottom:1px solid #2a3441}",
    "#" + PANEL_ID + " header button{background:none;border:none;color:#8b98a9;font-size:18px;cursor:pointer;padding:2px 8px}",
    "#" + PANEL_ID + " header button:hover{color:#e6edf3}",
    "#" + PANEL_ID + " iframe{flex:1;border:0;width:100%;background:#0f1419}",
    "#" + BUTTON_ID + "{position:fixed;right:18px;bottom:18px;z-index:2147482999;border:none;cursor:pointer;",
    "  border-radius:999px;padding:11px 17px;background:#2f81c7;color:#fff;font:600 14px system-ui,sans-serif;",
    "  box-shadow:0 4px 14px rgba(0,0,0,.4)}",
    "#" + BUTTON_ID + ":hover{background:#4aa3df}",
    "#" + NAV_ID + " a{display:flex;align-items:center;gap:8px;cursor:pointer}"
  ].join("");
  document.head.appendChild(style);

  // ------------------------------------------------------------------ panel
  function buildPanel() {
    var panel = document.createElement("div");
    panel.id = PANEL_ID;

    var header = document.createElement("header");
    var title = document.createElement("span");
    title.textContent = ROBOT + " AWX AI Agent";
    var actions = document.createElement("span");

    var expand = document.createElement("button");
    expand.title = "Open in a full tab";
    expand.textContent = "↗";
    expand.addEventListener("click", function () { window.open(AGENT_URL, "_blank", "noopener"); });

    var close = document.createElement("button");
    close.title = "Close";
    close.textContent = "×";
    close.addEventListener("click", function () { panel.classList.remove("open"); });

    actions.appendChild(expand);
    actions.appendChild(close);
    header.appendChild(title);
    header.appendChild(actions);

    var frame = document.createElement("iframe");
    frame.title = "AWX AI Agent";
    frame.setAttribute("loading", "lazy");

    panel.appendChild(header);
    panel.appendChild(frame);
    document.body.appendChild(panel);
    return { panel: panel, frame: frame };
  }

  var parts = null;
  function openAgent() {
    if (!parts) parts = buildPanel();
    if (!parts.frame.getAttribute("src")) parts.frame.setAttribute("src", AGENT_URL);
    parts.panel.classList.add("open");
  }

  // --------------------------------------------------------------- launcher
  var launcher = document.createElement("button");
  launcher.id = BUTTON_ID;
  launcher.type = "button";
  launcher.textContent = ROBOT + " " + LABEL;
  launcher.addEventListener("click", openAgent);
  document.body.appendChild(launcher);

  // ------------------------------------------------------- navigation entry
  // AWX renders its navigation client side, and the markup differs between
  // releases, so the anchor is looked up defensively and re-checked whenever
  // the SPA re-renders. If no nav is found the launcher above still works.
  function navList() {
    var selectors = [
      "nav .pf-c-nav__list",
      "nav .pf-v5-c-nav__list",
      "nav ul[role='list']",
      "#page-sidebar ul",
      "nav ul"
    ];
    for (var i = 0; i < selectors.length; i++) {
      var node = document.querySelector(selectors[i]);
      if (node) return node;
    }
    return null;
  }

  function addNavItem() {
    if (document.getElementById(NAV_ID)) return true;
    var list = navList();
    if (!list) return false;

    var sample = list.querySelector("li");
    var item = document.createElement("li");
    item.id = NAV_ID;
    if (sample && sample.className) item.className = sample.className;

    var link = document.createElement("a");
    var sampleLink = sample ? sample.querySelector("a") : null;
    if (sampleLink && sampleLink.className) link.className = sampleLink.className;
    link.setAttribute("role", "button");
    link.setAttribute("tabindex", "0");
    link.textContent = ROBOT + "  " + LABEL;
    link.addEventListener("click", function (event) {
      event.preventDefault();
      event.stopPropagation();
      openAgent();
    });

    item.appendChild(link);
    list.appendChild(item);
    return true;
  }

  if (!addNavItem()) {
    var observer = new MutationObserver(function () { addNavItem(); });
    observer.observe(document.body, { childList: true, subtree: true });
    // The AWX SPA can re-render the nav on route changes; keep a slow retry
    // for a minute, then rely on the observer alone.
    var retries = 0;
    var timer = setInterval(function () {
      retries += 1;
      if (addNavItem() || retries > 30) clearInterval(timer);
    }, 2000);
  }

  // Keyboard shortcut: Alt+A opens the agent from anywhere in AWX.
  document.addEventListener("keydown", function (event) {
    if (event.altKey && (event.key === "a" || event.key === "A")) {
      event.preventDefault();
      openAgent();
    }
  });
})();
