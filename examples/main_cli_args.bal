// Prints the SHA-512 checksum of each file named on the command line, as JSON or as a Markdown table, or writes it to
// a file in that format. It shows how a Ballerina program takes command-line arguments: the fields of main's included
// record parameter (*Options) are options, given as `--format markdown` or `--format=markdown`, and its rest parameter
// takes the other arguments, here the file paths. `--` ends the options, for a file whose name starts with `-`.
//   python3 bal_builder.py run examples/main_cli_args.bal --format markdown README.md Makefile
//   java -jar main.jar --format=json --out checksums.json a.txt b.txt   (main.jar from `bal_builder.py compile -o DIR`)
// bal_builder.py runs the program in its build container, where the files are those of the copied template (README.md,
// Makefile, main.bal, ...) and an --out file is removed with the container; the jar works with any file.
import ballerina/crypto;
import ballerina/file;
import ballerina/io;

const USAGE = "usage: [--format json|markdown] [--out FILE] FILE...";

# The command-line options.
#
# + format - how to write the checksums: `json` (the default) or `markdown`
# + out - the file to write them to, in that format; without it, they go to stdout
public type Options record {|
    string format = "json";
    string? out = ();
|};

# The SHA-512 checksum of a file.
#
# + path - the file's path, as given on the command line
# + sha512 - the checksum in lowercase hexadecimal, as `sha512sum` prints it
type Checksum record {|
    string path;
    string sha512;
|};

public function main(*Options options, string... paths) returns error? {
    if options.format != "json" && options.format != "markdown" {
        return error(string `unknown format '${options.format}', expected json or markdown; ${USAGE}`);
    }
    if paths.length() == 0 {
        return error(USAGE);
    }
    check checkFiles(paths);

    Checksum[] checksums = [];
    foreach string path in paths {
        byte[] content = check io:fileReadBytes(path); // the whole file, which is fine for an example
        checksums.push({path, sha512: crypto:hashSha512(content).toBase16()});
    }
    string output = options.format == "json" ? toJson(checksums) : toMarkdown(checksums);

    string? out = options.out;
    if out is () {
        io:print(output);
    } else {
        check io:fileWriteString(out, output);
        string files = checksums.length() == 1 ? "1 file" : string `${checksums.length()} files`;
        io:fprintln(io:stderr, string `Wrote the SHA-512 checksums of ${files} to ${out} as ${options.format}.`);
    }
}

// Fails, naming each path that is not a readable file.
function checkFiles(string[] paths) returns error? {
    string[] problems = [];
    foreach string path in paths {
        if !check file:test(path, file:EXISTS) {
            problems.push(path + " does not exist");
        } else if check file:test(path, file:IS_DIR) {
            problems.push(path + " is a directory");
        } else if !check file:test(path, file:READABLE) {
            problems.push(path + " is not readable");
        }
    }
    if problems.length() > 0 {
        return error("not a file: " + string:'join("; ", ...problems));
    }
}

// A JSON array with an object for each file, one per line.
function toJson(Checksum[] checksums) returns string =>
    "[\n" + string:'join(",\n", ...checksums.map(checksum => "  " + checksum.toJsonString())) + "\n]\n";

// A Markdown table with a row for each file.
function toMarkdown(Checksum[] checksums) returns string {
    string[] lines = ["| File | SHA-512 |", "| --- | --- |"];
    foreach Checksum checksum in checksums {
        // A | in a file name would end the table cell.
        lines.push("| " + re `\|`.replaceAll(checksum.path, "\\|") + " | `" + checksum.sha512 + "` |");
    }
    return string:'join("\n", ...lines) + "\n";
}
