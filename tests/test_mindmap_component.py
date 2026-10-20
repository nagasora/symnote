"""キャンバスの再描画と子追加ボタンのイベント契約を検証する。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


COMPONENT_HTML = (
    Path(__file__).parents[1]
    / "src"
    / "symnote"
    / "components"
    / "mindmap_canvas"
    / "index.html"
)


def test_component_clears_old_edges_before_redrawing() -> None:
    """再描画時は古い接続線を消してから新しいノードを処理する。"""
    source = COMPONENT_HTML.read_text(encoding="utf-8")
    clear_index = source.index("edges.replaceChildren();")
    draw_index = source.index("for (const node of nodes)", clear_index)
    assert clear_index < draw_index


def test_component_add_child_emits_valid_event_without_collapsing() -> None:
    """実際のJSで葉と親の追加ボタンが選択と一意なイベント送信だけを行う。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("JavaScriptのイベント検証にはNode.jsが必要です。")
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
const messages = [];
let cancelCount = 0;
let toggleCount = 0;
let selectionUpdates = 0;
let eventCounter = 0;
let selectedId = null;
let selectionTimer = 1;
const componentInstanceId = 'test-instance';
const latestArgs = {focus_ids: []};
const kindLabels = {root: 'ルート', idea: 'アイデア'};
const elementById = new Map();
const window = {parent: {postMessage: message => messages.push(message)}};
const clearTimeout = () => { cancelCount += 1; };
const updateSelection = () => { selectionUpdates += 1; };
const selectNode = () => { throw new Error('追加で通常選択の遅延処理は呼ばない'); };
const toggleNode = () => { toggleCount += 1; };
const hasChildren = node => Boolean(node.has_children || node.children.length);
const isCollapsed = node => Boolean(node.collapsed);
const makeElement = () => ({
  children: [], style: {}, attributes: {},
  classList: {toggle: () => {}},
  setAttribute(name, value) { this.attributes[name] = value; },
  append(...children) { this.children.push(...children); },
  appendChild(child) { this.children.push(child); },
});
const document = {createElement: makeElement};
const stage = makeElement();
function load(name) {
  const start = source.indexOf(`    function ${name}(`);
  assert.ok(start >= 0);
  const end = source.indexOf('\n    /**', start);
  return source.slice(start, end).trim();
}
eval(load('send') + '\n' + load('createNode') + `
for (const parent of [false, true]) {
  const node = {id: parent ? 2 : 1, kind: 'idea', title: '安全な<タイトル>', children: [], has_children: parent};
  const wrapper = createNode(node);
  const actions = wrapper.children[1];
  const add = actions.children.find(child => child.className === 'add-child');
  assert.ok(add);
  assert.equal(add.attributes['aria-label'], '安全な<タイトル>に子ノードを追加');
  assert.equal(actions.children.length, parent ? 2 : 1);
  const before = messages.length;
  add.onclick();
  assert.equal(selectedId, node.id);
  assert.equal(selectionTimer, null);
  assert.equal(messages.length, before + 1);
  assert.deepEqual(messages.at(-1).value, {
    type: 'add_child', node_id: node.id, event_id: 'test-instance:' + messages.length,
  });
  assert.equal(messages.at(-1).type, 'streamlit:setComponentValue');
  assert.equal(toggleCount, 0);
  if (parent) {
    const collapse = actions.children.find(child => child.className === 'collapse');
    assert.equal(collapse.textContent, '−');
    collapse.onclick();
    assert.equal(toggleCount, 1);
  }
}
assert.equal(cancelCount, 2);
assert.equal(selectionUpdates, 2);
`);
"""
    result = subprocess.run(
        [node, "-e", script, str(COMPONENT_HTML)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
