// Runs inside the headless VS Code (code-server) of bal_builder.py --visualize, next to the Ballerina extension.
// capture.py drives it over HTTP on 127.0.0.1:$BAL_VISUALIZER_PORT:
//   POST /health   the versions of VS Code and the Ballerina extension, once this extension is up
//   POST /targets  every .bal file and what the Ballerina extension offers to visualize in it
//   POST /open     open one diagram: {fsPath, position} for a function or service, {fsPath} for the file's overview
const http = require("http");
const path = require("path");
const vscode = require("vscode");

const BALLERINA = "wso2.ballerina";
const VISUALIZE = "ballerina.openIn.diagram"; // the command of the extension's "Visualize" code lenses
const FIRST_ANSWER_TIMEOUT = 180; // seconds for the language server to start (it may download dependencies)

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function ballerina() {
  const ext = vscode.extensions.getExtension(BALLERINA);
  if (!ext) throw new Error(`the ${BALLERINA} extension is not installed`);
  await ext.activate();
  return ext;
}

async function hideChrome() {
  // More room for the diagram, which opens in the editor area, and no notification toasts over it.
  const commands = ["workbench.action.closeSidebar", "workbench.action.closePanel", "workbench.action.closeAuxiliaryBar", "notifications.clearAll"];
  for (const command of commands) {
    await vscode.commands.executeCommand(command).then(undefined, () => undefined);
  }
}

async function visualizeLenses(uri) {
  const doc = await vscode.workspace.openTextDocument(uri);
  // The extension's code lens provider reads the active editor, not the document it is asked about.
  await vscode.window.showTextDocument(doc, { preview: false });
  const lenses = (await vscode.commands.executeCommand("vscode.executeCodeLensProvider", uri, 100)) || [];
  const symbols = (await vscode.commands.executeCommand("vscode.executeDocumentSymbolProvider", uri)) || [];
  return { doc, symbols, lenses: lenses.filter((lens) => lens.command && lens.command.command === VISUALIZE) };
}

// Names of the symbols (classes, services, ...) that contain `line`, outermost first, leaving out the one starting on it.
function parents(symbols, line) {
  for (const symbol of symbols) {
    const range = symbol.range || (symbol.location && symbol.location.range);
    if (range && range.start.line < line && line <= range.end.line) {
      return [symbol.name, ...parents(symbol.children || [], line)];
    }
  }
  return [];
}

async function targets() {
  await ballerina();
  await hideChrome();
  const root = vscode.workspace.workspaceFolders[0].uri.fsPath;
  const files = await vscode.workspace.findFiles("**/*.bal", "{**/target/**,**/tests/**}");
  files.sort((a, b) => a.fsPath.localeCompare(b.fsPath));
  const out = [];
  let answered = false; // the language server starts lazily; once it has answered, the other files only need a moment
  const started = Date.now();
  for (const uri of files) {
    let found;
    for (let attempt = 0; ; attempt++) {
      found = await visualizeLenses(uri);
      answered = answered || found.symbols.length > 0 || found.lenses.length > 0;
      if (found.lenses.length > 0 || (answered && attempt >= 2)) break;
      if (!answered && Date.now() - started > FIRST_ANSWER_TIMEOUT * 1000) {
        throw new Error(`the Ballerina language server did not answer within ${FIRST_ANSWER_TIMEOUT}s`);
      }
      await sleep(1000);
    }
    const items = found.lenses.map((lens) => {
      const position = lens.command.arguments[1]; // the syntax tree node's range, 0-based
      return {
        position,
        header: found.doc.lineAt(position.startLine).text.trim(),
        parents: parents(found.symbols, position.startLine),
      };
    });
    out.push({ file: path.relative(root, uri.fsPath), fsPath: uri.fsPath, items });
  }
  await vscode.commands.executeCommand("workbench.action.closeAllEditors");
  return out;
}

async function open(body) {
  await vscode.commands.executeCommand("workbench.action.closeAllEditors");
  // (file, position, true) opens that file's diagram focused on position ({} for the overview), as the extension's
  // "go to design" does; without the third argument the command works on the active editor instead.
  await vscode.commands.executeCommand("ballerina.show.diagram", body.fsPath, body.position || {}, true);
  await hideChrome();
  return { ok: true };
}

async function health() {
  const ext = await ballerina();
  return { vscode: vscode.version, ballerina: ext.packageJSON.version };
}

function activate(context) {
  const routes = { "/health": health, "/targets": targets, "/open": open };
  const server = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (chunk) => (raw += chunk));
    req.on("end", async () => {
      let status = 200;
      let result;
      try {
        if (!routes[req.url]) throw new Error(`no such route: ${req.url}`);
        result = await routes[req.url](raw ? JSON.parse(raw) : {});
      } catch (err) {
        status = 500;
        result = { error: String((err && err.stack) || err) };
      }
      res.writeHead(status, { "Content-Type": "application/json" });
      res.end(JSON.stringify(result));
    });
  });
  server.on("error", (err) => console.error(`bal-visualizer-driver: ${err}`));
  server.listen(Number(process.env.BAL_VISUALIZER_PORT || 8766), "127.0.0.1");
  context.subscriptions.push({ dispose: () => server.close() });
}

module.exports = { activate, deactivate() {} };
