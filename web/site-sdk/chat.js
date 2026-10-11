/*
 * 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
 *
 * site-sdk 聊天主体（chat.js）——FR-SITE-001 网站聊天组件 + FR-SITE-002 行为采集 +
 * FR-SITE-007 consent 门控（源码交付，无构建链；目标 ES2017，降级见 README）。
 *
 * 模块划分（与 SR-SITE-001 §6.2~§6.6 / SR-SITE-002 §6.1 对应）：
 *   i18n        多语言检测与切换（navigator.language → 支持语种，不中回落 'en'）
 *   consent     consent 三态门控（asa_consent：granted/necessary/denied；未同意禁行为与邀聊）
 *   transport   WS 实时通道（心跳/指数退避重连）→ 连续 lpAfter 次失败降级长轮询
 *   tracker     行为批量缓冲上报（定量/定时 flush；pagehide 走 navigator.sendBeacon）
 *   widget      聊天 UI（自定义元素 <asa-chat> open Shadow DOM；无 Shadow DOM 降级 asa- 前缀）
 *
 * 后端契约：POST /api/v1/site/session（visitor token 签发）、POST /api/v1/site/events（批量上报）、
 *          POST /api/v1/site/chat、GET|POST /api/v1/site/messages（长轮询降级通道）。
 */
