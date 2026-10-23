import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const compilerPath = join(
  process.cwd(),
  "node_modules",
  "typescript",
  "bin",
  "tsc",
);
const prettierPath = join(
  process.cwd(),
  "node_modules",
  "prettier",
  "bin",
  "prettier.cjs",
);

if (!existsSync(compilerPath)) {
  console.error(
    `TypeScript compiler not found at ${compilerPath}. Run 'npm ci' first.`,
  );
  process.exit(1);
}
if (!existsSync(prettierPath)) {
  console.error(`Prettier not found at ${prettierPath}. Run 'npm ci' first.`);
  process.exit(1);
}

const tempDirectory = mkdtempSync(join(tmpdir(), "mns-frontend-"));
const generatedPath = join(tempDirectory, "api_client.js");
const trackedPath = "static/js/api_client.js";

try {
  execFileSync(
    process.execPath,
    [compilerPath, "-p", "tsconfig.json", "--outFile", generatedPath],
    {
      stdio: "inherit",
    },
  );
  execFileSync(process.execPath, [prettierPath, "--write", generatedPath], {
    stdio: "inherit",
  });
  const generated = readFileSync(generatedPath);
  const tracked = readFileSync(trackedPath);
  if (!generated.equals(tracked)) {
    console.error(
      `${trackedPath} is out of date; run npm run compile and review the diff.`,
    );
    process.exitCode = 1;
  } else {
    console.log(`${trackedPath} matches the TypeScript output.`);
  }
} finally {
  rmSync(tempDirectory, { recursive: true, force: true });
}
