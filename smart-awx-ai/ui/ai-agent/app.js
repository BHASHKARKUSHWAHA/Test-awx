/* AWX AI Agent - chat client.
 *
 * Talks only to this server's own API (same origin). Streaming uses fetch +
 * ReadableStream so progress, tool results and approval prompts arrive while
 * the agent is still working.
 */
(function () {
  "use strict";

  var API = (function () {
    // When embedded through the AWX proxy the UI lives at /ai-agent/ and the
    // API at /api/ai/ on the same origin; standalone it is served from the
    // backend directly. Both resolve to the same relative base.
    return "/api/ai";
  })();

  var state = {
    conversationId: null,
    identity: null,
    pendingApproval: null,
    sending: false,
    lastTrace: []
  };

  // ---------------------------------------------------------------- utils
  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function show(node) { node.classList.remove("hidden"); }
  function hide(node) { node.classList.add("hidden"); }

  function api(path, options) {
    options = options || {};
    options.credentials = "same-origin";
    options.headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
    return fetch(API + path, options).then(function (response) {
      if (response.status === 401) { showLogin(); throw new Error("not authenticated"); }
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (!response.ok) throw new Error(body.detail || ("HTTP " + response.status));
        return body;
      });
    });
  }

  // ---------------------------------------------------------------- login
  function showLogin() { hide($("app")); show($("login")); }
  function showApp() { hide($("login")); show($("app")); }

  $("login-form").addEventListener("submit", function (event) {
    event.preventDefault();
    hide($("login-error"));
    api("/login", {
      method: "POST",
      body: JSON.stringify({ username: $("username").value, password: $("password").value })
    }).then(function (body) {
      state.identity = body.identity;
      start();
    }).catch(function (error) {
      $("login-error").textContent = error.message === "not authenticated" ? "Invalid username or password." : error.message;
      show($("login-error"));
    });
  });

  $("logout").addEventListener("click", function () {
    api("/logout", { method: "POST" }).then(showLogin).catch(showLogin);
  });

  // -------------------------------------------------------------- messages
  function addMessage(role, text, cls) {
    var wrap = el("div", "msg " + (cls || (role === "You" ? "user" : "agent")));
    wrap.appendChild(el("div", "who", role));
    wrap.appendChild(el("div", "body", text));
    $("messages").appendChild(wrap);
    $("messages").scrollTop = $("messages").scrollHeight;
    return wrap;
  }

  function addProgressBlock() {
    var wrap = el("div", "msg agent");
    wrap.appendChild(el("div", "who", "AI Agent"));
    var body = el("div", "body");
    var statusLine = el("div", "", "Working…");
    var progress = el("div", "progress");
    body.appendChild(statusLine);
    body.appendChild(progress);
    wrap.appendChild(body);
    $("messages").appendChild(wrap);
    $("messages").scrollTop = $("messages").scrollHeight;
    return { wrap: wrap, body: body, status: statusLine, progress: progress };
  }

  function progressLine(container, text, cls, level) {
    var line = el("div", "line " + (cls || "running"), text);
    if (level) {
      var badge = el("span", "badge " + level, level.replace("_", " "));
      line.appendChild(badge);
    }
    container.appendChild(line);
    $("messages").scrollTop = $("messages").scrollHeight;
    return line;
  }

  // --------------------------------------------------------------- sending
  function send(message) {
    if (state.sending || !message.trim()) return;
    state.sending = true;
    $("send").disabled = true;
    addMessage("You", message, "user");
    $("input").value = "";

    var block = addProgressBlock();
    var runningLine = null;
    state.lastTrace = [];

    fetch(API + "/chat/stream", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: message, conversation_id: state.conversationId })
    }).then(function (response) {
      if (response.status === 401) { showLogin(); throw new Error("not authenticated"); }
      if (!response.body) throw new Error("streaming is not supported by this browser");
      var reader = response.body.getReader();
      var decoder = new TextDecoder();
      var buffer = "";

      function pump() {
        return reader.read().then(function (chunk) {
          if (chunk.done) { finish(); return; }
          buffer += decoder.decode(chunk.value, { stream: true });
          var parts = buffer.split("\n\n");
          buffer = parts.pop();
          parts.forEach(function (part) {
            var line = part.replace(/^data: /, "").trim();
            if (!line || line === "[DONE]") return;
            var event;
            try { event = JSON.parse(line); } catch (error) { return; }
            handleEvent(event);
          });
          return pump();
        });
      }

      function handleEvent(event) {
        if (event.type === "status") {
          block.status.textContent = event.message || "Working…";
          if (event.conversation_id) state.conversationId = event.conversation_id;
        } else if (event.type === "plan") {
          block.status.textContent = "Plan: " + event.plan.goal;
          block.progress.innerHTML = "";
          (event.plan.steps || []).forEach(function (step) {
            var line = progressLine(block.progress, step.specialist_icon + " " + step.description, "running");
            line.dataset.index = step.index;
          });
        } else if (event.type === "step") {
          runningLine = block.progress.querySelector('[data-index="' + event.step.index + '"]');
        } else if (event.type === "tool") {
          state.lastTrace.push(event.tool);
          var line = event.step_index
            ? block.progress.querySelector('[data-index="' + event.step_index + '"]')
            : progressLine(block.progress, event.tool.tool, "running");
          if (line) {
            line.className = "line " + (event.tool.ok ? "done" : "failed");
            line.textContent = event.tool.summary || event.tool.tool;
            line.appendChild(el("span", "badge " + event.tool.level, event.tool.level.replace("_", " ")));
          }
          renderTrace();
        } else if (event.type === "approval") {
          state.pendingApproval = event.approval;
        } else if (event.type === "final") {
          block.status.textContent = "";
          var result = event.result;
          var answer = el("div", "", result.answer);
          block.body.insertBefore(answer, block.progress);
          if (result.conversation_id) state.conversationId = result.conversation_id;
          if (result.degraded) {
            block.body.appendChild(el("div", "muted small", "Answer rendered from raw tool output (local model unavailable)."));
          }
          (result.notes || []).forEach(function (note) {
            block.body.appendChild(el("div", "muted small", note));
          });
          if (result.pending_approval) {
            state.pendingApproval = result.pending_approval;
            openApproval(result.pending_approval);
          }
          state.lastTrace = result.tool_calls || state.lastTrace;
          renderTrace();
          loadConversations();
        } else if (event.type === "error") {
          block.status.textContent = "";
          block.wrap.className = "msg error";
          block.body.appendChild(el("div", "error", event.message));
        }
      }

      function finish() {
        state.sending = false;
        $("send").disabled = false;
        if (block.status.textContent === "Working…") block.status.textContent = "";
      }

      return pump();
    }).catch(function (error) {
      state.sending = false;
      $("send").disabled = false;
      block.status.textContent = "";
      block.wrap.className = "msg error";
      block.body.appendChild(el("div", "error", error.message));
    });
  }

  $("send").addEventListener("click", function () { send($("input").value); });
  $("input").addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); send($("input").value); }
  });

  // -------------------------------------------------------------- approval
  function openApproval(approval) {
    $("approval-action").textContent = approval.step_description || approval.tool;
    $("approval-tool").textContent = approval.tool;
    $("approval-args").textContent = JSON.stringify(approval.args || {});
    $("approval-risk").textContent = approval.risk + (approval.reason ? " - " + approval.reason : "");
    $("approval-note").textContent = approval.risk_note || "";
    hide($("approval-error"));
    $("approval-confirm").value = "";
    if (approval.confirmation_word) {
      $("approval-word").textContent = approval.confirmation_word;
      show($("approval-confirm-row"));
    } else {
      hide($("approval-confirm-row"));
    }
    show($("approval"));
  }

  $("approval-cancel").addEventListener("click", function () {
    hide($("approval"));
    state.pendingApproval = null;
    addMessage("AI Agent", "Cancelled. Nothing was changed.", "agent");
  });

  $("approval-ok").addEventListener("click", function () {
    if (!state.pendingApproval) return hide($("approval"));
    var payload = {
      token: state.pendingApproval.token,
      confirmation: $("approval-confirm").value || null,
      conversation_id: state.conversationId
    };
    $("approval-ok").disabled = true;
    api("/approve", { method: "POST", body: JSON.stringify(payload) }).then(function (result) {
      $("approval-ok").disabled = false;
      hide($("approval"));
      state.pendingApproval = null;
      addMessage("AI Agent", result.answer, result.intent === "approval_rejected" ? "error" : "agent");
      state.lastTrace = result.tool_calls || [];
      renderTrace();
    }).catch(function (error) {
      $("approval-ok").disabled = false;
      $("approval-error").textContent = error.message;
      show($("approval-error"));
    });
  });

  // ----------------------------------------------------------------- trace
  function renderTrace() {
    var body = $("trace-body");
    body.innerHTML = "";
    if (!state.lastTrace.length) {
      body.appendChild(el("p", "muted small", "No tools have run yet."));
      return;
    }
    state.lastTrace.forEach(function (call) {
      var details = document.createElement("details");
      var summary = document.createElement("summary");
      summary.textContent = (call.ok ? "✓ " : "✗ ") + call.tool + " (" + call.duration_ms + " ms)";
      summary.appendChild(el("span", "badge " + call.level, call.level.replace("_", " ")));
      details.appendChild(summary);
      var text = "args: " + JSON.stringify(call.args || {}) + "\n";
      if (call.error) text += "error: " + call.error + "\n";
      (call.evidence || []).forEach(function (row) {
        text += "\n$ " + row.command + "  (exit " + row.exit_code + ")\n" + (row.stdout || "").slice(0, 1200) + "\n";
      });
      var pre = el("pre", "", text);
      details.appendChild(pre);
      body.appendChild(details);
    });
  }

  $("toggle-trace").addEventListener("click", function () {
    $("trace").classList.toggle("hidden");
    $("app").classList.toggle("with-trace");
    renderTrace();
  });
  $("close-trace").addEventListener("click", function () {
    hide($("trace"));
    $("app").classList.remove("with-trace");
  });

  // --------------------------------------------------------- conversations
  function loadConversations() {
    api("/conversations").then(function (body) {
      var list = $("conversation-list");
      list.innerHTML = "";
      (body.conversations || []).forEach(function (conversation) {
        var item = el("li", conversation.id === state.conversationId ? "active" : "", conversation.preview || conversation.id);
        item.addEventListener("click", function () { openConversation(conversation.id); });
        list.appendChild(item);
      });
    }).catch(function () {});
  }

  function openConversation(id) {
    api("/conversations/" + id).then(function (conversation) {
      state.conversationId = id;
      $("messages").innerHTML = "";
      (conversation.messages || []).forEach(function (message) {
        addMessage(message.role === "user" ? "You" : "AI Agent", message.content, message.role === "user" ? "user" : "agent");
      });
      loadConversations();
    }).catch(function () {});
  }

  $("new-chat").addEventListener("click", function () {
    state.conversationId = null;
    $("messages").innerHTML = "";
    greet();
    loadConversations();
  });

  // ---------------------------------------------------------------- agents
  function loadAgents() {
    api("/agents").then(function (body) {
      var list = $("agent-list");
      list.innerHTML = "";
      (body.agents || []).forEach(function (agent) {
        list.appendChild(el("li", "", agent.name + " - " + agent.tool_count + " tools"));
      });
    }).catch(function () {});
  }

  var CATEGORIES = ["linux", "kubernetes", "awx", "monitoring", "networking"];
  $("create-agent").addEventListener("click", function () {
    var box = $("agent-categories");
    box.innerHTML = "";
    CATEGORIES.forEach(function (category) {
      var label = el("label");
      var input = document.createElement("input");
      input.type = "checkbox";
      input.value = category;
      label.appendChild(input);
      label.appendChild(document.createTextNode(category));
      box.appendChild(label);
    });
    hide($("agent-error"));
    show($("agent-modal"));
  });
  $("agent-cancel").addEventListener("click", function () { hide($("agent-modal")); });
  $("agent-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var categories = Array.prototype.slice
      .call($("agent-categories").querySelectorAll("input:checked"))
      .map(function (input) { return input.value; });
    api("/agents", {
      method: "POST",
      body: JSON.stringify({
        name: $("agent-name").value,
        purpose: $("agent-purpose").value,
        categories: categories,
        max_level: $("agent-level").value
      })
    }).then(function (body) {
      hide($("agent-modal"));
      loadAgents();
      addMessage("AI Agent", "Created agent \"" + body.agent.name + "\" with " + body.agent.tool_count + " tools (max level " + body.agent.max_level + ").", "agent");
    }).catch(function (error) {
      $("agent-error").textContent = error.message;
      show($("agent-error"));
    });
  });

  // ---------------------------------------------------------------- health
  function loadHealth() {
    fetch("/health", { credentials: "same-origin" })
      .then(function (response) { return response.json(); })
      .then(function (body) {
        var box = $("stack-health");
        box.innerHTML = "";
        Object.keys(body.components || {}).forEach(function (name) {
          var component = body.components[name];
          var row = el("div");
          row.appendChild(el("span", "dot " + (component.status === "healthy" ? "ok" : "bad")));
          row.appendChild(document.createTextNode(name.replace(/_/g, " ")));
          row.title = component.detail || "";
          box.appendChild(row);
        });
        var model = (body.components.ai_model || {});
        $("model-line").textContent = "Local model: " + (model.active_model || "unavailable") +
          "  |  tools grounded on live server data";
      })
      .catch(function () {});
  }

  // ----------------------------------------------------------------- setup
  var EXAMPLES = [
    "Server ka complete health report do",
    "What is my RAM?",
    "Show Kubernetes pods",
    "Show AWX status",
    "AWX me failed jobs dikhao",
    "Why is this pod failing?",
    "Server ko optimize karo"
  ];

  function greet() {
    addMessage(
      "AI Agent",
      "Ready. I answer only from live data on this machine - every number you see comes from a command or an API call you can inspect in the execution trace.\n" +
      "Write actions are never silent: you get the action, its risk level and an approval prompt first.",
      "agent"
    );
  }

  function start() {
    showApp();
    $("who").textContent = state.identity ? state.identity.username + " (" + state.identity.source.replace("_", " ") + ")" : "";
    $("messages").innerHTML = "";
    greet();
    loadConversations();
    loadAgents();
    loadHealth();
    setInterval(loadHealth, 30000);
  }

  var examplesBox = $("examples");
  EXAMPLES.forEach(function (text) {
    var button = el("button", "", text);
    button.addEventListener("click", function () { send(text); });
    examplesBox.appendChild(button);
  });

  api("/session").then(function (body) {
    state.identity = body.identity;
    start();
  }).catch(function () { showLogin(); });
})();
