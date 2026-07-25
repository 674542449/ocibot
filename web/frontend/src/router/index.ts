import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { public: true },
    },
    {
      path: '/',
      component: () => import('@/layouts/AppLayout.vue'),
      children: [
        {
          path: '',
          name: 'instances',
          component: () => import('@/views/InstancesView.vue'),
        },
        {
          path: 'instances/:tenantId/:instanceId',
          name: 'instance-detail',
          component: () => import('@/views/InstanceDetailView.vue'),
        },
        {
          path: 'launch',
          name: 'launch',
          component: () => import('@/views/LaunchView.vue'),
        },
        {
          path: 'tenants',
          name: 'tenants',
          component: () => import('@/views/TenantsView.vue'),
        },
        {
          path: 'jobs',
          name: 'jobs',
          component: () => import('@/views/JobsView.vue'),
        },
        {
          path: 'account',
          name: 'account',
          component: () => import('@/views/AccountView.vue'),
        },
        {
          path: 'storage',
          name: 'storage',
          component: () => import('@/views/StorageView.vue'),
        },
        {
          path: 'boot-volumes',
          name: 'boot-volumes',
          redirect: (to) => ({
            name: 'storage',
            query: { ...to.query, tab: (to.query.tab as string) || 'boot' },
          }),
        },
        {
          path: 'backup',
          name: 'backup',
          component: () => import('@/views/BackupView.vue'),
        },
        {
          path: 'audit',
          name: 'audit',
          component: () => import('@/views/AuditView.vue'),
        },
        {
          path: 'settings',
          name: 'settings',
          component: () => import('@/views/SettingsView.vue'),
        },
        {
          path: 'admin',
          name: 'admin',
          component: () => import('@/views/AdminView.vue'),
        },
      ],
    },
  ],
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (!auth.sessionChecked) {
    await auth.refreshMe()
  }
  if (!to.meta.public && !auth.isLoggedIn) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (to.name === 'login' && auth.isLoggedIn) {
    return { name: 'instances' }
  }
})

export default router
