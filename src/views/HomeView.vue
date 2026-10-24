<script setup lang="ts">
import TitleBar from '@/components/TitleBar.vue'
import MediaInput from '@/components/MediaInput.vue'
import DeviceList from '@/components/DeviceList.vue'
import CastController from '@/components/CastController.vue'
import ScreenCastPanel from '@/components/ScreenCastPanel.vue'
import StopWarningBanner from '@/components/StopWarningBanner.vue'
import Footer from '@/components/Footer.vue'
import AutostartPromptModal from '@/components/AutostartPromptModal.vue'
import { useCastStore } from '@/stores/cast'
import { useScreenCastStore } from '@/stores/screencast'
import { useI18n } from 'vue-i18n'
import { storeToRefs } from 'pinia'

const { t } = useI18n()
const castStore = useCastStore()
const screenCastStore = useScreenCastStore()
const { showAutostartPrompt } = storeToRefs(castStore)
const { isRunning: screenCastRunning } = storeToRefs(screenCastStore)
</script>

<template>
  <div class="home">
    <TitleBar />
    <main class="home__content">
      <div class="home__screencast-bar">
        <div class="home__heading">
          <h1>{{ t('home.title') }}</h1>
          <p>{{ t('home.subtitle') }}</p>
        </div>
        <button
          class="home__screencast-btn"
          :class="{ 'home__screencast-btn--live': screenCastRunning }"
          @click="screenCastStore.openPanel()"
        >
          <span v-if="screenCastRunning" class="home__screencast-dot"></span>
          <svg aria-hidden="true" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
            <rect x="3" y="4" width="18" height="13" rx="2" />
            <path d="M8 21h8M12 17v4" />
          </svg>
          <span>{{ screenCastRunning ? t('screencast.entryRunning') : t('home.screenCast') }}</span>
        </button>
        <div v-if="screenCastRunning" class="home__screencast-viewers">
          {{ screenCastStore.state.viewers ?? 0 }} {{ t('screencast.viewers') }}
        </div>
      </div>

      <MediaInput />
      <div v-if="castStore.lastError" class="home__error" role="alert">
        <p><strong>{{ t('cast.error') }}</strong> · {{ castStore.lastError }}</p>
        <button :aria-label="t('controller.close')" @click="castStore.dismissError()">×</button>
      </div>
      <!-- 停止未送达告警：控制器/投屏条在停止后都会关闭，只有页面能留住这句话 -->
      <StopWarningBanner
        :warning="castStore.stopWarning"
        @close="castStore.dismissStopWarning()"
      />
      <DeviceList />
    </main>
    <Footer />
    <!-- Embedded cast controller modal -->
    <CastController />
    <!-- 屏幕投屏面板 -->
    <ScreenCastPanel />
    <!-- Autostart prompt (first successful cast) -->
    <AutostartPromptModal
      :visible="showAutostartPrompt"
      @close="castStore.closeAutostartPrompt()"
    />
  </div>
</template>

<style scoped>
.home {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--bg-primary);
}

.home__content {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 20px;
  padding: var(--sp-lg);
  overflow-y: auto;
  min-height: 0;
}

.home__screencast-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px;
  flex-shrink: 0;
}

.home__heading h1 {
  font-size: 21px;
  font-weight: 650;
  letter-spacing: -0.6px;
  line-height: 1.35;
}

.home__heading p {
  margin-top: 4px;
  font-size: 12px;
  color: var(--text-secondary);
}

@media (max-width: 380px) {
  .home__content { padding: 12px; gap: 16px; }
  .home__heading h1 { font-size: 19px; }
  .home__heading p { max-width: 155px; }
}

.home__screencast-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 9px 12px;
  font-size: 12px;
  font-weight: 500;
  color: var(--text-primary);
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  cursor: pointer;
  transition: all var(--transition-fast);
  font-family: var(--font);
}

.home__screencast-btn:hover {
  border-color: var(--border-focus);
}

.home__screencast-btn--live {
  color: #3ecf8e;
  border-color: rgba(62, 207, 142, 0.4);
  background: rgba(62, 207, 142, 0.08);
}

.home__screencast-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #3ecf8e;
  animation: sc-home-pulse 1.6s ease-in-out infinite;
}

@keyframes sc-home-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.35; }
}

.home__screencast-viewers {
  font-size: 12px;
  color: var(--text-secondary);
}

.home__error {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 10px 12px;
  border-radius: var(--r-md);
  background: var(--danger-light);
  color: var(--danger);
  font-size: 12px;
  flex-shrink: 0;
}

.home__error p { flex: 1; min-width: 0; overflow-wrap: anywhere; }
.home__error button { width: 24px; height: 24px; flex-shrink: 0; }
</style>
