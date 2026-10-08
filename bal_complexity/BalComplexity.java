/*
 * Complexity metrics for Ballerina source code, measured on the syntax tree of Ballerina's own parser.
 *
 * bal_builder.py --complexity runs it with the parser of the Ballerina version the template builds with:
 *     java -cp '<bal home>/bre/lib/*' BalComplexity.java PROJECT_DIR OUTPUT_JSON
 * (Java's single-file launcher compiles it on the fly; that needs a JDK, not just a JRE.)
 * With --tree FILE.bal instead, it prints the file's syntax tree: the node kinds walk() switches on.
 *
 * For every function, method and resource function in the package's .bal files (the default module and
 * modules/*, not tests/) it reports cyclomatic complexity, cognitive complexity, the deepest block nesting,
 * lines of code and parameters. All the counting rules are in walk(); README.md in this folder lists them
 * and explains how to keep them up to date with new Ballerina versions.
 */

import io.ballerina.compiler.syntax.tree.BinaryExpressionNode;
import io.ballerina.compiler.syntax.tree.ClassDefinitionNode;
import io.ballerina.compiler.syntax.tree.ElseBlockNode;
import io.ballerina.compiler.syntax.tree.FunctionCallExpressionNode;
import io.ballerina.compiler.syntax.tree.FunctionDefinitionNode;
import io.ballerina.compiler.syntax.tree.MatchClauseNode;
import io.ballerina.compiler.syntax.tree.MethodCallExpressionNode;
import io.ballerina.compiler.syntax.tree.Minutiae;
import io.ballerina.compiler.syntax.tree.Node;
import io.ballerina.compiler.syntax.tree.NonTerminalNode;
import io.ballerina.compiler.syntax.tree.QueryPipelineNode;
import io.ballerina.compiler.syntax.tree.ServiceDeclarationNode;
import io.ballerina.compiler.syntax.tree.SyntaxKind;
import io.ballerina.compiler.syntax.tree.SyntaxTree;
import io.ballerina.compiler.syntax.tree.Token;
import io.ballerina.tools.text.LineRange;
import io.ballerina.tools.text.TextDocuments;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Collectors;
import java.util.stream.Stream;

public final class BalComplexity {

    // Bump when a counting rule changes, so that every report says which rules produced it.
    static final int RULES_VERSION = 1;

    // `// complexity-expect: cyclomatic=4 cognitive=6` right above a function: the values it should get.
    // fixture.bal uses them to check the rules; bal_builder.py reports any mismatch.
    static final Pattern EXPECT = Pattern.compile("//\\s*complexity-expect:(.*)");
    static final Pattern EXPECTED_VALUE = Pattern.compile("(\\w+)\\s*=\\s*(\\d+)");

    static final class Function {
        String name;
        String container; // class, service or object constructor the function belongs to, or null
        String kind; // function, method, remote method or resource
        int line;
        int endLine;
        int parameters;
        int cyclomatic = 1;
        int cognitive;
        int nesting;
        boolean recursive;
        final Set<Integer> lines = new TreeSet<>(); // lines with code on them, not blank or comment-only
        final Map<String, Integer> expected = new LinkedHashMap<>();
    }

    private final List<Function> functions = new ArrayList<>();
    private final Deque<String> containers = new ArrayDeque<>();

    public static void main(String[] args) throws IOException {
        if (args.length != 2) {
            System.err.println("usage: java -cp '<bal home>/bre/lib/*' BalComplexity.java PROJECT_DIR OUTPUT_JSON");
            System.err.println("       java -cp '<bal home>/bre/lib/*' BalComplexity.java --tree FILE.bal");
            System.exit(2);
        }
        if (args[0].equals("--tree")) {
            printTree(SyntaxTree.from(TextDocuments.from(Files.readString(Paths.get(args[1])))).rootNode(), 0);
            return;
        }
        Path root = Paths.get(args[0]).toAbsolutePath().normalize();
        StringBuilder json = new StringBuilder("{\"rulesVersion\": " + RULES_VERSION + ", \"files\": [");
        String separator = "";
        for (Path path : sources(root)) {
            String file = root.relativize(path).toString().replace('\\', '/');
            SyntaxTree tree = SyntaxTree.from(TextDocuments.from(Files.readString(path)), file);
            int syntaxErrors = 0;
            for (Object diagnostic : tree.diagnostics()) {
                syntaxErrors++;
            }
            BalComplexity analysis = new BalComplexity();
            analysis.walk(tree.rootNode(), null, 0);
            System.out.println("Analysed " + file + ": " + analysis.functions.size() + " function(s)"
                    + (syntaxErrors > 0 ? ", " + syntaxErrors + " syntax error(s)" : ""));
            json.append(separator).append("\n  {\"file\": ").append(quote(file))
                    .append(", \"syntaxErrors\": ").append(syntaxErrors).append(", \"functions\": [");
            String fnSeparator = "";
            for (Function fn : analysis.functions) {
                json.append(fnSeparator).append("\n    ").append(toJson(fn));
                fnSeparator = ",";
            }
            json.append("]}");
            separator = ",";
        }
        json.append("\n]}\n");
        Files.write(Paths.get(args[1]), json.toString().getBytes(StandardCharsets.UTF_8));
    }

