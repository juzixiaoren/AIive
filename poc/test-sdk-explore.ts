/**
 * AIive P0 验证 — 步骤 1: 探索 @tencent-ai/codebuddy-code SDK 的导出和 API
 *
 * 目标：了解 SDK 提供了哪些接口，确定程序化调用方式
 */

async function exploreSdk() {
  console.log("=== P0 验证: 探索 CodeBuddy SDK ===\n");

  // 1. 尝试导入 SDK
  try {
    const sdk = await import("@tencent-ai/codebuddy-code");
    console.log("✅ SDK 导入成功");
    console.log("\n--- SDK 顶层导出 ---");
    const exports = Object.keys(sdk);
    console.log(`导出数量: ${exports.length}`);
    exports.forEach((key) => {
      const val = (sdk as any)[key];
      const type = typeof val;
      if (type === "function") {
        console.log(`  📦 ${key}: [Function] (${val.length} params)`);
        // 尝试打印函数的 toString 前 200 字符
        const str = val.toString().substring(0, 200);
        console.log(
          `     preview: ${str.replace(/\n/g, " ").substring(0, 150)}...`,
        );
      } else if (type === "object" && val !== null) {
        console.log(
          `  📦 ${key}: [Object] keys=${Object.keys(val).join(", ")}`,
        );
      } else {
        console.log(
          `  📦 ${key}: [${type}] = ${String(val).substring(0, 100)}`,
        );
      }
    });

    // 2. 检查 default export
    if (sdk.default) {
      console.log("\n--- Default Export ---");
      const def = sdk.default;
      console.log(`Type: ${typeof def}`);
      if (typeof def === "function") {
        console.log(`Name: ${def.name}`);
        console.log(`Params: ${def.length}`);
      }
      if (typeof def === "object") {
        console.log(`Keys: ${Object.keys(def).join(", ")}`);
      }
    }

    return { success: true, exports };
  } catch (error: any) {
    console.log("❌ SDK 导入失败:", error.message);
    console.log("\n完整错误:");
    console.log(error.stack);
    return { success: false, error: error.message };
  }
}

// 3. 检查 CLI 二进制文件
async function checkCliBinary() {
  console.log("\n\n=== 检查 CLI 二进制文件 ===\n");

  const { execSync } = await import("child_process");

  // 检查 node_modules/.bin/ 下是否有 codebuddy
  try {
    const bins = execSync("ls -la node_modules/.bin/ | grep -i codebuddy", {
      encoding: "utf-8",
    });
    console.log("✅ 找到 CLI 二进制:");
    console.log(bins);
  } catch {
    console.log("❌ node_modules/.bin/ 下没有 codebuddy");
  }

  // 检查包的 bin 字段
  try {
    const pkgJson = execSync(
      "cat node_modules/@tencent-ai/codebuddy-code/package.json",
      { encoding: "utf-8" },
    );
    const pkg = JSON.parse(pkgJson);
    console.log("包名:", pkg.name);
    console.log("版本:", pkg.version);
    console.log("bin:", JSON.stringify(pkg.bin, null, 2));
    console.log("main:", pkg.main);
    console.log(
      "exports:",
      JSON.stringify(pkg.exports, null, 2)?.substring(0, 500),
    );
    console.log("type:", pkg.type);

    // 检查 README
    try {
      const readme = execSync(
        "head -100 node_modules/@tencent-ai/codebuddy-code/README.md 2>/dev/null",
        { encoding: "utf-8" },
      );
      console.log("\n--- README (前 100 行) ---");
      console.log(readme);
    } catch {
      console.log("没有 README");
    }

    return {
      success: true,
      version: pkg.version,
      bin: pkg.bin,
      main: pkg.main,
    };
  } catch (error: any) {
    console.log("❌ 无法读取 package.json:", error.message);
    return { success: false, error: error.message };
  }
}

// 4. 检查 SDK 的类型定义
async function checkTypes() {
  console.log("\n\n=== 检查类型定义 ===\n");

  const { execSync } = await import("child_process");

  try {
    // 查找 .d.ts 文件
    const dtsFiles = execSync(
      'find node_modules/@tencent-ai/codebuddy-code -name "*.d.ts" -maxdepth 3 2>/dev/null | head -20',
      { encoding: "utf-8" },
    );
    console.log("类型定义文件:");
    console.log(dtsFiles || "(无)");

    // 如果有主类型文件，读取前 100 行
    const mainDts = dtsFiles
      .split("\n")
      .find((f) => f.includes("index.d.ts") || f.includes("main.d.ts"));
    if (mainDts) {
      const content = execSync(`head -100 "${mainDts.trim()}"`, {
        encoding: "utf-8",
      });
      console.log(`\n--- ${mainDts.trim()} (前 100 行) ---`);
      console.log(content);
    }
  } catch (error: any) {
    console.log("检查类型定义失败:", error.message);
  }
}

async function main() {
  const sdkResult = await exploreSdk();
  const cliResult = await checkCliBinary();
  await checkTypes();

  console.log("\n\n========================================");
  console.log("=== 探索结果汇总 ===");
  console.log("========================================");
  console.log("SDK 导入:", sdkResult.success ? "✅" : "❌");
  console.log("CLI 二进制:", cliResult.success ? "✅" : "❌");
  if (cliResult.success) {
    console.log("版本:", (cliResult as any).version);
    console.log("bin:", JSON.stringify((cliResult as any).bin));
  }
}

main().catch(console.error);
