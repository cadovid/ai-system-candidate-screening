import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../../", import.meta.url);

test("candidate page exposes the editable voice draft controls", async () => {
  const template = await readFile(new URL("templates/candidate.html", root), "utf8");
  assert.match(template, /<link rel="icon" href="\/favicon\.ico" type="image\/svg\+xml">/);
  assert.match(template, /id="microphone"/);
  assert.match(template, /id="interim-transcript"/);
  assert.match(template, /id="read-aloud"/);
  assert.match(template, /type="module" src="\/static\/candidate\.mjs"/);
  assert.doesNotMatch(template, /candidate\.js/);
});

test("the favicon asset is available", async () => {
  const favicon = await readFile(new URL("static/favicon.svg", root), "utf8");
  assert.match(favicon, /^<svg[\s>]/);
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

test("successful in-progress turns are not treated as terminal conversations", async () => {
  const candidate = await readFile(new URL("static/candidate.mjs", root), "utf8");
  assert.match(candidate, /Only the[\s\S]*screening status determines whether the conversation should be locked/);
  assert.doesNotMatch(candidate, /data\.status === "completed"/);
  assert.match(
    candidate,
    /\["qualified", "disqualified", "needs_review", "abandoned"\]\.includes\(data\.screening_status\)/,
  );
});
