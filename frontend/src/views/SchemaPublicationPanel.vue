<template>
  <section class="publish-panel" data-testid="publish-panel">
    <header class="publish-head">
      <div>
        <h3>结构发布批次</h3>
        <p class="page-desc">
          勾选要一起发布的 Schema 版本，按字段清单和依赖版本整组提交；不合格的表会被排除并记录原因。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="preview-batch" type="button" @click="preview" :disabled="busy">
          预览发布资格
        </button>
        <button class="btn primary" data-testid="publish-batch" type="button" @click="publish" :disabled="busy || !selection.length">
          批量发布选中表
        </button>
      </div>
    </header>

    <label class="publish-key">
      <span>批次键（同一次发布重复提交时必填，用于幂等）</span>
      <input v-model="batchKey" data-testid="batch-key" placeholder="例如 PUB-20261122-01" />
    </label>

    <table class="data-table publish-table">
      <thead>
        <tr>
          <th class="col-check">
            <input
              type="checkbox"
              data-testid="select-all"
              :checked="allSelected"
              @change="toggleAll(($event.target as HTMLInputElement).checked)"
            />
          </th>
          <th>版本编号</th>
          <th>关联资产</th>
          <th>字段清单</th>
          <th>来源版本</th>
          <th>版本状态</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in entries" :key="String(row.id)" :data-testid="`publish-row-${row.id}`">
          <td class="col-check">
            <input
              type="checkbox"
              :data-testid="`select-${row.id}`"
              :value="Number(row.id)"
              :checked="selection.includes(Number(row.id))"
              @change="toggleRow(Number(row.id), ($event.target as HTMLInputElement).checked)"
            />
          </td>
          <td>{{ row['版本编号'] }}</td>
          <td>{{ row['关联资产'] }}</td>
          <td class="field-cell">{{ row['字段清单'] }}</td>
          <td>{{ row['版本编号'] }}</td>
          <td>{{ row['版本状态'] ?? row.status }}</td>
        </tr>
        <tr v-if="!entries.length">
          <td colspan="6" class="empty-state">暂无可发布的 Schema 版本</td>
        </tr>
      </tbody>
    </table>

    <div v-if="verdicts.length" class="publish-verdicts" data-testid="verdict-list">
      <h4>本次结论</h4>
      <article
        v-for="verdict in verdicts"
        :key="verdict.asset"
        class="verdict-card"
        :class="verdict.eligible ? 'ok' : 'blocked'"
        :data-testid="`verdict-${verdict.asset}`"
      >
        <div class="verdict-title">
          <strong>{{ verdict.asset }}</strong>
          <span>{{ verdict.version }}</span>
          <span class="verdict-badge">{{ verdict.eligible ? '可发布' : '排除' }}</span>
        </div>
        <p class="verdict-fields">
          字段：{{ verdict.fields.map((field) => field.type ? `${field.name}:${field.type}` : field.name).join('、') || '—' }}
        </p>
        <p v-if="verdict.dependencies.length" class="verdict-deps">
          依赖：
          <span v-for="dep in verdict.dependencies" :key="dep.upstream">
            {{ dep.upstream }}@{{ dep.version ?? '未声明' }}
            <template v-if="dep.required.length">（需要 {{ dep.required.map((f) => f.name).join('、') }}）</template>
          </span>
        </p>
        <ul v-if="verdict.reasons.length" class="verdict-reasons">
          <li v-for="reason in verdict.reasons" :key="reason.code + reason.message">{{ reason.message }}</li>
        </ul>
        <button
          v-for="dep in unconfirmedDeps(verdict)"
          :key="`confirm-${dep.upstream}`"
          class="link"
          type="button"
          :data-testid="`confirm-${verdict.asset}-${dep.upstream}`"
          @click="confirm(verdict, dep)"
        >
          确认 {{ dep.upstream }} 字段依赖
        </button>
      </article>
    </div>

    <div v-if="lastBatch" class="batch-result" data-testid="batch-result">
      <h4>批次 {{ lastBatch.批次编号 }} · {{ lastBatch.发布状态 }}</h4>
      <p class="page-desc">
        提交 {{ lastBatch.提交表数 }} 张，发布 {{ lastBatch.已发布表数 }} 张，排除 {{ lastBatch.排除表数 }} 张。
      </p>
      <table class="data-table">
        <thead>
          <tr><th>数据表</th><th>来源版本</th><th>快照编号</th><th>依赖关系</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in lastBatch.快照集合.快照" :key="item.快照编号">
            <td>{{ item.关联资产 }}</td>
            <td>{{ item.来源版本 }}</td>
            <td>{{ item.快照编号 }}</td>
            <td>{{ formatDependencies(item.依赖关系) }}</td>
          </tr>
          <tr v-if="!lastBatch.快照集合.快照.length">
            <td colspan="4" class="empty-state">本次没有表进入发布</td>
          </tr>
        </tbody>
      </table>
      <h5>未发布表</h5>
      <ul class="verdict-reasons">
        <li v-for="item in lastBatch.未发布表" :key="item.关联资产">
          {{ item.关联资产 }}：{{ item.原因.map((reason) => reason.message).join('；') }}
        </li>
        <li v-if="!lastBatch.未发布表.length">无</li>
      </ul>
    </div>

    <p v-if="message" class="error-text" data-testid="publish-message">{{ message }}</p>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import { confirmUpstreamFields, previewPublication, publishPublication } from '@/api/backend-service'
