/**
 * Tests for Interactions API response parsing.
 *
 * Fixtures follow the SDK v2 shape (`steps` with `model_output` blocks), which
 * replaced the flat `outputs` array retired by Google in May 2026.
 */

import { describe, it, expect } from 'vitest'
import { extractInteractionText, mapInteractionStatus } from '../interaction-parse.js'

describe('mapInteractionStatus', () => {
  it('maps completed to completed', () => {
    expect(mapInteractionStatus('completed')).toBe('completed')
  })

  it.each(['failed', 'cancelled', 'incomplete', 'budget_exceeded'])('maps %s to failed', (status) => {
    expect(mapInteractionStatus(status)).toBe('failed')
  })

  it('treats in_progress and requires_action as still processing', () => {
    expect(mapInteractionStatus('in_progress')).toBe('processing')
    expect(mapInteractionStatus('requires_action')).toBe('processing')
  })

  it('treats unknown or missing status as still processing', () => {
    expect(mapInteractionStatus('some_future_status')).toBe('processing')
    expect(mapInteractionStatus(undefined)).toBe('processing')
  })
})

describe('extractInteractionText', () => {
  it('extracts text from model_output steps in order', () => {
    const interaction = {
      status: 'completed',
      steps: [
        { type: 'user_input', content: [{ type: 'text', text: 'the question' }] },
        { type: 'thought', content: [{ type: 'text', text: 'internal reasoning' }] },
        { type: 'model_output', content: [{ type: 'text', text: 'first part' }] },
        { type: 'model_output', content: [{ type: 'text', text: 'second part' }] },
      ],
    }

    expect(extractInteractionText(interaction)).toEqual(['first part', 'second part'])
  })

  it('ignores non-text content blocks inside model_output', () => {
    const interaction = {
      steps: [
        {
          type: 'model_output',
          content: [
            { type: 'image', image: { uri: 'https://example.com/chart.png' } },
            { type: 'text', text: 'the report' },
          ],
        },
      ],
    }

    expect(extractInteractionText(interaction)).toEqual(['the report'])
  })

  it('ignores tool-call steps that carry no model text', () => {
    const interaction = {
      steps: [
        { type: 'google_search_call', arguments: { queries: ['meta ads benchmarks'] } },
        { type: 'google_search_result', results: [{ url: 'https://example.com' }] },
      ],
    }

    expect(extractInteractionText(interaction)).toEqual([])
  })

  it('still reads the pre-2.0 flat outputs shape', () => {
    const interaction = {
      status: 'completed',
      outputs: [
        { type: 'text', text: 'legacy report' },
        { type: 'other', text: 'not model text' },
      ],
    }

    expect(extractInteractionText(interaction)).toEqual(['legacy report'])
  })

  it('returns empty array for malformed or empty interactions', () => {
    expect(extractInteractionText(undefined)).toEqual([])
    expect(extractInteractionText(null)).toEqual([])
    expect(extractInteractionText({})).toEqual([])
    expect(extractInteractionText({ steps: 'not-an-array' })).toEqual([])
    expect(extractInteractionText({ steps: [{ type: 'model_output' }] })).toEqual([])
  })
})
