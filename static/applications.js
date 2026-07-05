/* 入札・工程＆協力会社 管理 — bid-next-eta（川野電気システム）のクライアントアプリを
   DGSS(Flask)へ忠実移植したもの。サーバ(applications/companies)をデータ源とし、
   揮発DB対策で localStorage にミラー保存する。 */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var esc = function (s) { var d = document.createElement("div"); d.textContent = (s == null ? "" : s); return d.innerHTML; };
  var read = function (id) { try { return JSON.parse($(id).textContent); } catch (e) { return null; } };

  var CFG = read("cfgData") || {};
  var CASES = read("casesData") || [];
  var COMPANIES = read("companiesData") || [];
  var TODAY = new Date((CFG.today || "") + "T00:00:00");
  var STATUSES = CFG.statuses || [];                       // [{id,accent}]
  var ACCENT = {}; STATUSES.forEach(function (s) { ACCENT[s.id] = s.accent; });
  var ASSIGNEES = CFG.assignees || [];                     // [{id,color}]
  var ACOLOR = {}; ASSIGNEES.forEach(function (a) { ACOLOR[a.id] = a.color; });
  var WORK = CFG.works || {};                              // {name:color}
  var METHODS = CFG.submit_methods || [];

  /* ---------- 共通ヘルパー（reference の es/en/er/ed/ei/ec と同等） ---------- */
  var fmtMan = function (y) { return Math.round((Number(y) || 0) / 1e4).toLocaleString("ja-JP") + "万"; };
  var toYen = function (s) { return Number(String(s == null ? "" : s).replace(/[^\d]/g, "")) || 0; };
  var dUntil = function (d) { if (!d) return null; var t = new Date(d + "T00:00:00"); if (isNaN(t)) return null; return Math.round((t - TODAY) / 864e5); };
  var md = function (d) { var t = new Date(d + "T00:00:00"); return isNaN(t) ? "" : (t.getMonth() + 1) + "/" + t.getDate(); };
  var workColor = function (w) { return WORK[w] || "#94a3b8"; };

  // 次の締切マイルストーン（ei）
  function milestone(c) {
    var apply = c.apply_deadline || c.deadline || "";
    if (c.status === "参加申請準備前") return apply ? { label: "参加申請", date: apply } : null;
    if (["入札参加申請済み", "協力会社探し中", "見積取得"].indexOf(c.status) >= 0)
      return c.bid_deadline ? { label: "入札書提出", date: c.bid_deadline } : null;
    if (c.status === "入札書提出済み") return c.open_date ? { label: "開札", date: c.open_date } : null;
    return null;
  }
  // 残日数→色帯（ec）
  function band(days) {
    if (days == null) return { bg: "#f1f5f9", fg: "#94a3b8" };
    if (days <= 7) return { bg: "#fee2e2", fg: "#dc2626" };
    if (days <= 14) return { bg: "#fef3c7", fg: "#b45309" };
    return { bg: "#ecfdf5", fg: "#047857" };
  }
  function daysLabel(days) {
    if (days == null) return "";
    if (days < 0) return Math.abs(days) + "日超";
    if (days === 0) return "本日";
    return "あと" + days + "日";
  }

  /* ---------- localStorage ミラー ---------- */
  function mirrorCase(c) {
    var KEY = "kawanoApplications", map;
    try { map = JSON.parse(localStorage.getItem(KEY)) || {}; } catch (e) { map = {}; }
    if (!c.external_id) return;
    map[c.external_id] = {
      external_id: c.external_id, status: c.status, assignee: c.assignee,
      flag: c.flag, applied_date: c.applied_date || "", note: c.note || "",
      apply_deadline: c.apply_deadline || "", bid_deadline: c.bid_deadline || "",
      open_date: c.open_date || "", submit_method: c.submit_method || "",
      work: c.work || "", materials: c.materials || "",
      needs_check: c.needs_check ? 1 : 0, award_called: c.award_called ? 1 : 0,
      bid_plan: c.bid_plan || 0, win_amount: c.win_amount || 0,
      partner: c.partner || "", partners: c.partners || []
    };
    try { localStorage.setItem(KEY, JSON.stringify(map)); } catch (e) {}
  }
  function mirrorCompanies() {
    try { localStorage.setItem("kawanoCompanies", JSON.stringify(COMPANIES)); } catch (e) {}
  }

  /* ---------- ステート ---------- */
  var state = { tab: "案件", assignee: null, statuses: [], q: "", coTag: null, coPartner: false, coSort: false };

  /* ============================================================
     タブバー
  ============================================================ */
  function renderTabs() {
    var tabs = [
      { id: "案件", label: "案件・工程", sub: CASES.length + "件" },
      { id: "会社", label: "協力会社", sub: COMPANIES.length + "社" },
      { id: "もうけ", label: "利益計算", sub: "落札−発注" },
      { id: "レポート", label: "月次レポート", sub: "月別" }
    ];
    $("bidTabs").innerHTML = tabs.map(function (t) {
      return '<button class="bidtab' + (state.tab === t.id ? " active" : "") + '" data-tab="' + t.id + '">' +
        '<span class="bt-label">' + t.label + '</span><span class="bt-sub">' + t.sub + "</span></button>";
    }).join("");
    Array.prototype.forEach.call($("bidTabs").querySelectorAll(".bidtab"), function (b) {
      b.addEventListener("click", function () { state.tab = b.getAttribute("data-tab"); render(); });
    });
  }

  /* ============================================================
     タブ1: 案件・工程（カンバン）
  ============================================================ */
  function renderKanban() {
    // 担当者で絞り込み
    var rows = state.assignee ? CASES.filter(function (c) { return (c.assignee || "未割当") === state.assignee; }) : CASES;

    // 担当者チップ件数
    var counts = {}; ASSIGNEES.forEach(function (a) { counts[a.id] = 0; });
    CASES.forEach(function (c) { var a = c.assignee || "未割当"; counts[a] = (counts[a] || 0) + 1; });

    // サマリー
    var active = rows.filter(function (c) { return ["自社落札", "他社落札", "NG", "見積集まらず"].indexOf(c.status) < 0; });
    var soon = [], over = 0;
    rows.forEach(function (c) {
      var ms = milestone(c); if (!ms) return;
      var d = dUntil(ms.date); if (d == null) return;
      if (d < 0) over++;
      if (d >= 0 && d <= 7) soon.push({ c: c, ms: ms, d: d });
    });
    soon.sort(function (a, b) { return a.d - b.d; });

    var chips = '<div class="chips"><span class="chips-label">担当で絞る:</span>' +
      '<button class="chip2' + (!state.assignee ? " on" : "") + '" data-as="">全員 <b>' + CASES.length + "</b></button>" +
      ASSIGNEES.map(function (a) {
        return '<button class="chip2' + (state.assignee === a.id ? " on" : "") + '" data-as="' + esc(a.id) + '" style="--c:' + a.color + '">' +
          '<span class="dot" style="background:' + a.color + '"></span>' + esc(a.id) + " <b>" + (counts[a.id] || 0) + "</b></button>";
      }).join("") + "</div>";

    // 状態（ステータス）で絞る。担当の絞り込み後(rows)の件数で表示。
    var scnt = {}; rows.forEach(function (c) { scnt[c.status] = (scnt[c.status] || 0) + 1; });
    var chipsStatus = '<div class="chips"><span class="chips-label">状態で絞る:</span>' +
      '<button class="chip2' + (!state.statuses.length ? " on" : "") + '" data-st="">すべて <b>' + rows.length + "</b></button>" +
      STATUSES.map(function (s) {
        var on = state.statuses.indexOf(s.id) >= 0;
        return '<button class="chip2' + (on ? " on" : "") + '" data-st="' + esc(s.id) + '" style="--c:' + s.accent + '">' +
          '<span class="dot" style="background:' + s.accent + '"></span>' + esc(s.id) + " <b>" + (scnt[s.id] || 0) + "</b></button>";
      }).join("") + "</div>";
    chips += chipsStatus;

    var cards = '<div class="sumrow">' +
      sumCard(active.length, "進行中の案件") +
      sumCard(soon.length, "1週間以内の締切", soon.length ? "warn" : "") +
      sumCard(over, "期限超過・要対応", over ? "danger" : "") +
      '<button class="addcase" onclick="location.href=\'/\'">＋ 案件を追加<small>案件を探すから登録</small></button>' +
      "</div>";

    var warn = "";
    if (soon.length) {
      warn = '<div class="warnbar"><span class="wb-title">直近の締切（入札1週間前〜）</span>' +
        soon.slice(0, 6).map(function (x) {
          var b = band(x.d);
          return '<span class="wb-item" style="background:' + b.bg + ";color:" + b.fg + '">' +
            esc(x.ms.label) + " " + md(x.ms.date) + "・" + daysLabel(x.d) + "</span>";
        }).join("") + "</div>";
    }

    // カンバン列（状態フィルタ時は選んだ状態の列だけ表示・複数可）
    var shownStatuses = state.statuses.length
      ? STATUSES.filter(function (s) { return state.statuses.indexOf(s.id) >= 0; })
      : STATUSES;
    var cols = shownStatuses.map(function (s) {
      var list = rows.filter(function (c) { return c.status === s.id; });
      return '<div class="kcol">' +
        '<div class="kcol-head" style="border-top:3px solid ' + s.accent + '">' +
          '<span>' + esc(s.id) + '</span><span class="kcnt">' + list.length + "</span></div>" +
        '<div class="kcol-body">' + (list.map(card).join("") || '<div class="kempty">—</div>') + "</div></div>";
    }).join("");

    var html = chips + cards + warn + '<div class="kanban">' + cols + "</div>";
    if (!CASES.length) {
      html = chips + cards + '<p class="empty">まだ管理中の案件がありません。<a href="/">案件を探す</a>から案件を開き、「入札参加申請の管理」で登録するとここに並びます。</p>';
    }
    return html;
  }

  function sumCard(num, label, cls) {
    return '<div class="sumc ' + (cls || "") + '"><span class="n">' + num + '</span><span class="l">' + label + "</span></div>";
  }

  function card(c) {
    var ms = milestone(c), d = ms ? dUntil(ms.date) : null, b = band(d);
    var qreq = (c.partners || []).length, qrep = (c.partners || []).filter(function (p) { return p.replied; }).length;
    return '<div class="kcard" data-id="' + c.case_id + '">' +
      '<div class="kc-top"><span class="wdot" style="background:' + workColor(c.work_eff) + '"></span>' +
        '<span class="kc-tag">' + esc(c.agency || c.work_eff || "") + "</span>" +
        '<span class="kc-as" style="background:' + (ACOLOR[c.assignee || "未割当"] || "#a8a29e") + '">' + esc(c.assignee || "未割当") + "</span></div>" +
      '<div class="kc-title">' + esc(c.title) + "</div>" +
      (ms ? '<div class="kc-dl" style="background:' + b.bg + ";color:" + b.fg + '">' + esc(ms.label) + " " + md(ms.date) + " ・ " + daysLabel(d) + "</div>" : "") +
      (qreq ? '<div class="kc-q">見積 依頼' + qreq + "社/回答" + qrep + "</div>" : "") +
      (c.flag ? '<div class="kc-flag">' + esc(c.flag) + "</div>" : "") +
      '<div class="kc-acts" onclick="event.stopPropagation()">' +
        '<a class="kc-link" href="/case/' + c.case_id + '" target="_blank" rel="noopener">詳細・公告</a>' +
        (c.detail_url ? '<a class="kc-link" href="' + esc(c.detail_url) + '" target="_blank" rel="noopener">NJSS</a>' : "") +
        '<a class="kc-link" href="/case/' + c.case_id + '#aiRun" target="_blank" rel="noopener">AI</a>' +
        (c.status !== "NG" ? '<button type="button" class="kc-ng" data-ng="' + c.case_id + '">NGへ</button>' : "") +
      "</div>" +
      "</div>";
  }

  /* ============================================================
     タブ2: 協力会社
  ============================================================ */
  function renderCompanies() {
    var q = state.q.trim();
    var list = COMPANIES.filter(function (c) {
      if (state.coPartner && !c.partner) return false;
      if (state.coTag && (c.tags || []).indexOf(state.coTag) < 0) return false;
      if (!q) return true;
      var hay = [c.name, c.area, c.note].concat(c.tags || []).join(" ");
      return hay.indexOf(q) >= 0;
    });
    if (state.coSort) list = list.slice().sort(function (a, b) { return (b.rating || 0) - (a.rating || 0); });
    var tagChips = Object.keys(WORK).slice(0, 10).map(function (t) {
      var on = state.coTag === t;
      return '<button class="cochip co-tagf' + (on ? " on" : "") + '" data-t="' + esc(t) + '" style="--c:' + workColor(t) + '">' + esc(t) + (on ? " ✕" : "") + "</button>";
    }).join("");
    var head = '<div class="co-head"><input id="coSearch" class="co-search" placeholder="会社名・エリア・工事内容でさがす" value="' + esc(state.q) + '">' +
      '<button class="btn primary" id="coAdd">＋ 会社を追加</button></div>' +
      '<div class="co-filters"><button class="cochip co-pf' + (state.coPartner ? " on" : "") + '">★よく頼む会社</button>' +
      '<button class="cochip co-sf' + (state.coSort ? " on" : "") + '">評価が高い順</button><span class="co-sep"></span>' + tagChips + "</div>";
    var cards = list.map(function (c) {
      var stars = "★★★★★".slice(0, c.rating || 0) + "☆☆☆☆☆".slice(0, 5 - (c.rating || 0));
      return '<div class="cocard" data-id="' + c.id + '">' +
        '<div class="co-row1">' + (c.partner ? '<span class="co-fav">★よく頼む</span>' : "") +
          '<span class="co-name">' + esc(c.name) + "</span>" +
          '<span class="co-stars">' + stars + "</span></div>" +
        '<div class="co-tags">' + (c.tags || []).map(function (t) {
          return '<span class="co-tag" style="background:' + workColor(t) + '22;color:' + workColor(t) + '">' + esc(t) + "</span>";
        }).join("") + "</div>" +
        '<div class="co-meta">' + (c.area ? esc(c.area) : "") + (c.tel ? " ・ TEL " + esc(c.tel) : "") +
          (c.url ? ' ・ <a href="' + esc(c.url) + '" target="_blank" rel="noopener">サイト</a>' : "") + "</div>" +
        (c.note ? '<div class="co-note">' + esc(c.note) + "</div>" : "") +
        ((c.reviews || []).length ? '<div class="co-revs">' + c.reviews.map(function (r) { return "<div>・" + esc(r) + "</div>"; }).join("") + "</div>" : "") +
        '<div class="co-acts"><button class="btn ghost small co-edit">編集</button><button class="btn ghost small co-del">削除</button></div>' +
        "</div>";
    }).join("");
    return head + '<div class="cogrid">' + (cards || '<p class="empty">協力会社がまだありません。「会社を追加」から登録してください。</p>') + "</div>";
  }

  /* ============================================================
     タブ3: 利益計算
  ============================================================ */
  function partnerCost(c) {
    var sel = (c.partners || []).filter(function (p) { return p.selected; });
    if (sel.length) return sel.reduce(function (s, p) { return s + toYen(p.amount); }, 0);
    return 0;
  }
  function renderProfit() {
    var rows = CASES.filter(function (c) { return c.win_amount > 0 || c.status === "自社落札"; });
    var byCo = {};
    var body = rows.map(function (c) {
      var cost = partnerCost(c), win = c.win_amount || 0, profit = win - cost;
      var rate = win ? Math.round(profit / win * 100) : 0;
      if (c.partner) { byCo[c.partner] = byCo[c.partner] || { order: 0, profit: 0, cnt: 0 }; byCo[c.partner].order += cost; byCo[c.partner].profit += profit; byCo[c.partner].cnt++; }
      return "<tr><td>" + esc(c.title) + "</td><td>" + esc(c.partner || "—") + "</td>" +
        '<td class="num">' + (cost ? fmtMan(cost) : "—") + "</td>" +
        '<td class="num">' + (win ? fmtMan(win) : "—") + "</td>" +
        '<td class="num" style="color:' + (profit < 0 ? "#dc2626" : "#16a34a") + '">' + fmtMan(profit) + "</td>" +
        '<td class="num">' + rate + "%</td></tr>";
    }).join("");
    var totWin = rows.reduce(function (s, c) { return s + (c.win_amount || 0); }, 0);
    var totCost = rows.reduce(function (s, c) { return s + partnerCost(c); }, 0);
    var coRows = Object.keys(byCo).map(function (k) {
      var v = byCo[k];
      return "<tr><td>" + esc(k) + "</td><td class=num>" + v.cnt + "</td><td class=num>" + fmtMan(v.order) + "</td><td class=num>" + fmtMan(v.profit) + "</td></tr>";
    }).join("");
    return '<div class="sumrow">' + sumCard(fmtMan(totWin), "落札 合計") + sumCard(fmtMan(totCost), "発注 合計") +
        sumCard(fmtMan(totWin - totCost), "利益 合計", (totWin - totCost) < 0 ? "danger" : "") + "</div>" +
      "<h3>案件ごとの内訳</h3>" +
      '<table class="list"><thead><tr><th>案件名</th><th>発注先</th><th>発注</th><th>落札</th><th>利益</th><th>利益率</th></tr></thead><tbody>' +
        (body || '<tr><td colspan="6" class="dim">落札済みの案件がまだありません（編集で落札額を入力）。</td></tr>') + "</tbody></table>" +
      (coRows ? '<h3>会社ごとの発注・利益</h3><table class="list"><thead><tr><th>協力会社</th><th>件数</th><th>発注</th><th>利益</th></tr></thead><tbody>' + coRows + "</tbody></table>" : "");
  }

  /* ============================================================
     タブ4: 月次レポート
  ============================================================ */
  function renderReports() {
    var months = {};
    CASES.forEach(function (c) {
      var m = (c.open_date || "").slice(0, 7); if (!m) return;
      (months[m] = months[m] || []).push(c);
    });
    var keys = Object.keys(months).sort();
    var saved = [];
    try { saved = JSON.parse(localStorage.getItem("kawanoReports")) || []; } catch (e) {}
    var blocks = keys.map(function (m) {
      var list = months[m];
      var plan = list.reduce(function (s, c) { return s + (c.bid_plan || 0); }, 0);
      var cost = list.reduce(function (s, c) { return s + partnerCost(c); }, 0);
      return '<div class="rep"><div class="rep-h"><b>' + m + "</b><span>" + list.length + "件・想定利益 " + fmtMan(plan - cost) + "</span></div>" +
        '<ul class="rep-list">' + list.map(function (c) { return "<li>" + esc(c.title) + ' <span class="dim">' + esc(c.status) + "</span></li>"; }).join("") + "</ul>" +
        '<div class="rep-save"><input class="rep-memo" placeholder="この月のメモ（例：照明改修は安定。次月は空調強化…）"><button class="btn ghost small rep-btn" data-m="' + m + '" data-plan="' + (plan - cost) + '" data-cnt="' + list.length + '">この月を保存</button></div></div>';
    }).join("");
    var savedHtml = saved.length ? '<h3>保存したレポート</h3>' + saved.map(function (r) {
      return '<div class="rep-saved"><b>' + esc(r.month) + "</b>（" + r.cnt + "件・想定利益 " + fmtMan(r.plan) + "）<div>" + esc(r.memo || "") + "</div></div>";
    }).join("") : "";
    return (blocks || '<p class="empty">開札日が入った案件がまだありません（編集で開札日を入力すると月別に集計されます）。</p>') + savedHtml;
  }

  /* ============================================================
     レンダリング統合 + イベント
  ============================================================ */
  function render() {
    renderTabs();
    var panel = $("tabPanel"), html;
    if (state.tab === "案件") html = renderKanban();
    else if (state.tab === "会社") html = renderCompanies();
    else if (state.tab === "もうけ") html = renderProfit();
    else html = renderReports();
    panel.innerHTML = html;
    bind();
  }

  function bind() {
    // 担当チップ
    Array.prototype.forEach.call(document.querySelectorAll(".chip2"), function (b) {
      b.addEventListener("click", function () {
        if (b.hasAttribute("data-st")) {
          var v = b.getAttribute("data-st");
          if (!v) state.statuses = [];                       // 「すべて」で解除
          else {
            var i = state.statuses.indexOf(v);
            if (i >= 0) state.statuses.splice(i, 1);          // 選択中なら外す
            else state.statuses.push(v);                       // 未選択なら追加
          }
        } else {
          state.assignee = b.getAttribute("data-as") || null;
        }
        render();
      });
    });
    // カードクリック→編集
    Array.prototype.forEach.call(document.querySelectorAll(".kcard"), function (el) {
      el.addEventListener("click", function () {
        var c = CASES.filter(function (x) { return String(x.case_id) === el.getAttribute("data-id"); })[0];
        if (c) openCaseModal(c);
      });
    });
    // カードのワンクリック「NGへ」（クリックはkc-actsでstopPropagation済み）
    Array.prototype.forEach.call(document.querySelectorAll(".kc-ng"), function (btn) {
      btn.addEventListener("click", function (e) {
        e.stopPropagation();
        var c = CASES.filter(function (x) { return String(x.case_id) === btn.getAttribute("data-ng"); })[0];
        if (c && confirm("「" + c.title + "」をNG（不参加）に移動しますか？")) { c.status = "NG"; saveCase(c); }
      });
    });
    // 協力会社
    var s = $("coSearch"); if (s) s.addEventListener("input", function () { state.q = s.value; var p = s.selectionStart; render(); var n = $("coSearch"); if (n) { n.focus(); try { n.setSelectionRange(p, p); } catch (e) {} } });
    var pf = document.querySelector(".co-pf"); if (pf) pf.addEventListener("click", function () { state.coPartner = !state.coPartner; render(); });
    var sf = document.querySelector(".co-sf"); if (sf) sf.addEventListener("click", function () { state.coSort = !state.coSort; render(); });
    Array.prototype.forEach.call(document.querySelectorAll(".co-tagf"), function (b) {
      b.addEventListener("click", function () { var t = b.getAttribute("data-t"); state.coTag = state.coTag === t ? null : t; render(); });
    });
    var add = $("coAdd"); if (add) add.addEventListener("click", function () { openCompanyModal(null); });
    Array.prototype.forEach.call(document.querySelectorAll(".cocard"), function (el) {
      var id = el.getAttribute("data-id");
      el.querySelector(".co-edit").addEventListener("click", function () { openCompanyModal(COMPANIES.filter(function (c) { return String(c.id) === id; })[0]); });
      el.querySelector(".co-del").addEventListener("click", function () {
        if (!confirm("この協力会社を削除しますか？")) return;
        fetch("/companies/" + id + "/delete", { method: "POST" }).then(function (r) { return r.json(); })
          .then(function (d) { COMPANIES = d.companies || []; mirrorCompanies(); render(); });
      });
    });
    // 月次レポート保存
    Array.prototype.forEach.call(document.querySelectorAll(".rep-btn"), function (b) {
      b.addEventListener("click", function () {
        var memo = b.parentNode.querySelector(".rep-memo").value;
        var saved = []; try { saved = JSON.parse(localStorage.getItem("kawanoReports")) || []; } catch (e) {}
        saved = saved.filter(function (r) { return r.month !== b.getAttribute("data-m"); });
        saved.push({ month: b.getAttribute("data-m"), plan: Number(b.getAttribute("data-plan")), cnt: Number(b.getAttribute("data-cnt")), memo: memo });
        try { localStorage.setItem("kawanoReports", JSON.stringify(saved)); } catch (e) {}
        render();
      });
    });
  }

  /* ---------- 見積の状態/最安（bid-next の qState/cheapest 準拠） ---------- */
  function qState(q) {
    if (!q.requested) return { label: "未依頼", color: "#94a3b8" };
    if (!q.replied) return { label: "返事待ち", color: "#d97706" };
    if (q.feasible === -1) return { label: "不可（断り）", color: "#dc2626" };
    if (q.feasible === 1 && !toYen(q.amount)) return { label: "可・金額待ち", color: "#0891b2" };
    if (q.feasible === 1 && toYen(q.amount) > 0) return { label: "回答あり", color: "#16a34a" };
    return { label: "返信あり", color: "#2563eb" };
  }
  function cheapestQ(qs) {
    var ans = qs.filter(function (q) { return q.feasible === 1 && toYen(q.amount) > 0; });
    if (!ans.length) return null;
    return ans.reduce(function (m, q) { return toYen(q.amount) < toYen(m.amount) ? q : m; });
  }

  /* ---------- 案件 編集モーダル（情報/見積の2タブ・bid-next 準拠） ---------- */
  function openCaseModal(c0) {
    var c = JSON.parse(JSON.stringify(c0));
    c.partners = c.partners || [];
    if (!c.win_amount) c.win_amount = toYen(c0.win_price) || 0;
    var mtab = "情報";
    var chOpen = false, chQ = "", chPartner = false;
    var aiResult = null, aiBusy = false;   // AI応募可否判定の結果
    var opt = function (arr, sel) { return arr.map(function (v) { return '<option value="' + esc(v) + '"' + (v === sel ? " selected" : "") + ">" + esc(v) + "</option>"; }).join(""); };

    // AI応募可否判定パネル（公告を読み、自社の等級・資格と照合してOK/NGを出す）
    function aiPanel() {
      if (aiBusy) return '<div class="m-ai busy"><div class="ai-loading">'
        + '<div class="ai-loading-msg">AIが公告を読み込み、応募可否（等級など）を判定中'
        + '<span class="ai-dots"><i>.</i><i>.</i><i>.</i></span></div>'
        + '<div class="ai-bar"></div>'
        + '<div class="ai-loading-sub">10〜30秒ほどかかります。そのままお待ちください。</div></div></div>';
      if (!aiResult) return '<div class="m-ai"><button type="button" class="btn primary small" id="aiJudge">AIで応募可否を判定</button>' +
        '<small>公告を読み、自社の保有資格・等級と照合して 〇/△/✕ を出します</small></div>';
      if (aiResult.enabled === false) return '<div class="m-ai dim">AI判定はAIプラン（管理者がGEMINI_API_KEYとアカウント許可を設定）で使えます。</div>';
      if (aiResult.error) return '<div class="m-ai dim">AI判定に失敗しました。' + '<button type="button" class="btn ghost small" id="aiRedo">再判定</button></div>';
      var e = aiResult.eligibility || {}, v = e.verdict || "不明";
      var cls = ({ "〇": "ok", "△": "warn", "✕": "ng" })[v] || "unk";
      var reasons = (e.reasons || []).map(function (r) { return "<li>" + esc(r) + "</li>"; }).join("");
      return '<div class="m-ai res ' + cls + '"><div class="m-ai-top"><b>AI応募可否</b> <span class="ai-v ' + cls + '">' + esc(v) + "</span>" +
        '<button type="button" class="btn ghost small" id="aiRedo">再判定</button></div>' +
        '<ul class="m-ai-rs">' + reasons + "</ul>" +
        (v === "〇" ? '<span class="dim">応募できそうです</span>'
          : '<button type="button" class="btn small ngbtn" id="aiNG">理由を添えてNGに入れる</button>') + "</div>";
    }

    function infoHtml() {
      var steps = [
        { label: "公開開始", date: c.announced_date },
        { label: "参加申請期限", date: c.apply_deadline || c.deadline },
        { label: "入札書提出期限", date: c.bid_deadline },
        { label: "開札日", date: c.open_date }
      ];
      var tl = steps.map(function (s) {
        var d = dUntil(s.date), b = band(d);
        return '<div class="tl-row"><span class="tl-dot" style="background:' + (d == null ? "#cbd5e1" : b.fg) + '"></span>' +
          '<span class="tl-label">' + s.label + '</span><span class="tl-date">' + (s.date ? md(s.date) : "—") + '</span>' +
          '<span class="tl-days" style="color:' + (d == null ? "#a8a29e" : b.fg) + '">' + (d == null ? "" : daysLabel(d)) + "</span></div>";
      }).join("");
      return '<div class="m-grid">' +
        fld("元機関（発注機関）", '<input name="agency_override" value="' + esc(c.agency || "") + '" placeholder="発注機関名（修正可）">', "full") +
        fld("状況", '<select name="status">' + opt(STATUSES.map(function (s) { return s.id; }), c.status) + "</select>") +
        fld("担当者", '<select name="assignee">' + opt(ASSIGNEES.map(function (a) { return a.id; }), c.assignee || "未割当") + "</select>") +
        fld("工事カテゴリ", '<select name="work"><option value="">（案件の業種）</option>' + opt(Object.keys(WORK), c.work || c.work_eff) + "</select>") +
        fld("入札タイプ", '<select name="submit_method"><option value="">—</option>' + opt(METHODS, c.submit_method) + "</select>") +
        fld("参加申請 期限", '<input type="date" name="apply_deadline" value="' + esc(c.apply_deadline) + '">') +
        fld("入札書提出 期限", '<input type="date" name="bid_deadline" value="' + esc(c.bid_deadline) + '">') +
        fld("開札日", '<input type="date" name="open_date" value="' + esc(c.open_date) + '">') +
        fld("資料受取", '<input name="materials" value="' + esc(c.materials) + '" placeholder="6/1以降受取 等">') +
        fld("要確認の一言", '<input name="flag" value="' + esc(c.flag) + '" placeholder="資料待ち 等">') +
        fld("メモ", '<textarea name="note" rows="3" class="note-area">' + esc(c.note) + '</textarea>', "full") +
      "</div><div class=\"tl\"><div class=\"tl-h\">工程タイムライン</div>" + tl + "</div>";
    }

    function quoteHtml() {
      var sel = c.partners.filter(function (q) { return q.selected; })[0];
      var exp = (toYen(c.bid_plan) && sel && toYen(sel.amount)) ? toYen(c.bid_plan) - toYen(sel.amount) : null;
      var ch = cheapestQ(c.partners);
      var money = '<div class="mq-money">' +
        '<label class="mb"><span>入札予定額</span><input name="bid_plan" value="' + (c.bid_plan || "") + '" placeholder="3000000"></label>' +
        '<label class="mb"><span>落札額(取れたら)</span><input name="win_amount" value="' + (c.win_amount || "") + '" placeholder="取れたら"></label>' +
        '<div class="mb exp ' + (exp == null ? "" : exp < 0 ? "neg" : "pos") + '"><span>想定利益</span><b>' + (exp == null ? "—" : fmtMan(exp)) + "</b><small>入札−採用見積</small></div></div>";
      var asked = {}; c.partners.forEach(function (q) { asked[q.company] = 1; });
      var work = c.work || c.work_eff;
      var clist = chQ ? COMPANIES.filter(function (co) { return ([co.name, co.area].concat(co.tags || []).join(" ")).indexOf(chQ) >= 0; })
        : COMPANIES.filter(function (co) { return (co.tags || []).indexOf(work) >= 0 || co.partner; });
      if (chPartner) clist = clist.filter(function (co) { return co.partner; });
      clist = clist.slice().sort(function (a, b) { return (b.rating || 0) - (a.rating || 0); });
      var chooser = '<button type="button" class="mq-chtoggle" id="chToggle">＋ ① 依頼先を選ぶ（おすすめ＝この工事に合う会社）</button>';
      if (chOpen) {
        chooser += '<div class="mq-chooser"><input id="chSearch" class="mq-chsearch" placeholder="会社をさがす（空欄＝おすすめ）" value="' + esc(chQ) + '">' +
          '<label class="mq-chpartner"><input type="checkbox" id="chPartner"' + (chPartner ? " checked" : "") + ">★取引先のみ</label>" +
          '<div class="mq-chlist">' + (clist.map(function (co) {
            return '<button type="button" class="mq-chitem' + (asked[co.name] ? " on" : "") + '" data-co="' + esc(co.name) + '"><span class="ck">' + (asked[co.name] ? "選択中" : "") + "</span>" +
              "<span>" + (co.partner ? "[常] " : "") + esc(co.name) + " <small>" + esc(co.area || "") + "</small></span></button>";
          }).join("") || '<p class="dim">該当なし</p>') + "</div></div>";
      }
      var rows = c.partners.length ? c.partners.map(function (q, i) {
        var st = qState(q), isMin = ch && ch === q;
        var co = COMPANIES.filter(function (x) { return x.name === q.company; })[0];
        var tel = q.tel || (co && co.tel) || "";
        return '<div class="mq-row' + (q.selected ? " sel" : "") + '" data-i="' + i + '"><div class="mq-r1">' +
          '<button type="button" class="mq-star' + (q.selected ? " on" : "") + '" data-act="select"' + (q.feasible === 1 ? "" : " disabled") + ">★</button>" +
          '<span class="mq-co">' + esc(q.company) + (isMin ? ' <span class="mq-min">最安</span>' : "") + "</span>" +
          '<span class="mq-stt" style="background:' + st.color + "1a;color:" + st.color + '">' + st.label + "</span>" +
          (tel ? '<a class="mq-tel" href="tel:' + esc(tel) + '">電話</a>' : "") +
          '<button type="button" class="mq-del" data-act="del">×</button></div><div class="mq-r2">' +
          '<button type="button" class="stp' + (q.requested ? " on" : "") + '" data-act="req">①依頼</button>' +
          '<button type="button" class="stp' + (q.replied ? " on" : "") + '" data-act="rep"' + (q.requested ? "" : " disabled") + ">②返信</button>" +
          (q.replied ? '<span class="yn"><button type="button" class="ynb' + (q.feasible === 1 ? " yes" : "") + '" data-act="yes">可</button><button type="button" class="ynb' + (q.feasible === -1 ? " no" : "") + '" data-act="no">否</button></span>' : "") +
          (q.feasible === 1 ? '<span class="mq-amt">¥<input class="q-amt" data-i="' + i + '" value="' + esc(q.amount) + '" placeholder="金額"></span>' : "") +
          "</div></div>";
      }).join("") : '<p class="dim">①から依頼先を選ぶか、＋追加で会社を足してください。</p>';
      var call = "";
      if (sel) {
        var co2 = COMPANIES.filter(function (x) { return x.name === sel.company; })[0];
        var tel2 = sel.tel || (co2 && co2.tel) || "";
        call = '<div class="mq-call"><b>落札の連絡</b><p>落札したら <b>' + esc(sel.company) + "</b> に連絡</p>" +
          (tel2 ? '<a class="btn primary small" href="tel:' + esc(tel2) + '">' + esc(tel2) + " に電話</a> " : '<span class="dim">電話番号が未登録</span> ') +
          '<button type="button" class="btn ' + (c.award_called ? "primary" : "ghost") + ' small" id="awardBtn">' + (c.award_called ? "連絡済み" : "連絡したら押す") + "</button></div>";
      }
      return money + chooser +
        '<div class="mq-rowshead">② 依頼先ごとの進捗（' + c.partners.length + "社）" + (ch ? ' <span class="dim">最安 ' + fmtMan(toYen(ch.amount)) + "</span>" : "") +
          '<button type="button" class="btn ghost small" id="mqAdd">＋手入力</button></div><div class="mq-rows">' + rows + "</div>" + call;
    }

    function bodyHtml() {
      var links = '<div class="m-links">' + (c.case_id ? '<a href="/case/' + c.case_id + '" target="_blank" rel="noopener">案件詳細</a>' : "") +
        (c.detail_url ? ' ・ <a href="' + esc(c.detail_url) + '" target="_blank" rel="noopener">公告ページ</a>' : "") + "</div>";
      var tabs = '<div class="m-tabs"><button type="button" class="m-tab' + (mtab === "情報" ? " on" : "") + '" data-mt="情報">案件情報</button>' +
        '<button type="button" class="m-tab' + (mtab === "見積" ? " on" : "") + '" data-mt="見積">見積もり・発注</button></div>';
      return aiPanel() + links + tabs + (mtab === "情報" ? infoHtml() : quoteHtml());
    }
    function pullInfo(root) {
      ["status", "assignee", "work", "submit_method", "apply_deadline", "bid_deadline", "open_date", "materials", "flag", "note", "agency_override"].forEach(function (n) {
        var e = root.querySelector('[name="' + n + '"]'); if (e) c[n] = e.value;
      });
    }
    function pullMoney(root) {
      var bp = root.querySelector('[name="bid_plan"]'); if (bp) c.bid_plan = toYen(bp.value);
      var wa = root.querySelector('[name="win_amount"]'); if (wa) c.win_amount = toYen(wa.value);
    }
    function redraw(root) { root.querySelector(".modal-b").innerHTML = bodyHtml(); bindModal(root); }
    function bindModal(root) {
      Array.prototype.forEach.call(root.querySelectorAll(".m-tab"), function (b) {
        b.addEventListener("click", function () {
          if (mtab === "情報") pullInfo(root); else pullMoney(root);
          mtab = b.getAttribute("data-mt"); redraw(root);
        });
      });
      if (mtab === "見積") bindQuote(root);
      bindAi(root);
      root.querySelector(".m-save").onclick = function () { if (mtab === "情報") pullInfo(root); else pullMoney(root); saveCase(c); };
    }
    function runAi(root, refresh) {
      aiBusy = true; redraw(root);
      var url = "/case/" + c.case_id + "/ai-assist" + (refresh ? "?refresh=1" : "");
      fetch(url, { method: "POST", headers: { "X-Requested-With": "XMLHttpRequest" } })
        .then(function (r) { if (r.status === 401 || r.status === 403) return { enabled: false }; return r.json(); })
        .then(function (j) { aiResult = j || { error: true }; aiBusy = false; redraw(root); })
        .catch(function () { aiResult = { error: true }; aiBusy = false; redraw(root); });
    }
    function bindAi(root) {
      var j = root.querySelector("#aiJudge"); if (j) j.onclick = function () { if (mtab === "情報") pullInfo(root); else pullMoney(root); runAi(root, false); };
      var rd = root.querySelector("#aiRedo"); if (rd) rd.onclick = function () { runAi(root, true); };
      var ng = root.querySelector("#aiNG"); if (ng) ng.onclick = function () {
        var e = (aiResult && aiResult.eligibility) || {};
        var reasons = (e.reasons || []);
        var first = reasons[0] || "AIが参加資格を満たさないと判定";
        var memo = "【AI判定: 参加不可】" + (reasons.length ? reasons.join(" / ") : first);
        if (mtab === "情報") pullInfo(root); else pullMoney(root);  // フォーム現在値を取り込む
        // 何が足りなかったかを理由メモ(note)に残し、NG列へ振り分ける
        c.status = "NG";
        c.flag = "AI: " + String(first).slice(0, 40);
        c.note = memo + (c.note ? " ／ " + c.note : "");
        saveCase(c);
      };
    }
    function bindQuote(root) {
      pullMoney(root);
      var ct = root.querySelector("#chToggle"); if (ct) ct.onclick = function () { pullMoney(root); chOpen = !chOpen; redraw(root); };
      var cs = root.querySelector("#chSearch"); if (cs) cs.oninput = function () { chQ = cs.value; var p = cs.selectionStart; redraw(root); var n = root.querySelector("#chSearch"); if (n) { n.focus(); try { n.setSelectionRange(p, p); } catch (e) {} } };
      var cp = root.querySelector("#chPartner"); if (cp) cp.onclick = function () { chPartner = cp.checked; redraw(root); };
      Array.prototype.forEach.call(root.querySelectorAll(".mq-chitem"), function (b) {
        b.onclick = function () {
          var name = b.getAttribute("data-co");
          if (c.partners.filter(function (q) { return q.company === name; })[0]) c.partners = c.partners.filter(function (q) { return q.company !== name; });
          else { var co = COMPANIES.filter(function (x) { return x.name === name; })[0] || {}; c.partners.push({ company: name, tel: co.tel || "", area: co.area || "", amount: "", requested: 0, replied: 0, feasible: 0, selected: 0 }); }
          redraw(root);
        };
      });
      var add = root.querySelector("#mqAdd"); if (add) add.onclick = function () { c.partners.push({ company: "新規会社", amount: "", requested: 0, replied: 0, feasible: 0, selected: 0 }); redraw(root); };
      var aw = root.querySelector("#awardBtn"); if (aw) aw.onclick = function () { c.award_called = c.award_called ? 0 : 1; redraw(root); };
      Array.prototype.forEach.call(root.querySelectorAll(".q-amt"), function (inp) {
        inp.oninput = function () { c.partners[Number(inp.getAttribute("data-i"))].amount = inp.value; };
      });
      Array.prototype.forEach.call(root.querySelectorAll(".mq-row"), function (rowEl) {
        var i = Number(rowEl.getAttribute("data-i"));
        Array.prototype.forEach.call(rowEl.querySelectorAll("[data-act]"), function (b) {
          b.onclick = function () {
            var q = c.partners[i], act = b.getAttribute("data-act");
            if (act === "del") c.partners.splice(i, 1);
            else if (act === "req") q.requested = q.requested ? 0 : 1;
            else if (act === "rep") q.replied = q.replied ? 0 : 1;
            else if (act === "yes") q.feasible = q.feasible === 1 ? 0 : 1;
            else if (act === "no") { q.feasible = q.feasible === -1 ? 0 : -1; q.selected = 0; }
            else if (act === "select") { var w = !q.selected; c.partners.forEach(function (x, j) { x.selected = (j === i && w) ? 1 : 0; }); if (w) c.partner = q.company; }
            redraw(root);
          };
        });
      });
    }
    showModal(esc(c.title), bodyHtml(), function (root) { bindModal(root); }, {
      danger: {
        label: "申請管理から削除",
        onClick: function () {
          if (!confirm("「" + c.title + "」を申請管理から削除しますか？\n（案件自体は残り、カンバンから外れます）")) return;
          // localStorageのミラーからも消す（自動復元で戻らないように）
          try {
            var KEY = "kawanoApplications", map = JSON.parse(localStorage.getItem(KEY)) || {};
            if (c.external_id && map[c.external_id]) { delete map[c.external_id]; localStorage.setItem(KEY, JSON.stringify(map)); }
          } catch (e) {}
          fetch("/applications/" + c.case_id + "/delete", {
            method: "POST", headers: { "X-Requested-With": "fetch" }
          }).then(function () { location.reload(); }).catch(function () { location.reload(); });
        }
      }
    });
  }
  function saveCase(c) {
    // 採用(selected)した見積があり発注先が未入力なら、その会社名を自動セット（擦り合わせ）。
    if (!c.partner) {
      var sel = (c.partners || []).filter(function (p) { return p.selected; })[0];
      if (sel) c.partner = sel.company;
    }
    mirrorCase(c);
    var MANAGED = "status,assignee,work,submit_method,apply_deadline,bid_deadline,open_date," +
      "materials,partner,flag,note,needs_check,bid_plan,win_amount,award_called,partners,agency_override";
    var fd = new URLSearchParams();
    fd.append("ajax", "1");
    fd.append("managed", MANAGED);
    ["status", "assignee", "work", "submit_method", "apply_deadline", "bid_deadline",
      "open_date", "materials", "partner", "flag", "note", "agency_override"].forEach(function (k) { fd.append(k, c[k] || ""); });
    fd.append("bid_plan", c.bid_plan || 0); fd.append("win_amount", c.win_amount || 0);
    if (c.award_called) fd.append("award_called", "1");
    fd.append("partners", JSON.stringify(c.partners || []));
    fetch("/case/" + c.case_id + "/apply", {
      method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded", "X-Requested-With": "fetch" }, body: fd.toString()
    }).then(function () { location.reload(); }).catch(function () { location.reload(); });
  }

  /* ---------- 協力会社 編集モーダル ---------- */
  function openCompanyModal(c) {
    c = c || { tags: [], reviews: [], rating: 0 };
    var tagBtns = Object.keys(WORK).map(function (t) {
      var on = (c.tags || []).indexOf(t) >= 0;
      return '<button type="button" class="tagpick' + (on ? " on" : "") + '" data-t="' + esc(t) + '" style="--c:' + workColor(t) + '">' + esc(t) + "</button>";
    }).join("");
    var stars = [1, 2, 3, 4, 5].map(function (n) { return '<button type="button" class="starpick' + (n <= (c.rating || 0) ? " on" : "") + '" data-n="' + n + '">★</button>'; }).join("");
    var revs = (c.reviews || []).join("\n");
    var body = '<div class="m-grid">' +
      fld("会社名", '<input name="name" value="' + esc(c.name) + '">', "full") +
      fld("対応エリア", '<input name="area" value="' + esc(c.area) + '" placeholder="大阪府八尾市 等">') +
      fld("電話", '<input name="tel" value="' + esc(c.tel) + '">') +
      fld("URL", '<input name="url" value="' + esc(c.url) + '">', "full") +
      fld("特徴・メモ", '<input name="note" value="' + esc(c.note) + '">', "full") +
      '<label class="m-check"><input type="checkbox" name="partner"' + (c.partner ? " checked" : "") + "> ★よく頼む</label>" +
      '<div class="m-fld"><span>評価</span><div class="stars" id="starPick">' + stars + "</div></div>" +
      '<div class="m-fld full"><span>工事カテゴリ</span><div class="tagpicks" id="tagPick">' + tagBtns + "</div></div>" +
      fld("口コミ（1行に1件）", '<textarea name="reviews" rows="3">' + esc(revs) + "</textarea>", "full") +
      "</div>";
    showModal(c.id ? "協力会社を編集" : "協力会社を追加", body, function (root) {
      var rating = c.rating || 0;
      Array.prototype.forEach.call(root.querySelectorAll(".starpick"), function (b) {
        b.addEventListener("click", function () {
          rating = Number(b.getAttribute("data-n"));
          Array.prototype.forEach.call(root.querySelectorAll(".starpick"), function (x) { x.classList.toggle("on", Number(x.getAttribute("data-n")) <= rating); });
        });
      });
      Array.prototype.forEach.call(root.querySelectorAll(".tagpick"), function (b) {
        b.addEventListener("click", function () { b.classList.toggle("on"); });
      });
      root.querySelector(".m-save").addEventListener("click", function () {
        var g = function (n) { var e = root.querySelector('[name="' + n + '"]'); return e ? e.value.trim() : ""; };
        if (!g("name")) { alert("会社名は必須です"); return; }
        var data = {
          id: c.id, name: g("name"), area: g("area"), tel: g("tel"), url: g("url"), note: g("note"),
          partner: root.querySelector('[name="partner"]').checked, rating: rating,
          tags: Array.prototype.filter.call(root.querySelectorAll(".tagpick.on"), function () { return true; }).map(function (b) { return b.getAttribute("data-t"); }),
          reviews: g("reviews").split("\n").map(function (s) { return s.trim(); }).filter(Boolean)
        };
        fetch("/companies", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) })
          .then(function (r) { return r.json(); }).then(function (d) {
            if (d.error) { alert(d.error); return; }
            COMPANIES = d.companies || []; mirrorCompanies(); closeModal(); render();
          });
      });
    });
  }

  /* ---------- モーダル土台 ---------- */
  function fld(label, inner, cls) { return '<label class="m-fld ' + (cls || "") + '"><span>' + label + "</span>" + inner + "</label>"; }
  function showModal(title, body, onMount, opts) {
    opts = opts || {};
    var del = opts.danger ? '<button type="button" class="btn modal-del">' + esc(opts.danger.label) + '</button>' : '';
    $("modalRoot").innerHTML = '<div class="modal-bg"><div class="modal"><div class="modal-h"><h2>' + title +
      '</h2><button class="modal-x">×</button></div><div class="modal-b">' + body +
      '</div><div class="modal-f">' + del + '<button class="btn ghost modal-x2">閉じる</button><button class="btn primary m-save">保存</button></div></div></div>';
    var root = $("modalRoot");
    var close = function () { closeModal(); };
    root.querySelector(".modal-x").addEventListener("click", close);
    root.querySelector(".modal-x2").addEventListener("click", close);
    root.querySelector(".modal-bg").addEventListener("click", function (e) { if (e.target.classList.contains("modal-bg")) close(); });
    if (opts.danger) { var dl = root.querySelector(".modal-del"); if (dl) dl.addEventListener("click", opts.danger.onClick); }
    if (onMount) onMount(root);
  }
  function closeModal() { $("modalRoot").innerHTML = ""; }

  /* ---------- 協力会社の localStorage 復元（サーバが空のとき） ---------- */
  function restoreCompanies() {
    if (COMPANIES.length) return;
    var raw; try { raw = JSON.parse(localStorage.getItem("kawanoCompanies")); } catch (e) { return; }
    if (!raw || !raw.length) return;
    fetch("/companies/restore", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ items: raw }) })
      .then(function (r) { return r.json(); }).then(function (d) {
        if (d.restored > 0) { COMPANIES = d.companies || []; render(); }
      }).catch(function () {});
  }

  render();
  restoreCompanies();
})();
