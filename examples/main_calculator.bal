// A calculator with three front ends that share one schema. The input is two numbers, a and b, and a symbol
// (+ - * / % ^); the output is the input with its result, as {"a":6.0, "b":7.0, "symbol":"*", "result":42.0}.
// --mode picks the front end:
//   cli (the default)  the input as the options --a, --b and --symbol, and the output printed as JSON
//   api                a REST API on --port (8080): POST /calculate takes the input as a JSON body and answers with
//                      the output, GET /schema answers with the MCP tool, and Swagger UI at /docs shows the API's
//                      OpenAPI document (GET /openapi.json) and sends its requests
//   mcp                an MCP server on stdin and stdout, with a tool `calculate` whose arguments are the input and
//                      whose structured content is the output
// Every mode checks its input with toCalculation() and computes it with calculate(), so the same input gives the same
// output or the same error message in each, and the OpenAPI document and the MCP tool describe both with the same JSON
// Schemas (CALCULATION_SCHEMA and RESULT_SCHEMA).
//   python3 bal_builder.py run examples/main_calculator.bal --a 6 --symbol '*' --b 7
//   python3 bal_builder.py compile examples/main_calculator.bal -o out
//   java -jar out/main.jar --mode api --port 8080          then open http://localhost:8080/docs, or:
//   curl --json '{"a": 6, "symbol": "*", "b": 7}' http://localhost:8080/calculate
//   curl http://localhost:8080/schema
//   claude mcp add calculator -- java -jar "$PWD/out/main.jar" --mode mcp
// Quote the symbol on a command line, because the shell expands *. bal_builder.py run doesn't publish the container's
// ports, so the API needs the jar; the CLI and the MCP server work with either, as run passes stdin and stdout through.
import ballerina/http;
import ballerina/io;
import ballerina/lang.runtime;

const USAGE = "usage: [--mode cli] --a NUMBER --symbol SYMBOL --b NUMBER | --mode api [--port PORT] | --mode mcp";

# The command-line options.
#
# + mode - the front end: `cli` (the default), `api` or `mcp`
# + port - the port of the `api` REST API
# + a - the `cli` input's first number
# + b - the `cli` input's second number
# + symbol - the `cli` input's symbol
public type Options record {|
    string mode = "cli";
    int port = 8080;
    float? a = ();
    float? b = ();
    string? symbol = ();
|};

# The input of every mode, which CALCULATION_SCHEMA describes.
#
# + a - the number on the left of the symbol
# + b - the number on the right of the symbol
# + symbol - the operation, one of SYMBOLS
type Calculation record {|
    float a;
    float b;
    string symbol;
|};

# The output of every mode, which RESULT_SCHEMA describes: the input and its result. The fields are listed
# rather than included with *Calculation, which would put result first in the JSON.
#
# + a - the number on the left of the symbol
# + b - the number on the right of the symbol
# + symbol - the operation
# + result - `a symbol b`
type Result record {|
    float a;
    float b;
    string symbol;
    float result;
|};

// The symbols that operate() knows, with the names that the schema and the error messages give them.
final readonly & map<string> SYMBOLS = {"+": "add", "-": "subtract", "*": "multiply", "/": "divide", "%": "remainder", "^": "power"};
final string SYMBOL_LIST = string:'join(", ", ...from var [symbol, name] in SYMBOLS.entries() select string `${symbol} (${name})`);

// Calculation's fields as JSON Schema properties. Result has these and result; a closed record, rather than a map,
// can be spread next to result.
final readonly & record {|json a; json b; json symbol;|} CALCULATION_PROPERTIES = {
    a: {"type": "number", description: "the number on the left of the symbol"},
    b: {"type": "number", description: "the number on the right of the symbol"},
    symbol: {"type": "string", "enum": SYMBOLS.keys().cloneReadOnly(), description: "the operation: " + SYMBOL_LIST}
};

// The JSON Schema of Calculation: the CLI's options, the API's request body and the MCP tool's arguments.
final readonly & map<json> CALCULATION_SCHEMA = {
    "type": "object",
    properties: CALCULATION_PROPERTIES,
    required: ["a", "b", "symbol"],
    additionalProperties: false
};

// The JSON Schema of Result: the CLI's output, the API's response and the MCP tool's structured content.
final readonly & map<json> RESULT_SCHEMA = {
    "type": "object",
    properties: {...CALCULATION_PROPERTIES, result: {"type": "number", description: "a symbol b"}},
    required: ["a", "b", "symbol", "result"],
    additionalProperties: false
};

final string DESCRIPTION = "Calculates `a symbol b` for two numbers a and b, where the symbol is " + SYMBOL_LIST + ".";

