<script setup lang="ts">
/**
 * 一块界面的错误边界。
 *
 * 存在的理由：子组件在 setup/渲染里抛错时，Vue 会把它整块卸掉——用户看到的是
 * 「这块区域自己消失了」，而打包版里前端异常不落任何日志，事后查不到任何线索
 * （2026-11-03 用户报「首页中间的媒体输入区自己消失了」就是这么卡住的）。
 * 现在：拦下错误 → 记进 hancast.log → 原地显示一条可见的失败提示 + 重试按钮，
 * 重试由父组件换 key 重新挂载这一块。
 */
import { onErrorCaptured, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { frontendLog } from '@/api/commands'

const props = defineProps<{ label: string }>()
const emit = defineEmits<{ (e: 'retry'): void }>()

const { t } = useI18n()
const failed = ref('')

onErrorCaptured((err, _instance, info) => {
  const e = err as { message?: string; stack?: string } | undefined
  failed.value = e?.message || String(err)
  void frontendLog({
    level: 'error',
    message: `界面区块「${props.label}」渲染失败（${info || '未知阶段'}）：${failed.value}`,
    stack: e?.stack,
    source: 'section',
  })
  // 不再往上抛：上面还有一层 app.config.errorHandler，重复记一遍没意义，
  // 而且抛上去会让整页白掉——一块坏掉不该拖垮整个首页。
  return false
})

function onRetry() {
  failed.value = ''
  emit('retry')
}
</script>

<template>
  <div v-if="failed" class="section-fail" role="alert">
    <!-- 区块名单独成节点、不走 t() 的插值：文案里带参数时，自定义渲染器 / 测试宿主下
         曾出现插值参数丢空的情况，把标签和固定文案拆开渲染就不受这条影响 -->
    <p>
      <strong>{{ label }}</strong>
      <span class="section-fail__detail">· {{ t('home.sectionFailed') }} · {{ failed }}</span>
    </p>
    <button type="button" @click="onRetry">{{ t('home.retry') }}</button>
  </div>
  <slot v-else />
</template>

<style scoped>
.section-fail {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 12px;
  border-radius: var(--r-md);
  border: 1px dashed var(--danger);
  background: var(--danger-light);
  color: var(--danger);
  font-size: 12px;
  flex-shrink: 0;
}

.section-fail p {
  flex: 1;
  min-width: 0;
  overflow-wrap: anywhere;
}

.section-fail__detail {
  opacity: 0.8;
}

.section-fail button {
  flex-shrink: 0;
  padding: 5px 10px;
  font-size: 12px;
  color: var(--danger);
  background: transparent;
  border: 1px solid var(--danger);
  border-radius: var(--r-sm);
  cursor: pointer;
  font-family: var(--font);
}
</style>
