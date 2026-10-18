const buildAgentConfig = window.buildAgentConfig || {};

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function formatPrice(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `¥ ${number.toFixed(2)}` : "-";
}

function buildBubbleShell(role, bodyHtml) {
  return `
    <span class="chat-role">${role === "user" ? "我" : "装机代理"}</span>
    ${bodyHtml}
  `;
}

function appendAgentMessage(role, text) {
  const container = document.getElementById("agentConversation");
  if (!container) {
    return;
  }
  const article = document.createElement("article");
  article.className = `chat-bubble ${role === "user" ? "chat-bubble-user" : "chat-bubble-assistant"}`;
  article.innerHTML = buildBubbleShell(role, `<p>${escapeHtml(text)}</p>`);
  container.appendChild(article);
  container.scrollTop = container.scrollHeight;
}

function renderSuggestions(suggestions) {
  const container = document.getElementById("agentSuggestionList");
  if (!container) {
    return;
  }
  container.innerHTML = (suggestions || [])
    .map((prompt) => `<button type="button" class="suggestion-chip" data-prompt="${escapeHtml(prompt)}">${escapeHtml(prompt)}</button>`)
    .join("");
  container.querySelectorAll(".suggestion-chip").forEach((button) => {
    button.addEventListener("click", () => {
      const input = document.getElementById("agentMessageInput");
      if (input) {
        input.value = button.dataset.prompt || "";
        input.focus();
      }
    });
  });
}

async function sendAgentMessage(message) {
  const formContext = collectStructuredFormContext();
  const response = await fetch(buildAgentConfig.chatUrl, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, form_context: formContext }),
  });
  const payload = await response.json();
  if (!response.ok || !payload.ok) {
    throw new Error(payload.error || "装机代理调用失败");
  }
  return payload.data;
}

function collectStructuredFormContext() {
  const budgetInput = document.getElementById("buildBudgetInput");
  const usageSelect = document.getElementById("buildUsageSelect");
  const fieldMap = {
    CPU: 'select[name="selected_cpu"]',
    显卡: 'select[name="selected_gpu"]',
    主板: 'select[name="selected_motherboard"]',
    内存: 'select[name="selected_memory"]',
    固态硬盘: 'select[name="selected_storage"]',
    电源: 'select[name="selected_power"]',
    机箱: 'select[name="selected_case"]',
  };

  const selectedParts = {};
  Object.entries(fieldMap).forEach(([key, selector]) => {
    const node = document.querySelector(selector);
    const value = node ? String(node.value || "").trim() : "";
    if (value) {
      selectedParts[key] = value;
    }
  });

  return {
    budget: budgetInput ? String(budgetInput.value || "").trim() : "",
    usage_scenario: usageSelect ? String(usageSelect.value || "").trim() : "",
    selected_parts: selectedParts,
  };
}