import type {
  PublicationBatch,
  PublicationDependency,
  PublicationPreview,
  PublicationTablePayload,
  PublicationVerdict,
} from '@/data/publication'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ entries: EntryRow[] }>()

const selection = ref<number[]>([])
const batchKey = ref('')
const verdicts = ref<PublicationVerdict[]>([])
const lastBatch = ref<PublicationBatch | null>(null)
const busy = ref(false)
const message = ref('')

const allSelected = computed(
  () => props.entries.length > 0 && selection.value.length === props.entries.length,
)

function toggleAll(checked: boolean) {
  selection.value = checked ? props.entries.map((row) => Number(row.id)) : []
}

function toggleRow(id: number, checked: boolean) {
  selection.value = checked
    ? Array.from(new Set([...selection.value, id]))
    : selection.value.filter((item) => item !== id)
}

function toPayload(): PublicationTablePayload[] {
  return selection.value.map((id) => {
    const row = props.entries.find((item) => Number(item.id) === id)
    const fields = String(row?.['字段清单'] ?? '')
    return {
      entry_id: id,
      asset: String(row?.['关联资产'] ?? ''),
      version: String(row?.['版本编号'] ?? ''),
      fields: fields || null,
    }
  })
}

function unconfirmedDeps(verdict: PublicationVerdict): PublicationDependency[] {
  return verdict.dependencies.filter(
    (dep) => dep.required.length > 0 && dep.confirmed !== true
      && verdict.reasons.some((reason) => reason.code === 'upstream_unconfirmed'
        && reason.related.includes(dep.upstream)),
  )
}

function formatDependencies(dependencies: PublicationDependency[]): string {
  if (!dependencies.length) return '—'
  return dependencies
    .map((dep) => `${dep.upstream}${dep.version ? `@${dep.version}` : ''}`)
    .join('、')
}

async function preview() {
  if (!selection.value.length) {
    message.value = '请先勾选要发布的 Schema 版本'
    return
  }
  busy.value = true
  message.value = ''
  try {
    const result: PublicationPreview = await previewPublication(
      toPayload(), batchKey.value || undefined)
    verdicts.value = result.表结论
    lastBatch.value = result.batch ?? null
    if (result.replayed && result.batch) {
      message.value = `该次发布已存在批次 ${result.batch.批次编号}，重复提交不会生成新快照`
    }
  } catch (error) {
    message.value = error instanceof Error ? error.message : '预览失败'
  } finally {
    busy.value = false
  }
}

async function publish() {
  if (!selection.value.length) {
    message.value = '请先勾选要发布的 Schema 版本'
    return
  }
  busy.value = true
  message.value = ''
  try {
    const result = await publishPublication(toPayload(), batchKey.value || undefined)
    lastBatch.value = result.batch
    message.value = result.message
    verdicts.value = []
  } catch (error) {
    message.value = error instanceof Error ? error.message : '发布失败'
  } finally {
    busy.value = false
  }
}

async function confirm(verdict: PublicationVerdict, dependency: PublicationDependency) {
  busy.value = true
  message.value = ''
  try {
    const result = await confirmUpstreamFields({
      downstream: verdict.asset,
      upstream: dependency.upstream,
      version: dependency.version,
      fields: dependency.required.map((field) => field.name),
    })
    message.value = result.message
    await preview()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '确认失败'
  } finally {
    busy.value = false
  }
}
</script>