// The MCP tool that tools/list announces, which the API serves at GET /schema too.
final readonly & map<json> TOOL = {
    name: "calculate",
    title: "Calculator",
    description: DESCRIPTION,
    inputSchema: CALCULATION_SCHEMA,
    outputSchema: RESULT_SCHEMA
};

// The API's OpenAPI document, which its Swagger UI shows. Ballerina can generate one from a service declaration
// (@openapi:ServiceInfo {embed: true}), but not from the service object that api mode attaches, so it is written here,
// with the MCP tool's schemas.
final readonly & map<json> OPENAPI = {
    openapi: "3.0.3",
    info: {title: "Calculator", version: "0.1.0", description: DESCRIPTION},
    paths: {
        "/calculate": {
            post: {
                operationId: "calculate",
                summary: "Calculate a symbol b",
                requestBody: {
                    required: true,
                    content: {
                        "application/json": {
                            schema: {"$ref": "#/components/schemas/Calculation"},
                            example: {a: 6, b: 7, symbol: "*"}
                        }
                    }
                },
                responses: {
                    "200": {
                        description: "the input and its result",
                        content: {
                            "application/json": {
                                schema: {"$ref": "#/components/schemas/Result"},
                                example: {a: 6.0, b: 7.0, symbol: "*", result: 42.0}
                            }
                        }
                    },
                    "400": {
                        description: "the input doesn't match the schema, or has no finite result",
                        content: {
                            "application/json": {
                                schema: {"$ref": "#/components/schemas/Error"},
                                example: {"error": "division by zero"}
                            }
                        }
                    }
                }
            }
        },
        "/schema": {
            get: {
                operationId: "schema",
                summary: "The MCP tool calculate, with the JSON Schemas of the input and the output",
                responses: {
                    "200": {description: "the MCP tool", content: {"application/json": {schema: {"type": "object"}}}}
                }
            }
        }
    },
    components: {
        schemas: {
            Calculation: CALCULATION_SCHEMA,
            Result: RESULT_SCHEMA,
            Error: {
                "type": "object",
                properties: {"error": {"type": "string", description: "what is wrong"}},
                required: ["error"],
                additionalProperties: false
            }
        }
    }
};

public function main(*Options options) returns error? {
    match options.mode {
        "cli" => {
            return runCli(options);
        }
        "api" => {
            return serveApi(options.port);
        }
        "mcp" => {
            return serveMcp();
        }
    }
    return error(string `unknown mode '${options.mode}', expected cli, api or mcp; ${USAGE}`);
}

// Every mode's input comes through here: the CLI's options, the API's request body and the MCP tool's arguments.
function evaluate(json input) returns Result|error => calculate(check toCalculation(input));

// Checks an input against the schema: an object with the numbers a and b and the string symbol, and nothing else.
function toCalculation(json input) returns Calculation|error {
    if input !is map<json> {
        return error("the input must be a JSON object with the fields a, b and symbol");
    }
    foreach string name in input.keys() {
        if !CALCULATION_PROPERTIES.hasKey(name) {
            return error(string `unknown field '${name}', the fields are a, b and symbol`);
        }
    }
    json a = input["a"];
    json b = input["b"];
    json symbol = input["symbol"];
    if a !is int|float|decimal || b !is int|float|decimal || symbol !is string {
        return error(string `a and b must be numbers and symbol a string, not ${input.toJsonString()}`);
    }
    return {a: <float>a, b: <float>b, symbol};
}

// Calculates `a symbol b`.
function calculate(Calculation input) returns Result|error {
    float result = check operate(input.a, input.symbol, input.b);
    // JSON has no infinity or NaN, the results of 10 ^ 400 or -8 ^ 0.5.
    if !result.isFinite() {
        return error(string `${input.a} ${input.symbol} ${input.b} is not a finite number`);
    }
    return {a: input.a, b: input.b, symbol: input.symbol, result};
}

// The operation of each symbol in SYMBOLS.
function operate(float a, string symbol, float b) returns float|error {
    match symbol {
        "+" => {
            return a + b;
        }
        "-" => {
            return a - b;
        }
        "*" => {
            return a * b;
        }
        "/"|"%" if b == 0.0 => {
            return error("division by zero");
        }
        "/" => {
            return a / b;
        }
        "%" => {
            return a % b;
        }
        "^" => {
            return float:pow(a, b);
        }
    }
    return error(string `unknown symbol '${symbol}', expected ${SYMBOL_LIST}`);
}

// cli: the options are the input, and the output goes to stdout. An error goes to stderr, with exit code 1.
function runCli(Options options) returns error? {
    if options.a is () || options.b is () || options.symbol is () {
        return error(USAGE);
    }
    Result result = check evaluate({a: options.a, b: options.b, symbol: options.symbol});
    io:println(result.toJsonString());
}

