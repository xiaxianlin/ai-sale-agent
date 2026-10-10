<!--
  知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
  说明：前端构建链路于 Phase 1 后续迭代引入，本文件为源码交付（Vue 3 SFC），
  暂不参与构建；构建链路就绪后随 web/training 工程接入。

  GuidanceTour.vue —— NFR-USA-03 ① 首次登录分步导览（driver.js 用法源码）。
  流程：拉取 tour 配置（GET /api/v1/training/guidance/tour）→ should_show 克制门控
  判断（本地 localStorage + 服务端版本比较双保险）→ driver.js 渲染（高亮元素 +
  气泡 + 步骤进度）→ 导览销毁后上报状态（POST /api/v1/training/guidance/tour/state）。

  对接契约（与 src/services/training/core/guidance/ 同源）：
  - 锚点规约：步骤 target_selector 一律为 [data-tour='xxx']（CI 守护门禁，
    改版删锚点而未同步导览配置 → 后端模型校验 + 测试层断言双重拦截）；
  - localStorage 键名：guide:seen:{trainee_id}:{tour_id}，
    JSON 形态 {"major":1,"minor":0,"dismissed":true,"done":false,"at":1760000000000}；
  - 重显规则：已看主版本 < 当前主版本（界面改版）→ 重显一次；次版本升级不打扰；
  - 降级：配置拉取/校验失败或 driver.js 加载失败 → 静默跳过，页面功能零影响。

  依据：SR-NFR-USA-03-引导式交互.md §6.1（配置）/§6.2（useTour 门控与编排）。
-->
<script setup lang="ts">
import { inject, onMounted, ref } from 'vue'
import type { Router } from 'vue-router'

/** 步骤/导览配置的最小类型（后端 TourStep/TourConfig 的前端投影，字段同名） */
interface TourStep {
  seq: number
  route: string
  target_selector: string
  title: string
  body: string
  action: 'view' | 'click' | 'input' | 'navigate'
  skippable: boolean
}
interface TourConfig {
  tour_id: string
  version: string
  title: string
  enabled: boolean
  steps: TourStep[]
}
interface TourView {
  tour: TourConfig
  current_version: string
  tour_id: string
  seen_version: string | null
  major_changed: boolean
  should_show: boolean
}

const props = defineProps<{
  traineeId: number
  tourId?: string
  /** 启动延迟（ms）：首屏 data-perf-ready 就绪后再弹，不与骨架屏抢焦点（SR §6.2） */
  startDelayMs?: number
}>()

const emit = defineEmits<{ (e: 'finished', done: boolean): void }>()

/** 跨路由续走所需的路由器（inject 注入；脱离路由上下文时为 null，导览只在当前路由内走） */
const router = inject<Router | null>('$router', null)

const active = ref(false)
/** driver.js 实例的最小接口投影（源码交付期不引入其类型包） */
interface DriverLike {
  drive: () => void
  moveNext: () => void
  destroy: () => void
}
let currentDriver: DriverLike | null = null

const TOUR_API = '/api/v1/training/guidance'

/** localStorage 键名契约：guide:seen:{trainee_id}:{tour_id}（core/guidance/state.py 同源） */
function seenKey(traineeId: number, tourId: string): string {
  return `guide:seen:${traineeId}:${tourId}`
}

/** 本地已看态（契约 JSON 形态；解析失败按未看过处理，引导不因脏缓存而丢失） */
function readSeen(traineeId: number, tourId: string): { major: number; dismissed: boolean } | null {
  try {
    const raw = window.localStorage.getItem(seenKey(traineeId, tourId))
    if (!raw) return null
    const parsed = JSON.parse(raw) as { major?: number; dismissed?: boolean }
    if (typeof parsed.major !== 'number') return null
    return { major: parsed.major, dismissed: Boolean(parsed.dismissed) }
  } catch {
    return null
  }
}

/** 写本地已看态（与服务端上报同步进行，双口径一致） */
function writeSeen(traineeId: number, tourId: string, version: string, done: boolean): void {
  const [major, minor] = version.split('.').map(Number)
  window.localStorage.setItem(
    seenKey(traineeId, tourId),
    JSON.stringify({ major, minor, dismissed: true, done, at: Date.now() }),
  )
}

/** 解析版本主号（"1.0" → 1；防御非法串返回 0） */
function majorOf(version: string): number {
  const major = Number(version.split('.')[0])
  return Number.isFinite(major) ? major : 0
}

/** 拉取当前导览 + 版本比较（服务端门控结论 should_show 为权威口径） */
async function fetchTourView(traineeId: number, tourId: string): Promise<TourView | null> {
  try {
    const resp = await fetch(
      `${TOUR_API}/tour?tour_id=${encodeURIComponent(tourId)}&trainee_id=${traineeId}`,
    )
    const envelope = await resp.json()
    if (envelope.code !== 0) return null // 静默降级：引导层故障不影响页面（SR §8）
    return envelope.data as TourView
  } catch {
    return null
  }
}

