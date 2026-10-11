<!-- 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物（FR-SC-006）。 -->
<!-- 经营看板：一屏一请求（告警待办/审批待办/接管中/今日 KPI 四卡片），15s 短缓存由 BFF 保证。 -->
<template>
  <view class="page">
    <view class="header">
      <text class="title">经营看板</text>
      <text class="subtitle">移动端一屏总览（数据每 15 秒聚合刷新）</text>
    </view>

    <view class="grid">
      <view class="cell" @click="goAlerts">
        <text class="num" :class="{ danger: (summary.todo_alerts || 0) > 0 }">
          {{ summary.todo_alerts === null ? "—" : summary.todo_alerts }}
        </text>
        <text class="label">告警待办</text>
      </view>
      <view class="cell" @click="goApprovals">
        <text class="num" :class="{ warn: (summary.todo_approvals || 0) > 0 }">
          {{ summary.todo_approvals === null ? "—" : summary.todo_approvals }}
        </text>
        <text class="label">审批待办</text>
      </view>
      <view class="cell">
        <text class="num">{{ summary.taking_over === null ? "—" : summary.taking_over }}</text>
        <text class="label">接管中会话</text>
      </view>
      <view class="cell">
        <text class="num">
          {{ summary.today && summary.today.inquiries != null ? summary.today.inquiries : "—" }}
        </text>
        <text class="label">今日询盘</text>
      </view>
    </view>

    <view v-if="summary.today" class="kpi">
      <text class="kpi-row">今日回复：{{ summary.today.replies != null ? summary.today.replies : "—" }} 条</text>
      <text class="kpi-row">今日成交额：{{ summary.today.gmv != null ? summary.today.gmv + " 美元" : "—" }}</text>
    </view>

    <button class="refresh" size="mini" plain @click="load">刷新</button>
  </view>
</template>

<script>
// #ifdef H5
// H5（钉钉微应用）：免登换发后访问，Bearer 令牌存本地存储
const API = "/api/v1/mobile";
// #endif
// #ifdef MP-WEIXIN
// 微信小程序：wx.login code 换发；合法域名需在平台后台登记
const API = "/api/v1/mobile";
// #endif

export default {
  data() {
    // 四卡片缺省：null 表示该路下游降级（端上显示「—」），不阻塞其余卡片
    return { summary: { todo_alerts: null, todo_approvals: null, taking_over: null, today: null } };
  },
  onShow() {
    this.load();
  },
  methods: {
    load() {
      uni.request({
        url: `${API}/home`,   // GET /api/v1/mobile/home 一屏聚合
        header: { Authorization: "Bearer " + (uni.getStorageSync("token") || "") },
        success: (res) => {
          if (res.statusCode === 200 && res.data) {
            this.summary = res.data;
          } else if (res.statusCode === 401) {
            // 登录态过期：回到容器重新免登（demo 提示占位）
            uni.showToast({ title: "登录态已过期，请重新进入应用", icon: "none" });
          }
        },
      });
    },
    goAlerts() {
      uni.navigateTo({ url: "/pages/alert/center" });
    },
    goApprovals() {
      uni.navigateTo({ url: "/pages/approval/list" });
    },
  },
};
</script>

<style>
.page { padding: 24rpx; background: #f5f6f8; min-height: 100vh; }
.header { margin-bottom: 20rpx; }
.title { font-size: 40rpx; font-weight: 600; color: #1f2329; }
.subtitle { display: block; font-size: 24rpx; color: #86909c; margin-top: 8rpx; }
.grid { display: flex; flex-wrap: wrap; gap: 20rpx; }
.cell { width: 315rpx; background: #ffffff; border-radius: 16rpx; padding: 32rpx 24rpx; box-sizing: border-box; }
.num { font-size: 52rpx; font-weight: 700; color: #1f2329; }
.num.danger { color: #f53f3f; }
.num.warn { color: #ff7d00; }
.label { display: block; font-size: 24rpx; color: #86909c; margin-top: 8rpx; }
.kpi { background: #ffffff; border-radius: 16rpx; padding: 24rpx; margin-top: 20rpx; }
.kpi-row { display: block; font-size: 26rpx; color: #4e5969; line-height: 48rpx; }
.refresh { margin-top: 24rpx; }
</style>
