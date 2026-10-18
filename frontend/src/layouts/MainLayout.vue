<template>
  <el-container class="app-shell">
    <el-aside :width="collapsed ? '64px' : '220px'" class="app-aside">
      <div class="brand">
        <el-icon class="brand-icon"><Collection /></el-icon>
        <span v-show="!collapsed" class="brand-text">私有化知识库</span>
      </div>
      <el-menu
        :default-active="activeMenu"
        :collapse="collapsed"
        :collapse-transition="false"
        router
        class="nav-menu"
      >
        <el-menu-item index="/documents">
          <el-icon><FolderOpened /></el-icon>
          <template #title>文档管理</template>
        </el-menu-item>
        <el-menu-item index="/chat">
          <el-icon><ChatDotRound /></el-icon>
          <template #title>知识库问答</template>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="app-header">
        <el-button text class="collapse-btn" @click="collapsed = !collapsed">
          <el-icon :size="20">
            <Expand v-if="collapsed" />
            <Fold v-else />
          </el-icon>
        </el-button>
        <div class="header-title">{{ currentTitle }}</div>
        <div class="header-right">
          <el-dropdown trigger="click" @command="onCommand">
            <span class="user-box">
              <el-avatar :size="30" class="user-avatar">
                {{ avatarLetter }}
              </el-avatar>
              <span class="user-name">{{ auth.username || '用户' }}</span>
              <el-icon><CaretBottom /></el-icon>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="logout" divided>
                  <el-icon><SwitchButton /></el-icon>退出登录
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>

      <el-main class="app-main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { useAuthStore } from '../stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const collapsed = ref(false)
const activeMenu = computed(() => route.path)
const currentTitle = computed(() => (route.meta.title as string) ?? '')
const avatarLetter = computed(() => (auth.username || '用').slice(0, 1).toUpperCase())

async function onCommand(command: string) {
  if (command !== 'logout') return
  await ElMessageBox.confirm('确认退出当前账号？', '退出登录', {
    confirmButtonText: '退出',
    cancelButtonText: '取消',
    type: 'warning',
  })
  auth.logout()
  router.push({ name: 'login' })
}
</script>

<style scoped>
.app-shell {
  height: 100%;
}

.app-aside {
  background: #ffffff;
  border-right: 1px solid var(--rag-border);
  transition: width 0.2s ease;
  overflow: hidden;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 60px;
  padding: 0 18px;
  white-space: nowrap;
}

.brand-icon {
  font-size: 24px;
  color: var(--rag-primary);
  flex-shrink: 0;
}

.brand-text {
  font-size: 17px;
  font-weight: 600;
  letter-spacing: 1px;
  color: var(--rag-text-main);
}

.nav-menu {
  border-right: none;
  padding: 8px;
}

.nav-menu :deep(.el-menu-item) {
  border-radius: 8px;
  margin-bottom: 4px;
  height: 46px;
}

.app-header {
  display: flex;
  align-items: center;
  gap: 12px;
  background: #ffffff;
  border-bottom: 1px solid var(--rag-border);
  padding: 0 20px;
  height: 60px;
}

.collapse-btn {
  padding: 6px;
}

.header-title {
  font-size: 16px;
  font-weight: 600;
}

.header-right {
  margin-left: auto;
}

.user-box {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  outline: none;
}

.user-avatar {
  background: var(--rag-primary);
  font-size: 14px;
}

.user-name {
  font-size: 14px;
  color: var(--rag-text-main);
}

.app-main {
  padding: 24px;
  overflow-y: auto;
}
</style>
