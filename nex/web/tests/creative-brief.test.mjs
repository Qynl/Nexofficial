import { test } from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import { buildRunCard, onRunPlan } from '../js/runview.js';

test('run card surfaces a bounded creative brief as inert text', () => {
  const dom = new JSDOM('<!doctype html><div id="scroller"></div>', {
    url: 'http://localhost/',
  });
  global.document = dom.window.document;
  document.getElementById('scroller').scrollTo = () => {};

  const card = buildRunCard({
    id: 'brief-run', content: 'Create a game', meta: { run_id: 'brief-run' },
  });
  document.body.appendChild(card);
  onRunPlan({
    run_id: 'brief-run',
    plan: {
      steps: [],
      creative_brief: {
        title: 'Tidekeeper',
        pitch: '<img src=x onerror=alert(1)> rescue adventure',
        core_loop: ['Read clues', 'Redirect water', 'Rescue a teammate'],
        acceptance_criteria: [{
          criterion: 'Complete a rescue',
          verification: 'Run one server and inspect its result',
        }],
      },
      creative_brief_health: {
        complete: false,
        missing: ['visual_direction', 'progression'],
      },
    },
  });

  const panel = card.querySelector('.run-creative-brief');
  assert.ok(panel);
  assert.equal(panel.hidden, false);
  assert.match(panel.textContent, /Creative contract · Tidekeeper/);
  assert.match(panel.textContent, /visual_direction, progression/);
  assert.match(panel.textContent, /Complete a rescue — Verify:/);
  assert.equal(panel.querySelector('img'), null,
    'model-proposed brief content must stay text, never become markup');
  assert.match(panel.textContent, /<img src=x onerror=alert\(1\)>/);

  dom.window.close();
  delete global.document;
});
