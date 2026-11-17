"use strict";
const tabButtons = [...document.querySelectorAll("[data-tab]")];
const initialTab = location.hash.slice(1);
let historySequence = 0;

function selectTab(name, updateUrl = true) {
  const button = tabButtons.find(item => item.dataset.tab === name) || tabButtons[0];
  document.querySelectorAll(".tab").forEach(panel => { panel.hidden = panel.id !== button.dataset.tab; });
  tabButtons.forEach(item => {
    item.classList.toggle("selected", item === button);
    item.setAttribute("aria-pressed", String(item === button));
  });
  $("section-title").textContent = button.textContent;
  button.scrollIntoView({block: "nearest", inline: "nearest", behavior: "instant"});
  if (updateUrl) history.replaceState(null, "", "#" + button.dataset.tab);
  notice();
  if (button.dataset.tab === "history" && token) {
    action(button, loadHistory);
  }
}
tabButtons.forEach((button, index) => {
  button.onclick = () => selectTab(button.dataset.tab);
  button.onkeydown = event => {
    const directions = {ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1};
    if (directions[event.key]) {
      event.preventDefault();
      tabButtons[(index + directions[event.key] + tabButtons.length) % tabButtons.length].focus();
    }
  };
});
window.addEventListener("hashchange", () => selectTab(location.hash.slice(1), false));
selectTab(initialTab, false);
$("today-label").dateTime = today;
$("today-label").textContent = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", month: "long", day: "numeric", weekday: "long"
}).format(new Date());

document.querySelectorAll("input, select, textarea").forEach(input => {
  if (!input.name && input.id) input.name = input.id;
  if (!input.hasAttribute("autocomplete")) input.autocomplete = "off";
});
document.querySelectorAll("[data-prompt]").forEach(button => {
  button.onclick = () => {
    $("message").value = button.dataset.prompt;
    $("message").focus();
  };
});

new MutationObserver(() => {
  const connected = !$("workspace").hidden;
  $("space-status").textContent = connected ? "个人空间已连接" : "个人习惯管理";
  $("space-status").classList.toggle("connected", connected);
  if (!connected) {
    historySequence++;
    ["history-counts", "history-output", "domain-output"].forEach(id => $(id).replaceChildren());
  } else if (!$("history").hidden) {
    loadHistory().catch(error => notice(error.message));
  }
}).observe($("workspace"), {attributes: true, attributeFilter: ["hidden"]});

async function loadHistory() {
  const requestToken = token;
  const sequence = ++historySequence;
  const data = await api("/history");
  if (!token || requestToken !== token || sequence !== historySequence) return;
  const previous = $("history-table").value;
  $("history-table").replaceChildren();
  $("history-counts").replaceChildren();
  data.tables.forEach(item => {
    const option = element("option", `${item.label}（${item.count}）`);
    option.value = item.table;
    $("history-table").append(option);
    if (item.count) $("history-counts").append(element("span", `${item.label} ${item.count}`));
  });
  if (previous) $("history-table").value = previous;
  if (!data.record_count) {
    $("history-output").replaceChildren(element("p", "当前空间暂无迁入资料。历史资料只在导入时指定的个人空间中可见。", "hint"));
    return;
  }
  await loadHistoryRecords(0);
}

async function loadHistoryRecords(offset) {
  const requestToken = token;
  const sequence = ++historySequence;
  const table = $("history-table").value;
  const data = await api(`/history/${encodeURIComponent(table)}?offset=${offset}&limit=30`);
  if (sequence !== historySequence || !token || requestToken !== token) return;
  const parent = $("history-output");
  parent.replaceChildren(element("p", `${data.label}，共 ${data.record_count} 条。以下保留历史原值，仅供查阅。`, "hint"));
  if (!data.records.length) parent.append(element("p", "这类资料尚无记录。"));
  data.records.forEach(record => {
    const values = record.payload;
    const role = {user: "你", assistant: "FitLife", system: "历史系统信息", tool: "工具记录"}[values.role] || values.role;
    const title = [values.record_date || values.meal_date, values.name || values.meal_type || role || values.title, `记录 ${record.key}`].filter(Boolean).join(" · ");
    const row = element("details", undefined, "history-row");
    row.append(element("summary", title));
    row.append(element("span", "历史资料 · 未重新核验", "badge"));
    row.append(element("pre", JSON.stringify(values, null, 2)));
    parent.append(row);
  });
  if (offset || data.has_more) {
    const controls = element("div", undefined, "history-pagination");
    if (offset) {
      const previous = element("button", "上一页", "secondary");
      previous.onclick = () => action(previous, () => loadHistoryRecords(Math.max(0, offset - 30)));
      controls.append(previous);
    }
    controls.append(element("small", `第 ${Math.floor(offset / 30) + 1} 页`));
    if (data.has_more) {
      const next = element("button", "下一页", "secondary");
      next.onclick = () => action(next, () => loadHistoryRecords(offset + 30));
      controls.append(next);
    }
    parent.append(controls);
  }
}
$("history-form").onsubmit = event => {
  event.preventDefault();
  action(event.submitter, () => loadHistoryRecords(0));
};

// Enable login only after every deferred application script has initialized.
$("login-form").querySelector("button").disabled = false;
document.documentElement.dataset.appReady = "true";
