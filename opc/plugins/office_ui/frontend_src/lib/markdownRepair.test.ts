import assert from 'node:assert/strict'
import test from 'node:test'

import { restoreCollapsedMarkdown } from './markdownRepair'

test('repairs Jiuwen Markdown after whitespace-only stream chunks are dropped', () => {
  const collapsed = [
    '无法获取实时数据。',
    '## 当前台风季节（2026年8月）',
    '8月是台风活跃期。',
    '### 官方来源',
    '1. **中央气象台** - http://typhoon.nmc.cn -权威发布',
    '2. **香港天文台** - https://www.hko.gov.hk -实时更新',
    '##查询方法：',
    '1. **访问官网**',
    '2. **查看路径**',
    '##防护提示：',
    '-随时关注预警',
    '-避免前往沿海',
  ].join('')

  const repaired = restoreCollapsedMarkdown(collapsed)

  assert.match(repaired, /。\n\n## 当前台风季节（2026年8月）\n\n8月是/)
  assert.match(repaired, /\n\n### 官方来源\n1\. \*\*中央气象台\*\*/)
  assert.match(repaired, /\n2\. \*\*香港天文台\*\*/)
  assert.match(repaired, /\n\n## 查询方法：\n1\. \*\*访问官网\*\*/)
  assert.match(repaired, /\n\n## 防护提示：\n- 随时关注预警\n- 避免前往沿海/)
})

test('leaves prose and valid Markdown unchanged', () => {
  const prose = '版本 1.2 使用 https://example.com/a-b，并支持 C#。'
  const markdown = '## 标题\n\n1. 第一项\n2. 第二项\n\n- A\n- B'

  assert.equal(restoreCollapsedMarkdown(prose), prose)
  assert.equal(restoreCollapsedMarkdown(markdown), markdown)
})

test('keeps Jiuwen heading emphasis scoped to the intended field', () => {
  const collapsed = [
    '根据配置，我可以访问以下路径：',
    '## 当前项目工作空间',
    '**当前项目目录**：`/workspace/0000`',
    '这是主要工作目录。',
    '## OpenOPC系统目录',
    '**OpenOPC根目录**：`/workspace/OpenOPC`',
    '-技能目录：`/workspace/OpenOPC/.opc/skills/`',
    '-项目内存：`/workspace/OpenOPC/.opc/memory/`',
    '##团队工作空间（共享）',
    '**绝对路径**：`/team-workspace`',
    '##系统目录',
    '**系统工作空间**：`/agent/workspace`',
    '---',
    '请告诉我需要查看哪个文件？',
  ].join('')

  const repaired = restoreCollapsedMarkdown(collapsed)

  assert.match(repaired, /## 当前项目工作空间\n\n\*\*当前项目目录\*\*/)
  assert.match(repaired, /## OpenOPC系统目录\n\n\*\*OpenOPC根目录\*\*/)
  assert.match(repaired, /\n- 技能目录：/)
  assert.match(repaired, /\n- 项目内存：/)
  assert.match(repaired, /## 团队工作空间（共享）\n\n\*\*绝对路径\*\*/)
  assert.match(repaired, /## 系统目录\n\n\*\*系统工作空间\*\*/)
  assert.match(repaired, /\n\n---\n\n请告诉我/)
  assert.equal(repaired.split('\n').filter((line) => line.startsWith('#')).some((line) => line.includes('**')), false)
})

test('repairs collapsed emoji bullets and GFM table rows', () => {
  const bullets = restoreCollapsedMarkdown([
    '可以修改代码。',
    '##我可以帮你做：',
    '- ✅读取代码文件',
    '- ✅分析代码结构',
    '- ✅修复错误',
  ].join(''))
  const table = restoreCollapsedMarkdown([
    '工具如下：',
    '## Core Tools',
    '| Tool | Purpose |',
    '|------|---------|',
    '| `read_file` | Read files |',
    '| `bash` | Run commands |',
  ].join(''))

  assert.match(bullets, /## 我可以帮你做：\n- ✅读取代码文件/)
  assert.match(bullets, /\n- ✅分析代码结构\n- ✅修复错误/)
  assert.match(table, /## Core Tools\n\| Tool \| Purpose \|\n\|------\|---------\|/)
  assert.match(table, /\n\| `read_file` \| Read files \|\n\| `bash` \| Run commands \|/)
})

test('repairs an isolated heading field without changing valid heading emphasis', () => {
  assert.equal(
    restoreCollapsedMarkdown('## 工作空间**路径**：`/workspace`'),
    '## 工作空间\n\n**路径**：`/workspace`',
  )
  assert.equal(
    restoreCollapsedMarkdown('## Release **v2**: notes'),
    '## Release **v2**: notes',
  )
})
