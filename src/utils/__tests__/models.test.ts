import { describe, it, expect, afterEach, beforeEach } from 'vitest'
import { getVideoModelName, resolveModel } from '../models.js'

const saved = { v: process.env.GEMINI_VIDEO_MODEL, p: process.env.GEMINI_PRO_MODEL }

afterEach(() => {
  if (saved.v === undefined) delete process.env.GEMINI_VIDEO_MODEL
  else process.env.GEMINI_VIDEO_MODEL = saved.v
  if (saved.p === undefined) delete process.env.GEMINI_PRO_MODEL
  else process.env.GEMINI_PRO_MODEL = saved.p
})

describe('getVideoModelName', () => {
  it('prefers GEMINI_VIDEO_MODEL', () => {
    process.env.GEMINI_VIDEO_MODEL = 'gemini-4-neon'
    process.env.GEMINI_PRO_MODEL = 'gemini-3.1-pro-preview'
    expect(getVideoModelName()).toBe('gemini-4-neon')
  })
  it('falls back to GEMINI_PRO_MODEL when video model unset', () => {
    delete process.env.GEMINI_VIDEO_MODEL
    process.env.GEMINI_PRO_MODEL = 'gemini-3.1-pro-preview'
    expect(getVideoModelName()).toBe('gemini-3.1-pro-preview')
  })
  it('falls back to default Pro when both unset', () => {
    delete process.env.GEMINI_VIDEO_MODEL
    delete process.env.GEMINI_PRO_MODEL
    expect(getVideoModelName()).toBe('gemini-3-pro-preview')
  })
})

describe('resolveModel', () => {
  const envKeys = ['GEMINI_VIDEO_MODEL', 'GEMINI_PRO_MODEL', 'GEMINI_FLASH_MODEL'] as const
  const savedEnv: Record<string, string | undefined> = {}
  beforeEach(() => {
    for (const k of envKeys) {
      savedEnv[k] = process.env[k]
      delete process.env[k]
    }
  })
  afterEach(() => {
    for (const k of envKeys) {
      if (savedEnv[k] === undefined) delete process.env[k]
      else process.env[k] = savedEnv[k]
    }
  })

  it('no choice: text -> default pro, flash -> default flash, video -> video model', () => {
    expect(resolveModel(undefined, 'text')).toBe('gemini-3-pro-preview')
    expect(resolveModel(undefined, 'flash')).toBe('gemini-3-flash-preview')
    expect(resolveModel(undefined, 'video')).toBe('gemini-3-pro-preview')
    expect(resolveModel('', 'text')).toBe('gemini-3-pro-preview')
  })

  it('no choice honors env overrides', () => {
    process.env.GEMINI_PRO_MODEL = 'gemini-3.1-pro-preview'
    process.env.GEMINI_FLASH_MODEL = 'gemini-3.8-flash'
    process.env.GEMINI_VIDEO_MODEL = 'gemini-4-neon'
    expect(resolveModel(undefined, 'text')).toBe('gemini-3.1-pro-preview')
    expect(resolveModel(undefined, 'flash')).toBe('gemini-3.8-flash')
    expect(resolveModel(undefined, 'video')).toBe('gemini-4-neon')
  })

  it('video falls back to GEMINI_PRO_MODEL when GEMINI_VIDEO_MODEL unset', () => {
    process.env.GEMINI_PRO_MODEL = 'gemini-3.1-pro-preview'
    expect(resolveModel(undefined, 'video')).toBe('gemini-3.1-pro-preview')
  })

  it("'pro' / 'flash' / 'video' map to env vars regardless of kind", () => {
    process.env.GEMINI_PRO_MODEL = 'p-model'
    process.env.GEMINI_FLASH_MODEL = 'f-model'
    process.env.GEMINI_VIDEO_MODEL = 'v-model'
    for (const kind of ['text', 'flash', 'video'] as const) {
      expect(resolveModel('pro', kind)).toBe('p-model')
      expect(resolveModel('flash', kind)).toBe('f-model')
      expect(resolveModel('video', kind)).toBe('v-model')
    }
  })

  it("'pro' / 'flash' use defaults when env unset", () => {
    expect(resolveModel('pro', 'flash')).toBe('gemini-3-pro-preview')
    expect(resolveModel('flash', 'text')).toBe('gemini-3-flash-preview')
  })

  it('short aliases map to gemini-4-<name>', () => {
    expect(resolveModel('argon', 'text')).toBe('gemini-4-argon')
    expect(resolveModel('carbon', 'text')).toBe('gemini-4-carbon')
    expect(resolveModel('hydrogen', 'flash')).toBe('gemini-4-hydrogen')
    expect(resolveModel('neon', 'video')).toBe('gemini-4-neon')
  })

  it('short aliases are not affected by env overrides', () => {
    process.env.GEMINI_PRO_MODEL = 'x'
    expect(resolveModel('neon', 'text')).toBe('gemini-4-neon')
  })

  it('full gemini- ids pass through unchanged', () => {
    expect(resolveModel('gemini-4-neon', 'text')).toBe('gemini-4-neon')
    expect(resolveModel('gemini-3.5-flash-lite', 'video')).toBe('gemini-3.5-flash-lite')
    expect(resolveModel('gemini-future-9000', 'flash')).toBe('gemini-future-9000')
  })

  it('invalid values throw and list the aliases', () => {
    expect(() => resolveModel('gpt-5', 'text')).toThrow(/Unknown model "gpt-5"/)
    expect(() => resolveModel('gpt-5', 'text')).toThrow(/pro, flash, video, argon, carbon, hydrogen, neon/)
    expect(() => resolveModel('gpt-5', 'text')).toThrow(/gemini-list-models/)
  })
})
