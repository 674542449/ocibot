import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import './styles.css'

// Apply saved theme before first paint so login/pages aren't stuck on light tokens.
const savedTheme = localStorage.getItem('ocibot_theme')
if (savedTheme === 'dark' || savedTheme === 'light') {
  document.documentElement.setAttribute('data-theme', savedTheme)
}

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.mount('#app')
