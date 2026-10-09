// Gives each service of a TOML document a name-based UUID, built with pinned versions of the toml and uuid packages:
//   python3 bal_builder.py run examples/main_dependencies.bal
// bal_builder.py locks each `// dependency: org/name:version` comment in the Dependencies.toml of its copy of the
// template, so the build pulls those versions from Ballerina Central and uses them, and it fails if the build used
// another version. `bal_builder.py compile examples/main_dependencies.bal` lists the versions used in its summary.
// Without these comments, Ballerina 2201.7.1 would build with the toml 0.4.0 and uuid 1.6.0 it bundles.
// dependency: ballerina/toml:0.3.0
// dependency: ballerina/uuid:1.5.0
import ballerina/io;
import ballerina/toml;
import ballerina/uuid;

public function main() returns error? {
    string services = string `
[orders]
port = 8080

[billing]
port = 8081
`;
    foreach [string, json] [name, settings] in (check toml:readString(services)).entries() {
        // Version 5 UUIDs come from the name, so they are the same on every run.
        string id = check uuid:createType5AsString(uuid:NAME_SPACE_DNS, name + ".example.com");
        io:println(name, " ", id, " ", settings);
    }
}