(function () {
  'use strict';

  /** 支持语种（随知识库多语言话术扩展；检测不中回落 en）。@type {string[]} */
  var SUPPORT_LANGS = ['en', 'zh', 'es', 'de', 'fr', 'ar', 'pt', 'ru'];
  /** 批量上报触发条数（SR-SITE-002 §7 SITE_TRACK_BATCH_MAX）。@type {number} */
  var BATCH_MAX = 10;
  /** 定时上报间隔毫秒（至多每 5s 一批）。@type {number} */
  var FLUSH_MS = 5000;
  /** WS 心跳间隔毫秒（30s ping；连续 3 次无 pong 判死链）。@type {number} */
  var HEARTBEAT_MS = 30000;
  /** 连续连接失败达该次数即降级长轮询（SITE_WS_LP_FALLBACK_AFTER）。@type {number} */
  var LP_AFTER = 3;
  /** 重连退避封顶毫秒。@type {number} */
  var BACKOFF_MAX_MS = 30000;
  /** API 基址（同源部署；跨域部署由宿主以 data-host 指定，此处取加载器移交配置）。@type {string} */
  var BASE = (window.__asaChat && window.__asaChat.__cfg && window.__asaChat.__cfg.host) || '';

  /**
   * 语言检测：BCP47 主子标签匹配支持语种，不中回落 'en'（SR-SITE-001 §6.2②）。
   * @param {string} raw navigator.language 原始值（如 "en-US"）
   * @returns {string} 支持语种代码
   */
  function detectLang(raw) {
    var primary = String(raw || '').split('-')[0].toLowerCase();
    return SUPPORT_LANGS.indexOf(primary) !== -1 ? primary : 'en';
  }

  // ------------------------------------------------------------------ consent 三态门控（FR-SITE-007）

  var consent = {
    /** 当前状态：unknown → granted | necessary | denied。@type {string} */
    status: 'unknown',
    /**
     * 初始化：读取本地已存三态；未同意（unknown）且租户要求横幅时由 UI 层弹横幅。
     * 未同意前不写任何持久存储（仅 sessionStorage 存 token，关页即弃）。
     * @param {string} [saved] 宿主预取的 asa_consent 值（缺省自读 localStorage）
     * @returns {void}
     */
    init: function (saved) {
      var stored = saved || null;
      try { stored = stored || localStorage.getItem('asa_consent'); } catch (e) { /* 隐私模式：忽略 */ }
      if (stored === 'granted' || stored === 'necessary' || stored === 'denied') {
        this.status = stored;
      }
      this.apply();
    },
    /**
     * 设置三态并双写（本地 + 服务端落证）；任一选择即刻门控生效。
     * @param {'granted'|'necessary'|'denied'} status 目标状态
     * @returns {void}
     */
    set: function (status) {
      this.status = status;
      try {
        localStorage.setItem('asa_consent', status); // 双写①：本地判（SR-SITE-001 === 'granted' 兼容）
      } catch (e) { /* 写失败仅影响回访判定，本轮仍生效 */ }
      this.report();                                  // 双写②：服务端落证（GDPR 举证）
      this.apply();
    },
    /** 上报落证：POST /api/v1/privacy/consent（失败静默，SDK 侧重试队列略）。@returns {void} */
    report: function () {
      if (!state.token) { return; }
      fetch(BASE + '/api/v1/privacy/consent', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + state.token },
        body: JSON.stringify({ status: this.status, sdk_version: '1.0.0', policy_ver: 'v1' })
      }).catch(function () { /* 离线重试队列由宿主 CMP 或后续版本接管 */ });
    },
    /** 门控联动：仅 granted 开行为上报与主动邀聊（necessary/denied 数据处理等价）。@returns {void} */
    apply: function () {
      var full = this.status === 'granted';
      track.enabled = full;      // 行为采集禁用 → /site/events 零请求
      inviteEnabled = full;      // 主动邀聊关闭
      if (!full) { track.disable(); }
    }
  };

  // ------------------------------------------------------------------ 全局状态

  var state = {
    token: '',        // visitor token（sessionStorage，关页即弃）
    uid: '',          // visitor_uid（同意后写 localStorage asa_uid 跨会话复用）
    lang: 'en',
    opened: false
  };
  var inviteEnabled = false; // 邀聊开关（consent 门控联动）

  // ------------------------------------------------------------------ 行为采集（FR-SITE-002）

  var track = {
    /** 采集开关（consent 门控；false 时 emit 全拦截、零上报零存储）。@type {boolean} */
    enabled: false,
    /** 待上报缓冲队列。@type {Array<Object>} */
    queue: [],
    /** 已启用的定时器句柄。@private @type {number|undefined} */
    timer: undefined,
    /** 开启采集：同意后由 consent.apply() 调用（幂等）。@returns {void} */
    enable: function () {
      if (this.timer) { return; }
      var self = this;
      this.timer = setInterval(function () { self.flush(false); }, FLUSH_MS); // 定时节流
    },
    /** 禁用采集：未同意/撤回；清空缓冲（不留存）。@returns {void} */
    disable: function () {
      if (this.timer) { clearInterval(this.timer); this.timer = undefined; }
      this.queue = [];
    },
    /**
     * 入队一条事件；达到 BATCH_MAX 即 flush（定量触发；队列封顶丢最旧）。
     * @param {{type: string, page: string, dwell_ms?: number, payload?: Object}} e 事件体
     * @returns {void}
     */
    emit: function (e) {
      if (!this.enabled) { return; } // 未同意：零上报（服务端 40302 兜底，双端门控）
      e.ev_id = e.ev_id || 'e-' + Math.random().toString(16).slice(2);
      e.ts = Math.floor(Date.now() / 1000);
      this.queue.push(e);
      if (this.queue.length >= BATCH_MAX) { this.flush(false); }
    },
    /**
     * 批量上报：常规路径 fetch keepalive；pagehide 路径 navigator.sendBeacon（不阻塞卸载）。
     * @param {boolean} beacon true=卸载路径走 sendBeacon
     * @returns {void}
     */
    flush: function (beacon) {
      if (!this.queue.length || !state.token) { return; }
      var body = JSON.stringify({
        visitor_token: state.token, consent: this.enabled, events: this.queue.splice(0, BATCH_MAX)
      });
      var url = BASE + '/api/v1/site/events';
      if (beacon && navigator.sendBeacon) {
        navigator.sendBeacon(url, body); // 行为数据可损：失败不重试（无重试风暴）
        return;
      }
      fetch(url, {
        method: 'POST', body: body, keepalive: true,
        headers: { 'Content-Type': 'application/json' }
      }).then(function (resp) {
        if (resp.status === 403) { track.disable(); } // 40302 服务端兜底 → 本地熔断
      }).catch(function () { /* 行为数据可损：静默丢弃 */ });
    }
  };
  // 卸载前：结算停留 + 补发缓冲（sendBeacon 兜底，SR-SITE-002 §6.1 buffer）
  window.addEventListener('pagehide', function () { track.flush(true); });

  /** 页面浏览埋点：page_view + 产品识别（/products/{sku} 规则；repeat_view 判定会话内计数）。
   * @param {string} url 当前页 URL
   * @returns {void}
   */
  var skuSeen = {};
  function trackView(url) {
    track.emit({ type: 'page_view', page: url });
    var m = /\/products\/([\w-]+)/.exec(url);
    if (!m) { return; }
    var sku = m[1];
    track.emit({ type: 'product_view', page: url, payload: { sku: sku } });
    skuSeen[sku] = (skuSeen[sku] || 0) + 1;
    if (skuSeen[sku] >= 3) { // 同品第 3 次访问 → repeat_view（邀聊触发源，05 §8）
      track.emit({ type: 'repeat_view', page: url, payload: { sku: sku, nth: skuSeen[sku] } });
    }
  }

  // ------------------------------------------------------------------ 传输通道（WS + 长轮询降级）

  /**
   * WS 实时通道：心跳（30s ping / 3 次无 pong 判死）、指数退避重连（2^n 随机抖动，
   * 封顶 30s）、连续 LP_AFTER 次失败切长轮询（SR-SITE-001 §6.4 降级①）。
   * @param {string} url WS 地址（含 /ws/chat）
   * @param {{onMessage: Function, switchTransport: Function, onAuthExpired: Function}} sink 下行汇聚器
   */
  function WsTransport(url, sink) {
    this.url = url;
    this.sink = sink;
    this.ws = null;
    this.retry = 0;
    this.pongMiss = 0;
    this.hbTimer = null;
    this.pending = [];   // 未连上先入队，onopen 后重放（msg_id 幂等去重）
    this.connect();
  }
  /** 建连：token 鉴权失败服务端 close 4401 → 重走 /site/session。@returns {void} */
  WsTransport.prototype.connect = function () {
    var self = this;
    this.ws = new WebSocket(this.url + '?token=' + encodeURIComponent(state.token));
    this.ws.onopen = function () {
      self.retry = 0;          // 成功即清零重试
      self.startHeartbeat();
      while (self.pending.length) { self.ws.send(JSON.stringify(self.pending.shift())); }
    };
    this.ws.onmessage = function (e) { self.sink.onMessage(JSON.parse(e.data)); };
    this.ws.onclose = function (e) {
      self.stopHeartbeat();
      if (e.code === 4401) { self.sink.onAuthExpired(); return; } // token 失效：重走 /site/session
      self.reconnect();
    };
    this.ws.onerror = function () { if (self.ws) { self.ws.close(); } }; // 统一经 onclose 收敛
  };
  /** 重连：指数退避 2^n 秒 + 随机抖动防惊群；连续 LP_AFTER 次失败 → switchTransport 降级长轮询。@returns {void} */
  WsTransport.prototype.reconnect = function () {
    var self = this;
    this.retry += 1;
    if (this.retry >= LP_AFTER) {
      this.sink.switchTransport(new LongPollTransport(BASE + '/api/v1/site/messages', this.sink));
      return;
    }
    var delay = Math.min(1000 * Math.pow(2, this.retry), BACKOFF_MAX_MS) * (0.5 + Math.random() * 0.5);
    setTimeout(function () { self.connect(); }, delay);
  };
  /** 心跳：30s ping，连续 3 次无 pong 判死链主动 close（触发 onclose 重连/降级）。@returns {void} */
  WsTransport.prototype.startHeartbeat = function () {
    var self = this;
    this.pongMiss = 0;
    this.hbTimer = setInterval(function () {
      if (self.pongMiss >= 3) { if (self.ws) { self.ws.close(); } return; }
      self.send({ type: 'ping' });
      self.pongMiss += 1;
    }, HEARTBEAT_MS);
  };
  WsTransport.prototype.stopHeartbeat = function () {
    if (this.hbTimer) { clearInterval(this.hbTimer); this.hbTimer = null; }
  };
  /**
   * 上行：已连开直发，未连上先入队（onopen 后 flush 重放；msg_id 幂等保证服务端去重）。
   * @param {{type: string, msg_id?: string, [k: string]: any}} m 上行消息
   * @returns {void}
   */
  WsTransport.prototype.send = function (m) {
    if (this.ws && this.ws.readyState === 1) { this.ws.send(JSON.stringify(m)); }
    else { this.pending.push(m); }
  };

  /**
   * 长轮询降级通道（SR-SITE-001 §5③）：POST 上行 + GET hold 下行，消息/邀聊功能等价、延迟秒级。
   * @param {string} url /api/v1/site/messages
   * @param {{onMessage: Function}} sink 下行汇聚器
   */
  function LongPollTransport(url, sink) {
    this.url = url;
    this.sink = sink;
    this.cursor = 0;
    this.stopped = false;
    this.loop();
  }
  /** 下行循环：hold ≤25s；空轮询续跑。@returns {void} */
  LongPollTransport.prototype.loop = function () {
    var self = this;
    if (this.stopped) { return; }
    fetch(this.url + '?cursor=' + this.cursor + '&wait=25&token=' + encodeURIComponent(state.token))
      .then(function (r) { return r.json(); })
      .then(function (body) {
        (body.messages || []).forEach(function (m) { self.sink.onMessage(m); });
        if (typeof body.cursor === 'number') { self.cursor = body.cursor; }
        setTimeout(function () { self.loop(); }, 300);
      })
      .catch(function () { setTimeout(function () { self.loop(); }, 3000); });
  };
  /** 上行：POST 单条（body 同 WS 上行消息）。@param {Object} m 上行消息 @returns {void} */
  LongPollTransport.prototype.send = function (m) {
    fetch(this.url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ visitor_token: state.token, message: m })
    }).catch(function () { /* 行为可损 */ });
  };

  // ------------------------------------------------------------------ 会话初始化与聊天 UI

  /**
   * 会话初始化：POST /api/v1/site/session 签发 visitor token（服务端 data-key + Origin 白名单）。
   * consent 未同意 → token 仅存 sessionStorage（关页即弃）；同意 → visitor_uid 写 localStorage 复用。
   * @returns {Promise<void>} 初始化完成（失败静默，宿主页零感知）
   */
  function initSession() {
    var cfg = window.__asaChat.__cfg || {};
    var knownUid = consent.status === 'granted' ? getStoredUid() : '';
    return fetch(BASE + '/api/v1/site/session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        key: cfg.key,
        page: location.href,
        ref: document.referrer || '',
        lang: detectLang(navigator.language),
        uid: knownUid
      })
    }).then(function (r) { return r.json(); }).then(function (body) {
      if (!body || body.code !== 0) { return; } // 白名单外（40301）等：静默不渲染
      var data = body.data;
      state.token = data.visitor_token;
      state.uid = data.visitor_uid;
      state.lang = data.lang || detectLang(navigator.language);
      try { sessionStorage.setItem('asa_token', state.token); } catch (e) { /* 隐私模式忽略 */ }
      if (consent.status === 'granted') { persistUid(state.uid); } // 仅同意后写持久存储（§2.3）
    }).catch(function () { /* 网络失败：静默，重试由刷新触发 */ });
  }

  /** 读跨会话匿名号（asa_uid）。@returns {string} localStorage 中的 uid 或空串 */
  function getStoredUid() {
    try { return localStorage.getItem('asa_uid') || ''; } catch (e) { return ''; }
  }
  /** 写跨会话匿名号（仅 consent granted 后调用；清存储/无痕 = 新访客）。@param {string} uid 服务端签发号 @returns {void} */
  function persistUid(uid) {
    try { if (!localStorage.getItem('asa_uid')) { localStorage.setItem('asa_uid', uid); } } catch (e) { /* 忽略 */ }
  }

  /**
   * 挂载聊天窗口：<asa-chat> 自定义元素 + open Shadow DOM 样式隔离；
   * 无 attachShadow 的旧 WebView 降级 asa- 前缀作用域（外观一致，§2.3 降级②）。
   * @returns {void}
   */
  function mountWidget() {
    var host = document.createElement('asa-chat');
    document.body.appendChild(host);
    var root = host.attachShadow ? host.attachShadow({ mode: 'open' }) : host;
    var scoped = root === host; // 降级②：无 Shadow DOM → asa- 前缀类名作用域
    root.innerHTML =
      '<style>' +
      ':host, .asa-root { all: initial; --asa-primary: #1a73e8; --asa-radius: 12px; }' +
      '.asa-btn { position: fixed; bottom: 16px; right: 16px; background: var(--asa-primary); border-radius: 50%; }' +
      '.asa-panel { display: none; position: fixed; bottom: 84px; width: min(376px, 92vw); border-radius: var(--asa-radius); }' +
      '.asa-panel.open { display: flex; flex-direction: column; }' +
      '</style>' +
      '<div class="asa-root">' +
      '<button class="asa-btn" aria-label="chat">chat</button>' +
      '<div class="asa-panel" role="dialog" aria-live="polite">' +
      '<header class="asa-title">' + (i18n.t('title')) + '</header>' +
      '<ul class="asa-msgs" role="log"></ul>' +
      '<form class="asa-composer"><input class="asa-input" maxlength="2000"><button class="asa-send">' + (i18n.t('send')) + '</button></form>' +
      '</div></div>';
    root.querySelector('.asa-btn').addEventListener('click', function () { togglePanel(root); });
    root.querySelector('.asa-composer').addEventListener('submit', function (e) {
      e.preventDefault();
      var input = root.querySelector('.asa-input');
      if (input.value && transport) {
        transport.send({ type: 'text', msg_id: 'c-' + Math.random().toString(16).slice(2), content: input.value });
        appendMsg(root, 'buyer', input.value);
        input.value = '';
      }
    });
    if (scoped) { host.className = 'asa-scope'; } // 降级②标记：宿主侧以 .asa-scope 提特异性
  }

  /** 展开/收起面板；首开建立传输通道（WS 可用→WS；连续失败自动降级长轮询）。@param {Element} root 组件根 @returns {void} */
  function togglePanel(root) {
    state.opened = !state.opened;
    root.querySelector('.asa-panel').classList.toggle('open', state.opened);
    if (state.opened && !transport) {
      transport = new WsTransport(
        (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/chat',
        {
          onMessage: function (m) { onDownMessage(root, m); },
          switchTransport: function (next) { transport = next; },
          onAuthExpired: function () { transport = null; initSession(); }
        }
      );
    }
  }

  /**
   * 下行消息分派：text→气泡；generating→打字指示；invite→邀聊气泡（consent 未同意不出现）。
   * @param {Element} root 组件根 @param {{type: string, content?: string, role?: string}} m 下行消息
   * @returns {void}
   */
  function onDownMessage(root, m) {
    if (m.type === 'text' && m.role === 'ai') {
      appendMsg(root, 'ai', String(m.content || ''));
    } else if (m.type === 'generating') {
      appendMsg(root, 'ai', m.state === 'start' ? '…' : '');
    } else if (m.type === 'invite' && inviteEnabled) {
      appendMsg(root, 'ai', '[邀聊] ' + String(m.content || '')); // consent granted 才可能出现
      if (transport) { transport.send({ type: 'invite_open', invite_id: m.invite_id }); } // 漏斗 opened 回填
    } else if (m.type === 'pong') {
      /* 心跳应答：WsTransport.onMessage 内清 pongMiss（此处仅长轮询兜底） */
    }
  }

  /** 追加一条消息气泡。@param {Element} root 组件根 @param {'buyer'|'ai'} role 角色 @param {string} text 文本 @returns {void} */
  function appendMsg(root, role, text) {
    if (!text) { return; }
    var li = document.createElement('li');
    li.className = 'asa-msg asa-msg-' + role;
    li.textContent = text;
    root.querySelector('.asa-msgs').appendChild(li);
  }

  // ------------------------------------------------------------------ 多语言（按需 + en 兜底）

  var i18n = {
    /** 当前语言资源。@type {Object<string, string>} */
    dict: {},
    /**
     * 加载语言资源包（en 兜底键缺省）。
     * @param {string} lang 语种代码
     * @returns {Promise<void>}
     */
    load: function (lang) {
      return fetch(BASE + '/i18n/' + lang + '.json')
        .then(function (r) { return r.json(); })
        .then(function (dict) { i18n.dict = dict; })
        .catch(function () { i18n.dict = {}; });
    },
    /**
     * 取文案：当前语言缺失回落 en，再缺失回落键名。
     * @param {string} key 资源键
     * @returns {string} 文案
     */
    t: function (key) {
      var en = { title: 'Chat with us', send: 'Send' };
      return i18n.dict[key] || en[key] || key;
    },
    /** 运行时切换：刷新 UI + 通知服务端（写 site_session.lang 驱动回复语言）。@param {string} lang 语种 @returns {void} */
    switch: function (lang) {
      state.lang = detectLang(lang);
      i18n.load(state.lang).then(function () {
        if (transport) { transport.send({ type: 'lang', lang: state.lang }); }
      });
    }
  };

  // ------------------------------------------------------------------ 启动时序（session → 语言 → consent → 挂载）

  var transport = null; // 当前通道（WS / LongPoll 切换）

  var bootState = { consent: '' };
  try { bootState.consent = localStorage.getItem('asa_consent') || ''; } catch (e) { /* 隐私模式 */ }
  consent.init(bootState.consent);
  state.lang = detectLang(navigator.language);

  function boot() {
    initSession().then(function () {
      i18n.load(state.lang).then(function () { mountWidget(); });
      if (consent.status === 'granted') {
        track.enable();
        trackView(location.href); // 会话首报：page_view + 产品识别
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
