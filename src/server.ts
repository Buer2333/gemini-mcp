/**
 * MCP Server for Gemini
 *
 * Provides Gemini models as MCP tools for Claude Code integration.
 * This is the original MCP server functionality, now modularized.
 */

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { parseArgs } from 'node:util'

// Import tools
import { getEnabledToolGroups, TOOL_GROUPS } from './tools/tool-groups.js'
import { registerQueryTool } from './tools/query.js'
import { registerBrainstormTool } from './tools/brainstorm.js'
import { registerAnalyzeTool } from './tools/analyze.js'
import { registerSummarizeTool } from './tools/summarize.js'
import { registerImageGenTool } from './tools/image-gen.js'
import { registerImageEditTool } from './tools/image-edit.js'
import { registerVideoGenTool } from './tools/video-gen.js'
import { registerCodeExecTool } from './tools/code-exec.js'
import { registerSearchTool } from './tools/search.js'
import { registerStructuredTool } from './tools/structured.js'
import { registerYouTubeTool } from './tools/youtube.js'
import { registerDocumentTool } from './tools/document.js'
import { registerUrlContextTool } from './tools/url-context.js'
import { registerCacheTool } from './tools/cache.js'
import { registerSpeechTool } from './tools/speech.js'
import { registerTokenCountTool } from './tools/token-count.js'
import { registerDeepResearchTool } from './tools/deep-research.js'
import { registerImageAnalyzeTool } from './tools/image-analyze.js'

// Import Gemini client and logger
import { initGeminiClient } from './gemini-client.js'
import { setupLogger, logger, LogLevel } from './utils/logger.js'

