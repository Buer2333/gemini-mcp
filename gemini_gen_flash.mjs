import { GoogleGenAI, Modality } from '@google/genai'
import fs from 'fs'

const ai = new GoogleGenAI({ apiKey: process.env.GEMINI_API_KEY })
const prompt = process.argv[2]
const outputPath = process.argv[3]
const model = process.env.GEMINI_IMAGE_MODEL || 'gemini-2.5-flash-image-preview'

const response = await ai.models.generateContent({
  model,
  contents: prompt,
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
    console.log('Saved:', outputPath, buf.length, 'bytes', 'model=' + model)
    process.exit(0)
  }
}
console.error('No image in response')
process.exit(1)
