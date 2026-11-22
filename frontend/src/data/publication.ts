/** 结构发布批次的前端数据层：多选预览、整组发布、批次回查和上游字段确认。 */

export type PublicationField = {
  name: string
  type: string | null
}

export type PublicationDependency = {
  upstream: string
  version: string | null
  required: PublicationField[]
  confirmed: boolean | null
}

export type PublicationReason = {
  code: string
  message: string
  related: string[]
}

export type PublicationVerdict = {
  asset: string
  entry_id: number | null
  version: string | null
  fields: PublicationField[]
  source_kind: string | null
  source_version: string | null
  dependencies: PublicationDependency[]
  eligible: boolean
  reasons: PublicationReason[]
}

export type PublicationTablePayload = {
  entry_id?: number | null
  asset: string
  version?: string | null
  fields?: PublicationField[] | string | null
  depends_on?: Record<string, { version?: string | null; requires?: string[]; confirmed?: boolean }>
}

export type PublicationPayload = {
  batch_key?: string
  tables: PublicationTablePayload[]
}

export type PublicationPreview = {
  ok: boolean
  persisted: boolean
  replayed: boolean
  batch: PublicationBatch | null
  表结论: PublicationVerdict[]
  可发布表: string[]
  不可发布表: string[]
  字段可用范围: Record<string, string[]>
  提交: Record<string, unknown>[]
  告警: string[]
}

export type PublicationBatch = {
  批次编号: string
  批次键: string | null
  发布状态: string
  提交表数: number
  已发布表数: number
  排除表数: number
  快照集合: {
    批次编号: string
    快照文件: string
    条目数: number
    快照: {
      批次编号: string
      快照编号: string
      关联资产: string
      来源版本: string
      字段依据: PublicationField[]
      依赖关系: PublicationDependency[]
    }[]
  }
  已发布表: {
    快照编号: string
    关联资产: string
    来源版本: string
    来源类型: string
    来源记录: number | null
    字段依据: PublicationField[]
    依赖关系: PublicationDependency[]
  }[]
  未发布表: {
    关联资产: string
    来源记录: number | null
    来源版本: string | null
    原因: PublicationReason[]
  }[]
  提交快照: Record<string, unknown>[]
  字段可用范围: { 发布前: Record<string, string[]>; 发布后: Record<string, string[]> }
  [key: string]: unknown
}

export type PublicationResult = {
  ok: boolean
  replayed: boolean
  message: string
  batch: PublicationBatch
}
