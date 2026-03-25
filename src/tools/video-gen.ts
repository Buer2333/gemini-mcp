/**
 * Video Generation Tool - Generate videos using Gemini's Veo model
 *
 * This tool generates videos from text descriptions. Video generation is async,
 * so we provide tools to start generation and check status.
 */

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { z } from 'zod'
import { startVideoGeneration, checkVideoStatus, getOutputDir } from '../gemini-client.js'
import { logger } from '../utils/logger.js'

/**
 * Register video generation tools with the MCP server
 */
export function registerVideoGenTool(server: McpServer): void {
  // Start video generation
  server.tool(
    'gemini-generate-video',
    {
      prompt: z.string().describe('Description of the video to generate (be detailed!)'),
      model: z
        .enum(['standard', 'fast'])
        .default('fast')
        .describe('Generation quality: "standard" ($0.40/s, highest quality) or "fast" ($0.15/s, faster & cheaper). Default: fast'),
      aspectRatio: z
        .enum(['16:9', '9:16'])
        .default('16:9')
        .describe('Aspect ratio (16:9 for landscape, 9:16 for portrait/mobile)'),
      negativePrompt: z.string().optional().describe('Things to avoid in the video (e.g., "text, watermarks, blurry")'),
      durationSeconds: z.number().optional().describe('Video duration in seconds (4, 6, or 8). Default depends on model.'),
      referenceImagePaths: z
        .array(z.string())
        .optional()
        .describe('Local file paths to 1-3 reference images. The AI will maintain visual consistency with these images (e.g., product photos, character portraits). Requires Veo 3.1+ model.'),
    },
    async ({ prompt, model, aspectRatio, negativePrompt, durationSeconds, referenceImagePaths }) => {
      logger.info(`Starting video generation: ${prompt.substring(0, 50)}...`)

      try {
        const modelOverride = model === 'standard' ? 'veo-3.1-generate-preview' : 'veo-3.1-fast-generate-preview'
        const result = await startVideoGeneration(prompt, {
          modelOverride,
          aspectRatio,
          negativePrompt,
          durationSeconds,
          referenceImagePaths,
        })

        return {
          content: [
            {
              type: 'text' as const,
              text: `Video generation started!

**Operation ID:** \`${result.operationName}\`
**Status:** ${result.status}

Video generation takes 1-5 minutes. Use the \`gemini-check-video\` tool with the operation ID above to check progress and download when complete.

**Tip:** Save the operation ID - you'll need it to check status and retrieve the video.`,
            },
          ],
        }
      } catch (error) {
        const errorMessage = error instanceof Error ? error.message : String(error)
        logger.error(`Error starting video generation: ${errorMessage}`)

        return {
          content: [
            {
              type: 'text' as const,
              text: `Error starting video generation: ${errorMessage}`,
            },
          ],
          isError: true,
        }
      }
    }
  )

  // Check video generation status
  server.tool(
    'gemini-check-video',
    {
      operationId: z.string().describe('The operation ID returned when starting video generation'),
    },
    async ({ operationId }) => {
      logger.info(`Checking video status: ${operationId}`)

      try {
        const result = await checkVideoStatus(operationId)

        if (result.status === 'completed') {
          return {
            content: [
              {
                type: 'text' as const,
                text: `Video generation complete!

**Status:** ${result.status}
${result.filePath ? `**Saved to:** ${result.filePath}` : ''}
${result.videoUri ? `**Video URI:** ${result.videoUri}` : ''}
**Output directory:** ${getOutputDir()}

The video has been downloaded and saved to disk.`,
              },
            ],
          }
        } else if (result.status === 'failed') {
          return {
            content: [
              {
                type: 'text' as const,
                text: `Video generation failed.

**Status:** ${result.status}
**Error:** ${result.error || 'Unknown error'}

Please try again with a different prompt.`,
              },
            ],
            isError: true,
          }
        } else {
          return {
            content: [
              {
                type: 'text' as const,
                text: `Video still generating...

**Status:** ${result.status}
**Operation ID:** ${result.operationName}

Please check again in 30-60 seconds using the \`gemini-check-video\` tool.`,
              },
            ],
          }
        }
      } catch (error) {
        const errorMessage = error instanceof Error ? error.message : String(error)
        logger.error(`Error checking video status: ${errorMessage}`)

        return {
          content: [
            {
              type: 'text' as const,
              text: `Error checking video status: ${errorMessage}`,
            },
          ],
          isError: true,
        }
      }
    }
  )
}
