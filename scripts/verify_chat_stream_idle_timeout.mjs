import { readFileSync } from "node:fs";
import vm from "node:vm";

// チャット SSE の無応答タイムアウト契約の回帰ゲート。
//
// streamChatReply() は以前 `await reader.read()` を無制限に待っていた。
// サーバーは delta 間に keepalive を送らないため沈黙自体は正常だが、TCP 半断や
// ワーカー再起動で読み取りが永久に返らないと、呼び出し元 sendChat() の finally
// (input/sendBtn の disabled 解除)に到達せずチャット UI がリロードまで固まる。
// 実ソースから該当関数を取り出し、沈黙時に isStreamFallback で失敗して
// 既存のポーリング経路へ落ちることを検証する。

const API_PATH = "static/js/api.js";

function fail(message) {
  throw new Error(message);
}

// 無応答タイムアウトが外れると、検証側の await も永久に返らず Node は
// "unsettled top-level await" で exit 13 になる(失敗理由が読めない)。
// 各チェックを上限付きで待ち、沈黙は明示的な失敗メッセージにする。
const CHECK_TIMEOUT_MS = 10000;

function withWatchdog(label, promise) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(new Error(`${label}: check hung (no settlement within ${CHECK_TIMEOUT_MS}ms)`));
    }, CHECK_TIMEOUT_MS);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        clearTimeout(timer);
        reject(error);
      },
    );
  });
}

function extractFunction(source, name, sourcePath) {
  const marker = `function ${name}(`;
  const start = source.indexOf(marker);
  if (start === -1) fail(`${sourcePath} no longer defines ${name}()`);
  // async 宣言を落とさないよう前方の修飾子まで含める
  const declStart = source.lastIndexOf("\n", start) + 1;
  const open = source.indexOf("{", start);
  if (open === -1) fail(`${sourcePath} has no body for ${name}()`);
  let depth = 0;
  for (let index = open; index < source.length; index += 1) {
    const char = source[index];
    if (char === "{") {
      depth += 1;
    } else if (char === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(declStart, index + 1);
    }
  }
  return fail(`${sourcePath} has unbalanced braces in ${name}()`);
}

function readApiSource() {
  return readFileSync(API_PATH, "utf8");
}

function readIdleTimeoutConstant(source) {
  const match = /const CHAT_STREAM_IDLE_TIMEOUT_MS = (\d+);/.exec(source);
  if (!match) fail(`${API_PATH} no longer declares CHAT_STREAM_IDLE_TIMEOUT_MS`);
  return Number(match[1]);
}

function makeReader(chunks, options = {}) {
  const state = { cancelCalls: 0, readCalls: 0, resolvePending: null };
  let index = 0;
  const reader = {
    state,
    read() {
      state.readCalls += 1;
      if (index < chunks.length) {
        const value = chunks[index];
        index += 1;
        return Promise.resolve({ done: false, value });
      }
      if (options.stall) {
        // 接続は生きているがサーバーが何も送らない状態。
        return new Promise((resolve) => {
          state.resolvePending = resolve;
        });
      }
      return Promise.resolve({ done: true, value: undefined });
    },
    cancel() {
      state.cancelCalls += 1;
      if (state.resolvePending) {
        state.resolvePending({ done: true, value: undefined });
        state.resolvePending = null;
      }
      return Promise.resolve();
    },
  };
  return reader;
}

function sseFrame(payload) {
  return new TextEncoder().encode(`data: ${JSON.stringify(payload)}\n\n`);
}

function loadChatStream(source, overrides = {}) {
  const bodies = [
    extractFunction(source, "readStreamChunk", API_PATH),
    extractFunction(source, "streamChatReply", API_PATH),
  ];
  const sandbox = {
    setTimeout,
    clearTimeout,
    TextDecoder,
    CHAT_STREAM_IDLE_TIMEOUT_MS: readIdleTimeoutConstant(source),
    ...overrides,
  };
  vm.createContext(sandbox);
  vm.runInContext(
    `${bodies.join("\n")}\nglobalThis.__chatStream = { readStreamChunk, streamChatReply };`,
    sandbox,
  );
  return sandbox.__chatStream;
}

