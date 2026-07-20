/**
 * Interactions API response parsing (SDK v2 shape)
 *
 * The Interactions API returns an interaction whose work is recorded as `steps`.
 * Model-authored text lives in `model_output` steps as `text` content blocks.
 * Older SDKs (<2.0.0) exposed a flat `outputs` array instead; the request schema
 * behind it was retired by Google in May 2026.
 */

/** Normalized status used by the deep research tools. */
export type ResearchStatus = 'processing' | 'completed' | 'failed'

/** Terminal statuses that mean the interaction produced no usable result. */
const FAILURE_STATUSES = new Set(['failed', 'cancelled', 'incomplete', 'budget_exceeded'])

/**
 * Map an Interactions API status onto the three states the tools care about.
 *
 * `requires_action` stays in `processing`: it only occurs with collaborative
 * planning, which these tools do not enable.
 */
export function mapInteractionStatus(status: string | undefined): ResearchStatus {
  if (status === 'completed') return 'completed'
  if (status && FAILURE_STATUSES.has(status)) return 'failed'
  return 'processing'
}

/**
 * Extract model-authored text blocks from an interaction, oldest first.
 *
 * Accepts both the v2 `steps` shape and the pre-2.0 `outputs` shape so a partial
 * rollout or a replayed fixture cannot silently yield an empty report.
 */
export function extractInteractionText(interaction: unknown): string[] {
  if (!isRecord(interaction)) return []

  const texts: string[] = []

  for (const step of asArray(interaction.steps)) {
    if (!isRecord(step) || step.type !== 'model_output') continue
    for (const block of asArray(step.content)) {
      if (isRecord(block) && block.type === 'text' && typeof block.text === 'string') {
        texts.push(block.text)
      }
    }
  }

  // Pre-2.0 shape: text blocks sat directly on `outputs`.
  for (const output of asArray(interaction.outputs)) {
    if (isRecord(output) && output.type === 'text' && typeof output.text === 'string') {
      texts.push(output.text)
    }
  }

  return texts
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}
