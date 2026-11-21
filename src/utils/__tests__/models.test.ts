import { describe, it, expect, afterEach } from 'vitest'
import { getVideoModelName } from '../models.js'

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
