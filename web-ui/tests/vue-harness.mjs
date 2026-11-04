import { createRenderer, defineComponent, h, nextTick } from 'vue'
import { createI18n } from 'vue-i18n'
import { sourceLoader } from './load-source.mjs'

// Vue's actual reactivity, setup, templates and watchers, without a browser or
// added DOM package. Only visual primitives and external API/router are doubled.
const node = (type, text = '') => ({ type, text, props: {}, children: [], parent: null })
const renderer = createRenderer({
  createElement: (type) => node(type),
  createText: (text) => node('#text', text),
  createComment: (text) => node('#comment', text),
  setText: (target, text) => { target.text = text },
  setElementText: (target, text) => { target.text = text; target.children = [] },
  parentNode: (target) => target.parent,
  nextSibling: (target) => target.parent?.children[target.parent.children.indexOf(target) + 1] || null,
  patchProp: (target, key, _previous, value) => { target.props[key] = value },
  insert(target, parent, anchor = null) {
    if (target.parent) target.parent.children.splice(target.parent.children.indexOf(target), 1)
    target.parent = parent
    const index = anchor ? parent.children.indexOf(anchor) : -1
    if (index < 0) parent.children.push(target)
    else parent.children.splice(index, 0, target)
  },
  remove(target) {
    if (target.parent) target.parent.children.splice(target.parent.children.indexOf(target), 1)
    target.parent = null
  },
})
const primitive = (name) => defineComponent({
  name,
  inheritAttrs: false,
  setup: (_props, { attrs, slots }) => () => h(name, attrs, slots.default?.()),
})
const dialogNames = ['Dialog', 'DialogContent', 'DialogDescription', 'DialogFooter', 'DialogHeader', 'DialogTitle', 'DialogTrigger']

export function mountSource(t, filename, props = {}, overrides = {}, locale = 'zh-CN') {
  const notices = []
  const load = sourceLoader({
    'vue-router': { useRoute: () => ({ query: {} }) },
    '@/components/ui/badge': { Badge: primitive('Badge') },
    '@/components/ui/button': { Button: primitive('Button') },
    '@/components/ui/dialog': Object.fromEntries(dialogNames.map((name) => [name, primitive(name)])),
    '@/components/ui/toast': { toast: (notice) => notices.push(notice) },
    '@/components/tasks/TaskForm.vue': { default: primitive('TaskForm') },
    ...overrides,
  })
  const component = load(filename).default
  const app = renderer.createApp(component, props)
  app.use(createI18n({
    legacy: false, locale,
    messages: {
      'zh-CN': load('src/i18n/messages/zh-CN-extra.ts').default,
      'en-US': load('src/i18n/messages/en-US-extra.ts').default,
    },
    missingWarn: false, fallbackWarn: false,
  }))
  const root = node('root')
  app.mount(root)
  t.after(() => app.unmount())
  return { root, notices }
}

export function allNodes(root) { return [root, ...root.children.flatMap(allNodes)] }
export function findNode(root, type) { return allNodes(root).find((entry) => entry.type === type) }
export function text(root) { return allNodes(root).filter((entry) => entry.type !== '#comment').map((entry) => entry.text).join(' ') }
export async function flush() { await Promise.resolve(); await nextTick(); await Promise.resolve(); await nextTick() }
