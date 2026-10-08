// Regression fixture for BalComplexity.java: one function per counting rule, each with the values it
// must get, counted by hand. Check the analyser with:
//     python3 bal_builder.py compile bal_complexity/fixture.bal --complexity
// which fails if any value differs. Comments on the right show what each line adds.
import ballerina/io;

// complexity-expect: cyclomatic=1 cognitive=0 nesting=0 lines=4
public function straight(int x) returns int {
    int y = x + 1;
    return y * 2;
}

// complexity-expect: cyclomatic=5 cognitive=5 nesting=1
function grade(int score) returns string {
    if score >= 90 { // if: cyclomatic +1, cognitive +1
        return "A";
    } else if score >= 80 { // else if: +1, +1
        return "B";
    } else if score >= 70 && score < 80 { // else if: +1, +1; &&: +1, +1
        return "C";
    } else { // else: cognitive +1
        return "F";
    }
}

// complexity-expect: cyclomatic=4 cognitive=6 nesting=3
function countPairs(int[] values) returns int {
    int count = 0;
    foreach int a in values { // +1, +1
        foreach int b in values { // +1, +1 +1 (nested once)
            if a < b { // +1, +1 +2 (nested twice)
                count += 1;
            }
        }
    }
    return count;
}

// complexity-expect: cyclomatic=3 cognitive=3 nesting=2
function parseAll(string[] texts) returns int {
    int total = 0;
    int i = 0;
    while i < texts.length() { // +1, +1
        do { // do: nothing, and its block is no deeper
            total += check int:fromString(texts[i]); // check: nothing
        } on fail error e { // on fail: +1, +1 +1 (nested once); its block is one deeper
            io:println(e.message());
            total -= 1;
        }
        i += 1;
    }
    return total;
}

// complexity-expect: cyclomatic=5 cognitive=2 nesting=1
function describe(int|string value) returns string {
    string result = "other";
    match value { // match: cognitive +1
        0 => { // clause: cyclomatic +1
            result = "zero";
        }
        1|2 => { // clause: +1
            result = "small";
        }
        var text if text is string => { // clause: +1; guard: +1, +1
            result = "text";
        }
        _ => { // catch-all clause: nothing
            result = "other";
        }
    }
    return result;
}

// complexity-expect: cyclomatic=9 cognitive=6 nesting=0
function label(int? count, boolean a, boolean b, boolean c) returns string {
    int n = count ?: 0; // elvis: cyclomatic +1 only; `int?` is a type, not a branch
    boolean mixed = a && b || c; // &&, ||: cyclomatic +2; two runs of operators: cognitive +2
    boolean same = a && b && c; // cyclomatic +2; one run: cognitive +1
    return n > 0 && (mixed || same) ? "some" : "none"; // ?:: +1, +1; &&: +1, +1; || in (): +1, +1
}

// complexity-expect: cyclomatic=5 cognitive=4 nesting=0
function pairs(int[] values, int[] others) returns int[] {
    return from int v in values // query: cognitive +1; first from: cyclomatic +1
        from int w in others // another from: +1, +1
        join int u in others on w equals u // join: +1, +1
        where v % 2 == 0 // where: +1, +1
        select v + w + u;
}

// complexity-expect: cyclomatic=4 cognitive=4 nesting=2
function printEvens(int[] values) {
    from int v in values // query action: cognitive +1; from: cyclomatic +1
    where v % 2 == 0 // where: +1, +1
    do { // the do block is inside the query, one deeper
        if v > 10 { // +1, +1 +1 (nested once)
            io:println(v);
        }
    };
}

// complexity-expect: cyclomatic=2 cognitive=2 nesting=1
function factorial(int n) returns int {
    if n <= 1 { // +1, +1
        return 1;
    }
    return n * factorial(n - 1); // recursion: cognitive +1
}

// complexity-expect: cyclomatic=2 cognitive=2 nesting=0
function applyAll(int[] values) returns int[] {
    var twice = function(int x) returns int { // anonymous function: counted here, one deeper
        return x > 0 ? x * 2 : 0; // ?:: +1, +1 +1 (nested once)
    };
    return values.map(twice);
}

class Counter {
    private int count = 0;

    // complexity-expect: cyclomatic=3 cognitive=3 nesting=1
    function add(int n) {
        if n > 0 { // +1, +1
            self.count += n;
        } else if n < -100 { // +1, +1
            self.add(n + 100); // recursion through self: cognitive +1
        }
    }

    // complexity-expect: cyclomatic=1 cognitive=0 nesting=0
    function total() returns int {
        return self.count;
    }
}

// complexity-expect: cyclomatic=1 cognitive=0 nesting=0
public function main() returns error? {
    Counter counter = new;
    counter.add(5);
    io:println(straight(1), grade(85), countPairs([1, 2, 3]), parseAll(["1", "x"]), describe("a"));
    io:println(label(2, true, false, true), pairs([2, 4], [4]), factorial(5), applyAll([1, -1]), counter.total());
    printEvens([12, 3]);
}
