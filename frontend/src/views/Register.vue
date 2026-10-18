<template>
  <div class="auth-page">
    <div class="auth-hero">
      <div class="hero-inner">
        <el-icon class="hero-icon"><Collection /></el-icon>
        <h1>私有化知识库</h1>
        <p>注册账号，构建专属的本地知识库</p>
      </div>
    </div>

    <div class="auth-panel">
      <el-card class="auth-card" shadow="never">
        <h2 class="auth-title">注册新账号</h2>
        <p class="auth-subtitle">用户名与密码规则与后端校验保持一致</p>

        <el-form
          ref="formRef"
          :model="form"
          :rules="rules"
          label-position="top"
          size="large"
        >
          <el-form-item label="用户名" prop="username">
            <el-input
              v-model="form.username"
              placeholder="3-50 位中英文、数字、下划线或连字符"
              :prefix-icon="User"
              clearable
            />
          </el-form-item>
          <el-form-item label="密码" prop="password">
            <el-input
              v-model="form.password"
              type="password"
              placeholder="6-128 位"
              :prefix-icon="Lock"
              show-password
            />
          </el-form-item>
          <el-form-item label="确认密码" prop="confirm">
            <el-input
              v-model="form.confirm"
              type="password"
              placeholder="请再次输入密码"
              :prefix-icon="Lock"
              show-password
              @keyup.enter="onSubmit"
            />
          </el-form-item>
          <el-button
            type="primary"
            class="submit-btn"
            :loading="loading"
            @click="onSubmit"
          >
            注 册
          </el-button>
        </el-form>

        <div class="auth-footer">
          已有账号？<router-link to="/login">返回登录</router-link>
        </div>
      </el-card>
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { Lock, User } from '@element-plus/icons-vue'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { ApiError } from '../api/client'
import { useAuthStore } from '../stores/auth'

const router = useRouter()
const auth = useAuthStore()

const formRef = ref<FormInstance>()
const loading = ref(false)
const form = reactive({ username: '', password: '', confirm: '' })

const rules: FormRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { min: 3, max: 50, message: '长度需为 3-50 个字符', trigger: 'blur' },
    {
      pattern: /^[\u4e00-\u9fa5A-Za-z0-9_-]+$/,
      message: '仅支持中英文、数字、下划线与连字符',
      trigger: 'blur',
    },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, max: 128, message: '长度需为 6-128 个字符', trigger: 'blur' },
  ],
  confirm: [
    { required: true, message: '请再次输入密码', trigger: 'blur' },
    {
      validator: (_rule, value: string, callback) => {
        if (value !== form.password) callback(new Error('两次输入的密码不一致'))
        else callback()
      },
      trigger: 'blur',
    },
  ],
}

async function onSubmit() {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return

  loading.value = true
  try {
    await auth.register(form.username.trim(), form.password)
    ElMessage.success('注册成功，请使用新账号登录')
    router.push({ name: 'login' })
  } catch (error) {
    const message =
      error instanceof ApiError ? error.message : '注册失败，请稍后重试'
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

.hero-inner p {
  font-size: 15px;
  opacity: 0.85;
  margin: 0;
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
