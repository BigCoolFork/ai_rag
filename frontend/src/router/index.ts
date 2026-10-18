/**
 * 路由与登录守卫：
 * - 无 token 访问业务页 → /login；
 * - 已登录访问登录/注册页 → /documents；
 * - 刷新后有 token 无用户信息时先拉 /auth/me，失败则回登录页。
 */

import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('../views/Login.vue'),
    meta: { public: true, title: '登录' },
  },
  {
    path: '/register',
    name: 'register',
    component: () => import('../views/Register.vue'),
    meta: { public: true, title: '注册' },
  },
  {
    path: '/',
    component: () => import('../layouts/MainLayout.vue'),
    redirect: '/documents',
    children: [
      {
        path: 'documents',
        name: 'documents',
        component: () => import('../views/Documents.vue'),
        meta: { title: '文档管理' },
      },
      {
        path: 'chat',
        name: 'chat',
        component: () => import('../views/Chat.vue'),
        meta: { title: '知识库问答' },
      },
    ],
  },
  { path: '/:pathMatch(.*)*', redirect: '/documents' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()

  if (to.meta.public) {
    // 已登录用户无需再看登录/注册页
    if (auth.isLoggedIn && (to.name === 'login' || to.name === 'register')) {
      return { name: 'documents' }
    }
    return true
  }

  if (!auth.isLoggedIn) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }

  const valid = await auth.ensureUser()
  if (!valid) {
    auth.logout()
    return { name: 'login' }
  }
  return true
})

router.afterEach((to) => {
  document.title = to.meta.title
    ? `${to.meta.title as string} - 私有化知识库`
    : '私有化知识库'
})

export default router
