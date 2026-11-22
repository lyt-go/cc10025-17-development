import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

export async function previewPublication(
  tables: import('@/data/publication').PublicationTablePayload[],
  batchKey?: string,
): Promise<import('@/data/publication').PublicationPreview> {
  const response = await request('/api/schema-publications/preview', {
    method: 'POST',
    body: JSON.stringify({ batch_key: batchKey, tables }),
  })
  if (!response.ok) throw new Error('结构发布预览失败')
  return await response.json()
}

export async function publishPublication(
  tables: import('@/data/publication').PublicationTablePayload[],
  batchKey?: string,
): Promise<import('@/data/publication').PublicationResult> {
  const response = await request('/api/schema-publications/publish', {
    method: 'POST',
    body: JSON.stringify({ batch_key: batchKey, tables }),
  })
  if (!response.ok) throw new Error('结构发布失败')
  return await response.json()
}

export async function confirmUpstreamFields(payload: {
  downstream: string
  upstream: string
  version?: string | null
  fields?: string[]
}): Promise<{ ok: boolean; message: string }> {
  const response = await request('/api/schema-publications/confirm', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  if (!response.ok) throw new Error('上游字段确认失败')
  return await response.json()
}
