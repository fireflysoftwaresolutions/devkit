// Copyright (c) 2026 Firefly Software Solutions Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Node's own reading of the bodies `dev test prove` cuts from a JS/TS test module before it rebuilds it
// (testx._js_whole_bodies): each helper's body and each test, all of one module in one run. A body that Node reads
// as one whole function body is where the reader put it; a reader that misplaced a boundary leaves, in Node's view, a
// brace that closes the function early or a bracket, string, template, comment or regular expression still open, and
// the Function constructors refuse both (a body that closes the function early is no single function literal).
//
// stdin: {"file": the module's path, "bodies": [text, ...]}. stdout: [null when Node reads that text as one function
// body, else why not, ...], in the same order.
//
// Each body is read as the module's file is: a .cjs module as a script, where <!-- and a --> at the start of a line
// open a comment; a .mjs, .ts, .mts, .cts, .jsx or .tsx module as a module, where they are code; a .js module, which
// may be either, both ways, and it must read whole in each. TypeScript's types are stripped first (Node 22.13 and
// later strip them in place, so the text keeps its length). A body of a .jsx or .tsx module that holds a < may hold
// JSX, which Node does not read. Each body is tried as a function, an async function, a generator and an async
// generator, after the syntax that only a class method or a module allows is put as names any function body reads,
// which keeps every token where it was: super and import (super.x, import.meta) as $uper and $mport, a private name
// #x as $x.
"use strict";

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const GeneratorFunction = Object.getPrototypeOf(function* () {}).constructor;
const AsyncGeneratorFunction = Object.getPrototypeOf(async function* () {}).constructor;
const FORMS = [Function, AsyncFunction, GeneratorFunction, AsyncGeneratorFunction];
const TS = new Set([".ts", ".mts", ".cts", ".tsx"]);
const JSX = new Set([".jsx", ".tsx"]);
const MODULE = new Set([".mjs", ".ts", ".mts", ".cts", ".jsx", ".tsx"]);
const WRAP = "async function* __devkit_body() {\n"; // stripTypeScriptTypes reads a whole module, so the body goes in one

function ext(file) {
  const m = /\.[^./]+$/.exec(file);
  return m ? m[0].toLowerCase() : "";
}

// the body with the syntax only a class method or a module allows put as names (the same tokens, where they were)
function neutral(text) {
  return text
    .replace(/\bsuper\b/g, () => "$uper")
    .replace(/\bimport\b/g, () => "$mport")
    .replace(/#(?=[A-Za-z_$\\\u0080-￿])/g, () => "$");
}

// why Node does not read `text` as one function body as a script reads it (HTML-like comments are comments), or null
function readAsScript(text) {
  let why = null;
  for (const Form of FORMS) {
    try {
      Form(text);
      return null;
    } catch (e) {
      why = e && e.message ? String(e.message) : String(e);
    }
  }
  return why;
}

// the same as a module reads it: <!-- and --> are code there, so they go apart, as the module's tokens have them
function readAsModule(text) {
  return readAsScript(text.replace(/<!--/g, () => "< !--").replace(/-->/g, () => "-- >"));
}

function stripTypes(text) {
  const { stripTypeScriptTypes } = require("node:module");
  if (typeof stripTypeScriptTypes !== "function") {
    throw new Error("this Node cannot strip TypeScript's types (Node 22.13 and later can)");
  }
  const whole = WRAP + text + "\n}";
  const out = stripTypeScriptTypes(whole, { mode: "strip" });
  if (out.length !== whole.length) {
    throw new Error("stripping its types moved its text");
  }
  return out.slice(WRAP.length, out.length - 2);
}

function why(file, text) {
  const kind = ext(file);
  if (JSX.has(kind) && text.includes("<")) {
    return "it may hold JSX, which Node does not read";
  }
  let body = text;
  if (TS.has(kind)) {
    try {
      body = stripTypes(text);
    } catch (e) {
      return "its TypeScript types do not strip: " + String((e && e.message) || e).split("\n")[0];
    }
  }
  body = neutral(body);
  if (kind === ".cjs") {
    return readAsScript(body);
  }
  if (MODULE.has(kind)) {
    return readAsModule(body);
  }
  if (kind === ".js") {
    const script = readAsScript(body);
    if (script) {
      return "as a script, " + script;
    }
    const module = readAsModule(body);
    return module ? "as a module, " + module : null;
  }
  return "Node does not read " + (kind || "this kind of") + " files";
}

let input = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  input += chunk;
});
process.stdin.on("end", () => {
  const { file, bodies } = JSON.parse(input);
  process.stdout.write(JSON.stringify(bodies.map((text) => why(String(file), String(text)))));
});
