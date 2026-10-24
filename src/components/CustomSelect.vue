<script setup lang="ts">
import { ref, nextTick, getCurrentInstance, onMounted, onUnmounted } from 'vue'

interface Option {
  label: string
  value: string | number
}

const props = defineProps<{
  modelValue: string | number
  options: Option[]
  ariaLabel?: string
  ariaLabelledby?: string
}>()

const emit = defineEmits<{
  'update:modelValue': [value: string | number]
}>()

const open = ref(false)
const dropdownRef = ref<HTMLElement | null>(null)
const triggerRef = ref<HTMLButtonElement | null>(null)
const activeIndex = ref(0)
const menuId = `custom-select-${getCurrentInstance()!.uid}`

const currentLabel = () => {
  return props.options.find(o => o.value === props.modelValue)?.label ?? ''
}

async function focusOption(index: number) {
  activeIndex.value = Math.max(0, Math.min(index, props.options.length - 1))
  await nextTick()
  if (!open.value) return
  const option = dropdownRef.value?.querySelectorAll<HTMLButtonElement>('[role="option"]')[activeIndex.value]
  option?.focus({ preventScroll: true })
  option?.scrollIntoView({ block: 'nearest' })
}

function openMenu(index = props.options.findIndex(o => o.value === props.modelValue)) {
  if (!props.options.length) return
  open.value = true
  focusOption(index)
}

function closeMenu(restoreFocus = false) {
  open.value = false
  if (restoreFocus) triggerRef.value?.focus({ preventScroll: true })
}

function toggle() {
  if (open.value) closeMenu(true)
  else openMenu()
}

function select(value: string | number) {
  emit('update:modelValue', value)
  closeMenu(true)
}

function onClickOutside(e: MouseEvent) {
  if (dropdownRef.value && !dropdownRef.value.contains(e.target as Node)) {
    closeMenu()
  }
}

function onFocusOut(e: FocusEvent) {
  // WebKit can report null when clicking an option; outside clicks are handled above.
  if (e.relatedTarget && !dropdownRef.value?.contains(e.relatedTarget as Node)) closeMenu()
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && open.value) {
    e.preventDefault()
    e.stopPropagation()
    closeMenu(true)
  } else if (e.key === 'Tab' && open.value) {
    // Return to the trigger before the browser advances focus; never trap Tab.
    closeMenu(true)
  } else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault()
    if (!open.value) openMenu()
    else focusOption(activeIndex.value + (e.key === 'ArrowDown' ? 1 : -1))
  } else if (e.key === 'Home' || e.key === 'End') {
    e.preventDefault()
    const index = e.key === 'Home' ? 0 : props.options.length - 1
    if (!open.value) openMenu(index)
    else focusOption(index)
  }
}

onMounted(() => {
  document.addEventListener('mousedown', onClickOutside)
})

onUnmounted(() => {
  document.removeEventListener('mousedown', onClickOutside)
})
</script>

<template>
  <div class="custom-select" ref="dropdownRef" @keydown="onKeydown" @focusout="onFocusOut">
    <button
      ref="triggerRef"
      class="custom-select__trigger"
      :class="{ 'custom-select__trigger--open': open }"
      :aria-label="ariaLabel"
      :aria-labelledby="ariaLabelledby"
      :aria-describedby="ariaLabel || ariaLabelledby ? `${menuId}-value` : undefined"
      aria-haspopup="listbox"
      :aria-expanded="open"
      :aria-controls="open ? menuId : undefined"
      :title="currentLabel()"
      @click="toggle"
      type="button"
    >
      <span :id="`${menuId}-value`" class="custom-select__value">{{ currentLabel() }}</span>
      <svg
        class="custom-select__arrow"
        :class="{ 'custom-select__arrow--open': open }"
        aria-hidden="true"
        viewBox="0 0 24 24" width="14" height="14" fill="none"
        stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
      >
        <polyline points="6 9 12 15 18 9" />
      </svg>
    </button>
    <Transition name="select-menu">
      <div
        v-if="open"
        :id="menuId"
        class="custom-select__menu"
        role="listbox"
        :aria-label="ariaLabel || currentLabel()"
        :aria-labelledby="ariaLabelledby"
      >
        <button
          v-for="(opt, index) in options"
          :key="String(opt.value)"
          class="custom-select__option"
          :class="{ 'custom-select__option--active': opt.value === modelValue }"
          role="option"
          :aria-selected="opt.value === modelValue"
          tabindex="-1"
          @focus="activeIndex = index"
          @click="select(opt.value)"
          type="button"
        >
          {{ opt.label }}
        </button>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.custom-select {
  position: relative;
  min-width: 0;
  max-width: 100%;
}

.custom-select__trigger {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
  min-width: 0;
  min-height: 32px;
  padding: 5px 9px;
  font-size: 13px;
  line-height: 1.5;
  text-align: left;
  background: var(--bg-input);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  color: var(--text-primary);
  cursor: pointer;
  transition: background var(--transition-fast), border-color var(--transition-fast);
}

.custom-select__trigger:hover {
  background: var(--bg-secondary);
}

.custom-select__trigger--open {
  border-color: var(--border-focus);
}

.custom-select__trigger:focus-visible {
  outline: 2px solid var(--border-focus);
  outline-offset: 2px;
}

.custom-select__value {
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.custom-select__arrow {
  color: var(--text-secondary);
  transition: transform var(--transition-fast);
  flex-shrink: 0;
}

.custom-select__arrow--open {
  transform: rotate(180deg);
}

/* ── Dropdown menu ── */
.custom-select__menu {
  position: absolute;
  top: calc(100% + 5px);
  right: 0;
  width: 100%;
  min-width: 0;
  max-height: min(240px, 40vh);
  padding: 4px;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  box-shadow: var(--shadow-md);
  z-index: 100;
  overflow-x: hidden;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.custom-select__option {
  display: block;
  width: 100%;
  min-height: 32px;
  padding: 6px 9px;
  font-size: 13px;
  line-height: 1.5;
  color: var(--text-primary);
  background: none;
  border: none;
  border-radius: var(--r-sm);
  text-align: left;
  overflow-wrap: anywhere;
  cursor: pointer;
  transition: background var(--transition-fast);
}

.custom-select__option:hover {
  background: var(--bg-secondary);
}

.custom-select__option:focus-visible {
  outline: 2px solid var(--border-focus);
  outline-offset: -2px;
}

.custom-select__option--active {
  color: var(--text-link);
  font-weight: 500;
  background: var(--primary-light);
}

/* ── Animation ── */
.select-menu-enter-active {
  transition: opacity var(--transition-fast), transform var(--transition-fast);
}
.select-menu-leave-active {
  transition: opacity var(--transition-fast), transform var(--transition-fast);
}
.select-menu-enter-from {
  opacity: 0;
  transform: translateY(-4px);
}
.select-menu-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}

@media (prefers-reduced-motion: reduce) {
  .custom-select__trigger,
  .custom-select__arrow,
  .select-menu-enter-active,
  .select-menu-leave-active {
    transition: none;
  }
}
</style>
