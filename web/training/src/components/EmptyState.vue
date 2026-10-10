<!--
  知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
  说明：前端构建链路于 Phase 1 后续迭代引入，本文件为源码交付（Vue 3 SFC），
  暂不参与构建；构建链路就绪后随 web/training 工程接入。

  EmptyState.vue —— NFR-USA-03 ② 空状态引导组件：文案 + 动作按钮。
  消费 GET /api/v1/training/guidance/empty-states（?scene_id= 精确过滤），
  场景表由后端 EMPTY_STATE_REGISTRY 提供（8 类：无问答记录/无会话/无缺口/
  无模拟会话/无演练记录/无雷达数据/无学习路径/无考试记录），中文文案 + 动作建议。

  设计口径（SR-NFR-USA-03 §6.3 GuideEmpty）：
  - 空数据 ≠ 空页面：给「下一步动作」按钮，让新人知道从哪开始；
  - 示例数据预览（sample_preview 文案位）：让新人先「看到成品」；
  - 降级：接口异常/场景未命中 → 内置默认文案，不阻塞页面（引导层故障零可见）；
  - data-tour 锚点：组件根挂 data-tour="empty-guide"（可被导览步骤引用）。
-->
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

/** 空状态场景（后端 EmptyStateSpec 的前端投影，字段同名） */
interface EmptyStateSpec {
  scene_id: string
  page_route: string
  title: string
  hint: string
  action_label: string
  action_route: string
  sample_preview: string
  reserved: boolean
}

const props = defineProps<{
  /** 场景标识（如 tutor_no_qa / gap_empty；见后端 EMPTY_STATE_REGISTRY 8 类） */
  sceneId: string
}>()

const router = useRouter()
const scene = ref<EmptyStateSpec | null>(null)
const loading = ref(true)

/** 接口异常/未命中时的内置兜底文案（降级链最末级，保证永不空白，SR §6.5 同思路） */
const fallbackScene: EmptyStateSpec = computed(() => ({
  scene_id: props.sceneId,
  page_route: '/',
  title: '这里还没有数据',
  hint: '完成第一个操作后，这里就会展示你的记录。',
  action_label: '去工作台',
  action_route: '/home',
  sample_preview: '',
  reserved: false,
}))

const view = computed<EmptyStateSpec>(() => scene.value ?? fallbackScene.value)

onMounted(async () => {
  try {
    const resp = await fetch(
      `/api/v1/training/guidance/empty-states?scene_id=${encodeURIComponent(props.sceneId)}`,
    )
    const envelope = await resp.json()
    if (envelope.code === 0 && Array.isArray(envelope.data?.scenes) && envelope.data.scenes.length > 0) {
      scene.value = envelope.data.scenes[0] as EmptyStateSpec
    }
    // 未命中 → 保持 null，渲染走兜底文案（不视为错误）
  } catch {
    /* 网络异常：保持 null，兜底文案生效——引导层故障不产生用户可见错误 */
  } finally {
    loading.value = false
  }
})

/** 动作按钮：跳转到场景建议的下一步路由（下一步建议是空态引导的核心价值） */
function goNext(): void {
  void router.push(view.value.action_route)
}
</script>

<template>
  <div class="empty-state-guide" data-tour="empty-guide">
    <!-- 示例数据预览文案位：让新人先「看到成品」（合成脱敏样例，不含真实客户数据） -->
    <p v-if="view.sample_preview" class="empty-state-guide__preview">
      {{ view.sample_preview }}
    </p>
    <h3 class="empty-state-guide__title">{{ view.title }}</h3>
    <p class="empty-state-guide__hint">{{ view.hint }}</p>
    <!-- 预留场景徽标：对应功能 Phase 1 后续迭代开放，文案口径为预告 -->
    <span v-if="view.reserved" class="empty-state-guide__reserved">后续迭代开放</span>
    <button type="button" class="empty-state-guide__action" :disabled="loading" @click="goNext">
      {{ view.action_label }}
    </button>
  </div>
</template>
