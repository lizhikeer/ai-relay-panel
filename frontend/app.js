/* ============================================================
 * AI 中转站管理面板 · 交互逻辑
 * 原生 JS，无框架、无构建、无 CDN，不使用 ES module import。
 * 全部网络请求走 /panel-api/*（仅调用 BFF 契约接口）。
 * ============================================================ */
(function () {
  'use strict';

  /* ---------------- 常量 ---------------- */
  var API_BASE = '/panel-api';
  var OVERVIEW_INTERVAL = 15000; // 运行看板 / 顶部徽章：15s
  var LOGS_INTERVAL = 5000;      // 调用日志：5s
  var TAB_IDS = ['tab-dashboard', 'tab-channels', 'tab-tokens', 'tab-logs', 'tab-system'];

  /* ---------------- 全局状态 ---------------- */
  var state = {
    username: '',
    currentTab: 'tab-dashboard',
    overview: null,
    channels: { items: [], total: 0, page: 1, size: 50, keyword: '', status: '' },
    channelTypes: [],
    tokens: { items: [], total: 0, page: 1, size: 100 },
    logs: { items: [], total: 0, page: 1, size: 50, type: '', keyword: '' },
    logsPaused: false,
    alertDismissed: false,
    timers: { overview: null, logs: null },
    loadedTabs: {}
  };

  /* ---------------- DOM 工具 ---------------- */
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  /* ---------------- 转义（防 XSS） ----------------
   * 上游返回的渠道名 / 模型名 / 用户名等可能含特殊字符，
   * 任何插入 innerHTML 的插值都必须先经此函数。 */
  function esc(v) {
    if (v === null || v === undefined) return '';
    return String(v)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  /* ---------------- 数值 / 时间格式化 ---------------- */
  function fmtInt(n) {
    n = Number(n);
    if (!isFinite(n)) return '0';
    return n.toLocaleString('en-US');
  }
  function fmtMoney(n, currency) {
    n = Number(n);
    if (!isFinite(n)) n = 0;
    var s = n.toFixed(2);
    return currency ? (s + ' ' + currency) : s;
  }
  function fmtTokens(n) {
    n = Number(n) || 0;
    if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
    if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
    if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
    return String(n);
  }
  function pad2(n) { return (n < 10 ? '0' : '') + n; }
  function fmtTime(ts) {
    var n = Number(ts);
    if (!n || n <= 0) return '—';
    var d = new Date(n * 1000);
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) +
      ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  }
  function fmtUptime(sec) {
    sec = Math.max(0, Number(sec) || 0);
    var d = Math.floor(sec / 86400);
    var h = Math.floor((sec % 86400) / 3600);
    var m = Math.floor((sec % 3600) / 60);
    if (d > 0) return d + ' 天 ' + h + ' 小时';
    if (h > 0) return h + ' 小时 ' + m + ' 分钟';
    return m + ' 分钟';
  }
  function nowText() {
    var d = new Date();
    return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
  }

  /* ---------------- Toast ---------------- */
  function toast(message, type, timeout) {
    var container = $('#toast-container');
    if (!container) return;
    type = type || 'info';
    var el = document.createElement('div');
    el.className = 'toast toast-' + type;
    el.textContent = message == null ? '' : String(message); // textContent：天然转义
    container.appendChild(el);

    var ttl = timeout || (type === 'error' ? 5000 : (type === 'warning' ? 4000 : 2500));
    setTimeout(function () {
      el.classList.add('fade-out');
      setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 220);
    }, ttl);
  }

  /* ---------------- API 封装 ----------------
   * 统一处理：
   *   - credentials: 'include'（HttpOnly cookie 登录态）
   *   - 401 → 切登录视图
   *   - success:false → 弹 toast（可用 silent 关闭，交给调用方渲染错误态）
   *   - 网络异常 → 抛 ApiError
   * 注意：绝不把响应里的 key/token 打到 console。 */
  function ApiError(message, status) {
    this.name = 'ApiError';
    this.message = message;
    this.status = status || 0;
  }
  ApiError.prototype = Object.create(Error.prototype);

  function api(path, opts) {
    opts = opts || {};
    var init = { method: opts.method || 'GET', credentials: 'include', headers: {} };
    if (opts.body !== undefined && opts.body !== null) {
      init.headers['Content-Type'] = 'application/json';
      init.body = (typeof opts.body === 'string') ? opts.body : JSON.stringify(opts.body);
    }

    return fetch(API_BASE + path, init).then(function (res) {
      var status = res.status;
      return res.text().then(function (text) {
        var body = null;
        try { body = text ? JSON.parse(text) : null; } catch (e) { body = null; }

        if (status === 401) {
          onUnauthorized(!!opts.quiet401);
          throw new ApiError((body && body.message) || '会话已失效，请重新登录', 401);
        }
        if (!body || typeof body.success !== 'boolean') {
          throw new ApiError('服务响应格式异常（HTTP ' + status + '）', status);
        }
        if (body.success === false && !opts.silent) {
          toast(body.message || '操作失败', 'error');
        }
        return body;
      });
    }, function () {
      throw new ApiError('网络错误：无法连接面板服务', 0);
    });
  }

  /* ---------------- 表格三态 ---------------- */
  function tableState(cols, kind, text) {
    if (kind === 'loading') {
      return '<tr><td class="table-state" colspan="' + cols + '"><span class="spinner"></span>加载中…</td></tr>';
    }
    if (kind === 'empty') {
      return '<tr><td class="table-state" colspan="' + cols + '">暂无数据</td></tr>';
    }
    return '<tr><td class="table-state error-text" colspan="' + cols + '">' + esc(text || '加载失败') + '</td></tr>';
  }

  /* ---------------- 认证视图切换 ---------------- */
  function showLogin() {
    stopTimers();
    closeModal();
    $('#app-view').hidden = true;
    $('#login-view').hidden = false;
    var pwd = $('#login-password');
    if (pwd) pwd.value = '';
    var err = $('#login-error');
    if (err) { err.hidden = true; err.textContent = ''; }
  }

  function showApp() {
    $('#login-view').hidden = true;
    $('#app-view').hidden = false;
  }

  function onUnauthorized(quiet) {
    if (!quiet) toast('登录状态已失效，请重新登录', 'warning');
    showLogin();
  }

  function onLoggedIn(username) {
    state.username = username || '';
    $('#nav-username').textContent = state.username ? ('👤 ' + state.username) : '';
    showApp();
    switchTab(state.currentTab || 'tab-dashboard', true);
    startTimers();
  }

  /* ---------------- 登录 / 登出 / 会话 ---------------- */
  function checkSession() {
    return api('/auth/session', { silent: true, quiet401: true }).then(function (body) {
      if (body.success && body.data && body.data.authenticated) {
        onLoggedIn(body.data.username);
      } else {
        showLogin();
      }
    }).catch(function (e) {
      showLogin();
      if (e.status !== 401) toast('无法连接面板服务：' + e.message, 'error');
    });
  }

  function handleLogin(ev) {
    ev.preventDefault();
    var username = $('#login-username').value.trim();
    var password = $('#login-password').value;
    var errBox = $('#login-error');
    var btn = $('#login-submit');

    errBox.hidden = true;
    if (!username || !password) {
      errBox.textContent = '请输入用户名与密码';
      errBox.hidden = false;
      return;
    }
    btn.disabled = true;
    btn.textContent = '登录中…';

    api('/auth/login', { method: 'POST', body: { username: username, password: password } })
      .then(function (body) {
        if (body.success) {
          toast('登录成功', 'success');
          onLoggedIn((body.data && body.data.username) || username);
        } else {
          // api() 已弹 toast；这里再给内联提示
          errBox.textContent = body.message || '登录失败';
          errBox.hidden = false;
        }
      })
      .catch(function (e) {
        errBox.textContent = e.status === 401 ? '用户名或密码错误' : e.message;
        errBox.hidden = false;
      })
      .then(function () {
        btn.disabled = false;
        btn.textContent = '登 录';
      });
  }

  function handleLogout() {
    api('/auth/logout', { method: 'POST', silent: true }).catch(function () { /* 忽略 */ })
      .then(function () {
        state.overview = null;
        state.loadedTabs = {};
        state.alertDismissed = false;   // 重新登录后应再次提醒
        showLogin();
        toast('已登出', 'success');
      });
  }

  /* ---------------- 标签切换 ---------------- */
  function switchTab(tabId, force) {
    if (TAB_IDS.indexOf(tabId) === -1) return;
    state.currentTab = tabId;

    $$('.tab-btn').forEach(function (btn) {
      btn.classList.toggle('active', btn.getAttribute('data-tab') === tabId);
    });
    $$('.tab-pane').forEach(function (pane) {
      pane.classList.toggle('active', pane.id === tabId);
    });

    if (force || !state.loadedTabs[tabId]) {
      state.loadedTabs[tabId] = true;
      loadTab(tabId);
    } else {
      // 再次进入时刷新一次，保证数据不陈旧
      loadTab(tabId);
    }
  }

  function loadTab(tabId) {
    switch (tabId) {
      case 'tab-dashboard': refreshOverview(); break;
      case 'tab-channels': loadChannels(); break;
      case 'tab-tokens': loadTokens(); break;
      case 'tab-logs': loadLogs(); loadLogStats(); break;
      case 'tab-system': loadSystem(); break;
    }
  }

  /* ---------------- 运行看板 / 顶部徽章 ---------------- */
  function refreshOverview(silent) {
    var opts = silent ? { silent: true } : {};
    return api('/overview', opts).then(function (body) {
      if (!body.success) {
        if (!silent) toast(body.message || '加载概览失败', 'error');
        renderOverviewError(body.message);
        return;
      }
      state.overview = body.data || {};
      renderOverview(state.overview);
    }).catch(function (e) {
      if (!silent) toast(e.message, 'error');
      renderOverviewError(e.message);
    });
  }

  function renderOverviewError(msg) {
    $('#dash-updated').textContent = '更新失败：' + (msg || '未知错误');
    ['dash-channels-value', 'dash-tokens-value', 'dash-req-value', 'dash-quota-value'].forEach(function (id) {
      var el = $('#' + id); if (el) el.textContent = '--';
    });
    $('#dash-abnormal-body').innerHTML = tableState(3, 'error', msg);
    setBadge('#badge-upstream', 'badge-red', '上游状态未知');
    setBadge('#badge-channels', 'badge-gray', '渠道 --/--');
    setBadge('#badge-today', 'badge-gray', '今日 -- 次');
  }

  function setBadge(sel, cls, text) {
    var el = $(sel);
    if (!el) return;
    el.className = 'badge ' + cls;
    el.textContent = text;
  }

  function renderOverview(d) {
    var ch = d.channels || {};
    var tk = d.tokens || {};
    var today = d.today || {};
    var up = d.upstream || {};
    var panel = d.panel || {};

    var chTotal = ch.total || 0;
    var chEnabled = ch.enabled || 0;
    var chDisabled = ch.disabled || 0;
    var chAbnormal = Array.isArray(ch.abnormal) ? ch.abnormal : [];

    /* 顶部常驻徽章 */
    if (up.reachable) {
      setBadge('#badge-upstream', 'badge-green', '上游正常 · ' + (up.latency_ms || 0) + 'ms');
    } else {
      setBadge('#badge-upstream', 'badge-red', '上游不可达');
    }
    setBadge('#badge-channels', (chEnabled === chTotal && chTotal > 0) ? 'badge-green' : 'badge-yellow',
      '渠道 ' + chEnabled + '/' + chTotal);
    setBadge('#badge-today', 'badge-blue', '今日 ' + fmtInt(today.requests || 0) + ' 次');

    /* 概览卡片 */
    $('#dash-channels-value').textContent = chEnabled + ' / ' + chTotal;
    $('#dash-channels-sub').textContent = '禁用 ' + chDisabled + ' · 异常 ' + chAbnormal.length;

    $('#dash-tokens-value').textContent = (tk.enabled || 0) + ' / ' + (tk.total || 0);
    $('#dash-tokens-sub').textContent = '禁用 ' + (tk.disabled || 0);

    $('#dash-req-value').textContent = fmtInt(today.requests || 0);
    $('#dash-req-sub').textContent = '今日累计调用次数';

    $('#dash-quota-value').textContent = fmtMoney(today.quota_spent || 0, today.quota_currency || '');
    $('#dash-quota-sub').textContent = '按上游计费口径统计';

    var upBox = $('#dash-upstream-value');
    if (up.reachable) { upBox.textContent = '可达'; upBox.style.color = 'var(--status-green)'; }
    else { upBox.textContent = '不可达'; upBox.style.color = 'var(--status-red)'; }
    $('#dash-upstream-sub').textContent = (up.name || '上游') + ' · ' + (up.latency_ms || 0) + 'ms';

    $('#dash-tokens-today').textContent = fmtTokens(today.tokens || 0);

    $('#dash-version').textContent = panel.version || '--';
    $('#dash-uptime').textContent = '运行时长：' + fmtUptime(panel.uptime_seconds);

    var abnormalValue = $('#dash-abnormal-value');
    abnormalValue.textContent = String(chAbnormal.length);
    abnormalValue.style.color = chAbnormal.length > 0 ? 'var(--status-red)' : 'var(--text-primary)';

    /* 异常渠道列表 */
    var badge = $('#dash-abnormal-badge');
    badge.className = 'badge ' + (chAbnormal.length ? 'badge-red' : 'badge-gray');
    badge.textContent = chAbnormal.length + ' 条';

    if (!chAbnormal.length) {
      $('#dash-abnormal-body').innerHTML = tableState(3, 'empty');
    } else {
      $('#dash-abnormal-body').innerHTML = chAbnormal.map(function (item) {
        return '<tr>' +
          '<td class="num">' + esc(item.id != null ? item.id : '—') + '</td>' +
          '<td>' + esc(item.name) + '</td>' +
          '<td><span class="badge badge-red">' + esc(item.reason || '异常') + '</span></td>' +
          '</tr>';
      }).join('');
    }

    $('#dash-updated').textContent = '更新时间：' + nowText();

    renderAlertBanner(chAbnormal);
  }

  /* ---------------- 全局告警横幅 ---------------- */
  /* 有渠道被自动禁用时置顶显示，便于定期巡检时一眼看到；
     用户手动关闭后本次会话内不再打扰（下次刷新页面若仍异常会重新出现）。 */
  function renderAlertBanner(list) {
    var el = $('#alert-banner');
    if (!el) return;
    if (!list || !list.length || state.alertDismissed) {
      el.style.display = 'none';
      el.innerHTML = '';
      return;
    }
    var items = list.map(function (it) {
      return '<li><span class="alert-chname">' + esc(it.name) + '</span>' +
             '（ID ' + esc(it.id != null ? it.id : '—') + '）— ' +
             esc(it.reason || '异常') + '</li>';
    }).join('');
    el.innerHTML =
      '<span class="alert-icon">⚠️</span>' +
      '<div class="alert-body">' +
        '<div class="alert-title">有 ' + list.length + ' 个渠道处于异常状态，需要处理</div>' +
        '<ul class="alert-list">' + items + '</ul>' +
        '<div class="alert-hint">常见原因：上游账号额度耗尽 → 到「渠道管理」检查该渠道，' +
        '续费或更换密钥后手动重新启用即可。</div>' +
      '</div>' +
      '<button class="alert-close" id="alert-banner-close" title="本次忽略">×</button>';
    el.style.display = 'flex';
    var btn = $('#alert-banner-close');
    if (btn) {
      btn.onclick = function () {
        state.alertDismissed = true;
        el.style.display = 'none';
      };
    }
  }

  /* ---------------- 渠道管理 ---------------- */
  function channelTypeName(type, fallback) {
    if (fallback) return fallback;
    for (var i = 0; i < state.channelTypes.length; i++) {
      if (state.channelTypes[i].id === type) return state.channelTypes[i].name;
    }
    return '类型 ' + type;
  }

  function loadChannelTypes() {
    if (state.channelTypes.length) return Promise.resolve();
    return api('/channels/types', { silent: true }).then(function (body) {
      if (body.success && Array.isArray(body.data)) state.channelTypes = body.data;
    }).catch(function () { /* 元数据失败不阻塞列表 */ });
  }

  function loadChannels() {
    $('#channels-body').innerHTML = tableState(8, 'loading');
    var c = state.channels;
    var q = '/channels?page=' + c.page + '&size=' + c.size +
      '&keyword=' + encodeURIComponent(c.keyword || '') +
      '&status=' + encodeURIComponent(c.status === '' ? '' : c.status);

    return loadChannelTypes().then(function () { return api(q, { silent: true }); })
      .then(function (body) {
        if (!body.success) {
          $('#channels-body').innerHTML = tableState(8, 'error', body.message);
          return;
        }
        var data = body.data || {};
        var items = Array.isArray(data.items) ? data.items : [];
        c.items = items;
        c.total = data.total || items.length;
        renderChannels(items);
      })
      .catch(function (e) {
        $('#channels-body').innerHTML = tableState(8, 'error', e.message);
      });
  }

  function renderChannels(items) {
    if (!items.length) {
      $('#channels-body').innerHTML = tableState(8, 'empty');
      return;
    }
    $('#channels-body').innerHTML = items.map(function (ch) {
      var enabled = ch.status === 1;
      var statusBadge = '<span class="badge ' + (enabled ? 'badge-green' : 'badge-red') + '">' +
        esc(ch.status_text || (enabled ? '启用' : '禁用')) + '</span>';

      var latency = (ch.last_test_latency_ms != null)
        ? '<span class="badge ' + (ch.last_test_ok ? 'badge-green' : 'badge-red') + '">' +
          esc(ch.last_test_latency_ms) + 'ms</span>'
        : '<span class="muted">未检测</span>';

      var lastTest = ch.last_test_at
        ? esc(fmtTime(ch.last_test_at))
        : '<span class="muted">—</span>';

      var models = ch.model_count != null
        ? ch.model_count
        : (Array.isArray(ch.models) ? ch.models.length : 0);

      return '<tr data-id="' + esc(ch.id) + '">' +
        '<td class="num">' + esc(ch.id) + '</td>' +
        '<td>' + esc(ch.name) + '</td>' +
        '<td>' + esc(channelTypeName(ch.type, ch.type_name)) + '</td>' +
        '<td>' + statusBadge + '</td>' +
        '<td class="num">' + esc(models) + '</td>' +
        '<td class="num">' + latency + '</td>' +
        '<td>' + lastTest + '</td>' +
        '<td><div class="cell-actions">' +
          '<button class="btn btn-outline btn-sm" data-act="test">🧪 测试</button>' +
          '<button class="btn btn-outline btn-sm" data-act="edit">✏️ 编辑</button>' +
          '<button class="btn ' + (enabled ? 'btn-warning' : 'btn-success') + ' btn-sm" data-act="toggle">' +
            (enabled ? '⏹️ 禁用' : '⚡ 启用') + '</button>' +
          '<button class="btn btn-danger btn-sm" data-act="del">🗑️ 删除</button>' +
        '</div></td>' +
        '</tr>';
    }).join('');
  }

  function findChannel(id) {
    id = Number(id);
    for (var i = 0; i < state.channels.items.length; i++) {
      if (Number(state.channels.items[i].id) === id) return state.channels.items[i];
    }
    return null;
  }

  function handleChannelAction(ev) {
    var btn = ev.target.closest ? ev.target.closest('button[data-act]') : null;
    if (!btn) return;
    var tr = btn.closest('tr');
    if (!tr) return;
    var id = Number(tr.getAttribute('data-id'));
    var act = btn.getAttribute('data-act');
    var ch = findChannel(id);

    if (act === 'test') testChannel(id, btn);
    else if (act === 'edit') openChannelModal(ch);
    else if (act === 'toggle') toggleChannelStatus(ch);
    else if (act === 'del') deleteChannel(ch);
  }

  function testChannel(id, btn) {
    btn.disabled = true;
    var original = btn.textContent;
    btn.textContent = '测试中…';

    api('/channels/' + id + '/test', { method: 'POST', body: { model: '' }, silent: true })
      .then(function (body) {
        if (!body.success) { toast(body.message || '测试失败', 'error'); return; }
        var d = body.data || {};
        if (d.ok) {
          toast('测试成功 · 耗时 ' + (d.latency_ms || 0) + 'ms（HTTP ' + (d.status_code || 200) + '）', 'success');
        } else {
          toast('测试失败 · HTTP ' + (d.status_code || '?') + '：' + (d.message || '未知原因'), 'error');
        }
        loadChannels();
      })
      .catch(function (e) {
        toast('测试请求失败：' + e.message, 'error');
      })
      .then(function () {
        btn.disabled = false;
        btn.textContent = original;
      });
  }

  function toggleChannelStatus(ch) {
    if (!ch) return;
    var next = ch.status === 1 ? 0 : 1;
    var label = next === 1 ? '启用' : '禁用';
    api('/channels/' + ch.id + '/status', { method: 'POST', body: { status: next } })
      .then(function (body) {
        if (body.success) {
          toast('已' + label + '渠道「' + ch.name + '」', 'success');
          loadChannels();
          refreshOverview(true);
        }
      })
      .catch(function (e) { toast(e.message, 'error'); });
  }

  function deleteChannel(ch) {
    if (!ch) return;
    confirmModal('确定删除渠道「' + ch.name + '」吗？此操作不可撤销。', { confirmText: '删除' })
      .then(function (ok) {
        if (!ok) return;
        return api('/channels/' + ch.id, { method: 'DELETE' }).then(function (body) {
          if (body.success) {
            toast('渠道已删除', 'success');
            loadChannels();
            refreshOverview(true);
          }
        });
      })
      .catch(function (e) { toast(e.message, 'error'); });
  }

  function splitModels(text) {
    return String(text || '').split(/[\s,，、\n]+/).map(function (s) { return s.trim(); })
      .filter(function (s) { return s.length > 0; });
  }

  function openChannelModal(ch) {
    var isEdit = !!ch;
    loadChannelTypes().then(function () {
      var types = state.channelTypes.length ? state.channelTypes
        : [{ id: 1, name: 'OpenAI' }, { id: 14, name: 'Anthropic' }];
      var typeOptions = types.map(function (t) {
        var sel = (ch && Number(ch.type) === Number(t.id)) ? ' selected' : (!ch && Number(t.id) === 1 ? ' selected' : '');
        return '<option value="' + esc(t.id) + '"' + sel + '>' + esc(t.name) + '（' + esc(t.id) + '）</option>';
      }).join('');

      var body = '' +
        '<div id="ch-form-error" class="field-error" hidden></div>' +
        '<div class="form-group"><label class="form-label">名称 *</label>' +
          '<input class="form-control" id="ch-name" value="' + esc(ch ? ch.name : '') + '" placeholder="如 TK-LzK-openai"></div>' +
        '<div class="form-row">' +
          '<div class="form-group"><label class="form-label">类型 *</label>' +
            '<select class="form-control" id="ch-type">' + typeOptions + '</select></div>' +
          '<div class="form-group"><label class="form-label">分组</label>' +
            '<input class="form-control" id="ch-group" value="' + esc(ch ? (ch.group || 'default') : 'default') + '"></div>' +
        '</div>' +
        '<div class="form-group"><label class="form-label">Base URL</label>' +
          '<input class="form-control" id="ch-baseurl" value="' + esc(ch ? (ch.base_url || '') : '') + '" placeholder="https://tokendance.space/gateway"></div>' +
        '<div class="form-group"><label class="form-label">API Key</label>' +
          '<input type="password" class="form-control" id="ch-key" placeholder="' + (isEdit ? '留空表示不修改' : 'sk-...') + '">' +
          '<div class="form-help">' + (isEdit
            ? '出于安全不回显明文；留空 = 保持原 key 不变。'
            : '新建渠道需要填写上游 API Key。') + '</div></div>' +
        '<div class="form-group"><label class="form-label">模型（逗号或空格分隔）</label>' +
          '<textarea class="form-control" id="ch-models">' + esc(ch && ch.models ? ch.models.join(', ') : '') + '</textarea></div>' +
        '<div class="form-row">' +
          '<div class="form-group"><label class="form-label">优先级</label>' +
            '<input type="number" class="form-control" id="ch-priority" value="' + esc(ch ? (ch.priority != null ? ch.priority : 0) : 0) + '"></div>' +
          '<div class="form-group"><label class="form-label">权重</label>' +
            '<input type="number" class="form-control" id="ch-weight" value="' + esc(ch ? (ch.weight != null ? ch.weight : 1) : 1) + '"></div>' +
        '</div>';

      var footer = '<button class="btn btn-outline" data-act="cancel">取消</button>' +
        '<button class="btn btn-primary" data-act="save">' + (isEdit ? '保存修改' : '创建') + '</button>';

      openModal(isEdit ? '编辑渠道' : '新建渠道', body, footer, function (act) {
        if (act !== 'save') { closeModal(); return; }

        var errBox = $('#ch-form-error');
        var name = $('#ch-name').value.trim();
        var keyVal = $('#ch-key').value;
        var payload = {
          name: name,
          type: Number($('#ch-type').value),
          base_url: $('#ch-baseurl').value.trim(),
          group: $('#ch-group').value.trim() || 'default',
          models: splitModels($('#ch-models').value),
          priority: Number($('#ch-priority').value) || 0,
          weight: Number($('#ch-weight').value) || 1
        };

        errBox.hidden = true;
        if (!name) { showFieldError('ch-name', '请填写渠道名称'); return; }
        if (isEdit) {
          if (keyVal) payload.key = keyVal; // 留空 = 不修改
        } else {
          if (!keyVal) { showFieldError('ch-key', '请填写 API Key'); return; }
          payload.key = keyVal;
        }

        var saveBtn = $('#modal-footer button[data-act="save"]');
        saveBtn.disabled = true;
        var original = saveBtn.textContent;
        saveBtn.textContent = '提交中…';

        var req = isEdit
          ? api('/channels/' + ch.id, { method: 'PUT', body: payload })
          : api('/channels', { method: 'POST', body: payload });

        req.then(function (body) {
          if (body.success) {
            toast(isEdit ? '渠道已更新' : '渠道已创建', 'success');
            closeModal();
            loadChannels();
            refreshOverview(true);
          }
          // 失败时 api() 已 toast（如 rc.41 的已知缺陷），保留弹窗供修改
        }).catch(function (e) {
          errBox.textContent = e.message;
          errBox.hidden = false;
        }).then(function () {
          saveBtn.disabled = false;
          saveBtn.textContent = original;
        });
      });
    });
  }

  function showFieldError(inputId, msg) {
    var input = $('#' + inputId);
    var errBox = $('#ch-form-error') || $('#tk-form-error');
    if (input) {
      input.classList.add('input-error');
      input.focus();
    }
    if (errBox) { errBox.textContent = msg; errBox.hidden = false; }
  }

  /* ---------------- 令牌管理 ---------------- */
  function loadTokens() {
    $('#tokens-body').innerHTML = tableState(8, 'loading');
    var t = state.tokens;
    return api('/tokens?page=' + t.page + '&size=' + t.size, { silent: true })
      .then(function (body) {
        if (!body.success) {
          $('#tokens-body').innerHTML = tableState(8, 'error', body.message);
          return;
        }
        var data = body.data || {};
        var items = Array.isArray(data.items) ? data.items : [];
        t.items = items;
        t.total = data.total || items.length;
        renderTokens(items);
      })
      .catch(function (e) {
        $('#tokens-body').innerHTML = tableState(8, 'error', e.message);
      });
  }

  function renderTokens(items) {
    if (!items.length) {
      $('#tokens-body').innerHTML = tableState(8, 'empty');
      return;
    }
    $('#tokens-body').innerHTML = items.map(function (tk) {
      var enabled = tk.status === 1;
      var statusBadge = '<span class="badge ' + (enabled ? 'badge-green' : 'badge-red') + '">' +
        esc(tk.status_text || (enabled ? '启用' : '禁用')) + '</span>';

      var used = fmtMoney(tk.quota_used || 0);
      var rest;
      if (tk.unlimited_quota) {
        rest = '<span class="badge badge-purple">不限额度</span>';
      } else {
        var left = Number(tk.quota || 0) - Number(tk.quota_used || 0);
        rest = '<span class="' + (left <= 0 ? 'error-text' : '') + '">' + fmtMoney(left) + '</span>';
      }

      return '<tr data-id="' + esc(tk.id) + '">' +
        '<td class="num">' + esc(tk.id) + '</td>' +
        '<td>' + esc(tk.name) + '</td>' +
        '<td class="mono">' + esc(tk.key_masked || '—') + '</td>' +
        '<td>' + statusBadge + '</td>' +
        '<td class="num">' + esc(used) + '</td>' +
        '<td class="num">' + rest + '</td>' +
        '<td>' + (tk.accessed_at ? esc(fmtTime(tk.accessed_at)) : '<span class="muted">—</span>') + '</td>' +
        '<td><div class="cell-actions">' +
          '<button class="btn btn-outline btn-sm" data-act="edit">✏️ 编辑</button>' +
          '<button class="btn ' + (enabled ? 'btn-warning' : 'btn-success') + ' btn-sm" data-act="toggle">' +
            (enabled ? '⏹️ 禁用' : '⚡ 启用') + '</button>' +
          '<button class="btn btn-danger btn-sm" data-act="del">🗑️ 删除</button>' +
        '</div></td>' +
        '</tr>';
    }).join('');
  }

  function findToken(id) {
    id = Number(id);
    for (var i = 0; i < state.tokens.items.length; i++) {
      if (Number(state.tokens.items[i].id) === id) return state.tokens.items[i];
    }
    return null;
  }

  function handleTokenAction(ev) {
    var btn = ev.target.closest ? ev.target.closest('button[data-act]') : null;
    if (!btn) return;
    var tr = btn.closest('tr');
    if (!tr) return;
    var id = Number(tr.getAttribute('data-id'));
    var act = btn.getAttribute('data-act');
    var tk = findToken(id);

    if (act === 'edit') openTokenModal(tk);
    else if (act === 'toggle') toggleTokenStatus(tk);
    else if (act === 'del') deleteToken(tk);
  }

  function toggleTokenStatus(tk) {
    if (!tk) return;
    var next = tk.status === 1 ? 0 : 1;
    var label = next === 1 ? '启用' : '禁用';
    api('/tokens/' + tk.id + '/status', { method: 'POST', body: { status: next } })
      .then(function (body) {
        if (body.success) {
          toast('已' + label + '令牌「' + tk.name + '」', 'success');
          loadTokens();
        }
      })
      .catch(function (e) { toast(e.message, 'error'); });
  }

  function deleteToken(tk) {
    if (!tk) return;
    confirmModal('确定删除令牌「' + tk.name + '」吗？使用该令牌的客户端将立即失效。', { confirmText: '删除' })
      .then(function (ok) {
        if (!ok) return;
        return api('/tokens/' + tk.id, { method: 'DELETE' }).then(function (body) {
          if (body.success) { toast('令牌已删除', 'success'); loadTokens(); refreshOverview(true); }
        });
      })
      .catch(function (e) { toast(e.message, 'error'); });
  }

  function openTokenModal(tk) {
    var isEdit = !!tk;
    var body = '' +
      '<div id="tk-form-error" class="field-error" hidden></div>' +
      '<div class="form-group"><label class="form-label">名称 *</label>' +
        '<input class="form-control" id="tk-name" value="' + esc(tk ? tk.name : '') + '" placeholder="如 张老师"></div>' +
      '<div class="form-row">' +
        '<div class="form-group"><label class="form-label">额度（消费上限）</label>' +
          '<input type="number" class="form-control" id="tk-quota" value="' + esc(tk && tk.quota != null ? tk.quota : 100) + '"></div>' +
        '<div class="form-group"><label class="form-label">有效期（天）</label>' +
          '<input type="number" class="form-control" id="tk-expired" value="-1">' +
          '<div class="form-help">-1 = 永久有效</div></div>' +
      '</div>' +
      '<div class="form-group"><label class="form-label">分组</label>' +
        '<input class="form-control" id="tk-group" value="' + esc(tk ? (tk.group || 'default') : 'default') + '"></div>' +
      '<div class="form-group"><label class="form-label">可用模型（逗号分隔，留空 = 全部）</label>' +
        '<input class="form-control" id="tk-models" placeholder="留空表示不限制"></div>' +
      '<div class="form-group checkbox-line">' +
        '<input type="checkbox" id="tk-unlimited"' + (tk && tk.unlimited_quota ? ' checked' : '') + '>' +
        '<label for="tk-unlimited">不限额度</label></div>';

    var footer = '<button class="btn btn-outline" data-act="cancel">取消</button>' +
      '<button class="btn btn-primary" data-act="save">' + (isEdit ? '保存修改' : '创建') + '</button>';

    openModal(isEdit ? '编辑令牌' : '新建令牌', body, footer, function (act) {
      if (act !== 'save') { closeModal(); return; }

      var name = $('#tk-name').value.trim();
      if (!name) { showFieldError('tk-name', '请填写令牌名称'); return; }

      var unlimited = $('#tk-unlimited').checked;
      var payload = {
        name: name,
        quota: unlimited ? 0 : (Number($('#tk-quota').value) || 0),
        unlimited_quota: unlimited,
        expired_days: Number($('#tk-expired').value),
        models: splitModels($('#tk-models').value),
        group: $('#tk-group').value.trim() || 'default'
      };
      if (!isFinite(payload.expired_days)) payload.expired_days = -1;

      var saveBtn = $('#modal-footer button[data-act="save"]');
      saveBtn.disabled = true;
      var original = saveBtn.textContent;
      saveBtn.textContent = '提交中…';

      var req = isEdit
        ? api('/tokens/' + tk.id, { method: 'PUT', body: payload })
        : api('/tokens', { method: 'POST', body: payload });

      req.then(function (body2) {
        if (body2.success) {
          toast(isEdit ? '令牌已更新' : '令牌已创建', 'success');
          closeModal();
          loadTokens();
          refreshOverview(true);
        }
      }).catch(function (e) {
        var errBox = $('#tk-form-error');
        errBox.textContent = e.message;
        errBox.hidden = false;
      }).then(function () {
        saveBtn.disabled = false;
        saveBtn.textContent = original;
      });
    });
  }

  /* ---------------- 调用日志 ---------------- */
  function loadLogs(silent) {
    if (!silent) $('#logs-body').innerHTML = tableState(12, 'loading');
    var g = state.logs;
    var q = '/logs?page=' + g.page + '&size=' + g.size +
      '&type=' + encodeURIComponent(g.type) +
      '&keyword=' + encodeURIComponent(g.keyword || '');

    return api(q, { silent: true })
      .then(function (body) {
        if (!body.success) {
          $('#logs-body').innerHTML = tableState(12, 'error', body.message);
          return;
        }
        var data = body.data || {};
        var items = Array.isArray(data.items) ? data.items : [];
        g.items = items;
        g.total = data.total || 0;
        renderLogs(items);
        updateLogsPager();
      })
      .catch(function (e) {
        $('#logs-body').innerHTML = tableState(12, 'error', e.message);
      });
  }

  function renderLogs(items) {
    if (!items.length) {
      $('#logs-body').innerHTML = tableState(12, 'empty');
      return;
    }
    $('#logs-body').innerHTML = items.map(function (lg) {
      var typeCls = 'badge-gray';
      if (lg.type === 2) typeCls = 'badge-blue';
      else if (lg.type === 5) typeCls = 'badge-red';
      else if (lg.type === 1) typeCls = 'badge-green';
      return '<tr>' +
        '<td class="num">' + esc(lg.id) + '</td>' +
        '<td>' + esc(fmtTime(lg.created_at)) + '</td>' +
        '<td><span class="badge ' + typeCls + '">' + esc(lg.type_text || lg.type) + '</span></td>' +
        '<td>' + esc(lg.username) + '</td>' +
        '<td>' + esc(lg.token_name) + '</td>' +
        '<td class="mono">' + esc(lg.model_name) + '</td>' +
        '<td>' + esc(lg.channel_name) + '</td>' +
        '<td class="num">' + esc(lg.prompt_tokens != null ? lg.prompt_tokens : 0) + '</td>' +
        '<td class="num">' + esc(lg.completion_tokens != null ? lg.completion_tokens : 0) + '</td>' +
        '<td class="num">' + esc(lg.quota != null ? lg.quota : 0) + '</td>' +
        '<td class="num">' + esc(lg.latency_ms != null ? lg.latency_ms : '—') + '</td>' +
        '<td>' + (lg.is_stream ? '<span class="badge badge-purple">流式</span>' : '<span class="muted">—</span>') + '</td>' +
        '</tr>';
    }).join('');
  }

  function updateLogsPager() {
    var g = state.logs;
    var pages = Math.max(1, Math.ceil(g.total / g.size));
    $('#logs-pageinfo').textContent = '第 ' + g.page + ' / ' + pages + ' 页 · 共 ' + fmtInt(g.total) + ' 条';
    $('#logs-prev').disabled = g.page <= 1;
    $('#logs-next').disabled = g.page >= pages;
  }

  function loadLogStats() {
    return api('/logs/stats?days=7', { silent: true })
      .then(function (body) {
        if (!body.success) return;
        var d = body.data || {};
        var daily = Array.isArray(d.daily) ? d.daily : [];
        var byModel = Array.isArray(d.by_model) ? d.by_model : [];

        var req = 0, tokens = 0, quota = 0;
        daily.forEach(function (x) {
          req += Number(x.requests || 0);
          tokens += Number(x.tokens || 0);
          quota += Number(x.quota || 0);
        });
        var top = byModel.slice().sort(function (a, b) { return (b.requests || 0) - (a.requests || 0); })[0];

        $('#logs-stat-requests').textContent = fmtInt(req);
        $('#logs-stat-tokens').textContent = fmtTokens(tokens);
        $('#logs-stat-quota').textContent = fmtMoney(quota);
        $('#logs-stat-topmodel').textContent = top ? top.model_name : '—';
        $('#logs-stats-updated').textContent = '更新：' + nowText();
      })
      .catch(function () { /* 统计失败不阻塞日志表 */ });
  }

  /* ---------------- 系统设置 ---------------- */
  function kvRows(pairs, cols) {
    if (!pairs.length) {
      return '<tr><td class="table-state" colspan="' + (cols || 2) + '">暂无数据</td></tr>';
    }
    return pairs.map(function (p) {
      return '<tr><td class="kv-key">' + esc(p[0]) + '</td><td class="kv-val">' + esc(p[1]) + '</td></tr>';
    }).join('');
  }

  function loadSystem() {
    $('#system-info-body').innerHTML = tableState(2, 'loading');
    $('#system-health-body').innerHTML = tableState(2, 'loading');
    $('#system-options-body').innerHTML = tableState(2, 'loading');

    api('/system/health', { silent: true }).then(function (body) {
      var d = (body.success && body.data) ? body.data : {};
      var pairs = [
        ['BFF 服务', d.bff || '--'],
        ['上游 New API', d.upstream || '--'],
        ['上游延迟', (d.upstream_latency_ms != null ? d.upstream_latency_ms + ' ms' : '--')]
      ];
      $('#system-health-body').innerHTML = kvRows(pairs);
    }).catch(function (e) {
      $('#system-health-body').innerHTML = tableState(2, 'error', e.message);
    });

    api('/system/info', { silent: true }).then(function (body) {
      if (!body.success) { $('#system-info-body').innerHTML = tableState(2, 'error', body.message); return; }
      var d = body.data || {};
      var pairs = [
        ['面板版本', d.version || '--'],
        ['上游地址', d.upstream_base_url || d.upstream || '--'],
        ['启动时间', d.started_at ? fmtTime(d.started_at) : '--'],
        ['运行时长', d.uptime_seconds != null ? fmtUptime(d.uptime_seconds) : '--']
      ];
      $('#system-info-body').innerHTML = kvRows(pairs);
    }).catch(function (e) {
      $('#system-info-body').innerHTML = tableState(2, 'error', e.message);
    });

    api('/system/options', { silent: true }).then(function (body) {
      if (!body.success) { $('#system-options-body').innerHTML = tableState(2, 'error', body.message); return; }
      var d = body.data || {};
      var pairs = Object.keys(d).map(function (k) { return [k, d[k]]; });
      $('#system-options-body').innerHTML = kvRows(pairs);
    }).catch(function (e) {
      $('#system-options-body').innerHTML = tableState(2, 'error', e.message);
    });
  }

  /* ---------------- 通用弹窗 ---------------- */
  var modalActionHandler = null;

  function openModal(title, bodyHtml, footerHtml, onAction) {
    $('#modal-title').innerHTML = esc(title);
    $('#modal-body').innerHTML = bodyHtml;
    $('#modal-footer').innerHTML = footerHtml || '';
    modalActionHandler = onAction || null;
    $('#modal-overlay').hidden = false;
    document.body.classList.add('modal-open');
    var first = $('#modal-body').querySelector('input, select, textarea');
    if (first) first.focus();
  }

  function closeModal() {
    $('#modal-overlay').hidden = true;
    document.body.classList.remove('modal-open');
    modalActionHandler = null;
  }

  function fireModalAction(act) {
    var h = modalActionHandler;
    if (h) h(act);
  }

  function confirmModal(message, options) {
    options = options || {};
    return new Promise(function (resolve) {
      var body = '<p class="confirm-text">' + esc(message) + '</p>';
      var footer =
        '<button class="btn btn-outline" data-act="cancel">取消</button>' +
        '<button class="btn ' + (options.danger === false ? 'btn-primary' : 'btn-danger') + '" data-act="ok">' +
        esc(options.confirmText || '确认') + '</button>';
      openModal(options.title || '确认操作', body, footer, function (act) {
        closeModal();
        resolve(act === 'ok');
      });
    });
  }

  /* ---------------- 定时器 ---------------- */
  function startTimers() {
    stopTimers();
    state.timers.overview = setInterval(function () {
      if (document.hidden) return;
      refreshOverview(true);
    }, OVERVIEW_INTERVAL);

    state.timers.logs = setInterval(function () {
      if (state.logsPaused || document.hidden) return;
      if (state.currentTab !== 'tab-logs') return;
      loadLogs(true);
    }, LOGS_INTERVAL);
  }

  function stopTimers() {
    if (state.timers.overview) { clearInterval(state.timers.overview); state.timers.overview = null; }
    if (state.timers.logs) { clearInterval(state.timers.logs); state.timers.logs = null; }
  }

  /* ---------------- 事件绑定 ---------------- */
  function bindEvents() {
    /* 标签切换 */
    $$('.tab-btn').forEach(function (btn) {
      btn.addEventListener('click', function () { switchTab(btn.getAttribute('data-tab')); });
    });

    /* 登录 / 登出 */
    $('#login-form').addEventListener('submit', handleLogin);
    $('#btn-logout').addEventListener('click', handleLogout);

    /* 运行看板 */
    $('#dash-refresh').addEventListener('click', function () { refreshOverview(); });

    /* 渠道 */
    $('#channel-refresh').addEventListener('click', loadChannels);
    $('#channel-search').addEventListener('click', function () {
      state.channels.keyword = $('#channel-keyword').value.trim();
      state.channels.status = $('#channel-status').value;
      state.channels.page = 1;
      loadChannels();
    });
    $('#channel-keyword').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') $('#channel-search').click();
    });
    $('#channel-create').addEventListener('click', function () { openChannelModal(null); });
    $('#channels-body').addEventListener('click', handleChannelAction);

    /* 令牌 */
    $('#token-refresh').addEventListener('click', loadTokens);
    $('#token-create').addEventListener('click', function () { openTokenModal(null); });
    $('#tokens-body').addEventListener('click', handleTokenAction);

    /* 日志 */
    $('#logs-refresh').addEventListener('click', function () { loadLogs(); loadLogStats(); });
    $('#logs-search').addEventListener('click', function () {
      state.logs.type = $('#logs-type').value;
      state.logs.keyword = $('#logs-keyword').value.trim();
      state.logs.size = Number($('#logs-size').value) || 50;
      state.logs.page = 1;
      loadLogs();
    });
    $('#logs-keyword').addEventListener('keydown', function (e) {
      if (e.key === 'Enter') $('#logs-search').click();
    });
    $('#logs-size').addEventListener('change', function () { $('#logs-search').click(); });
    $('#logs-prev').addEventListener('click', function () {
      if (state.logs.page > 1) { state.logs.page--; loadLogs(); }
    });
    $('#logs-next').addEventListener('click', function () {
      var pages = Math.max(1, Math.ceil(state.logs.total / state.logs.size));
      if (state.logs.page < pages) { state.logs.page++; loadLogs(); }
    });
    $('#logs-pause').addEventListener('click', function () {
      state.logsPaused = !state.logsPaused;
      var btn = $('#logs-pause');
      btn.textContent = state.logsPaused ? '▶️ 继续刷新' : '⏸️ 暂停刷新';
      btn.className = 'btn btn-sm ' + (state.logsPaused ? 'btn-success' : 'btn-warning');
      toast(state.logsPaused ? '已暂停自动刷新' : '已恢复自动刷新', 'info', 1800);
    });

    /* 系统 */
    $('#system-refresh').addEventListener('click', loadSystem);

    /* 弹窗 */
    $('#modal-close').addEventListener('click', function () { fireModalAction('close'); closeModal(); });
    $('#modal-overlay').addEventListener('click', function (e) {
      if (e.target === $('#modal-overlay')) { fireModalAction('close'); closeModal(); }
    });
    $('#modal-footer').addEventListener('click', function (e) {
      var btn = e.target.closest ? e.target.closest('button[data-act]') : null;
      if (btn) fireModalAction(btn.getAttribute('data-act'));
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !$('#modal-overlay').hidden) { fireModalAction('close'); closeModal(); }
    });
  }

  /* ---------------- 启动 ---------------- */
  function init() {
    bindEvents();
    checkSession();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