async function resetAgentConversation() {
  const response = await fetch(buildAgentConfig.resetUrl, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
  const payload = await response.json();
  if (!response.ok || !payload.ok) {
    throw new Error(payload.error || "重置对话失败");
  }
  return payload.data;
}

function setChatBusy(isBusy) {
  const sendButton = document.getElementById("agentSendButton");
  const resetButton = document.getElementById("agentResetButton");
  const input = document.getElementById("agentMessageInput");
  if (sendButton) {
    sendButton.disabled = isBusy;
    sendButton.textContent = isBusy ? "生成中..." : "发送";
  }
  if (resetButton) {
    resetButton.disabled = isBusy;
  }
  if (input) {
    input.disabled = isBusy;
  }
}

function syncStructuredPanelsHeight() {
  const formPanel = document.getElementById("structuredBuildFormPanel");
  const resultPanel = document.getElementById("structuredBuildResultPanel");
  const resultBody = document.getElementById("structuredBuildResult");
  if (!formPanel || !resultPanel || !resultBody) {
    return;
  }
  if (window.innerWidth <= 900) {
    resultPanel.style.height = "";
    resultBody.style.maxHeight = "";
    return;
  }
  const panelHeight = 748;
  const head = resultPanel.querySelector(".section-head");
  const headHeight = head ? head.getBoundingClientRect().height : 0;
  const paddingAllowance = 56;
  resultPanel.style.height = `${panelHeight}px`;
  resultBody.style.maxHeight = `${Math.max(220, panelHeight - headHeight - paddingAllowance)}px`;
}

function buildResultCard(item) {
  return `
    <article class="recommend-card">
      <div class="recommend-card-top">
        <p class="item-keyword">${escapeHtml(item.component_type || "")}</p>
        <span class="mini-price">${formatPrice(item.effective_price)}</span>
      </div>
      <h3>${escapeHtml(item.component_subtype || "")}</h3>
      <p class="item-shop">${escapeHtml(item.shop || "未提供店铺信息")}</p>
      <p class="item-sku">${escapeHtml(item.sku_name || "")}</p>
      ${
        item.source_url
          ? `<a class="jump-button" href="${escapeHtml(item.source_url)}" target="_blank" rel="noopener noreferrer">查看商品</a>`
          : ""
      }
      <p class="item-reason">${escapeHtml(item.reason_text || "")}</p>
    </article>
  `;
}

function buildExplanationCard(explanation) {
  const groups = [];
  if (explanation.reason) {
    groups.push(`
      <div class="explain-group">
        <label>选择原因</label>
        <p>${escapeHtml(explanation.reason)}</p>
      </div>
    `);
  }
  if (explanation.compatibility) {
    groups.push(`
      <div class="explain-group">
        <label>兼容性</label>
        <p>${escapeHtml(explanation.compatibility)}</p>
      </div>
    `);
  }
  if (explanation.budget) {
    groups.push(`
      <div class="explain-group">
        <label>预算分配</label>
        <p>${escapeHtml(explanation.budget)}</p>
      </div>
    `);
  }
  if (explanation.market) {
    groups.push(`
      <div class="explain-group">
        <label>市场依据</label>
        <p>${escapeHtml(explanation.market)}</p>
      </div>
    `);
  }
  if (explanation.soft) {
    groups.push(`
      <div class="explain-group">
        <label>偏好处理</label>
        <p>${escapeHtml(explanation.soft)}</p>
      </div>
    `);
  }

  return `
    <article class="explain-card">
      <div class="explain-card-head">
        <strong>${escapeHtml(explanation.title || "配件说明")}</strong>
        <span>${escapeHtml(explanation.price_text || "")}</span>
      </div>
      ${groups.join("")}
    </article>
  `;
}

function buildVariantCard(variant) {
  const requestedBudget = variant.requested_budget || variant.solver_budget || 0;
  const solverBudget = variant.solver_budget || requestedBudget || 0;
  const totalPrice = variant.total_price || 0;
  const budgetGap = totalPrice - requestedBudget;
  const gapText = Number.isFinite(budgetGap)
    ? budgetGap > 0
      ? `超预算 ${formatPrice(Math.abs(budgetGap))}`
      : budgetGap < 0
        ? `低于预算 ${formatPrice(Math.abs(budgetGap))}`
        : "预算刚好贴合"
    : "-";
  const explanations = Array.isArray(variant.explanations) ? variant.explanations : [];
  const notes = Array.isArray(variant.notes) ? variant.notes : [];

  return `
    <article class="variant-card">
      <div class="variant-card-head">
        <div>
          <span class="variant-code">方案 ${escapeHtml(variant.code || "")}</span>
          <h4>${escapeHtml(variant.label || "候选方案")}</h4>
          <p class="agent-plan-sku">${escapeHtml(variant.choice_reason || "")}</p>
        </div>
        <div class="variant-price-wrap">
          <span class="variant-budget">目标 ${formatPrice(requestedBudget)} / 求解 ${formatPrice(solverBudget)}</span>
          <strong>${formatPrice(totalPrice)}</strong>
        </div>
      </div>

      <div class="summary-grid summary-grid-compact">
        <article class="summary-card">
          <span>场景</span>
          <strong>${escapeHtml(variant.usage_label || "-")}</strong>
        </article>
        <article class="summary-card">
          <span>总价</span>
          <strong>${formatPrice(totalPrice)}</strong>
        </article>
        <article class="summary-card">
          <span>预算差</span>
          <strong>${escapeHtml(gapText)}</strong>
        </article>
      </div>

      <div class="recommend-grid recommend-grid-single">
        ${(variant.results || []).map(buildResultCard).join("")}
      </div>

      ${
        explanations.length
          ? `
            <div class="agent-explain-head">
              <h3>选择原因</h3>
              <span>${escapeHtml(`方案 ${variant.code || ""} 的配件取舍说明`)}</span>
            </div>
            <div class="explain-card-grid">
              ${explanations.map(buildExplanationCard).join("")}
            </div>
          `
          : ""
      }

      ${
        notes.length
          ? `<div class="note-list note-list-chat">${notes.map((note) => `<p>${escapeHtml(note)}</p>`).join("")}</div>`
          : ""
      }
    </article>
  `;
}

function buildAssistantRichReply(data) {
  const replyText = escapeHtml(data.reply || "已根据当前条件生成方案。");
  const variants = Array.isArray(data.plan_variants) ? data.plan_variants : [];
  if (!variants.length) {
    return `<p>${replyText}</p>`;
  }

  const primary = variants[0] || {};
  return `
    <div class="chat-plan-block">
      <p>${replyText}</p>
      <div class="chat-plan-summary">
        <span class="plan-pill">主推 方案 ${escapeHtml(primary.code || "A")} · ${escapeHtml(primary.label || "均衡方案")}</span>
        <span class="plan-pill">共 ${variants.length} 套候选方案</span>
        <span class="plan-pill">场景 ${escapeHtml(primary.usage_label || "-")}</span>
      </div>
      <div class="variant-stack">
        ${variants.map(buildVariantCard).join("")}
      </div>
    </div>
  `;
}

function normalizePayloadToStructuredResult(data) {
  if (Array.isArray(data.plan_variants) && data.plan_variants.length) {
    const primary = data.plan_variants[0];
    return {
      usage_label: primary.usage_label || "",
      budget: primary.requested_budget || primary.solver_budget || 0,
      total_price: primary.total_price || 0,
      results: primary.results || [],
      notes: primary.notes || [],
    };
  }

  if (data.build_payload) {
    return {
      usage_label: data.build_payload.usage_label || "",
      budget: data.build_payload.budget || 0,
      total_price: data.build_payload.total_price || 0,
      results: data.build_payload.results || [],
      notes: data.build_payload.notes || [],
    };
  }

  return null;
}

function renderStructuredBuildResult(data) {
  const container = document.getElementById("structuredBuildResult");
  if (!container) {
    return;
  }

  const payload = normalizePayloadToStructuredResult(data);
  if (!payload) {
    container.innerHTML = `
      <div class="empty-state build-empty">
        提交左侧条件后，这里会显示本次结构化搭配结果。
      </div>
    `;
    return;
  }

  container.innerHTML = `
    <div class="summary-grid summary-grid-compact">
      <article class="summary-card">
        <span>场景</span>
        <strong>${escapeHtml(payload.usage_label || "-")}</strong>
      </article>
      <article class="summary-card">
        <span>预算</span>
        <strong>${formatPrice(payload.budget)}</strong>
      </article>
      <article class="summary-card">
        <span>方案总价</span>
        <strong>${formatPrice(payload.total_price)}</strong>
      </article>
    </div>

    <div class="recommend-grid recommend-grid-single">
      ${(payload.results || []).map(buildResultCard).join("")}
    </div>

    ${
      payload.notes && payload.notes.length
        ? `<div class="note-list">${payload.notes.map((note) => `<p>${escapeHtml(note)}</p>`).join("")}</div>`
        : ""
    }
  `;
}

function replaceLastAssistantPlaceholder(text) {
  const container = document.getElementById("agentConversation");
  if (!container) {
    appendAgentMessage("assistant", text);
    return;
  }
  const placeholders = container.querySelectorAll(".chat-bubble-assistant");
  const lastPlaceholder = placeholders[placeholders.length - 1];
  if (lastPlaceholder && lastPlaceholder.textContent.includes("正在生成中")) {
    const paragraph = lastPlaceholder.querySelector("p");
    if (paragraph) {
      paragraph.textContent = text;
      return;
    }
  }
  appendAgentMessage("assistant", text);
}

function replaceLastAssistantResponse(data) {
  const container = document.getElementById("agentConversation");
  const responseHtml = buildBubbleShell("assistant", buildAssistantRichReply(data));
  const hasVariants = Array.isArray(data.plan_variants) && data.plan_variants.length;
  const className = `chat-bubble chat-bubble-assistant${hasVariants ? " chat-bubble-rich" : ""}`;
  if (!container) {
    return;
  }

  const placeholders = container.querySelectorAll(".chat-bubble-assistant");
  const lastPlaceholder = placeholders[placeholders.length - 1];
  if (lastPlaceholder && lastPlaceholder.textContent.includes("正在生成中")) {
    lastPlaceholder.className = className;
    lastPlaceholder.innerHTML = responseHtml;
    container.scrollTop = container.scrollHeight;
    return;
  }

  const article = document.createElement("article");
  article.className = className;
  article.innerHTML = responseHtml;
  container.appendChild(article);
  container.scrollTop = container.scrollHeight;
}

function bootstrapBuildAgent() {
  renderSuggestions(buildAgentConfig.suggestions || []);
  syncStructuredPanelsHeight();
  window.addEventListener("resize", syncStructuredPanelsHeight);

  appendAgentMessage(
    "assistant",
    "你好，我是对话式装机代理。你可以直接告诉我预算、用途、已有配件，也可以补充白色主题、静音优先或品牌倾向，我会帮你分析；生成出来的方案会显示在右侧结果区。"
  );

  const form = document.getElementById("agentChatForm");
  const input = document.getElementById("agentMessageInput");
  const resetButton = document.getElementById("agentResetButton");

  if (form && input) {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const message = input.value.trim();
      if (!message) {
        return;
      }

      appendAgentMessage("user", message);
      input.value = "";
      setChatBusy(true);
      appendAgentMessage("assistant", "正在生成中，请稍候...");

      try {
        const data = await sendAgentMessage(message);
        replaceLastAssistantResponse(data);
        renderSuggestions(data.suggestions || buildAgentConfig.suggestions || []);
        renderStructuredBuildResult(data);
      } catch (error) {
        replaceLastAssistantPlaceholder(error.message || "当前暂时无法生成搭配，请稍后重试。");
      } finally {
        setChatBusy(false);
      }
    });
  }

  if (resetButton) {
    resetButton.addEventListener("click", async () => {
      setChatBusy(true);
      try {
        const data = await resetAgentConversation();
        const conversation = document.getElementById("agentConversation");
        if (conversation) {
          conversation.innerHTML = "";
        }
        appendAgentMessage(
          "assistant",
          "对话已重置。你可以重新输入预算、用途和已有配件，方案仍会显示在右侧结果区。"
        );
        renderSuggestions(data.suggestions || buildAgentConfig.suggestions || []);
        renderStructuredBuildResult({});
      } catch (error) {
        appendAgentMessage("assistant", error.message || "重置失败，请稍后重试。");
      } finally {
        setChatBusy(false);
      }
    });
  }
}

bootstrapBuildAgent();