function fakeResponse(reader) {
  return {
    ok: true,
    headers: {
      get(name) {
        return String(name).toLowerCase() === "content-type"
          ? "text/event-stream"
          : null;
      },
    },
    body: { getReader: () => reader },
  };
}

async function expectRejection(promise, label) {
  try {
    await promise;
  } catch (error) {
    return error;
  }
  return fail(`${label}: expected a rejection`);
}

function verifyIdleTimeoutIsGenerous() {
  const timeoutMs = readIdleTimeoutConstant(readApiSource());
  // 推論中の沈黙を正常扱いできるだけの余裕が要る(短すぎると正常応答を切る)。
  if (!(timeoutMs >= 30000)) {
    fail(`CHAT_STREAM_IDLE_TIMEOUT_MS is too aggressive: ${timeoutMs}ms`);
  }
}

async function verifyReadReturnsChunkAndClearsTimer() {
  const cleared = [];
  const { readStreamChunk } = loadChatStream(readApiSource(), {
    // readStreamChunk がタイマーを残さないことを観測する
    clearTimeout: (id) => {
      cleared.push(id);
      return clearTimeout(id);
    },
  });
  const reader = makeReader([new Uint8Array([1, 2, 3])]);
  const result = await readStreamChunk(reader, 50_000);
  if (!result || result.done !== false) fail("expected a data chunk");
  if (cleared.length === 0) {
    fail("readStreamChunk left its idle timer armed after a chunk arrived");
  }
  if (reader.state.cancelCalls !== 0) {
    fail("readStreamChunk must not cancel a healthy stream");
  }
}

async function verifyIdleTimeoutRejectsWithFallback() {
  const { readStreamChunk } = loadChatStream(readApiSource());
  const reader = makeReader([], { stall: true });
  const error = await expectRejection(
    readStreamChunk(reader, 20),
    "readStreamChunk on a silent stream",
  );
  if (error.isStreamFallback !== true) {
    fail("idle timeout must set isStreamFallback so the caller can poll");
  }
  if (error.name !== "TimeoutError") {
    fail(`idle timeout must reject with name TimeoutError, got ${error.name}`);
  }
}

async function verifyZeroTimeoutIsPassthrough() {
  const { readStreamChunk } = loadChatStream(readApiSource());
  const reader = makeReader([new Uint8Array([7])]);
  const result = await readStreamChunk(reader, 0);
  if (!result || result.done !== false) fail("disabled timeout must still read");
  if (reader.state.readCalls !== 1) fail("disabled timeout must call read once");
}

async function verifyStreamResolvesOnDoneFrame() {
  const source = readApiSource();
  const reader = makeReader([
    sseFrame({ delta: "こん" }),
    sseFrame({ delta: "にちは" }),
    sseFrame({ done: true, reply: "こんにちは", request_token: "tok" }),
  ]);
  const { streamChatReply } = loadChatStream(source, {
    csrfFetch: () => Promise.resolve(fakeResponse(reader)),
  });
  const partials = [];
  const reply = await streamChatReply(
    { symbol: "AAPL", market: "us", message: "hi", request_token: "tok" },
    (partial) => partials.push(partial),
  );
  if (reply !== "こんにちは") fail(`unexpected reply: ${JSON.stringify(reply)}`);
  if (partials.join("|") !== "こん|こんにちは") {
    fail(`unexpected deltas: ${JSON.stringify(partials)}`);
  }
  if (reader.state.cancelCalls !== 1) {
    fail("streamChatReply must release the reader exactly once");
  }
}

