<!-- 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物（FR-SC-006）。 -->
<!-- 告警中心：异常卡片置顶（down/warn 高亮）、一键认领/去处理；数据源 /api/v1/mobile/home 聚合告警待办。 -->
<template>
  <view class="page">
    <view class="header">
      <text class="title">告警中心</text>
      <text class="subtitle">实例失联 / IP 异常 / 低置信兜底待办</text>
    </view>

    <view v-if="loading" class="tip">加载中…</view>
    <view v-else-if="alerts.length === 0" class="tip">暂无待办告警，一切正常</view>

    <view
      v-for="item in alerts"
      :key="item.id"
      class="card"
      :class="{ danger: item.level === 'high', warn: item.level === 'mid' }"
    >
      <view class="card-head">
        <text class="card-title">{{ item.title }}</text>
        <text class="badge">{{ levelText(item.level) }}</text>
      </view>
      <text class="card-desc">{{ item.account_name }} · {{ item.at }}</text>
      <view class="actions">
        <!-- #ifdef H5 -->
        <!-- 钉钉微应用容器：深链跳转集群状态墙详情 -->
        <button size="mini" @click="goDetail(item)">去处理</button>
        <!-- #endif -->
        <!-- #ifdef MP-WEIXIN -->
        <!-- 微信小程序端：页面内跳转 -->
        <button size="mini" @click="goDetail(item)">查看详情</button>
        <!-- #endif -->
        <button size="mini" plain @click="claim(item)">认领</button>
      </view>
    </view>
  </view>
</template>

<script>
// #ifdef H5
// H5（钉钉微应用）：免登凭据由容器注入，经后端 /api/v1/mobile/auth 换发会话令牌
const ENTRY = "/pages/alert/center";
// #endif
// #ifdef MP-WEIXIN
// 微信小程序：登录 code 由 wx.login 取得，同样走 /api/v1/mobile/auth 换发
const ENTRY = "/pages/alert/center";
// #endif

export default {
  data() {
    return { loading: false, alerts: [] };
  },
  onShow() {
    this.loadAlerts();
  },
  methods: {
    levelText(level) {
      // 告警级别中文化（NFR-USA-02）
      return { high: "紧急", mid: "重要", low: "提示" }[level] || "提示";
    },
    loadAlerts() {
      this.loading = true;
      uni.request({
        // 一屏聚合接口（BFF 短缓存）；告警明细由 alerts 路由扩展返回
        url: "/api/v1/mobile/home",
        header: { Authorization: "Bearer " + (uni.getStorageSync("token") || "") },
        success: (res) => {
          this.alerts = (res.data && res.data.todo_alerts != null)
          ? this.mockFromCount(res.data.todo_alerts)
          : [];
        },
        complete: () => { this.loading = false; },
      });
    },
    mockFromCount(count) {
      // demo 口径：聚合接口仅回数量，占位展开（生产 = /api/v1/alerts 列表分页）
      return Array.from({ length: Number(count) || 0 }, (_, i) => ({
        id: `demo-${i}`, title: "实例心跳失联待处理", account_name: "示例账号",
        level: i === 0 ? "high" : "mid", at: "刚刚",
      }));
    },
    goDetail(item) {
      uni.navigateTo({ url: `/pages/dashboard/index?focus=${item.id}` });
    },
    claim(item) {
      // 认领动作走待办通道（生产 = FR-AG-007 告警认领）
      uni.showToast({ title: `已认领：${item.title}`, icon: "success" });
    },
  },
};
</script>

<style>
.page { padding: 24rpx; background: #f5f6f8; min-height: 100vh; }
.header { margin-bottom: 20rpx; }
.title { font-size: 40rpx; font-weight: 600; color: #1f2329; }
.subtitle { display: block; font-size: 24rpx; color: #86909c; margin-top: 8rpx; }
.tip { text-align: center; color: #86909c; padding: 80rpx 0; font-size: 28rpx; }
.card { background: #ffffff; border-radius: 16rpx; padding: 24rpx; margin-bottom: 20rpx; }
.card.danger { border-left: 8rpx solid #f53f3f; }
.card.warn { border-left: 8rpx solid #ff7d00; }
.card-head { display: flex; justify-content: space-between; align-items: center; }
.card-title { font-size: 30rpx; font-weight: 600; color: #1f2329; }
.badge { font-size: 22rpx; color: #f53f3f; }
.card-desc { display: block; font-size: 24rpx; color: #86909c; margin: 12rpx 0; }
.actions { display: flex; gap: 16rpx; }
</style>
