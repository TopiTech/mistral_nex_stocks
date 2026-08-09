import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const tempDirectory = mkdtempSync(join(tmpdir(), "mns-frontend-"));
const generatedPath = join(tempDirectory, "api_client.js");
const trackedPath = "static/js/api_client.js";

try {
  const compilerPath = join(
    process.cwd(),
    "node_modules",
    "typescript",
    "bin",
    "tsc",
  );
  execFileSync(
    process.execPath,
    [compilerPath, "-p", "tsconfig.json", "--outFile", generatedPath],
    {
      stdio: "inherit",
    },
  );
  const prettierPath = join(
    process.cwd(),
    "node_modules",
    "prettier",
    "bin",
    "prettier.cjs",
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