async function verifySilentStreamFallsBackAndReleasesReader() {
  const source = readApiSource();
  const reader = makeReader([sseFrame({ delta: "途中" })], { stall: true });
  const { streamChatReply } = loadChatStream(source, {
    csrfFetch: () => Promise.resolve(fakeResponse(reader)),
    // 無応答タイムアウトを短縮して実時間で検証する
    CHAT_STREAM_IDLE_TIMEOUT_MS: 30,
  });
  const partials = [];
  const error = await expectRejection(
    streamChatReply({ symbol: "AAPL", market: "us", message: "hi" }, (partial) =>
      partials.push(partial),
    ),
    "streamChatReply on a stream that goes silent",
  );
  if (error.isStreamFallback !== true) {
    // 部分応答を完成品として返してはいけない。ポーリングで回収させる。
    fail(`idle timeout must request the polling fallback, got ${error.message}`);
  }
  if (partials.length !== 1) {
    fail("the first delta must still reach onDelta before the stall");
  }
  if (reader.state.cancelCalls < 1) {
    fail("streamChatReply must cancel the reader after an idle timeout");
  }
}

async function verifyEofWithoutDoneStillFallsBack() {
  const source = readApiSource();
  const reader = makeReader([sseFrame({ delta: "途中" })]);
  const { streamChatReply } = loadChatStream(source, {
    csrfFetch: () => Promise.resolve(fakeResponse(reader)),
  });
  const error = await expectRejection(
    streamChatReply({ symbol: "AAPL", market: "us", message: "hi" }, () => {}),
    "streamChatReply on EOF without a done frame",
  );
  if (error.isStreamFallback !== true) {
    fail("a truncated stream must request the polling fallback");
  }
}

async function verifyExplicitErrorFrameIsNotPolled() {
  const source = readApiSource();
  const reader = makeReader([sseFrame({ error: "AI呼び出しに失敗しました" })]);
  const { streamChatReply } = loadChatStream(source, {
    csrfFetch: () => Promise.resolve(fakeResponse(reader)),
  });
  const error = await expectRejection(
    streamChatReply({ symbol: "AAPL", market: "us", message: "hi" }, () => {}),
    "streamChatReply on an error frame",
  );
  if (error.isMistralError !== true) {
    fail("an explicit error frame must surface to the user, not fall back");
  }
  if (error.isStreamFallback === true) {
    fail("an explicit error frame must not trigger the polling fallback");
  }
}

function verifyReaderCallSiteIsBounded() {
  const source = readApiSource();
  const body = extractFunction(source, "streamChatReply", API_PATH);
  if (/\bawait reader\.read\(\)/.test(body)) {
    fail(`${API_PATH} reads the stream without an idle timeout again`);
  }
  if (!body.includes("readStreamChunk(")) {
    fail(`${API_PATH} streamChatReply no longer uses readStreamChunk()`);
  }
}

const checks = [
  ["idle timeout constant is generous", verifyIdleTimeoutIsGenerous],
  ["chunk read clears its timer", verifyReadReturnsChunkAndClearsTimer],
  ["silent stream rejects with fallback", verifyIdleTimeoutRejectsWithFallback],
  ["disabled timeout is a passthrough", verifyZeroTimeoutIsPassthrough],
  ["done frame resolves with the full reply", verifyStreamResolvesOnDoneFrame],
  [
    "silent stream falls back and releases the reader",
    verifySilentStreamFallsBackAndReleasesReader,
  ],
  ["EOF without done still falls back", verifyEofWithoutDoneStillFallsBack],
  ["explicit error frame is not polled", verifyExplicitErrorFrameIsNotPolled],
  ["reader call site stays bounded", verifyReaderCallSiteIsBounded],
];

for (const [label, check] of checks) {
  await withWatchdog(label, Promise.resolve().then(check));
  console.log(`ok - ${label}`);
}

console.log(`verify_chat_stream_idle_timeout: ${checks.length} checks passed`);