// api: a REST API that runs until Ctrl-C, as the registered listener keeps the program alive after main returns.
function serveApi(int port) returns error? {
    http:Listener server = check new (port);
    check server.attach(api, "/");
    check server.'start();
    runtime:registerListener(server);
    io:fprintln(io:stderr, string `The calculator API is at http://localhost:${port}, with Swagger UI at /docs`);
}

final http:Service api = service object {
    resource function get docs() returns http:Response {
        http:Response response = new;
        response.setTextPayload(SWAGGER_UI, "text/html; charset=utf-8");
        return response;
    }

    resource function get openapi\.json() returns json {
        return OPENAPI;
    }

    resource function get schema() returns json {
        return TOOL;
    }

    // The output with 200 OK, or {"error": message} with 400 Bad Request. The body is read here rather than bound
    // with @http:Payload, which would answer a body that isn't JSON with Ballerina's own error response; here it gets
    // the message of any other input that isn't a JSON object.
    resource function post calculate(http:Request request) returns http:Ok|http:BadRequest {
        json|error body = request.getJsonPayload();
        Result|error result = evaluate(body is error ? () : body);
        if result is error {
            http:BadRequest badRequest = {body: {"error": result.message()}};
            return badRequest;
        }
        http:Ok ok = {body: result};
        return ok;
    }
};

// The page of GET /docs: Swagger UI, loaded from unpkg as newer Ballerina versions' own does, showing the OpenAPI
// document. Its "Try it out" sends the requests to this API.
final string SWAGGER_UI = string `<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Calculator API</title>
<link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5.32.15/swagger-ui.css">
<div id="swagger-ui"></div>
<script src="https://unpkg.com/swagger-ui-dist@5.32.15/swagger-ui-bundle.js"></script>
<script>
  SwaggerUIBundle({url: "/openapi.json", dom_id: "#swagger-ui"});
</script>
`;

// The MCP protocol versions this server speaks, newest first. initialize answers with the client's version if it is
// one of these, else with the newest.
final readonly & string[] PROTOCOL_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"];

# A JSON-RPC error.
type RpcError error<record {|int code;|}>;

// mcp: the stdio transport, one JSON-RPC message per line, until stdin ends. io:readln() would fail at the end of
// stdin, where this stream ends instead; /dev/stdin is there on Linux and macOS. stdout carries only the responses.
function serveMcp() returns error? {
    stream<string, io:Error?> lines = check io:fileReadLinesAsStream("/dev/stdin");
    check from string line in lines
        where line.trim() != ""
        do {
            json response = respond(line);
            if response !is () {
                io:println(response.toJsonString());
            }
        };
}

// The response to a request, or () for a notification (such as notifications/initialized) or a client's response.
function respond(string line) returns json {
    json|error message = line.fromJsonString();
    if message !is map<json> {
        // JSON-RPC's codes for a line that isn't JSON, and for JSON that isn't a message object.
        json problem = message is error ? {code: -32700, message: "parse error"} : {code: -32600, message: "invalid request"};
        return {jsonrpc: "2.0", id: (), "error": problem};
    }
    json method = message["method"];
    if !message.hasKey("id") || method !is string {
        return ();
    }
    json|RpcError result = dispatch(method, message["params"]);
    if result is RpcError {
        return {jsonrpc: "2.0", id: message["id"], "error": {code: result.detail().code, message: result.message()}};
    }
    return {jsonrpc: "2.0", id: message["id"], result};
}

// The result of a request, or the JSON-RPC error to answer it with.
function dispatch(string method, json params) returns json|RpcError {
    match method {
        "initialize" => {
            json version = params is map<json> ? params["protocolVersion"] : ();
            return {
                protocolVersion: version is string && PROTOCOL_VERSIONS.indexOf(version) !is () ? version : PROTOCOL_VERSIONS[0],
                capabilities: {tools: {}},
                serverInfo: {name: "calculator", version: "0.1.0"}
            };
        }
        "ping" => {
            return {};
        }
        "tools/list" => {
            return {tools: [TOOL]};
        }
        "tools/call" => {
            return callTool(params);
        }
    }
    return error RpcError(string `unknown method '${method}'`, code = -32601);
}

// The output as structured content and as text, or the error message as text with isError: the tool ran and failed.
function callTool(json params) returns json|RpcError {
    if params !is map<json> || params["name"] != TOOL["name"] {
        return error RpcError("unknown tool, the tool is calculate", code = -32602);
    }
    Result|error result = evaluate(params["arguments"]);
    if result is error {
        return {content: [{"type": "text", text: result.message()}], isError: true};
    }
    return {content: [{"type": "text", text: result.toJsonString()}], structuredContent: result};
}
