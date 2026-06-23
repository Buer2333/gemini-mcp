import { GoogleGenAI, Modality } from '@google/genai'
import fs from 'fs'

const ai = new GoogleGenAI({ apiKey: process.env.GEMINI_API_KEY })
const prompt = process.argv[2]
const outputPath = process.argv[3]

const response = await ai.models.generateContent({
  model: 'gemini-3-pro-image-preview',
  contents: prompt,
  config: {
    responseModalities: [Modality.TEXT, Modality.IMAGE],
    imageConfig: {
      aspectRatio: '9:16',
      imageSize: '2K',
    },
  },
})

for (const part of response.candidates[0].content.parts) {
  if (part.inlineData) {
    const buf = Buffer.from(part.inlineData.data, 'base64')
    fs.writeFileSync(outputPath, buf)
    console.log('Saved:', outputPath, buf.length, 'bytes')
    break
  }
}
