<template>
  <div class="auth-page">
    <div class="auth-hero">
      <div class="hero-inner">
        <el-icon class="hero-icon"><Collection /></el-icon>
        <h1>私有化知识库</h1>
        <p>文档集中管理 · 本地向量检索 · 大模型问答</p>
        <ul>
          <li>支持 PDF / Word / Excel 文档入库</li>
          <li>答案附带文档与页码来源，可追溯</li>
          <li>全链路内网运行，数据不出本机</li>
        </ul>
      </div>
    </div>

    <div class="auth-panel">
      <el-card class="auth-card" shadow="never">
        <h2 class="auth-title">账号登录</h2>
        <p class="auth-subtitle">登录后管理文档并进行知识库问答</p>

        <el-form
          ref="formRef"
          :model="form"
          :rules="rules"
          label-position="top"
          size="large"
          @keyup.enter="onSubmit"
        >
          <el-form-item label="用户名" prop="username">
            <el-input
              v-model="form.username"
              placeholder="请输入用户名"
              :prefix-icon="User"
              clearable
              autocomplete="username"
            />
          </el-form-item>
          <el-form-item label="密码" prop="password">
            <el-input
              v-model="form.password"
              type="password"
              placeholder="请输入密码"
              :prefix-icon="Lock"
              show-password
              autocomplete="current-password"
            />
          </el-form-item>
          <el-button
            type="primary"
            class="submit-btn"
            :loading="loading"
            @click="onSubmit"
          >
            登 录
          </el-button>
        </el-form>

        <div class="auth-footer">
          还没有账号？<router-link to="/register">立即注册</router-link>
        </div>
      </el-card>
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Lock, User } from '@element-plus/icons-vue'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { ApiError } from '../api/client'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()

const formRef = ref<FormInstance>()
const loading = ref(false)
const form = reactive({ username: '', password: '' })

const rules: FormRules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

async function onSubmit() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  loading.value = true
  try {
    await auth.login(form.username.trim(), form.password)
    ElMessage.success('登录成功')
    const redirect = (route.query.redirect as string) || '/documents'
    router.push(redirect)
  } catch (error) {
    const message =
      error instanceof ApiError ? error.message : '登录失败，请稍后重试'
    ElMessage.error(message)
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.auth-page {
  display: flex;
  height: 100%;
}

.auth-hero {
  flex: 1;
  background: linear-gradient(150deg, #1d4a8c 0%, #2456a6 55%, #2f6bc4 100%);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
}

.hero-inner {
  max-width: 420px;
  padding: 40px;
}

.hero-icon {
  font-size: 44px;
  margin-bottom: 18px;
}

.hero-inner h1 {
  font-size: 32px;
  margin: 0 0 12px;
  letter-spacing: 2px;
}

.hero-inner > p {
  font-size: 15px;
  opacity: 0.85;
  margin: 0 0 28px;
}

.hero-inner ul {
  padding-left: 20px;
  margin: 0;
  line-height: 2.2;
  font-size: 14px;
  opacity: 0.92;
}

.auth-panel {
  width: 520px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #fff;
}

.auth-card {
  width: 380px;
  border: none;
}

.auth-title {
  margin: 0 0 6px;
  font-size: 22px;
}

.auth-subtitle {
  margin: 0 0 26px;
  color: var(--rag-text-sub);
  font-size: 13px;
}

.submit-btn {
  width: 100%;
  margin-top: 4px;
  height: 42px;
  font-size: 15px;
  letter-spacing: 4px;
}

.auth-footer {
  margin-top: 18px;
  text-align: center;
  font-size: 13px;
  color: var(--rag-text-sub);
}

.auth-footer a {
  color: var(--rag-primary);
  text-decoration: none;
}

@media (max-width: 900px) {
  .auth-hero {
    display: none;
  }
  .auth-panel {
    width: 100%;
  }
}
</style>
