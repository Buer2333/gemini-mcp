// M5迁移根治：原从旧机 npx 缓存绝对路径 import（机器特定哈希，跨机必炸）→ 改包名解析（undici 已入 package.json）
import { setGlobalDispatcher, EnvHttpProxyAgent } from 'undici'
setGlobalDispatcher(new EnvHttpProxyAgent())
