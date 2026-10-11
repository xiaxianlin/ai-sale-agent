<!-- 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物（FR-SC-006）。 -->
<!-- 待办审批：敏感操作（改价/订单等）移动端确认——verify_token 二次校验强制流（SRS §7.4 红线端上加严）。 -->
<template>
  <view class="page">
    <view class="header">
      <text class="title">待办审批</text>
      <text class="subtitle">高危操作 AI 只起草，人工二次校验后确认</text>
    </view>

    <view v-if="loading" class="tip">加载中…</view>
    <view v-else-if="items.length === 0" class="tip">暂无待审批事项</view>

    <view v-for="item in items" :key="item.id" class="card">
      <text class="card-title">{{ item.title }}</text>
      <text class="card-desc">{{ item.summary }}</text>
      <view class="actions">
        <button size="mini" type="primary" @click="startVerify(item)">同意</button>
        <button size="mini" type="warn" plain @click="reject(item)">驳回</button>
      </view>
    </view>

    <!-- 二次校验弹层：校验码 → verify_token → confirm -->
    <view v-if="verifying" class="mask">
      <view class="dialog">
        <text class="dialog-title">二次校验</text>
        <text class="dialog-desc">敏感操作需输入校验码确认（120 秒内有效，单次使用）</text>
        <input v-model="code" class="code-input" type="number" maxlength="6" placeholder="请输入 6 位校验码" />
        <view class="actions">
          <button size="mini" type="primary" :disabled="code.length !== 6" @click="confirm">确认执行</button>
          <button size="mini" plain @click="verifying = false">取消</button>
        </view>
      </view>
    </view>
  </view>
</template>

<script>
// #ifdef H5
// H5（钉钉微应用）：令牌经 /api/v1/mobile/auth 免登换发后存本地存储
const API = "/api/v1/mobile";
// #endif
// #ifdef MP-WEIXIN
// 微信小程序：wx.login code → /api/v1/mobile/auth 换发；域名需配置合法请求域
const API = "/api/v1/mobile";
// #endif

function authHeader() {
  return { Authorization: "Bearer " + (uni.getStorageSync("token") || "") };
}

export default {
  data() {
    return { loading: false, items: [], verifying: false, current: null, code: "" };
  },
  onShow() {
    this.load();
  },
  methods: {
    load() {
      this.loading = true;
      uni.request({
        url: `${API}/home`,   // 一屏聚合：todo_approvals 待办数（生产叠加审批列表分页）
        header: authHeader(),
        success: (res) => {
          const count = res.data && res.data.todo_approvals != null ? Number(res.data.todo_approvals) : 0;
          this.items = Array.from({ length: count }, (_, i) => ({
            id: `sa-demo-${i}`, title: "改价起草待确认", summary: "客户询价调整，AI 已起草改价单（demo 占位）",
          }));
        },
        complete: () => { this.loading = false; },
      });
    },
    startVerify(item) {
      // 第一步：请求下发校验码（POST /api/v1/mobile/verify/challenge）
      this.current = item;
      uni.request({
        url: `${API}/verify/challenge`,
        method: "POST",
        header: authHeader(),
        success: () => {
          this.code = "";
          this.verifying = true;
          uni.showToast({ title: "校验码已下发", icon: "none" });
        },
      });
    },
    confirm() {
      // 第二步：校验码换一次性 verify_token（POST /api/v1/mobile/verify/token）
      uni.request({
        url: `${API}/verify/token`,
        method: "POST",
        header: authHeader(),
        data: { answer: this.code },
        success: (res) => {
          const verify_token = res.data && res.data.verify_token;
          if (!verify_token) {
            uni.showToast({ title: "校验码错误或已过期", icon: "none" });
            return;
          }
          // 第三步：verify_token 随 confirm 提交（服务端单次消费，重放一律拒绝）
          uni.request({
            url: `${API}/approvals/${this.current.id}/confirm`,
            method: "POST",
            header: { ...authHeader(), "X-Idempotency-Key": `confirm-${this.current.id}-${Date.now()}` },
            data: { verify_token, note: "移动端已二次校验" },
            success: (done) => {
              this.verifying = false;
              uni.showToast({
                title: done.statusCode === 200 ? "已确认执行" : "确认失败，请重试",
                icon: done.statusCode === 200 ? "success" : "none",
              });
              this.load();
            },
          });
        },
      });
    },
    reject(item) {
      // 驳回：低风险免二次校验（服务端仍鉴权 + 幂等）
      uni.request({
        url: `${API}/approvals/${item.id}/reject`,
        method: "POST",
        header: { ...authHeader(), "X-Idempotency-Key": `reject-${item.id}-${Date.now()}` },
        data: { reason: "移动端驳回：信息存疑" },
        success: () => {
          uni.showToast({ title: "已驳回", icon: "success" });
          this.load();
        },
      });
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
.card-title { font-size: 30rpx; font-weight: 600; color: #1f2329; }
.card-desc { display: block; font-size: 24rpx; color: #86909c; margin: 12rpx 0 20rpx; }
.actions { display: flex; gap: 16rpx; }
.mask { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.45); display: flex; align-items: center; justify-content: center; }
.dialog { width: 600rpx; background: #ffffff; border-radius: 20rpx; padding: 40rpx; }
.dialog-title { font-size: 34rpx; font-weight: 600; color: #1f2329; }
.dialog-desc { display: block; font-size: 24rpx; color: #86909c; margin: 16rpx 0 24rpx; }
.code-input { border: 2rpx solid #e5e6eb; border-radius: 12rpx; padding: 16rpx; margin-bottom: 24rpx; font-size: 32rpx; letter-spacing: 8rpx; }
</style>
