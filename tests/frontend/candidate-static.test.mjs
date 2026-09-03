import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../../", import.meta.url);

test("candidate page exposes the editable voice draft controls", async () => {
  const template = await readFile(new URL("templates/candidate.html", root), "utf8");
  assert.match(template, /id="microphone"/);
  assert.match(template, /id="interim-transcript"/);
  assert.match(template, /id="read-aloud"/);
  assert.match(template, /type="module" src="\/static\/candidate\.mjs"/);
  assert.doesNotMatch(template, /candidate\.js/);
});

test("voice drafts use the existing turn API and do not persist audio", async () => {
  const candidate = await readFile(new URL("static/candidate.mjs", root), "utf8");
  assert.match(candidate, /\/turns/);
  assert.match(candidate, /input_mode: inputMode/);
  assert.match(candidate, /SpeechRecognition/);
  assert.match(candidate, /speechSynthesis/);
  assert.doesNotMatch(candidate, /MediaRecorder/);
  assert.doesNotMatch(candidate, /\/audio/);
});
