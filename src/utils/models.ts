/**
 * 按任务类型选 Gemini 模型（环境变量可覆盖）。模型选择真源与依据见
 * ~/claude-workspace/data/_shared/references/ai-generation-routing.md
 */

/**
 * 看视频类调用用的模型：GEMINI_VIDEO_MODEL → GEMINI_PRO_MODEL → 默认 Pro。
 * 视频理解单独配，是因为 2026-11-21 同片对照里 gemini-4-neon 找出的具体问题明显多于 3.1 Pro，
 * 但单价约 2.3 倍、时延约 3 倍，不适合给文本类工具当默认。
 */
export function getVideoModelName(): string {
  return process.env.GEMINI_VIDEO_MODEL || process.env.GEMINI_PRO_MODEL || 'gemini-3-pro-preview'
}

const DEFAULT_PRO_MODEL = 'gemini-3-pro-preview'
const DEFAULT_FLASH_MODEL = 'gemini-3-flash-preview'

/**
 * 短名别名 -> 完整模型 id。2026-11-22 用 API models.list 实拉（支持 generateContent）。
 * 新模型不需要进这张表：直接传完整 id（'gemini-' 开头）即可，原样透传。
 */
export const MODEL_ALIASES: Record<string, string> = {
  argon: 'gemini-4-argon',
  carbon: 'gemini-4-carbon',
  hydrogen: 'gemini-4-hydrogen',
  neon: 'gemini-4-neon',
}

/** 走环境变量的保留别名 */
const ENV_ALIASES = ['pro', 'flash', 'video'] as const

export type ModelKind = 'text' | 'video' | 'flash'

/** 所有可用别名（用于报错提示） */
export function listModelAliases(): string[] {
  return [...ENV_ALIASES, ...Object.keys(MODEL_ALIASES)]
}

/**
 * 解析 model 参数成真实模型 id。
 * - 不传：按 kind 取默认（text -> GEMINI_PRO_MODEL；flash -> GEMINI_FLASH_MODEL；video -> getVideoModelName()）
 * - 'pro' / 'flash' / 'video'：对应环境变量（与旧行为一致）
 * - 短名别名（argon/carbon/hydrogen/neon）：见 MODEL_ALIASES
 * - 'gemini-' 开头的完整 id：原样使用，不维护白名单；名字错了由 API 报错（可用 gemini-list-models 查）
 * - 其他：抛错并列出可用别名
 */
export function resolveModel(choice?: string, kind: ModelKind = 'text'): string {
  const pro = () => process.env.GEMINI_PRO_MODEL || DEFAULT_PRO_MODEL
  const flash = () => process.env.GEMINI_FLASH_MODEL || DEFAULT_FLASH_MODEL

  const c = choice?.trim()
  if (!c) {
    if (kind === 'video') return getVideoModelName()
    if (kind === 'flash') return flash()
    return pro()
  }
  const key = c.toLowerCase()
  if (key === 'pro') return pro()
  if (key === 'flash') return flash()
  if (key === 'video') return getVideoModelName()
  if (key in MODEL_ALIASES) return MODEL_ALIASES[key]
  if (key.startsWith('gemini-')) return c
  throw new Error(
    `Unknown model "${choice}". Use an alias (${listModelAliases().join(', ')}) or a full model id starting with "gemini-" ` +
      `(e.g. gemini-4-neon). Run the gemini-list-models tool to see available ids.`
  )
}

/** model 参数的统一 describe 文案 */
export const MODEL_PARAM_DESCRIPTION =
  'Model: alias pro/flash/video/argon/carbon/hydrogen/neon, or any full model id (e.g. gemini-4-neon). ' +
  'Use gemini-list-models to see available ids.'