export async function startMcpServer(argv: string[]): Promise<void> {
  // Parse command line arguments
  const { values } = parseArgs({
    args: argv,
    options: {
      verbose: {
        type: 'boolean',
        short: 'v',
        default: false,
      },
      quiet: {
        type: 'boolean',
        short: 'q',
        default: false,
      },
      help: {
        type: 'boolean',
        short: 'h',
        default: false,
      },
    },
    allowPositionals: true,
  })

  // Show help if requested
  if (values.help) {
    console.log(`
MCP Server Gemini - Integrates Google's Gemini models with Claude Code

Usage:
  gemini-mcp [options]
  gemini serve [options]

Options:
  -v, --verbose    Enable verbose logging (shows all prompts and responses)
  -q, --quiet      Run in quiet mode (minimal logging)
  -h, --help       Show this help message

Environment Variables:
  GEMINI_API_KEY   (required) Your Google Gemini API key
  VERBOSE          (optional) Set to "true" to enable verbose logging
  QUIET            (optional) Set to "true" to enable quiet mode
  GEMINI_MODEL     (optional) Default Gemini model to use
  GEMINI_PRO_MODEL (optional) Specify Pro model variant
  GEMINI_FLASH_MODEL (optional) Specify Flash model variant
  GEMINI_ENABLED_TOOLS (optional) Comma-separated list of tool groups to load
  GEMINI_TOOL_PRESET   (optional) Preset profile: minimal, text, image, research, media, full

For CLI mode, run: gemini --help
  `)
    process.exit(0)
  }

  // Configure logging mode based on command line args or environment variables
  let logLevel: LogLevel = 'normal'
  if (values.verbose || process.env.VERBOSE === 'true') {
    logLevel = 'verbose'
  } else if (values.quiet || process.env.QUIET === 'true') {
    logLevel = 'quiet'
  }
  setupLogger(logLevel)

  // Check for required API key
  if (!process.env.GEMINI_API_KEY) {
    logger.error('Error: GEMINI_API_KEY environment variable is required')
    process.exit(1)
  }

  // Get model name from environment or use default
  const defaultModel = 'gemini-3-pro-preview'
  const geminiModel = process.env.GEMINI_MODEL || defaultModel

  // Log model configuration for debugging
  logger.debug(`Model configuration:
  - GEMINI_MODEL: ${process.env.GEMINI_MODEL || '(not set, using default)'}
  - GEMINI_PRO_MODEL: ${process.env.GEMINI_PRO_MODEL || '(not set, using default)'}
  - GEMINI_FLASH_MODEL: ${process.env.GEMINI_FLASH_MODEL || '(not set, using default)'}`)

  // Log tool configuration
  const enabledGroups = getEnabledToolGroups()
  logger.debug(`Tool configuration: ${enabledGroups.size} of ${Object.keys(TOOL_GROUPS).length} groups enabled`)
  logger.info(`Loading ${enabledGroups.size} tool groups`)

  logger.info(`Starting MCP Gemini Server with model: ${geminiModel}`)
  logger.info(`Logging mode: ${logLevel}`)

  // Handle unexpected stdio errors
  process.stdin.on('error', (err) => {
    logger.error('STDIN error:', err)
  })

  // stdin EOF/close = parent pipe gone. Exit immediately so a dead pipe never
  // gets polled in a busy-loop and the process never lingers as an orphan.
  process.stdin.on('end', () => {
    logger.warn('STDIN closed (EOF) — exiting')
    process.exit(0)
  })
  process.stdin.on('close', () => {
    logger.warn('STDIN closed — exiting')
    process.exit(0)
  })

  process.stdout.on('error', (err) => {
    logger.error('STDOUT error:', err)
  })

  try {
    // Initialize Gemini client
    await initGeminiClient()

    // Create MCP server
    const server = new McpServer({
      name: 'Gemini',
      version: '0.7.2',
    })

    // Registry map of group ID -> register function
    const toolRegistrations: Record<string, (server: McpServer) => void> = {
      query: registerQueryTool,
      brainstorm: registerBrainstormTool,
      analyze: registerAnalyzeTool,
      summarize: registerSummarizeTool,
      'image-gen': registerImageGenTool,
      'image-edit': registerImageEditTool,
      'video-gen': registerVideoGenTool,
      'code-exec': registerCodeExecTool,
      search: registerSearchTool,
      structured: registerStructuredTool,
      youtube: registerYouTubeTool,
      document: registerDocumentTool,
      'url-context': registerUrlContextTool,
      cache: registerCacheTool,
      speech: registerSpeechTool,
      'token-count': registerTokenCountTool,
      'deep-research': registerDeepResearchTool,
      'image-analyze': registerImageAnalyzeTool,
    }

    // Register tools based on configuration
    for (const [groupId, registerFn] of Object.entries(toolRegistrations)) {
      if (enabledGroups.has(groupId)) {
        registerFn(server)
      }
    }

    // Start server with stdio transport
    const transport = new StdioServerTransport()

    // Transport close = parent (Claude) gone. For a stdio server the stdin
    // pipe cannot be reconnected, so exit instead of looping — the previous
    // reconnect-with-backoff left orphaned processes (PPID=1) busy-looping on
    // the dead stdin fd at ~85% CPU. See task T-20260617-001.
    transport.onclose = () => {
      logger.warn('MCP transport closed (parent gone) — exiting')
      process.exit(0)
    }

    transport.onerror = (error) => {
      logger.error('MCP transport error:', error)
      if (error instanceof Error) {
        logger.debug(`Error name: ${error.name}, message: ${error.message}`)
        logger.debug(`Stack trace: ${error.stack}`)
      }
    }

    // Connect to transport
    try {
      logger.debug(`Process details - PID: ${process.pid}, Node version: ${process.version}`)
      logger.debug(
        `Environment variables: API_KEY=${process.env.GEMINI_API_KEY ? 'SET' : 'NOT_SET'}, VERBOSE=${process.env.VERBOSE || 'not set'}`
      )
      logger.debug(`Process stdin/stdout state - isTTY: ${process.stdin.isTTY}, ${process.stdout.isTTY}`)

      await server.connect(transport)
      logger.info('MCP Gemini Server running')
    } catch (err) {
      logger.error('Failed to connect MCP server transport:', err)

      if (err instanceof Error) {
        logger.debug(`Error stack: ${err.stack}`)
        logger.debug(`Error details: name=${err.name}, message=${err.message}`)
      } else {
        logger.debug(`Non-Error object thrown: ${JSON.stringify(err)}`)
      }

      logger.warn('Server will attempt to continue running despite connection error')
    }

    // Handle process termination
    process.on('SIGINT', async () => {
      logger.info('Shutting down MCP Gemini Server')
      await server.close()
      process.exit(0)
    })

    process.on('SIGTERM', async () => {
      logger.info('Shutting down MCP Gemini Server')
      await server.close()
      process.exit(0)
    })
  } catch (error) {
    logger.error('Failed to start MCP Gemini Server:', error)
    process.exit(1)
  }
}
