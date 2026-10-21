<template>
  <div class="documents-page">
    <!-- 上传区 -->
    <el-card shadow="never" class="upload-card">
      <div class="upload-options">
        <span class="option-label">可见性：</span>
        <el-radio-group v-model="uploadPublic" size="small">
          <el-radio-button :value="false">私有</el-radio-button>
          <el-radio-button :value="true">公开</el-radio-button>
        </el-radio-group>
        <span class="option-hint">
          {{ uploadPublic ? '公开：所有用户均可检索使用' : '私有：仅当前用户可见' }}
        </span>
      </div>
      <el-upload
        drag
        multiple
        :accept="ACCEPT"
        :show-file-list="false"
        :http-request="handleUpload"
        :before-upload="beforeUpload"
        :disabled="uploadingCount > 0"
        class="upload-dragger"
      >
        <el-icon class="upload-icon"><UploadFilled /></el-icon>
        <div class="upload-text">
          将文件拖到此处，或<em>点击选择</em>
        </div>
        <template #tip>
          <div class="upload-hint">
            仅支持 PDF / Word（docx）/ Excel（xlsx），单文件不超过
            {{ MAX_MB }}MB；相同文件重复上传将自动去重
          </div>
        </template>
      </el-upload>
      <div v-if="uploadingCount > 0" class="upload-progress">
        <el-icon class="is-loading"><Loading /></el-icon>
        正在解析并入库 {{ uploadingCount }} 个文件，请稍候……
      </div>
    </el-card>

    <!-- 文档列表 -->
    <el-card shadow="never" class="list-card">
      <template #header>
        <div class="list-header">
          <span class="list-title">我的文档</span>
          <el-button text :icon="Refresh" @click="loadList">刷新</el-button>
        </div>
      </template>

      <el-table
        v-loading="loading"
        :data="documents"
        style="width: 100%"
        empty-text="暂无文档，请先上传"
      >
        <el-table-column label="文档名称" min-width="280" show-overflow-tooltip>
          <template #default="{ row }">
            <div class="doc-name">
              <el-icon class="doc-type-icon" :class="`type-${row.file_type}`">
                <component :is="typeIcon(row.file_type)" />
              </el-icon>
              <span>{{ row.original_name }}</span>
              <el-tag
                v-if="row.is_public"
                type="success"
                size="small"
                effect="plain"
              >公开</el-tag>
              <el-tag
                v-else
                type="info"
                size="small"
                effect="plain"
              >私有</el-tag>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="类型" width="90" align="center">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ row.file_type.toUpperCase() }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="大小" width="110" align="right">
          <template #default="{ row }">{{ formatFileSize(row.file_size) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="170">
          <template #default="{ row }">
            <el-tooltip
              v-if="row.status === 'failed'"
              :content="row.error_msg || '入库失败'"
              placement="top"
            >
              <el-tag type="danger" size="small">入库失败</el-tag>
            </el-tooltip>
            <el-tag v-else-if="row.status === 'pending'" type="warning" size="small">
              <template v-if="row.total_chunks > 0">
                处理中 {{ row.processed_chunks }}/{{ row.total_chunks }}
              </template>
              <template v-else>解析中…</template>
            </el-tag>
            <el-tag v-else type="success" size="small">已入库</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="上传时间" width="170">
          <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90" align="center" fixed="right">
          <template #default="{ row }">
            <el-popconfirm
              title="确认删除该文档及其全部问答切片？"
              confirm-button-text="删除"
              cancel-button-text="取消"
              :icon="WarningFilled"
              icon-color="#e6a23c"
              @confirm="onDelete(row)"
            >
              <template #reference>
                <el-button link type="danger" :icon="Delete">删除</el-button>
              </template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>

      <div class="pagination">
        <el-pagination
          background
          layout="total, sizes, prev, pager, next"
          :total="total"
          :current-page="page"
          :page-size="pageSize"
          :page-sizes="[10, 20, 50]"
          @current-change="onPageChange"
          @size-change="onSizeChange"
        />
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import {
  Delete,
  Document,
  Files,
  Loading,
  Refresh,
  Tickets,
  UploadFilled,
  WarningFilled,
} from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { ApiError, documentApi } from '../api/client'
import type { DocumentListItem } from '../api/types'
import { formatDateTime, formatFileSize } from '../utils/format'

const ACCEPT = '.pdf,.docx,.xlsx'
const MAX_MB = 50

const loading = ref(false)
const uploadingCount = ref(0)
const documents = ref<DocumentListItem[]>([])
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const pollTimers = ref<number[]>([])
const uploadPublic = ref(false)

function typeIcon(fileType: string) {
  if (fileType === 'pdf') return Document
  if (fileType === 'xlsx') return Tickets
  return Files
}

async function loadList() {
  loading.value = true
  try {
    const data = await documentApi.list(page.value, pageSize.value)
    documents.value = data.items
    total.value = data.total
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : '加载文档列表失败')
  } finally {
    loading.value = false
  }
}

