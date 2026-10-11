<script setup lang="ts">
import TitleBar from '@/components/TitleBar.vue'
import MediaInput from '@/components/MediaInput.vue'
import DeviceList from '@/components/DeviceList.vue'
import CastController from '@/components/CastController.vue'
import ScreenCastPanel from '@/components/ScreenCastPanel.vue'
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
      <!-- 屏幕投屏入口 -->
      <div class="home__screencast-bar">
        <button
          class="home__screencast-btn"
          :class="{ 'home__screencast-btn--live': screenCastRunning }"
          @click="screenCastStore.openPanel()"
        >
          <span v-if="screenCastRunning" class="home__screencast-dot"></span>
          <span class="home__screencast-icon">🖥️</span>
          <span>
            {{ screenCastRunning
              ? t('screencast.entryRunning')
              : t('screencast.entry') }}
          </span>
        </button>
        <div v-if="screenCastRunning" class="home__screencast-viewers">
          {{ screenCastStore.state.viewers ?? 0 }} {{ t('screencast.viewers') }}
        </div>
      </div>

      <MediaInput />
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
  gap: var(--sp-lg);
  padding: var(--sp-lg) 0;
  overflow: hidden;
  min-height: 0;
}

.home__screencast-bar {
  display: flex;
  align-items: center;
  gap: 10px;
}

.home__screencast-btn {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 16px;
  font-size: 13px;
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
</style>
