import assert from 'node:assert/strict'
import test from 'node:test'

import { compileProjectIdPolicy, toProjectSlug } from './projectIdPolicy'

test('policy compilation fails closed for missing, unsupported, or malformed input', () => {
  assert.equal(compileProjectIdPolicy(null), null)
  assert.equal(compileProjectIdPolicy({ version: 2, pattern: '[a-z]+' }), null)
  assert.equal(compileProjectIdPolicy({ version: 1, pattern: '' }), null)
  assert.equal(compileProjectIdPolicy({ version: 1, pattern: '[' }), null)
})

test('validation follows the server policy and requires a full match', () => {
  const words = compileProjectIdPolicy({ version: 1, pattern: '[a-z]+' })
  const numbers = compileProjectIdPolicy({ version: 1, pattern: '[0-9]+' })
  assert.ok(words)
  assert.ok(numbers)

  assert.equal(words.matches('project'), true)
  assert.equal(words.matches('project-1'), false)
  assert.equal(words.matches('project\n'), false)
  assert.equal(numbers.matches('123'), true)
  assert.equal(numbers.matches('project'), false)
})

test('slug generation remains deterministic for valid and problematic names', () => {
  assert.equal(toProjectSlug(' My App '), 'my-app')
  assert.equal(toProjectSlug('知识工程'), '')
  assert.equal(toProjectSlug('---'), '-')
  assert.equal(toProjectSlug('----'), '--')
  assert.equal(toProjectSlug('___'), '___')
  assert.equal(toProjectSlug('_abc'), '_abc')
  assert.equal(toProjectSlug('--abc'), '-abc')
})
