<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'

const box = ref<HTMLElement | null>(null)
let previousFocus: HTMLElement | null = null

const props = withDefaults(defineProps<{
  visible: boolean
  title?: string
  /** Show circular icon area above title */
  showIcon?: boolean
  /** Modal size variant */
  size?: 'default' | 'small' | 'mini' | 'wide'
}>(), {
  size: 'default'
})

const emit = defineEmits<{
  (e: 'close'): void
}>()

function onOverlay(e: MouseEvent) {
  if ((e.target as HTMLElement).classList.contains('modal-overlay')) {
    emit('close')
  }
}

function onKeydown(e: KeyboardEvent) {
  const overlays = document.querySelectorAll('.modal-overlay')
  if (box.value?.parentElement !== overlays[overlays.length - 1]) return
  if (e.key === 'Escape') {
    e.preventDefault()
    e.stopPropagation()
    emit('close')
  } else if (e.key === 'Tab') {
    const targets = Array.from(box.value.querySelectorAll<HTMLElement>(
      'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex="0"]',
    )).filter(el => el.getClientRects().length > 0)
    const first = targets[0]
    const last = targets[targets.length - 1]
    if (!first) {
      e.preventDefault()
      box.value.focus()
    } else if (e.shiftKey && (document.activeElement === first || !targets.includes(document.activeElement as HTMLElement))) {
      e.preventDefault()
      last.focus()
    } else if (!e.shiftKey && (document.activeElement === last || !targets.includes(document.activeElement as HTMLElement))) {
      e.preventDefault()
      first.focus()
    }
  }
}

function releaseFocus() {
  document.removeEventListener('keydown', onKeydown)
  if (previousFocus?.isConnected) previousFocus.focus()
  previousFocus = null
}

watch(() => props.visible, async (visible) => {
  if (!visible) {
    releaseFocus()
    return
  }
  previousFocus = document.activeElement as HTMLElement
  document.addEventListener('keydown', onKeydown)
  await nextTick()
  if (props.visible) box.value?.focus()
}, { immediate: true })

onBeforeUnmount(releaseFocus)
</script>

<template>
  <Teleport to="body">
    <Transition name="modal">
      <div v-if="visible" class="modal-overlay" @click="onOverlay">
        <div ref="box" class="modal-box" role="dialog" aria-modal="true" :aria-label="title" tabindex="-1" :class="{ 'modal-box--small': size === 'small', 'modal-box--mini': size === 'mini', 'modal-box--wide': size === 'wide' }">

          <!-- Optional circular icon area -->
          <div v-if="showIcon" class="modal-icon-area">
            <div class="modal-icon-circle">
              <slot name="icon">
                <svg viewBox="0 0 24 24" width="36" height="36" fill="none" stroke="#5B9BF5" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                  <rect x="2" y="3" width="20" height="14" rx="2" />
                  <path d="M8 21h8M12 17v4" />
                </svg>
              </slot>
              <div class="modal-icon-badge">
                <svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="#5B9BF5" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M2 16.1A5 5 0 015.9 20M2 12.05A9 9 0 019.95 20M2 8V6a2 2 0 012-2h16a2 2 0 012 2v12a2 2 0 01-2 2h-6" />
                  <circle cx="2" cy="20" r="1" fill="#5B9BF5" />
                </svg>
              </div>
            </div>
          </div>

          <div v-if="title" class="modal-title">{{ title }}</div>
          <div class="modal-body">
            <slot />
          </div>
          <div class="modal-actions">
            <slot name="actions" />
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(15, 12, 26, 0.48);
  padding: 16px;
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}

.modal-box {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--r-xl);
  padding: 20px;
  width: 360px;
  min-width: 0;
  max-width: 100%;
  max-height: 100%;
  overflow-y: auto;
  overflow-wrap: anywhere;
  box-shadow: var(--shadow-lg);
  outline: none;
}

/* Wide variant: 日志查看这类需要横向空间的内容 */
.modal-box--wide {
  width: 760px;
}

.modal-box--wide .modal-title {
  text-align: left;
}

.modal-box--wide .modal-body {
  text-align: left;
}

/* Small variant */
.modal-box--small {
  width: 260px;
  padding: var(--sp-md);
  border-radius: 14px;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.18);
}

.modal-box--small .modal-title {
  font-size: 14px;
  margin-bottom: var(--sp-sm);
  text-align: left;
}

.modal-box--small .modal-body {
  font-size: 12px;
  margin-bottom: var(--sp-md);
}

.modal-box--small .modal-actions button {
  padding: var(--sp-xs) var(--sp-sm);
  font-size: 12px;
}

/* Mini variant */
.modal-box--mini {
  width: 280px;
  padding: 16px;
  border-radius: var(--r-lg);
  box-shadow: 0 6px 20px rgba(0, 0, 0, 0.15);
}

.modal-box--mini .modal-title {
  font-size: 13px;
  font-weight: 600;
  text-align: left;
  margin-bottom: 4px;
}

.modal-box--mini .modal-body {
  font-size: 12px;
  text-align: left;
  margin-bottom: 6px;
}