    /** The package's source files: the default module's .bal files, then each module's under modules/. */
    static List<Path> sources(Path root) throws IOException {
        List<Path> sources = new ArrayList<>(balFiles(root));
        Path modules = root.resolve("modules");
        if (Files.isDirectory(modules)) {
            try (Stream<Path> dirs = Files.list(modules)) {
                for (Path dir : dirs.filter(Files::isDirectory).sorted().collect(Collectors.toList())) {
                    sources.addAll(balFiles(dir));
                }
            }
        }
        return sources;
    }

    static List<Path> balFiles(Path dir) throws IOException {
        try (Stream<Path> files = Files.list(dir)) {
            return files.filter(p -> p.toString().endsWith(".bal") && Files.isRegularFile(p)).sorted().collect(Collectors.toList());
        }
    }

    // ------------------------------------------------------------------ the counting rules

    /**
     * Visits `node` inside `fn` (null outside functions) at `nesting` levels of control structures.
     * Cognitive increments follow SonarSource's Cognitive Complexity: +1 for a structure, plus `nesting`
     * for those that nest. Kinds without a case here are walked without counting anything.
     */
    private void walk(Node node, Function fn, int nesting) {
        if (node instanceof Token) {
            if (fn != null && !node.isMissing()) {
                LineRange range = node.lineRange();
                for (int line = range.startLine().line(); line <= range.endLine().line(); line++) {
                    fn.lines.add(line + 1);
                }
            }
            return;
        }
        switch (node.kind()) {
            // Named functions get their own entry. Anonymous functions and workers count towards the
            // function they are written in, one level deeper.
            case FUNCTION_DEFINITION:
            case OBJECT_METHOD_DEFINITION:
            case RESOURCE_ACCESSOR_DEFINITION:
                function((FunctionDefinitionNode) node);
                return;
            case EXPLICIT_ANONYMOUS_FUNCTION_EXPRESSION:
            case IMPLICIT_ANONYMOUS_FUNCTION_EXPRESSION:
            case NAMED_WORKER_DECLARATION:
                children(node, fn, nesting + 1, nesting + 1);
                return;

            case CLASS_DEFINITION:
                within(((ClassDefinitionNode) node).className().text(), node, fn, nesting);
                return;
            case SERVICE_DECLARATION: {
                String path = source(((ServiceDeclarationNode) node).absoluteResourcePath());
                within(path.isEmpty() ? "service" : "service " + path, node, fn, nesting);
                return;
            }
            case OBJECT_CONSTRUCTOR:
                within(fn == null ? "object" : qualifiedName(fn) + ".object", node, fn, nesting);
                return;

            // `if` +1 (+nesting); `else if` and `else` +1 without nesting, as their bodies are no deeper.
            case IF_ELSE_STATEMENT:
                count(fn, 1, node.parent().kind() == SyntaxKind.ELSE_BLOCK ? 1 : 1 + nesting);
                break;
            case ELSE_BLOCK:
                if (((ElseBlockNode) node).elseBody().kind() == SyntaxKind.BLOCK_STATEMENT) {
                    count(fn, 0, 1);
                }
                break;

            // Loops. `retry` runs its block again on failure, so it is one too.
            case WHILE_STATEMENT:
            case FOREACH_STATEMENT:
            case RETRY_STATEMENT:
                count(fn, 1, 1 + nesting);
                break;

            // `match` +1 (+nesting) for cognitive, like a switch. Cyclomatic counts each clause except
            // the catch-all `_`, plus each `if` guard.
            case MATCH_STATEMENT:
                count(fn, 0, 1 + nesting);
                break;
            case MATCH_CLAUSE:
                count(fn, isCatchAll((MatchClauseNode) node) ? 0 : 1, 0);
                break;
            case MATCH_GUARD:
                count(fn, 1, 1);
                break;

            // `on fail` (after do, while, foreach, match, lock, transaction, retry) is like `catch`.
            case ON_FAIL_CLAUSE:
                count(fn, 1, 1 + nesting);
                break;

            // `c ? a : b`: a and b are one level deeper.
            case CONDITIONAL_EXPRESSION: {
                count(fn, 1, 1 + nesting);
                int index = 0;
                for (Node child : ((NonTerminalNode) node).children()) {
                    walk(child, fn, index++ == 0 ? nesting : nesting + 1);
                }
                return;
            }

            // `&&` and `||` +1 cyclomatic each; cognitive +1 per run of the same operator (a && b && c is +1,
            // a && b || c is +2). The elvis operator `x ?: y` is a branch, but shorthand, so cyclomatic only.
            case BINARY_EXPRESSION: {
                SyntaxKind operator = ((BinaryExpressionNode) node).operator().kind();
                if (operator == SyntaxKind.LOGICAL_AND_TOKEN || operator == SyntaxKind.LOGICAL_OR_TOKEN) {
                    Node parent = node.parent();
                    boolean continuesRun = parent.kind() == SyntaxKind.BINARY_EXPRESSION
                            && ((BinaryExpressionNode) parent).operator().kind() == operator;
                    count(fn, 1, continuesRun ? 0 : 1);
                } else if (operator == SyntaxKind.ELVIS_TOKEN) {
                    count(fn, 1, 0);
                }
                break;
            }

            // Queries are loops: the query +1 (+nesting), its clauses one level deeper. Cyclomatic +1 per
            // `from`, `join` and `where`; cognitive +1 for each `from` or `join` after the first and each `where`.
            case QUERY_EXPRESSION:
            case QUERY_ACTION:
                count(fn, 0, 1 + nesting);
                children(node, fn, nesting + 1, nesting + 1);
                return;
            case FROM_CLAUSE: {
                QueryPipelineNode pipeline = (QueryPipelineNode) node.parent();
                count(fn, 1, pipeline.fromClause().position() == node.position() ? 0 : 1);
                break;
            }
            case JOIN_CLAUSE:
            case WHERE_CLAUSE:
                count(fn, 1, 1);
                break;

            // Recursion: cognitive +1, once per function (added in function()).
            case FUNCTION_CALL:
                if (fn != null && fn.container == null
                        && source(((FunctionCallExpressionNode) node).functionName()).equals(fn.name)) {
                    fn.recursive = true;
                }
                break;
            case METHOD_CALL: {
                MethodCallExpressionNode call = (MethodCallExpressionNode) node;
                if (fn != null && fn.container != null && source(call.expression()).equals("self")
                        && source(call.methodName()).equals(fn.name)) {
                    fn.recursive = true;
                }
                break;
            }

            // Nesting: the deepest block, counting the function body as 0.
            case BLOCK_STATEMENT:
                if (fn != null) {
                    fn.nesting = Math.max(fn.nesting, nesting);
                }
                break;

            default:
                break;
        }
        // Block statements among the children are the bodies of the structure above, one level deeper.
        boolean nests = node.kind() == SyntaxKind.IF_ELSE_STATEMENT || node.kind() == SyntaxKind.ELSE_BLOCK
                || node.kind() == SyntaxKind.WHILE_STATEMENT || node.kind() == SyntaxKind.FOREACH_STATEMENT
                || node.kind() == SyntaxKind.RETRY_STATEMENT || node.kind() == SyntaxKind.MATCH_CLAUSE
                || node.kind() == SyntaxKind.ON_FAIL_CLAUSE;
        children(node, fn, nesting, nests ? nesting + 1 : nesting);
    }