function beforeUpload(file: File): boolean {
  const ext = file.name.split('.').pop()?.toLowerCase() ?? ''
  if (!['pdf', 'docx', 'xlsx'].includes(ext)) {
    ElMessage.warning('仅支持 PDF / Word（docx）/ Excel（xlsx）文件')
    return false
  }
  if (file.size > MAX_MB * 1024 * 1024) {
    ElMessage.warning(`文件大小不能超过 ${MAX_MB}MB`)
    return false
  }
  if (file.size === 0) {
    ElMessage.warning('文件内容为空，请重新选择')
    return false
  }
  return true
}

// el-upload 自定义上传：返回 Promise 以接管成功/失败提示
async function handleUpload(options: { file: File }) {
  uploadingCount.value += 1
  try {
    const result = await documentApi.upload(options.file, uploadPublic.value)
    if (result.duplicated) {
      ElMessage.info(`「${options.file.name}」已在知识库中，未重复入库`)
    } else {
      ElMessage.info(`「${options.file.name}」已提交，正在后台入库`)
      pollUntilDone(result.document.id, options.file.name)
    }
    await loadList()
  } catch (error) {
    ElMessage.error(
      `「${options.file.name}」${
        error instanceof ApiError ? error.message : '上传失败，请稍后重试'
      }`,
    )
    // 失败（加密/损坏/向量库异常）后刷新列表，让 failed 状态可见
    await loadList()
  } finally {
    uploadingCount.value -= 1
  }
}

// 后台入库轮询：每 3s 查一次该文档状态，直到非 pending（success / failed / 被删）
function pollUntilDone(docId: number, fileName: string, attempt = 0) {
  const MAX_ATTEMPTS = 2400 // 3s × 2400 ≈ 2 小时上限
  const timer = window.setTimeout(async () => {
    try {
      // 拉取第一页（上传文档按 id 倒序，最新的必在第一页）
      const data = await documentApi.list(1, 100)
      const doc = data.items.find((d) => d.id === docId) ?? null
      // 局部更新该行，不打断用户当前浏览页
      if (doc) {
        const idx = documents.value.findIndex((d) => d.id === docId)
        if (idx >= 0) documents.value[idx] = doc
        else documents.value.unshift(doc)
      }
      if (!doc || doc.status !== 'pending') {
        if (doc?.status === 'success') {
          ElMessage.success(`「${fileName}」入库成功，生成 ${doc.total_chunks} 个切片`)
        } else if (doc?.status === 'failed') {
          ElMessage.error(`「${fileName}」入库失败：${doc.error_msg || '未知错误'}`)
        }
        return
      }
      if (attempt < MAX_ATTEMPTS) pollUntilDone(docId, fileName, attempt + 1)
    } catch {
      if (attempt < MAX_ATTEMPTS) pollUntilDone(docId, fileName, attempt + 1)
    }
  }, 3000)
  pollTimers.value.push(timer)
}

onUnmounted(() => {
  pollTimers.value.forEach((t) => clearTimeout(t))
  pollTimers.value = []
})

async function onDelete(row: DocumentListItem) {
  try {
    await documentApi.remove(row.id)
    ElMessage.success(`「${row.original_name}」已删除`)
    // 删完后若当前页空了，回退一页
    if (documents.value.length === 1 && page.value > 1) {
      page.value -= 1
    }
    await loadList()
  } catch (error) {
    ElMessage.error(error instanceof ApiError ? error.message : '删除失败')
  }
}

function onPageChange(value: number) {
  page.value = value
  loadList()
}

function onSizeChange(value: number) {
  pageSize.value = value
  page.value = 1
  loadList()
}

onMounted(loadList)
</script>

<style scoped>
.upload-card {
  margin-bottom: 16px;
}

.upload-options {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}

.option-label {
  font-size: 13px;
  color: var(--rag-text-main);
}

.option-hint {
  font-size: 12px;
  color: var(--rag-text-sub);
}

.upload-dragger :deep(.el-upload-dragger) {
  padding: 24px;
}

.upload-icon {
  font-size: 42px;
  color: var(--rag-primary);
  margin-bottom: 6px;
}

.upload-text {
  font-size: 14px;
  color: var(--rag-text-main);
}

.upload-text em {
  color: var(--rag-primary);
  font-style: normal;
}

.upload-hint {
  margin-top: 6px;
  font-size: 12px;
  color: var(--rag-text-sub);
}

.upload-progress {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 10px;
  font-size: 13px;
  color: var(--rag-primary);
}

.list-card {
  min-height: 360px;
}

.list-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.list-title {
  font-size: 15px;
  font-weight: 600;
}

.doc-name {
  display: flex;
  align-items: center;
  gap: 8px;
}

.doc-type-icon {
  font-size: 17px;
  flex-shrink: 0;
}

.type-pdf {
  color: #d9534f;
}

.type-docx {
  color: #2b6cb0;
}

.type-xlsx {
  color: #2f9e44;
}

.pagination {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}
</style>
