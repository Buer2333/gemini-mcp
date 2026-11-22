/**
 * The model parameter of every model-selectable tool must accept aliases and full ids,
 * and stay optional (omitting it keeps the previous default behaviour).
 */
import { describe, it, expect, vi } from 'vitest'
import { z } from 'zod'
import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { registerQueryTool } from '../query.js'
import { registerImageAnalyzeTool } from '../image-analyze.js'
import { registerTokenCountTool } from '../token-count.js'
import { registerDocumentTool } from '../document.js'
import { registerYouTubeTool } from '../youtube.js'
import { registerListModelsTool, listGenerateContentModels } from '../list-models.js'

vi.mock('../../utils/logger.js', () => ({
  logger: { warn: vi.fn(), debug: vi.fn(), info: vi.fn(), error: vi.fn(), prompt: vi.fn(), response: vi.fn() },
}))

function captureTools(register: (s: McpServer) => void): Record<string, z.ZodRawShape> {
  const tools: Record<string, z.ZodRawShape> = {}
  const fake = {
    tool: (name: string, shape: z.ZodRawShape) => {
      tools[name] = shape
    },
  } as unknown as McpServer
  register(fake)
  return tools
}

const cases: Array<[string, (s: McpServer) => void]> = [
  ['gemini-query', registerQueryTool],
  ['gemini-analyze-image', registerImageAnalyzeTool],
  ['gemini-count-tokens', registerTokenCountTool],
  ['gemini-analyze-document', registerDocumentTool],
  ['gemini-youtube', registerYouTubeTool],
]

describe('model parameter on tools', () => {
  for (const [toolName, register] of cases) {
    it(`${toolName} accepts alias, full id, and omission`, () => {
      const shape = captureTools(register)[toolName]
      expect(shape).toBeDefined()
      const schema = shape.model
      expect(schema).toBeDefined()
      expect(schema.safeParse(undefined).success).toBe(true)
      for (const v of ['pro', 'flash', 'video', 'argon', 'carbon', 'hydrogen', 'neon', 'gemini-4-neon']) {
        expect(schema.safeParse(v).success).toBe(true)
      }
      expect(schema.description).toContain('gemini-list-models')
    })
  }
})

describe('gemini-list-models', () => {
  it('registers under the expected name', () => {
    expect(Object.keys(captureTools(registerListModelsTool))).toEqual(['gemini-list-models'])
  })

  it('paginates, keeps only generateContent models, applies filter', async () => {
    const pages = [
      {
        models: [
          {
            name: 'models/gemini-4-neon',
            displayName: 'Gemini 4 Neon',
            inputTokenLimit: 1000000,
            supportedGenerationMethods: ['generateContent'],
          },
          { name: 'models/text-embedding-9', displayName: 'Embed', supportedGenerationMethods: ['embedContent'] },
        ],
        nextPageToken: 'p2',
      },
      {
        models: [
          {
            name: 'models/gemini-3.5-flash',
            displayName: 'Gemini 3.5 Flash',
            inputTokenLimit: 500000,
            supportedGenerationMethods: ['generateContent'],
          },
        ],
      },
    ]
    let i = 0
    const fakeFetch = vi.fn(async () => ({ ok: true, json: async () => pages[i++] }) as Response)

    const all = await listGenerateContentModels('k', undefined, fakeFetch as unknown as typeof fetch)
    expect(all.map((m) => m.id)).toEqual(['gemini-3.5-flash', 'gemini-4-neon'])
    expect(fakeFetch).toHaveBeenCalledTimes(2)

    i = 0
    const filtered = await listGenerateContentModels('k', 'NEON', fakeFetch as unknown as typeof fetch)
    expect(filtered).toEqual([{ id: 'gemini-4-neon', displayName: 'Gemini 4 Neon', inputTokenLimit: 1000000 }])
  })

  it('throws on HTTP error', async () => {
    const fakeFetch = vi.fn(async () => ({ ok: false, status: 403, text: async () => 'denied' }) as Response)
    await expect(listGenerateContentModels('k', undefined, fakeFetch as unknown as typeof fetch)).rejects.toThrow(/403/)
  })
})
