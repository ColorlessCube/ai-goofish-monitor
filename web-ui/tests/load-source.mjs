// Compile the real TypeScript sources in memory using the existing compiler.
// Only explicit test doubles replace modules; unresolved imports fail closed.
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { Script } from 'node:vm'
import ts from 'typescript'
import { parse, compileScript } from '@vue/compiler-sfc'

const require = createRequire(import.meta.url)
const root = fileURLToPath(new URL('../', import.meta.url))

export function sourceLoader(doubles = {}) {
  const cache = new Map()
  function load(filename) {
    const path = resolve(root, filename)
    if (cache.has(path)) return cache.get(path).exports
    let source = readFileSync(path, 'utf8')
    if (path.endsWith('.vue')) {
      const { descriptor } = parse(source, { filename: path })
      source = compileScript(descriptor, { id: filename, inlineTemplate: true }).content
    }
    const { outputText } = ts.transpileModule(source, {
      fileName: path,
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
    })
    const module = { exports: {} }
    cache.set(path, module)
    const localRequire = (name) => {
      if (Object.hasOwn(doubles, name)) return doubles[name]
      if (name.startsWith('@/') || name.startsWith('.')) {
        const target = name.startsWith('@/') ? resolve(root, 'src', name.slice(2)) : resolve(dirname(path), name)
        return load(/\.(ts|vue)$/.test(target) ? target : `${target}.ts`)
      }
      return require(name)
    }
    new Script(`(function(require, module, exports) {\n${outputText}\n})`, { filename: path })
      .runInThisContext()(localRequire, module, module.exports)
    return module.exports
  }
  return load
}

export function fakeWindow(t) {
  let sequence = 0
  const timers = new Map()
  const previous = globalThis.window
  const cleanup = []
  globalThis.window = {
    performance: globalThis.performance,
    setTimeout(callback) { const id = ++sequence; timers.set(id, callback); return id },
    clearTimeout(id) { timers.delete(id) },
  }
  t.after(() => { try { cleanup.forEach((callback) => callback()) } finally { globalThis.window = previous } })
  return {
    onCleanup: (callback) => cleanup.push(callback),
    timers,
    async tick() {
      const entries = [...timers.entries()]
      for (const [id, callback] of entries) { timers.delete(id); callback() }
      await Promise.resolve()
      await Promise.resolve()
    },
  }
}