.modal-box--mini .modal-actions {
  gap: 4px;
  margin-top: 6px;
}

.modal-box--mini .modal-actions button {
  padding: 3px 8px;
  font-size: 11px;
  border-radius: 4px;
}

/* ── Circular icon area ── */
.modal-icon-area {
  display: flex;
  justify-content: center;
  margin-bottom: 16px;
}

.modal-icon-circle {
  position: relative;
  width: 88px;
  height: 88px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #EBF3FE;
  border-radius: 50%;
}

/* Ripple rings expanding outward */
.modal-icon-circle::before,
.modal-icon-circle::after {
  content: '';
  position: absolute;
  inset: 0;
  border-radius: 50%;
  border: 2px solid #5B9BF5;
  opacity: 0;
  animation: ripple 3s ease-out infinite;
}
.modal-icon-circle::after {
  animation-delay: 1.2s;
}

.modal-icon-badge {
  position: absolute;
  bottom: 4px;
  right: 4px;
  width: 22px;
  height: 22px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: white;
  border: 2px solid #EBF3FE;
  border-radius: 50%;
}

.modal-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary);
  text-align: center;
  margin-bottom: var(--sp-sm);
}

.modal-body {
  font-size: 13px;
  color: var(--text-secondary);
  text-align: center;
  margin-bottom: var(--sp-xl);
}

.modal-body input {
  width: 100%;
  padding: var(--sp-sm) var(--sp-md);
  font-size: 13px;
  color: var(--text-primary);
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  outline: none;
  transition: border-color var(--transition-fast);
  font-family: var(--font);
}

.modal-body input:focus {
  border-color: var(--border-focus);
}

.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--sp-sm);
}

.modal-actions :slotted(button) {
  padding: var(--sp-sm) var(--sp-lg);
  font-size: 13px;
  font-weight: 500;
  border-radius: var(--r-md);
  transition: all var(--transition-fast);
  font-family: var(--font);
}

.modal-actions :slotted(button.btn-cancel) {
  color: var(--text-secondary);
  background: var(--bg-secondary);
}

.modal-actions :slotted(button.btn-cancel:hover) {
  background: var(--border);
}

.modal-actions :slotted(button.btn-confirm) {
  color: var(--on-primary);
  background: var(--primary);
}

.modal-actions :slotted(button.btn-confirm:hover) {
  background: var(--primary-hover);
}

.modal-actions :slotted(button.btn-danger) {
  color: white;
  background: #EF4444;
}

.modal-actions :slotted(button.btn-danger:hover) {
  background: #DC2626;
}

/* ── Transition with icon bounce ── */
.modal-enter-active,
.modal-leave-active {
  transition: opacity var(--transition-normal);
}
.modal-enter-active .modal-box,
.modal-leave-active .modal-box {
  transition: transform var(--transition-normal);
}
.modal-enter-from,
.modal-leave-to {
  opacity: 0;
}
.modal-enter-from .modal-box {
  transform: scale(0.92) translateY(12px);
}
.modal-leave-to .modal-box {
  transform: scale(0.92) translateY(12px);
}

/* Icon area animations */
.modal-enter-active .modal-icon-area {
  animation: icon-area-in 0.5s cubic-bezier(0.34, 1.56, 0.64, 1) 0.05s both;
}
.modal-enter-active .modal-icon-circle {
  animation: icon-pop 0.5s cubic-bezier(0.34, 1.56, 0.64, 1) 0.15s both;
}
.modal-enter-active .modal-icon-badge {
  animation: badge-pop 0.35s cubic-bezier(0.34, 1.56, 0.64, 1) 0.4s both;
}

/* Idle floating animation for the circle */
.modal-enter-active .modal-icon-circle {
  animation: icon-pop 0.5s cubic-bezier(0.34, 1.56, 0.64, 1) 0.15s both,
             icon-float 3s ease-in-out 0.7s infinite;
}

@keyframes icon-area-in {
  0% { transform: translateY(-20px) scale(0.8); opacity: 0; }
  100% { transform: translateY(0) scale(1); opacity: 1; }
}

@keyframes icon-pop {
  0% { transform: scale(0); opacity: 0; }
  60% { transform: scale(1.1); }
  100% { transform: scale(1); opacity: 1; }
}

@keyframes badge-pop {
  0% { transform: scale(0) rotate(-30deg); opacity: 0; }
  60% { transform: scale(1.2) rotate(5deg); }
  100% { transform: scale(1) rotate(0deg); opacity: 1; }
}

@keyframes icon-float {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-4px); }
}

@keyframes ripple {
  0% { transform: scale(1); opacity: 0.5; }
  100% { transform: scale(1.8); opacity: 0; }
}

/* ── Dark mode ── */
[data-theme="dark"] .modal-icon-circle {
  background: rgba(99, 102, 241, 0.15);
}
[data-theme="dark"] .modal-icon-badge {
  background: var(--bg-card);
  border-color: rgba(99, 102, 241, 0.15);
}
</style>