    private void children(Node node, Function fn, int nesting, int blockNesting) {
        for (Node child : ((NonTerminalNode) node).children()) {
            walk(child, fn, child.kind() == SyntaxKind.BLOCK_STATEMENT ? blockNesting : nesting);
        }
    }

    private static void count(Function fn, int cyclomatic, int cognitive) {
        if (fn != null) { // module-level code (variable initialisers, ...) is not in any function
            fn.cyclomatic += cyclomatic;
            fn.cognitive += cognitive;
        }
    }

    private static boolean isCatchAll(MatchClauseNode clause) {
        if (clause.matchGuard().isPresent()) {
            return false;
        }
        for (Node pattern : clause.matchPatterns()) {
            if (source(pattern).equals("_")) {
                return true;
            }
        }
        return false;
    }

    private void within(String container, Node node, Function fn, int nesting) {
        containers.addLast(container);
        children(node, fn, nesting, nesting);
        containers.removeLast();
    }

    private void function(FunctionDefinitionNode node) {
        Function fn = new Function();
        fn.name = node.functionName().text();
        if (node.kind() == SyntaxKind.RESOURCE_ACCESSOR_DEFINITION) {
            String path = source(node.relativeResourcePath());
            fn.name += " " + (path.isEmpty() ? "." : path);
            fn.kind = "resource";
        } else if (node.kind() == SyntaxKind.OBJECT_METHOD_DEFINITION) {
            boolean remote = false;
            for (Token qualifier : node.qualifierList()) {
                remote |= qualifier.kind() == SyntaxKind.REMOTE_KEYWORD;
            }
            fn.kind = remote ? "remote method" : "method";
        } else {
            fn.kind = "function";
        }
        fn.container = containers.isEmpty() ? null : String.join(".", containers);
        fn.line = node.functionName().lineRange().startLine().line() + 1;
        fn.endLine = node.lineRange().endLine().line() + 1;
        fn.parameters = node.functionSignature().parameters().size();
        for (Minutiae minutiae : node.leadingMinutiae()) {
            Matcher expect = EXPECT.matcher(minutiae.text());
            if (minutiae.kind() == SyntaxKind.COMMENT_MINUTIAE && expect.matches()) {
                Matcher value = EXPECTED_VALUE.matcher(expect.group(1));
                while (value.find()) {
                    fn.expected.put(value.group(1), Integer.parseInt(value.group(2)));
                }
            }
        }
        functions.add(fn);
        for (Node child : node.children()) {
            if (child.kind() != SyntaxKind.METADATA) { // documentation and annotations are not code to follow
                walk(child, fn, 0);
            }
        }
        if (fn.recursive) {
            fn.cognitive++;
        }
    }

