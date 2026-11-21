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