/** 上报导览终态（跳过/完成同权重，SR §6.2「不区分对待」） */
async function reportState(
  traineeId: number,
  tourId: string,
  outcome: 'done' | 'skipped',
  version: string,
  lastStepSeq: number | null,
): Promise<void> {
  try {
    await fetch(`${TOUR_API}/tour/state`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        trainee_id: traineeId,
        tour_id: tourId,
        outcome,
        seen_version: version,
        last_step_seq: lastStepSeq,
      }),
    })
  } catch {
    /* 上报失败不重试不打扰：本地 localStorage 已兜底，服务端下次仍会给出正确门控 */
  }
}

/** 等待目标路由页面的锚点元素挂载（跨路由续走：页面就绪后再走下一步） */
function waitForAnchor(selector: string, timeoutMs = 5000): Promise<boolean> {
  return new Promise((resolve) => {
    const startedAt = Date.now()
    const timer = window.setInterval(() => {
      if (document.querySelector(selector)) {
        window.clearInterval(timer)
        resolve(true)
      } else if (Date.now() - startedAt > timeoutMs) {
        window.clearInterval(timer)
        resolve(false) // 超时：锚点缺失说明界面改版未同步导览，停在本步不硬走
      }
    }, 100)
  })
}

/**
 * 自动触发入口（首登/路由就绪后调用）——克制门控：
 * 1. 服务端 should_show（无记录/未终结→显示；已终结且主版本未变→不显示）；
 * 2. 本地 localStorage 同规则复核（双保险，接口异常时本地口径仍生效）；
 * 3. 手动入口 startManual() 绕过本门控（页脚「使用指引」是用户权利）。
 */
async function maybeStart(): Promise<void> {
  const tourId = props.tourId ?? 'first-login'
  const view = await fetchTourView(props.traineeId, tourId)
  if (!view || !view.tour.enabled || view.tour.steps.length === 0) return
  if (!view.should_show) return
  const seen = readSeen(props.traineeId, tourId)
  if (seen?.dismissed && seen.major >= majorOf(view.current_version)) return
  await start(view.tour)
}

/** 手动入口：直接渲染导览（不受门控限制，SR §6.2 start()） */
async function startManual(): Promise<void> {
  const tourId = props.tourId ?? 'first-login'
  const view = await fetchTourView(props.traineeId, tourId)
  if (view?.tour.enabled) await start(view.tour)
}

/** driver.js 渲染：步骤映射 + 跨路由续走 + 销毁回执（SR §6.2 编排工程化） */
async function start(config: TourConfig): Promise<void> {
  if (active.value) return
  // 动态加载 driver.js：独立异步 chunk，不进首包（NFR-PER-05 预算纪律）；
  // 加载失败（依赖未安装/网络异常）→ 静默放弃本次导览，页面功能零影响。
  let createDriver: (options: Record<string, unknown>) => DriverLike
  try {
    createDriver = (await import('driver.js')).default as typeof createDriver
  } catch {
    return
  }
  active.value = true

  let done = false // 终态：完整走完=done=true；关闭/中断=skipped
  let lastStepSeq: number | null = null // 中断步骤号（流失分析上报字段）

  const stepsForDriver = config.steps.map((step, index) => ({
    element: step.target_selector, // 一律 [data-tour=...] 锚点（CI 守护规约）
    popover: {
      title: `${step.title}（${step.seq}/${config.steps.length}）`,
      description: step.body,
      showButtons: step.seq > 1, // 首步无「上一步」
      onNextClick: async () => {
        lastStepSeq = step.seq
        const next = config.steps[index + 1]
        if (!next) {
          // 末步点「完成」：标记完成态并销毁（onDestroyed 统一回执上报）
          done = true
          currentDriver?.destroy()
          return
        }
        // 跨路由步骤：先导航、等锚点挂载后续走，避免气泡指向空元素
        if (router && next.route !== step.route) {
          void router.push(next.route)
          const ready = await waitForAnchor(next.target_selector)
          if (!ready) return // 未就绪：停在本步，用户可重试或退出
        }
        currentDriver?.moveNext()
      },
    },
  }))

  currentDriver = createDriver({
    showProgress: true, // 「2/7」步骤进度（SR §6.2）
    allowClose: true, // 点遮罩可关（克制：退出永远可用）
    nextBtnText: '下一步',
    prevBtnText: '上一步',
    doneBtnText: '完成',
    steps: stepsForDriver,
    onDestroyed: () => {
      active.value = false
      currentDriver = null
      // 销毁即终态：完成/跳过同权重，写本地 + 上报服务端（口径一致）
      writeSeen(props.traineeId, config.tour_id, config.version, done)
      void reportState(
        props.traineeId,
        config.tour_id,
        done ? 'done' : 'skipped',
        config.version,
        lastStepSeq,
      )
      emit('finished', done)
    },
  })
  currentDriver.drive()
}

// 启动时机：首屏就绪后延迟触发（默认 800ms，SR §6.2 guide.tour.start_delay_ms）
onMounted(() => {
  window.setTimeout(() => {
    void maybeStart()
  }, props.startDelayMs ?? 800)
})

defineExpose({ maybeStart, startManual })
</script>

<template>
  <!-- 导览组件本身不渲染内容：driver.js 直接操作 DOM 高亮与气泡 -->
  <span class="guidance-tour-host" aria-hidden="true"></span>
</template>