    // ------------------------------------------------------------------ output

    /** --tree: one line per node, `line: KIND`, with each token's text. */
    private static void printTree(Node node, int depth) {
        String text = node instanceof Token ? "  " + quote(((Token) node).text()) : "";
        System.out.println(String.format("%4d: ", node.lineRange().startLine().line() + 1) + "  ".repeat(depth) + node.kind() + text);
        if (node instanceof NonTerminalNode) {
            for (Node child : ((NonTerminalNode) node).children()) {
                printTree(child, depth + 1);
            }
        }
    }

    private static String qualifiedName(Function fn) {
        return fn.container == null ? fn.name : fn.container + "." + fn.name;
    }

    private static String source(Node node) {
        return node.toSourceCode().trim();
    }

    private static String source(Iterable<? extends Node> nodes) {
        StringBuilder text = new StringBuilder();
        for (Node node : nodes) {
            text.append(source(node));
        }
        return text.toString();
    }

    private static String toJson(Function fn) {
        String expected = fn.expected.entrySet().stream()
                .map(e -> quote(e.getKey()) + ": " + e.getValue())
                .collect(Collectors.joining(", ", "{", "}"));
        return "{\"name\": " + quote(fn.name) + ", \"container\": " + (fn.container == null ? "null" : quote(fn.container))
                + ", \"kind\": " + quote(fn.kind) + ", \"line\": " + fn.line + ", \"endLine\": " + fn.endLine
                + ", \"parameters\": " + fn.parameters + ", \"lines\": " + fn.lines.size()
                + ", \"cyclomatic\": " + fn.cyclomatic + ", \"cognitive\": " + fn.cognitive + ", \"nesting\": " + fn.nesting
                + ", \"recursive\": " + fn.recursive + ", \"expected\": " + expected + "}";
    }

    private static String quote(String text) {
        StringBuilder quoted = new StringBuilder("\"");
        for (char c : text.toCharArray()) {
            if (c == '"' || c == '\\') {
                quoted.append('\\').append(c);
            } else if (c < 0x20) {
                quoted.append(String.format("\\u%04x", (int) c));
            } else {
                quoted.append(c);
            }
        }
        return quoted.append('"').toString();
    }
}
