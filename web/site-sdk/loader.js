/*
 * 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
 *
 * site-sdk 加载器（sdk.js / loader.js，ES5，<2KB gzip）——FR-SITE-001 一行接入唯一入口。
 *
 * 接入形态（宿主页一行）：
 *   <script src="https://<域名>/sdk.js" data-key="pk_site_xxx" data-host="https://site-api" defer></script>
 *
 * 职责边界（SR-SITE-001 §6.1）：只做三件事——
 *   ① 读取 data-* 配置（data-key 接入密钥 / data-host API 域，缺省同源）；
 *   ② 建立唯一全局命名空间 window.__asaChat（预初始化调用队列，宿主可提前入队）；
 *   ③ 异步注入主体 chat.js（不阻塞 LCP；加载失败静默退出，宿主页零感知）。
 *
 * ES5 约束（§2.3 降级③基线）：无箭头函数、无块级作用域变量声明、无模板字符串，任何环境可安全执行。
 */
(function () {
  'use strict';
  // ① 定位自身 <script> 标签，读取 data-* 配置（currentScript 缺失时按 src 兜底查找）
  var tag = document.currentScript || (function () {
    var all = document.getElementsByTagName('script');
    for (var i = all.length - 1; i >= 0; i--) {
      if (all[i].src && all[i].src.indexOf('/sdk.js') !== -1) { return all[i]; }
    }
    return null;
  })();
  var cfg = {
    key: tag ? tag.getAttribute('data-key') : '',          // 接入密钥（缺失则主体静默不启动）
    host: tag ? (tag.getAttribute('data-host') || '') : '' // API 域名，缺省同源推导
  };
  // ② 唯一全局命名空间：预初始化调用队列（__asaChat('track','order','SKU-1') 形态）
  var queue = [];
  window.__asaChat = function () {
    queue.push([].slice.call(arguments));
  };
  window.__asaChat.q = queue;       // 队列私有移交主体
  window.__asaChat.__cfg = cfg;     // 配置私有移交主体
  // ③ 异步注入主体：不阻塞宿主页解析与 LCP；失败静默（不抛错、不提示）
  var s = document.createElement('script');
  s.async = true;
  s.src = cfg.host + '/chat.js?v=1';
  s.onerror = function () { /* 主体加载失败：静默退出，宿主页零感知（§2.3 降级③） */ };
  (document.body || document.documentElement).appendChild(s);
})();
