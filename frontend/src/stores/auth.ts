/**
 * 认证状态：JWT 持久化于 localStorage，用户信息内存缓存。
 * 路由守卫在页面刷新后通过 ensureUser() 拉取 /auth/me 恢复会话。
 */

import { defineStore } from 'pinia'
import { authApi, clearToken, getToken, setToken } from '../api/client'
import type { UserInfo } from '../api/types'

interface AuthState {
  token: string
  user: UserInfo | null
  restoring: boolean
}

export const useAuthStore = defineStore('auth', {
  state: (): AuthState => ({
    token: getToken(),
    user: null,
    restoring: false,
  }),

  getters: {
    isLoggedIn: (state) => !!state.token,
    username: (state) => state.user?.username ?? '',
  },

  actions: {
    async login(username: string, password: string): Promise<void> {
      const result = await authApi.login({ username, password })
      setToken(result.access_token)
      this.token = result.access_token
      await this.fetchMe()
    },

    async register(username: string, password: string): Promise<void> {
      // 注册成功不自动登录，引导用户前往登录页
      await authApi.register({ username, password })
    },

    async fetchMe(): Promise<void> {
      this.user = await authApi.me()
    },

    /** 路由守卫调用：有 token 但用户信息缺失（如刷新后）时拉取当前用户。 */
    async ensureUser(): Promise<boolean> {
      if (!this.token) {
        return false
      }
      if (this.user) {
        return true
      }
      this.restoring = true
      try {
        await this.fetchMe()
        return true
      } finally {
        this.restoring = false
      }
    },

    logout(): void {
      clearToken()
      this.token = ''
      this.user = null
    },
  },
})
