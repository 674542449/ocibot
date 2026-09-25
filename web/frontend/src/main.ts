import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import './styles.css'

// 只有一套配色（Claude 风格的亮色），暗色模式在 0.4.119 移除。以前存下的主题偏好
// 已经没有任何代码会读，顺手清掉，免得它在浏览器里一直留着。
try {
  localStorage.removeItem('ocibot_theme')
} catch {
  /* storage blocked (private mode etc.) — nothing to clean up */
}

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.mount('#app')
