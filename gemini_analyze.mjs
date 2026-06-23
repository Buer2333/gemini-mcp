import { GoogleGenAI } from '@google/genai'
import fs from 'fs'

const ai = new GoogleGenAI({ apiKey: process.env.GEMINI_API_KEY })
const imagePath = process.argv[2]
const prompt = process.argv[3]

const imageData = fs.readFileSync(imagePath)
const base64 = imageData.toString('base64')
const mimeType = imagePath.endsWith('.png') ? 'image/png' : 'image/jpeg'

const response = await ai.models.generateContent({
  model: 'gemini-2.5-flash',
  contents: [
    {
      role: 'user',
      parts: [{ inlineData: { data: base64, mimeType } }, { text: prompt }],
    },
  ],
})

console.log(response.candidates[0].content.parts[0].text)
