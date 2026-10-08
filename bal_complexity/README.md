# bal_complexity

Complexity metrics for Ballerina, used by `bal_builder.py --complexity` and `--max-complexity N`. No standard tool measures Ballerina: `bal` has no complexity option, `bal scan` has no complexity rule, scc and similar tools don't know the language, and the complexity counters in the `--test` coverage report count the generated Java, not your code. So [BalComplexity.java](BalComplexity.java) walks the syntax tree from Ballerina's own parser.

| File | Purpose |
|---|---|
| [BalComplexity.java](BalComplexity.java) | The analyser. All the counting rules are in `walk()`. |
| [fixture.bal](fixture.bal) | One function per rule, each with the values it must get, counted by hand. This is the regression test. |

## How it runs

After a successful build, `bal_builder.py` does the following:

1. It copies `ballerina-parser-*.jar` and `ballerina-tools-api-*.jar` from `$(bal home)/bre/lib` in the **build container**. The code is therefore parsed by the same Ballerina version that compiled it.
2. It starts a helper container from `COMPLEXITY_JDK_IMAGE`, the official `ballerina/ballerina` image. The analyser needs a full JDK, because Java's single-file launcher compiles `BalComplexity.java` on the fly, and the template's Ballerina ships only a JRE.
3. It runs `java -cp '<jars>/*' BalComplexity.java /workspace /tmp/complexity.json` there.
4. It writes `complexity/complexity.json` (every function, in file order) and `complexity/complexity.txt` (a table, most complex first) to `-o DIR`, and lists the 10 most complex functions in the summary.

The analyser reads the package's source files: the `.bal` files in the root and in each `modules/<name>/`. It skips `tests/`. Each function, method and resource function gets its own entry. The code of anonymous functions and workers counts towards the function they're written in. Module-level code, such as variable initialisers, isn't counted.

## The metrics

- **cyclomatic**: McCabe's count of independent paths, 1 plus one per decision. `--max-complexity N` checks this one; 10 is the classic limit.
- **cognitive**: SonarSource's [Cognitive Complexity](https://www.sonarsource.com/docs/CognitiveComplexity.pdf), which measures how hard the code is to read. Each structure adds 1, and structures that nest also add their nesting depth. Sonar's default limit is 15.
- **nesting**: the deepest block. The function body is 0, and a block inside an `if` is 1.
- **lines**: lines with code on them. Blank lines, comment-only lines and documentation are left out.
- **parameters**: the number of parameters.

### Counting rules (rules version 1)

| Construct | Cyclomatic | Cognitive |
|---|---|---|
| `if` | +1 | +1 + nesting |
| `else if` | +1 | +1 |
| `else` | | +1 |
| `while`, `foreach`, `retry` | +1 | +1 + nesting |
| `match` | | +1 + nesting |
| each `match` clause, except a catch-all `_` | +1 | |
| `if` guard on a `match` clause | +1 | +1 |
| `on fail` (like `catch`) | +1 | +1 + nesting |
| `c ? a : b` | +1 | +1 + nesting |
| `&&`, `\|\|` | +1 each | +1 per run of the same operator (`a && b && c` is +1, `a && b \|\| c` is +2) |
| `x ?: y` (elvis) | +1 | |
| query expression or query action (`from ... do`) | | +1 + nesting |
| each `from` and `join` in a query, and `where` | +1 | +1, except the first `from` |
| the function calls itself (by name, or as `self.name(...)` in a method) | | +1, once |
| `check`, `checkpanic`, `trap`, `do`, `lock`, `transaction`, `fork` | | |

The following count one level deeper for nesting:
- the bodies of `if`, `else`, loops, `match` clauses and `on fail`;
- the branches of `? :`;
- everything inside a query;
- anonymous functions and workers.

`do`, `lock` and `transaction` blocks aren't deeper.

Some of these rules are judgement calls. The obvious alternatives are:
- counting `check` as a branch, since it can return early;
- making `retry` and queries plain blocks instead of loops.

If you change a rule, follow the steps under "Changing a counting rule" below.

## Checking the analyser

```sh
python3 bal_builder.py compile bal_complexity/fixture.bal --complexity
```

