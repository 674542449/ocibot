import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import './styles.css'

// Apply the theme before first paint so the shell never flashes the wrong one.
// Dark is the default the interface is designed against; an explicit choice wins.
const savedTheme = localStorage.getItem('ocibot_theme')
document.documentElement.setAttribute(
  'data-theme',
  savedTheme === 'light' || savedTheme === 'dark' ? savedTheme : 'dark',
)

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.mount('#app')
