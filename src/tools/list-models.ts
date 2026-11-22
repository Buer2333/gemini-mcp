/**
 * List Models Tool - Discover which Gemini model ids the API key can use
 *
 * Calls v1beta/models (paginated) and returns the models that support generateContent.
 * Use the returned ids with the `model` parameter of the other tools.
 */

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { z } from 'zod'
import { logger } from '../utils/logger.js'

const MODELS_ENDPOINT = 'https://generativelanguage.googleapis.com/v1beta/models'
const MAX_PAGES = 20

export interface ListedModel {
  id: string
  displayName: string
  inputTokenLimit?: number
}

interface RawModel {
  name?: string
  displayName?: string
  inputTokenLimit?: number
  supportedGenerationMethods?: string[]
}

/**
 * Fetch every page of models.list and keep the ones supporting generateContent.
 * `filter` is a case-insensitive substring matched against id and displayName.
 */
export async function listGenerateContentModels(
  apiKey: string,
  filter?: string,
  fetchFn: typeof fetch = fetch
): Promise<ListedModel[]> {
  const models: ListedModel[] = []
  let pageToken: string | undefined

  for (let page = 0; page < MAX_PAGES; page++) {
    const url = new URL(MODELS_ENDPOINT)
    url.searchParams.set('pageSize', '1000')
    if (pageToken) url.searchParams.set('pageToken', pageToken)

    const res = await fetchFn(url, { headers: { 'x-goog-api-key': apiKey } })
    if (!res.ok) {
      const body = await res.text().catch(() => '')
      throw new Error(`models.list failed: HTTP ${res.status} ${body.substring(0, 200)}`)
    }
    const data = (await res.json()) as { models?: RawModel[]; nextPageToken?: string }

    for (const m of data.models ?? []) {
      if (!m.supportedGenerationMethods?.includes('generateContent')) continue
      models.push({
        id: (m.name ?? '').replace(/^models\//, ''),
        displayName: m.displayName ?? '',
        inputTokenLimit: m.inputTokenLimit,
      })
    }

    pageToken = data.nextPageToken
    if (!pageToken) break
  }

  const needle = filter?.trim().toLowerCase()
  const result = needle
    ? models.filter((m) => m.id.toLowerCase().includes(needle) || m.displayName.toLowerCase().includes(needle))
    : models
  return result.sort((a, b) => a.id.localeCompare(b.id))
}

/**
 * Register list-models tool with the MCP server
 */
export function registerListModelsTool(server: McpServer): void {
  server.tool(
    'gemini-list-models',
    {
      filter: z
        .string()
        .optional()
        .describe('Optional case-insensitive substring to filter model ids / display names (e.g. "flash", "gemini-4")'),
    },
    async ({ filter }) => {
      logger.info(`Listing Gemini models${filter ? ` (filter: ${filter})` : ''}`)

      try {
        const apiKey = process.env.GEMINI_API_KEY
        if (!apiKey) {
          throw new Error('GEMINI_API_KEY not set')
        }

        const models = await listGenerateContentModels(apiKey, filter)
        if (models.length === 0) {
          return { content: [{ type: 'text', text: 'No models found supporting generateContent.' }] }
        }

        const lines = models.map(
          (m) => `${m.id} | ${m.displayName} | ${m.inputTokenLimit !== undefined ? m.inputTokenLimit : 'n/a'}`
        )
        const text =
          `${models.length} model(s) supporting generateContent (id | displayName | inputTokenLimit).\n` +
          `Pass an id (or alias pro/flash/video/argon/carbon/hydrogen/neon) as the \`model\` parameter of other tools.\n\n` +
          lines.join('\n')

        return { content: [{ type: 'text', text }] }
      } catch (error) {
        const errorMessage = error instanceof Error ? error.message : String(error)
        logger.error(`Error listing models: ${errorMessage}`)
        return {
          content: [{ type: 'text', text: `Error listing models: ${errorMessage}` }],
          isError: true,
        }
      }
    }
  )
}