This builds the fixture like any other file, so it also has to compile. The run fails with `expectation not met: <file>:<line> <function>: <measure> is X, expected Y` for every value that differs from the `// complexity-expect:` comment above a function. Run it whenever the template's Ballerina version, `BalComplexity.java` or `COMPLEXITY_JDK_IMAGE` changes.

To see the syntax tree the parser produces for a file, run `--tree` in the official image of the same Ballerina version. Each line shows the line number and node kind, plus the token text:

```sh
docker run --rm -v "$PWD":/src -w /src ballerina/ballerina:2201.7.1 \
  sh -c 'java -cp "$(bal home)/bre/lib/*" bal_complexity/BalComplexity.java --tree bal_complexity/fixture.bal'
```

## Upgrading

### The template moves to a newer Ballerina

Nothing needs changing for this to work, because the analyser always uses the build container's own parser. Run the fixture check, and then look at the language's release notes for new syntax.

- **`BalComplexity.java` no longer compiles.** The build output shows `javac` errors such as `cannot find symbol`. A class, method or `SyntaxKind` of the parser API was renamed or removed. Look the new name up:
  - with `javap -cp '<bal home>/bre/lib/*' io.ballerina.compiler.syntax.tree.<Class>` in `ballerina/ballerina:<version>`;
  - or in [ballerina-lang](https://github.com/ballerina-platform/ballerina-lang) at the release tag, under `compiler/ballerina-parser/src/main/java/io/ballerina/compiler/syntax/tree/`. `SyntaxKind.java` lists every node kind.
- **`UnsupportedClassVersionError` or `class file has wrong version`.** The new Ballerina was compiled for a newer Java than the JDK in `COMPLEXITY_JDK_IMAGE`; 2201.7 uses Java 11 and 2201.13 uses Java 21. Point `COMPLEXITY_JDK_IMAGE` in `bal_builder.py` at a `ballerina/ballerina` tag (or any JDK image) with at least that Java version.
- **"could not find Ballerina's parser".** `bal home` or the `bre/lib/ballerina-parser-*.jar` layout changed. Update the lookup at the top of `_complexity()` in `bal_builder.py`.
- **A changed package layout**, such as a new folder for source files. Update `sources()` in `BalComplexity.java`.

### The language gains a construct

Examples are a new loop, a new kind of branch, a new error-handling clause or a new kind of function.

1. Write the construct in a small file and run `--tree` on it to find its `SyntaxKind`, and the kinds of its body and of the clauses it contains.
2. Decide what it adds, along the lines of the table above:
   - **A branch or loop**: in `walk()`, add `count(fn, cyclomatic, cognitive)` in a `case` for its kind. Use `1 + nesting` for cognitive when it nests, like a loop or `if`, and `1` when it only continues a structure, like `else if`.
   - **Its block is deeper**: add the kind to `nests`, just after the `switch`, so that the `BLOCK_STATEMENT` children of the construct count one level deeper.
   - **A new kind of named function**: add its kind to the `FUNCTION_DEFINITION` cases. If its node class differs, give it its own case that builds a `Function`.
   - **Code that belongs to the enclosing function, one level deeper** (like anonymous functions): add it to the `EXPLICIT_ANONYMOUS_FUNCTION_EXPRESSION` cases.
   - **A new container for methods** (like a class or service): add a `within(...)` case so its functions get a qualified name.
3. Add a function that uses the construct to `fixture.bal`, with the values counted by hand in a `// complexity-expect:` comment and the reason on each line.
4. Bump `RULES_VERSION` and update the table above.
5. Run the fixture check.

A construct the analyser doesn't know isn't an error. It walks through it and counts the decisions inside, but the construct itself adds nothing. Without these steps, new syntax is measured too low, not wrongly reported.

### Changing a counting rule

Reports made with different rules can't be compared. So when a rule changes:
1. bump `RULES_VERSION`, which `complexity.json` and `complexity.txt` record;
2. update the table above;
3. fix the affected `complexity-expect` values in `fixture.bal`, counted by hand rather than copied from the new output;
4. run the fixture check.
