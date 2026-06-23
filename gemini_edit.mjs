import { GoogleGenAI, Modality } from '@google/genai'
import fs from 'fs'
import path from 'path'

const ai = new GoogleGenAI({ apiKey: process.env.GEMINI_API_KEY })
const inputPath = process.argv[2]
const editPrompt = process.argv[3]
const outputPath = process.argv[4]

// Read input image
const imageData = fs.readFileSync(inputPath)
const base64 = imageData.toString('base64')
const ext = path.extname(inputPath).toLowerCase()
const mimeType = ext === '.png' ? 'image/png' : 'image/jpeg'

const response = await ai.models.generateContent({
  model: 'gemini-3-pro-image-preview',
  contents: [
    {
      role: 'user',
      parts: [{ inlineData: { data: base64, mimeType } }, { text: editPrompt }],
    },
  ],
  config: {
    responseModalities: [Modality.TEXT, Modality.IMAGE],
    imageConfig: { personGeneration: 'allow_all' },
  },
})

if (!response.candidates?.[0]?.content?.parts) {
  console.error('No parts in response:', JSON.stringify(response.candidates?.[0], null, 2))
  process.exit(1)
}
for (const part of response.candidates[0].content.parts) {
  if (part.inlineData) {
    const buf = Buffer.from(part.inlineData.data, 'base64')
    fs.writeFileSync(outputPath, buf)
    console.log('Saved:', outputPath, buf.length, 'bytes')
    break
  }
}
