/**
 * Lexer-aware JS function extractor shared by Node contract verification scripts.
 * Skips strings, template literals (including `${...}` nesting), comments, and
 * regex literals so braces inside literals or comments never corrupt extraction.
 */

export function extractFunction(source, name, sourcePath, options = {}) {
  const marker = `function ${name}(`;
  const start = source.indexOf(marker);
  if (start === -1) {
    throw new Error(`${sourcePath} no longer defines ${name}()`);
  }
  const sliceStart = options.includeModifiers
    ? source.lastIndexOf("\n", start) + 1
    : start;
  const open = source.indexOf("{", start);
  if (open === -1) {
    throw new Error(`${sourcePath} has no body for ${name}()`);
  }

  let depth = 0;
  let inLineComment = false;
  let inBlockComment = false;
  let inString = null;
  let escaped = false;
  // Stack entries track either ordinary block/object `{` or template `${`
  const templateBraceDepths = [];

  for (let i = open; i < source.length; i += 1) {
    const ch = source[i];
    const next = source[i + 1] || "";

    if (inLineComment) {
      if (ch === "\n") inLineComment = false;
      continue;
    }
    if (inBlockComment) {
      if (ch === "*" && next === "/") {
        inBlockComment = false;
        i += 1;
      }
      continue;
    }
    if (escaped) {
      escaped = false;
      continue;
    }
    if (inString) {
      if (ch === "\\") {
        escaped = true;
      } else if (inString === "`" && ch === "$" && next === "{") {
        templateBraceDepths.push(depth);
        depth += 1;
        inString = null;
        i += 1;
      } else if (ch === inString) {
        inString = null;
      }
      continue;
    }

    if (ch === "/" && next === "/") {
      inLineComment = true;
      i += 1;
      continue;
    }
    if (ch === "/" && next === "*") {
      inBlockComment = true;
      i += 1;
      continue;
    }
    if (ch === '"' || ch === "'" || ch === "`") {
      inString = ch;
      continue;
    }
    if (ch === "{") {
      depth += 1;
      continue;
    }
    if (ch === "}") {
      depth -= 1;
      if (
        templateBraceDepths.length > 0 &&
        depth === templateBraceDepths[templateBraceDepths.length - 1]
      ) {
        templateBraceDepths.pop();
        inString = "`";
        continue;
      }
      if (depth === 0) {
        return source.slice(sliceStart, i + 1);
      }
    }
  }

  throw new Error(`${sourcePath} has unbalanced braces in ${name}()`);
}
