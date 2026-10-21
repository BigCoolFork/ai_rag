<template>
  <el-dialog
    v-model="visible"
    :title="docName"
    width="720px"
    top="6vh"
    destroy-on-close
    class="doc-preview-dialog"
  >
    <div v-loading="loading" class="doc-preview-body">
      <div v-if="errorMsg" class="doc-error">{{ errorMsg }}</div>
      <template v-else>
        <div
          v-for="chunk in chunks"
          :key="chunk.chunk_id"
          :id="`chunk-${chunk.chunk_id}`"
          class="doc-chunk"
          :class="{ 'is-highlight': chunk.chunk_id === highlightChunkId }"
        >
          <div v-if="chunk.section || chunk.page" class="doc-chunk-meta">
            <el-tag v-if="chunk.section" size="small" effect="plain">{{ chunk.section }}</el-tag>
            <el-tag v-if="chunk.page" size="small" type="info">第 {{ chunk.page }} 页</el-tag>
          </div>
          <div class="doc-chunk-text">{{ chunk.text }}</div>
        </div>
      </template>
    </div>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref, watch, nextTick } from 'vue'
import { documentApi } from '../api/client'
import type { DocumentChunkItem } from '../api/types'

const props = defineProps<{
  modelValue: boolean
  docId: number | null
  highlightChunkId?: number | null
}>()

const emit = defineEmits<{ (e: 'update:modelValue', v: boolean): void }>()

const visible = ref(props.modelValue)
const loading = ref(false)
const errorMsg = ref('')
const docName = ref('')
const chunks = ref<DocumentChunkItem[]>([])

watch(
  () => props.modelValue,
  (v) => {
    visible.value = v
    if (v && props.docId) loadChunks()
  },
)

watch(visible, (v) => emit('update:modelValue', v))

async function loadChunks() {
  if (!props.docId) return
  loading.value = true
  errorMsg.value = ''
  chunks.value = []
  try {
    const resp = await documentApi.getChunks(props.docId)
    docName.value = resp.doc_name
    chunks.value = resp.chunks
    await nextTick()
    scrollToHighlight()
  } catch (e: any) {
    errorMsg.value = e?.response?.data?.detail?.message || e?.message || '加载文档内容失败'
  } finally {
    loading.value = false
  }
}

function scrollToHighlight() {
  if (!props.highlightChunkId) return
  const el = document.getElementById(`chunk-${props.highlightChunkId}`)
  if (el) {
    el.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }
}
</script>

<style scoped>
.doc-preview-body {
  max-height: 70vh;
  overflow-y: auto;
  padding: 4px 8px;
}

.doc-chunk {
  padding: 12px 14px;
  margin-bottom: 10px;
  border-radius: 6px;
  background: #f7f8fa;
  border: 1px solid transparent;
  transition: background 0.3s, border-color 0.3s;
}

.doc-chunk.is-highlight {
  background: #fff7e6;
  border-color: #ffd591;
  box-shadow: 0 0 0 2px rgba(255, 165, 0, 0.2);
}

.doc-chunk-meta {
  margin-bottom: 6px;
  display: flex;
  gap: 6px;
}

.doc-chunk-text {
  font-size: 14px;
  line-height: 1.7;
  color: #303133;
  white-space: pre-wrap;
  word-break: break-word;
}

.doc-error {
  padding: 24px;
  text-align: center;
  color: #f56c6c;
}
</style>
